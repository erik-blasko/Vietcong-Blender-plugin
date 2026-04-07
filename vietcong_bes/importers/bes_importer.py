# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2024 Vietcong Blender Tools Contributors

"""
BES Importer

Main import function that converts BES data to Blender objects.
"""

import os
import bpy
import bmesh
import base64
import json
from mathutils import Vector, Matrix, Euler
from typing import Dict, Optional, List

# Blender 4.3+ removed EEVEE Legacy material properties (blend_method, shadow_method, show_transparent_back).
# In EEVEE Next, transparency is handled automatically via the Principled BSDF Alpha input.
_LEGACY_EEVEE = bpy.app.version < (4, 3, 0)

from ..core.bes_reader import read_bes_file
from ..core.bes_types import (
    BESFile,
    BESNode,
    BESMesh,
    BESMaterial,
    BESStandardMaterial,
    BESPteroMat,
    BESPteroLayer,
    BESBoneVertex,
    BESBonePart,
    BESSkeleton,
)
from ..core.constants import (
    COLLISION_PREFIXES,
    SPECIAL_PREFIXES,
    ALL_PREFIXES,
    NodeType,
)
from ..utils.math_utils import bes_to_blender_uv, bes_to_blender_coords
from ..properties import parse_user_properties, apply_properties_to_object


def is_collision_object(name: str) -> bool:
    """Check if object is collision mesh (^K, ^SF, ^SK prefix).

    Args:
        name: Object name

    Returns:
        True if object is a collision mesh
    """
    for prefix in COLLISION_PREFIXES:
        if name.startswith(prefix):
            return True
    return False


def get_object_type(name: str) -> tuple:
    """Get object type and clean name from prefix.

    Detects collision prefixes (^K, ^SF, ^SK), effect spheres (@),
    auxiliary objects (!), and LOD objects (>).

    Note: _LOD suffix objects are NOT detected here - LOD is determined
    by User Defined Properties (Lod=N) from the BES file.

    Args:
        name: Object name with potential prefix

    Returns:
        Tuple of (object_type, clean_name, prefix)
        object_type: 'player', 'bullets', 'sphere', 'auxiliary', 'effect', 'lod_hidden', or 'normal'
        clean_name: Name without prefix
        prefix: The detected prefix or empty string
    """
    # Check collision prefixes first (longest first for proper matching)
    for prefix in sorted(COLLISION_PREFIXES.keys(), key=len, reverse=True):
        if name.startswith(prefix):
            return COLLISION_PREFIXES[prefix], name[len(prefix):], prefix

    # Check special prefixes
    for prefix in sorted(SPECIAL_PREFIXES.keys(), key=len, reverse=True):
        if name.startswith(prefix):
            return SPECIAL_PREFIXES[prefix], name[len(prefix):], prefix

    return 'normal', name, ''


def get_collision_name(name: str) -> str:
    """Get name without collision or special prefix.

    Args:
        name: Object name with potential prefix

    Returns:
        Name without prefix
    """
    _, clean_name, _ = get_object_type(name)
    return clean_name


def parse_effect_sphere(name: str) -> dict:
    """Parse effect sphere name to extract material and slowdown.

    Effect sphere format: @[material][slowdown]-name
    Example: @WATR5-pond -> material='WATR', slowdown=5, name='pond'

    Args:
        name: Object name starting with @

    Returns:
        Dict with 'material', 'slowdown', 'name' keys, or empty dict if invalid
    """
    if not name.startswith('@'):
        return {}

    # Remove @ prefix
    rest = name[1:]

    # Try to extract material (4 chars) and slowdown (1 digit)
    if len(rest) >= 5 and rest[4].isdigit():
        material = rest[:4]
        slowdown = int(rest[4])

        # Rest after - is the name
        if len(rest) > 5 and rest[5] == '-':
            obj_name = rest[6:]
        else:
            obj_name = rest[5:]

        return {
            'material': material,
            'slowdown': slowdown,
            'name': obj_name,
        }

    return {'material': '', 'slowdown': 0, 'name': rest}


def import_bes(context, filepath: str, **options) -> set:
    """Import a BES file into Blender.

    Args:
        context: Blender context
        filepath: Path to BES file
        **options: Import options

    Returns:
        {'FINISHED'} on success, {'CANCELLED'} on failure
    """
    try:
        # Read BES file
        bes_file = read_bes_file(filepath)

        # Create importer instance
        importer = BESImporter(context, bes_file, options)
        importer.import_scene()

        return {'FINISHED'}

    except Exception as e:
        print(f"BES Import Error: {e}")
        import traceback
        traceback.print_exc()
        return {'CANCELLED'}


class BESImporter:
    """BES to Blender importer."""

    def __init__(self, context, bes_file: BESFile, options: dict):
        """Initialize importer.

        Args:
            context: Blender context
            bes_file: Parsed BES file
            options: Import options
        """
        self.context = context
        self.bes_file = bes_file
        self.options = options

        # Texture search settings from options
        self._search_textures = options.get('search_textures', True)
        self._search_subfolders = options.get('search_subfolders', True)
        self._search_game_folders = options.get('search_game_folders', True)
        self._import_materials = options.get('import_materials', True)
        self._import_hierarchy = options.get('import_hierarchy', True)
        self._import_collision = options.get('import_collision', True)
        self._hide_collision = options.get('hide_collision', True)

        # Material cache: BES index -> Blender material
        self._materials: Dict[int, bpy.types.Material] = {}

        # Texture search directories
        self._texture_dirs: List[str] = []
        if bes_file.filepath:
            self._texture_dirs.append(os.path.dirname(bes_file.filepath))

    def import_scene(self):
        """Import the complete BES scene."""
        # Create materials first
        self._create_materials()

        # Track object order for roundtrip export
        self._object_order = 0

        # Import scene hierarchy
        if self.bes_file.root_node:
            self._import_node(self.bes_file.root_node, None)

        # Import skeletal bone parts if this is a skeletal model
        if self.bes_file.skeleton:
            self._import_skeleton(self.bes_file.skeleton)

    def _create_materials(self):
        """Create Blender materials from BES materials."""
        for i, bes_mat in enumerate(self.bes_file.materials):
            blender_mat = self._create_material(bes_mat, i)
            self._materials[i] = blender_mat

    def _create_material(self, bes_mat: BESMaterial, list_index: int) -> bpy.types.Material:
        """Create a Blender material from BES material.

        Args:
            bes_mat: BES material
            list_index: Position in the materials list (for roundtrip export)

        Returns:
            Blender material
        """
        mat = bpy.data.materials.new(name=bes_mat.name)
        mat.use_nodes = True
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links

        # Clear default nodes
        nodes.clear()

        # Create output node
        output = nodes.new('ShaderNodeOutputMaterial')
        output.location = (300, 0)

        # Create Principled BSDF
        principled = nodes.new('ShaderNodeBsdfPrincipled')
        principled.location = (0, 0)
        links.new(principled.outputs['BSDF'], output.inputs['Surface'])

        # Store BES properties as custom properties
        mat['bes_material_type'] = bes_mat.material_type
        # Use list_index for roundtrip (position in BES materials list)
        mat['bes_index'] = list_index

        # Determine if material needs transparency
        needs_alpha = False

        if isinstance(bes_mat, BESPteroMat):
            # Store all PteroMat properties for roundtrip export
            mat['bes_collision_material'] = bes_mat.collision_material
            mat['bes_surface'] = bes_mat.surface
            mat['bes_transparency_type'] = bes_mat.transparency_type
            mat['bes_two_sided'] = bes_mat.two_sided
            mat['bes_faceted'] = bes_mat.faceted
            mat['bes_grow_type'] = bes_mat.grow_type
            mat['bes_grass_type'] = bes_mat.grass_type

            # Store material colors
            mat['bes_mat_diffuse'] = list(bes_mat.mat_diffuse)
            mat['bes_mat_ambient'] = list(bes_mat.mat_ambient)
            mat['bes_mat_specular'] = list(bes_mat.mat_specular)
            mat['bes_mat_self_illum'] = list(bes_mat.mat_self_illum)

            # Store material properties
            mat['bes_mat_opacity'] = bes_mat.mat_opacity
            mat['bes_mat_opacity_falloff'] = bes_mat.mat_opacity_falloff
            mat['bes_mat_glossiness'] = bes_mat.mat_glossiness
            mat['bes_mat_spec_level'] = bes_mat.mat_spec_level

            # Store shader properties
            mat['bes_shader_type_name'] = bes_mat.shader_type_name
            mat['bes_shader_filename'] = bes_mat.shader_filename

            # Store water/glass flags
            mat['bes_is_water'] = bes_mat.is_water
            mat['bes_is_glass'] = bes_mat.is_glass

            # Store water properties if applicable
            if bes_mat.is_water or bes_mat.water_env_blend != 0.0:
                mat['bes_water_env_blend'] = bes_mat.water_env_blend
                mat['bes_water_alpha_angle'] = bes_mat.water_alpha_angle
                mat['bes_water_sharpness'] = list(bes_mat.water_sharpness)
                mat['bes_water_shifting_xy'] = list(bes_mat.water_shifting_xy)
                mat['bes_water_shifting_uv'] = list(bes_mat.water_shifting_uv)

            # Apply material colors to Principled BSDF
            principled.inputs['Base Color'].default_value = (*bes_mat.mat_diffuse, 1.0)

            # Specular settings (convert 0-100 to 0-1 range)
            if 'Specular IOR Level' in principled.inputs:
                principled.inputs['Specular IOR Level'].default_value = bes_mat.mat_spec_level / 100.0
            elif 'Specular' in principled.inputs:
                principled.inputs['Specular'].default_value = bes_mat.mat_spec_level / 100.0

            # Roughness (inverse of glossiness)
            principled.inputs['Roughness'].default_value = 1.0 - (bes_mat.mat_glossiness / 100.0)

            # Self-illumination as emission
            if any(v > 0 for v in bes_mat.mat_self_illum):
                if 'Emission Color' in principled.inputs:
                    principled.inputs['Emission Color'].default_value = (*bes_mat.mat_self_illum, 1.0)
                    principled.inputs['Emission Strength'].default_value = 1.0
                elif 'Emission' in principled.inputs:
                    principled.inputs['Emission'].default_value = (*bes_mat.mat_self_illum, 1.0)

            # Handle transparency
            if bes_mat.is_transparent or bes_mat.mat_opacity < 100:
                needs_alpha = True
                if _LEGACY_EEVEE:
                    mat.blend_method = 'BLEND'
                principled.inputs['Alpha'].default_value = bes_mat.mat_opacity / 100.0

            # Handle two-sided
            if bes_mat.two_sided:
                mat.use_backface_culling = False

            # Handle faceted (flat shading) - stored for mesh import
            # Note: Flat shading is applied per-mesh, not per-material in Blender

            # Store texture_flags and texture names for roundtrip export
            mat['bes_texture_flags'] = bes_mat.texture_flags
            texture_names = {}
            for tex_slot, tex in bes_mat.textures.items():
                texture_names[tex_slot] = tex.filename
            mat['bes_texture_names'] = json.dumps(texture_names)

            # Load diffuse texture (base layer)
            if 'diffuse_1' in bes_mat.textures:
                self._add_texture_node(mat, bes_mat.textures['diffuse_1'], principled, 'Base Color', needs_alpha)

            # Load overlay textures (diffuse_2 = multitexture, diffuse_3 = overlay)
            if 'diffuse_2' in bes_mat.textures:
                self._add_overlay_texture(mat, bes_mat.textures['diffuse_2'], principled, 'diffuse_2', 'MULTIPLY')

            if 'diffuse_3' in bes_mat.textures:
                self._add_overlay_texture(mat, bes_mat.textures['diffuse_3'], principled, 'diffuse_3', 'OVERLAY')

        elif isinstance(bes_mat, BESPteroLayer):
            # Store all PteroLayer properties for roundtrip export
            mat['bes_surface'] = bes_mat.surface
            mat['bes_transparency_type'] = bes_mat.transparency_type
            mat['bes_two_sided'] = bes_mat.two_sided
            mat['bes_faceted'] = bes_mat.faceted
            mat['bes_grow_type'] = bes_mat.grow_type
            mat['bes_grass_type'] = bes_mat.grass_type
            mat['bes_shader_type'] = bes_mat.shader_type
            mat['bes_shader_filename'] = bes_mat.shader_filename

            # Store material colors
            mat['bes_mat_diffuse'] = list(bes_mat.mat_diffuse)
            mat['bes_mat_ambient'] = list(bes_mat.mat_ambient)
            mat['bes_mat_specular'] = list(bes_mat.mat_specular)
            mat['bes_mat_self_illum'] = list(bes_mat.mat_self_illum)

            # Store material properties
            mat['bes_mat_opacity'] = bes_mat.mat_opacity
            mat['bes_mat_opacity_falloff'] = bes_mat.mat_opacity_falloff
            mat['bes_mat_glossiness'] = bes_mat.mat_glossiness
            mat['bes_mat_spec_level'] = bes_mat.mat_spec_level

            # Store water/glass flags
            mat['bes_is_water'] = bes_mat.is_water
            mat['bes_is_glass'] = bes_mat.is_glass

            # Apply material colors to Principled BSDF
            principled.inputs['Base Color'].default_value = (*bes_mat.mat_diffuse, 1.0)

            # Specular settings
            if 'Specular IOR Level' in principled.inputs:
                principled.inputs['Specular IOR Level'].default_value = bes_mat.mat_spec_level / 100.0
            elif 'Specular' in principled.inputs:
                principled.inputs['Specular'].default_value = bes_mat.mat_spec_level / 100.0

            principled.inputs['Roughness'].default_value = 1.0 - (bes_mat.mat_glossiness / 100.0)

            # Self-illumination as emission
            if any(v > 0 for v in bes_mat.mat_self_illum):
                if 'Emission Color' in principled.inputs:
                    principled.inputs['Emission Color'].default_value = (*bes_mat.mat_self_illum, 1.0)
                    principled.inputs['Emission Strength'].default_value = 1.0

            # Handle transparency
            if bes_mat.is_transparent or bes_mat.mat_opacity < 100:
                needs_alpha = True
                if _LEGACY_EEVEE:
                    mat.blend_method = 'BLEND'
                principled.inputs['Alpha'].default_value = bes_mat.mat_opacity / 100.0

            # Handle two-sided
            if bes_mat.two_sided:
                mat.use_backface_culling = False

            # Load texture layers
            if bes_mat.layers:
                from ..core.bes_types import BESTexture

                for layer_idx, layer in enumerate(bes_mat.layers):
                    if not layer.filename:
                        continue

                    tex = BESTexture(
                        filename=layer.filename,
                        u_tile=layer.tile_u,
                        v_tile=layer.tile_v,
                    )

                    if layer_idx == 0:
                        # First layer = base diffuse
                        self._add_texture_node(mat, tex, principled, 'Base Color', needs_alpha)
                    elif layer_idx == 1:
                        # Second layer = multitexture (multiply blend)
                        self._add_overlay_texture(mat, tex, principled, f'layer_{layer_idx}', 'MULTIPLY')
                    elif layer_idx == 2:
                        # Third layer = overlay
                        self._add_overlay_texture(mat, tex, principled, f'layer_{layer_idx}', 'OVERLAY')
                    else:
                        # Additional layers = mix
                        self._add_overlay_texture(mat, tex, principled, f'layer_{layer_idx}', 'MIX')

        elif isinstance(bes_mat, BESStandardMaterial):
            mat['bes_map_flags'] = bes_mat.map_flags
            mat['bes_material_id'] = bes_mat.material_id
            # Store unknown_field as bytes converted to list (Blender can't store raw bytes)
            if bes_mat.unknown_field:
                mat['bes_unknown_field'] = list(bes_mat.unknown_field)

            # Store all texture information for roundtrip export
            for slot_name, tex in bes_mat.textures.items():
                mat[f'bes_tex_{slot_name}_filename'] = tex.filename
                mat[f'bes_tex_{slot_name}_flags'] = tex.flags
                mat[f'bes_tex_{slot_name}_u_tile'] = tex.u_tile
                mat[f'bes_tex_{slot_name}_v_tile'] = tex.v_tile
                mat[f'bes_tex_{slot_name}_u_mirror'] = tex.u_mirror
                mat[f'bes_tex_{slot_name}_v_mirror'] = tex.v_mirror

            # Load diffuse texture - Standard materials can also have alpha
            if 'diffuse' in bes_mat.textures:
                self._add_texture_node(mat, bes_mat.textures['diffuse'], principled, 'Base Color', check_alpha=True)

            # Load opacity texture if present
            if 'opacity' in bes_mat.textures:
                # For opacity, we need to connect to alpha
                self._add_opacity_texture(mat, bes_mat.textures['opacity'], principled)

        return mat

    def _add_texture_node(self, mat, bes_tex, principled, input_name: str,
                          needs_alpha: bool = False, check_alpha: bool = False):
        """Add texture node to material.

        Args:
            mat: Blender material
            bes_tex: BES texture
            principled: Principled BSDF node
            input_name: Name of input to connect to
            needs_alpha: Material definitely needs alpha blending
            check_alpha: Check if image has alpha channel
        """
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links

        # Try to find texture file
        tex_path = self._find_texture(bes_tex.filename)

        if tex_path:
            try:
                image = bpy.data.images.load(tex_path)

                tex_node = nodes.new('ShaderNodeTexImage')
                tex_node.image = image
                tex_node.location = (-300, 0)

                links.new(tex_node.outputs['Color'], principled.inputs[input_name])

                # Check if image has alpha channel
                has_alpha = False
                if check_alpha and image.channels == 4:
                    has_alpha = True

                # Set up alpha blending if needed
                if needs_alpha or has_alpha:
                    if _LEGACY_EEVEE:
                        mat.blend_method = 'BLEND'
                        mat.shadow_method = 'CLIP'
                        mat.show_transparent_back = True

                    # Connect alpha
                    links.new(tex_node.outputs['Alpha'], principled.inputs['Alpha'])

            except Exception as e:
                print(f"Failed to load texture {tex_path}: {e}")

    def _add_overlay_texture(self, mat, bes_tex, principled, layer_name: str, blend_type: str = 'OVERLAY'):
        """Add overlay texture using MixRGB node to blend with base color.

        Args:
            mat: Blender material
            bes_tex: BES texture
            principled: Principled BSDF node
            layer_name: Name for the texture node
            blend_type: Blend mode (OVERLAY, MULTIPLY, MIX, etc.)
        """
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links

        # Try to find texture file
        tex_path = self._find_texture(bes_tex.filename)

        if not tex_path:
            return

        try:
            image = bpy.data.images.load(tex_path)

            # Create texture node
            tex_node = nodes.new('ShaderNodeTexImage')
            tex_node.image = image
            tex_node.name = layer_name
            tex_node.label = layer_name

            # Position texture node (stack vertically)
            existing_tex_nodes = [n for n in nodes if n.type == 'TEX_IMAGE']
            tex_node.location = (-500, -200 * len(existing_tex_nodes))

            # Find what's currently connected to Base Color
            base_color_input = principled.inputs['Base Color']

            # Create MixRGB node
            mix_node = nodes.new('ShaderNodeMixRGB')
            mix_node.blend_type = blend_type
            mix_node.inputs['Fac'].default_value = 1.0
            mix_node.name = f'Mix_{layer_name}'
            mix_node.label = f'{blend_type} {layer_name}'
            mix_node.location = (-200, -200 * (len(existing_tex_nodes) - 1))

            # If something is connected to Base Color, use it as Color1
            if base_color_input.links:
                # Get the existing connection
                existing_link = base_color_input.links[0]
                from_socket = existing_link.from_socket

                # Remove existing link
                links.remove(existing_link)

                # Connect: existing -> Mix Color1, new texture -> Mix Color2, Mix -> Base Color
                links.new(from_socket, mix_node.inputs['Color1'])
                links.new(tex_node.outputs['Color'], mix_node.inputs['Color2'])
                links.new(mix_node.outputs['Color'], base_color_input)
            else:
                # No existing connection, just connect overlay directly
                # (shouldn't happen normally, but handle it)
                links.new(tex_node.outputs['Color'], base_color_input)

        except Exception as e:
            print(f"Failed to load overlay texture {tex_path}: {e}")

    def _add_opacity_texture(self, mat, bes_tex, principled):
        """Add opacity texture to material.

        Args:
            mat: Blender material
            bes_tex: BES texture
            principled: Principled BSDF node
        """
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links

        tex_path = self._find_texture(bes_tex.filename)

        if tex_path:
            try:
                image = bpy.data.images.load(tex_path)

                tex_node = nodes.new('ShaderNodeTexImage')
                tex_node.image = image
                tex_node.name = 'Opacity'
                tex_node.label = 'Opacity'
                tex_node.location = (-300, -200)

                # Set up alpha blending
                if _LEGACY_EEVEE:
                    mat.blend_method = 'BLEND'
                    mat.shadow_method = 'CLIP'
                    mat.show_transparent_back = True

                # Connect alpha output to principled alpha input
                links.new(tex_node.outputs['Color'], principled.inputs['Alpha'])

            except Exception as e:
                print(f"Failed to load opacity texture {tex_path}: {e}")

    def _find_texture(self, filename: str) -> Optional[str]:
        """Find texture file with intelligent path resolution.

        Search priority (controlled by options):
        1. Same folder as model
        2. Subfolders of model's folder (recursive) - if search_subfolders
        3. Game structure (G folder) if in LEVELS or G folder - if search_game_folders

        Args:
            filename: Texture filename from BES

        Returns:
            Full path to texture file, or None if not found
        """
        # Check if texture search is disabled
        if not self._search_textures:
            return None

        base_name = os.path.splitext(filename)[0]
        extensions = ['.dds', '.DDS', '.tga', '.TGA', '.bmp', '.BMP', '.png', '.PNG', '.jpg', '.JPG']

        model_dir = os.path.dirname(self.bes_file.filepath)

        # Phase 1: Same folder as model (always enabled if search_textures is True)
        result = self._search_in_directory(model_dir, base_name, extensions)
        if result:
            return result

        # Phase 2: Subfolders of model's folder (if enabled)
        if self._search_subfolders:
            result = self._search_recursive(model_dir, base_name, extensions)
            if result:
                return result

        # Phase 3: Game structure search (if enabled)
        if self._search_game_folders:
            g_folder = self._get_g_folder_path()
            if g_folder:
                # Search G folder excluding _FOR_ALL
                result = self._search_recursive(g_folder, base_name, extensions,
                                                exclude_dirs=['_FOR_ALL'])
                if result:
                    return result

                # Finally search G/TEXTURES/_FOR_ALL
                for_all = os.path.join(g_folder, 'TEXTURES', '_FOR_ALL')
                if os.path.isdir(for_all):
                    result = self._search_recursive(for_all, base_name, extensions)
                    if result:
                        return result

        return None

    def _search_in_directory(self, dir_path: str, base_name: str,
                             extensions: List[str]) -> Optional[str]:
        """Search for texture in a single directory.

        Args:
            dir_path: Directory to search in
            base_name: Texture base name (without extension)
            extensions: List of extensions to try

        Returns:
            Full path if found, None otherwise
        """
        if not os.path.isdir(dir_path):
            return None

        try:
            # Get list of files in directory (case-insensitive matching)
            files_lower = {}
            for entry in os.scandir(dir_path):
                if entry.is_file():
                    files_lower[entry.name.lower()] = entry.path

            # Try each extension
            for ext in extensions:
                test_name = (base_name + ext).lower()
                if test_name in files_lower:
                    return files_lower[test_name]

        except OSError:
            pass

        return None

    def _search_recursive(self, root_dir: str, base_name: str,
                          extensions: List[str],
                          exclude_dirs: List[str] = None) -> Optional[str]:
        """Search for texture recursively in directory tree.

        Args:
            root_dir: Root directory to start search
            base_name: Texture base name (without extension)
            extensions: List of extensions to try
            exclude_dirs: Directory names to skip (case-insensitive)

        Returns:
            Full path if found, None otherwise
        """
        if not os.path.isdir(root_dir):
            return None

        exclude_dirs = exclude_dirs or []
        exclude_lower = [d.lower() for d in exclude_dirs]

        try:
            for root, dirs, files in os.walk(root_dir):
                # Filter out excluded directories
                dirs[:] = [d for d in dirs if d.lower() not in exclude_lower]

                # Build case-insensitive file lookup
                files_lower = {f.lower(): f for f in files}

                # Try each extension
                for ext in extensions:
                    test_name = (base_name + ext).lower()
                    if test_name in files_lower:
                        return os.path.join(root, files_lower[test_name])

        except OSError:
            pass

        return None

    def _get_g_folder_path(self) -> Optional[str]:
        """Get G folder path based on model location.

        Detects if model is in LEVELS or G folder structure and returns
        the appropriate G folder path.

        Returns:
            Path to G folder, or None if not in game structure
        """
        if not self.bes_file.filepath:
            return None

        # Normalize path separators
        model_path = self.bes_file.filepath.replace('\\', '/')
        model_path_upper = model_path.upper()

        # Check if in LEVELS folder
        if '/LEVELS/' in model_path_upper:
            # Find position of LEVELS in path
            idx = model_path_upper.find('/LEVELS/')
            game_root = model_path[:idx]
            g_folder = os.path.join(game_root, 'G')
            if os.path.isdir(g_folder):
                return g_folder

        # Check if in G folder
        if '/G/' in model_path_upper:
            # Find position of G in path (get the root G folder)
            idx = model_path_upper.find('/G/')
            g_folder = model_path[:idx + 2]  # Include /G
            if os.path.isdir(g_folder):
                return g_folder

        return None

    def _import_node(self, bes_node: BESNode, parent_obj) -> Optional[bpy.types.Object]:
        """Import a BES node as Blender object.

        Args:
            bes_node: BES node to import
            parent_obj: Parent Blender object (or None)

        Returns:
            Created Blender object
        """
        # Get object type from prefix
        obj_type, clean_name, prefix = get_object_type(bes_node.name)

        # Check if this is a collision object
        is_collision = obj_type in ('player', 'bullets', 'sphere')

        # Skip collision objects if not importing them
        if is_collision and not self._import_collision:
            # Still import children
            for child_node in bes_node.children:
                self._import_node(child_node, parent_obj)
            return None

        obj = None

        if bes_node.meshes:
            # Create mesh object
            obj = self._create_mesh_object(bes_node)
        else:
            # Create empty for hierarchy node
            obj = bpy.data.objects.new(bes_node.get_visible_name(), None)
            self.context.collection.objects.link(obj)

        if obj:
            # Set parent FIRST
            if parent_obj:
                obj.parent = parent_obj

            # Apply transform (BES stores LOCAL coordinates relative to parent)
            # Use safe version to protect against zero scale (v0005 skeletal models)
            if bes_node.transform:
                self._apply_transform_safe(obj, bes_node.transform)

            # Store BES properties
            if bes_node.properties:
                props = bes_node.properties.properties
                for key, value in props.items():
                    obj[f'bes_{key}'] = value

                # Handle LOD objects based on User Defined Properties
                # Two property conventions exist in BES files:
                # - "Islod" = Boolean flag (1 = this IS an LOD object)
                # - "Lod" = Distance threshold (>0 = LOD distance, -1 = base object)

                # Check for Islod property (case-insensitive)
                # Islod=1 means this IS an LOD variant
                islod_value = props.get('Islod') or props.get('islod') or props.get('IsLod')
                if islod_value and islod_value != 0:
                    obj['bes_is_lod'] = True
                    obj.hide_viewport = True
                    obj.hide_render = True

                # Get LOD distance from lod_distances list (NOT properties dict!)
                # BES files can have multiple Lod= lines like "Lod=20\nLod=-1"
                # properties dict only keeps LAST value (-1), lod_distances keeps ALL [20, -1]
                if bes_node.properties.lod_distances:
                    for lod_dist in bes_node.properties.lod_distances:
                        if lod_dist > 0:
                            obj['bes_lod_distance'] = lod_dist
                            break  # Use first positive value

                # Store raw User Defined Properties text (like 3ds Max plugin)
                # Always store if properties exist (even if empty) for roundtrip
                obj['bes_user_properties'] = bes_node.properties.raw_text or ''

                # Auto-sync properties to UI PropertyGroup
                if bes_node.properties.raw_text:
                    try:
                        props = parse_user_properties(bes_node.properties.raw_text)
                        apply_properties_to_object(obj, props)
                    except Exception:
                        pass  # Silently ignore parse errors during import

            # Store object type info
            obj['bes_object_type'] = obj_type
            obj['bes_original_prefix'] = prefix
            obj['bes_source_file'] = os.path.splitext(os.path.basename(self.bes_file.filepath or ''))[0]

            # Store import order for roundtrip export
            obj['bes_import_order'] = self._object_order
            self._object_order += 1

            # Handle collision objects (^K, ^SF, ^SK)
            if is_collision:
                obj['bes_is_collision'] = True
                obj['bes_collision_type'] = obj_type  # 'player', 'bullets', 'sphere'
                obj['bes_collision_name'] = clean_name

                # Hide collision objects in viewport if option is enabled
                if self._hide_collision:
                    obj.hide_viewport = True
                    obj.hide_render = True

                # Set display type to wireframe for collision visualization
                obj.display_type = 'WIRE'

            # Handle effect spheres (@)
            elif obj_type == 'effect':
                obj['bes_is_effect_sphere'] = True
                effect_data = parse_effect_sphere(bes_node.name)
                if effect_data:
                    obj['bes_effect_material'] = effect_data.get('material', '')
                    obj['bes_effect_slowdown'] = effect_data.get('slowdown', 0)

                # Effect spheres should be visible but with special display
                obj.display_type = 'BOUNDS'
                obj.display_bounds_type = 'SPHERE'

            # Handle auxiliary objects (!)
            elif obj_type == 'auxiliary':
                obj['bes_is_auxiliary'] = True
                obj.hide_viewport = True
                obj.hide_render = True

            # Handle LOD objects (> prefix)
            elif obj_type == 'lod_hidden':
                obj['bes_is_lod'] = True
                # Count > prefixes to determine LOD level
                lod_level = 0
                for char in bes_node.name:
                    if char == '>':
                        lod_level += 1
                    else:
                        break
                obj['bes_lod_level'] = lod_level
                obj.hide_viewport = True
                obj.hide_render = True

            # Set hidden state (for objects marked as hidden in BES)
            elif bes_node.is_hidden:
                obj.hide_viewport = True
                obj.hide_render = True

            # Store collision chunk data for roundtrip export
            if bes_node.collision:
                collision = bes_node.collision
                collision_data = {
                    'collision_type': collision.collision_type,
                    'center': list(collision.center),
                    'raw_vertex_data': base64.b64encode(collision.raw_vertex_data).decode('ascii') if collision.raw_vertex_data else '',
                    'raw_face_data': base64.b64encode(collision.raw_face_data).decode('ascii') if collision.raw_face_data else '',
                    'raw_bone_data': base64.b64encode(collision.raw_bone_data).decode('ascii') if collision.raw_bone_data else '',
                    'raw_trailing': base64.b64encode(collision.raw_trailing).decode('ascii') if collision.raw_trailing else '',
                }
                obj['bes_collision_chunk'] = json.dumps(collision_data)

        # Import children
        for child_node in bes_node.children:
            self._import_node(child_node, obj)

        return obj

    def _create_mesh_object(self, bes_node: BESNode) -> bpy.types.Object:
        """Create Blender mesh object from BES node.

        Args:
            bes_node: BES node with mesh data

        Returns:
            Created Blender mesh object
        """
        # Create mesh data
        mesh = bpy.data.meshes.new(bes_node.get_visible_name())

        # Combine all meshes in node
        all_verts = []
        all_faces = []
        all_uvs = []
        face_materials = []
        vert_offset = 0
        has_original_uvs = False  # Track if any mesh actually has UV data

        for bes_mesh in bes_node.meshes:
            # Add vertices - use raw coordinates (no conversion needed)
            # The old Blender plugin also used raw coords without conversion
            for vert in bes_mesh.vertices:
                all_verts.append(vert.position)

            # Add faces with offset
            for face in bes_mesh.faces:
                all_faces.append((
                    face.a + vert_offset,
                    face.b + vert_offset,
                    face.c + vert_offset,
                ))
                face_materials.append(bes_mesh.material_index)

            # Store UV data - handle both BESVertex (.uvs) and BESBoneVertex (.uv)
            if bes_mesh.vertices:
                for vert in bes_mesh.vertices:
                    if isinstance(vert, BESBoneVertex):
                        # BESBoneVertex has single .uv tuple
                        all_uvs.append(bes_to_blender_uv(vert.uv))
                        has_original_uvs = True
                    elif hasattr(vert, 'uvs') and vert.uvs:
                        # BESVertex has .uvs list
                        all_uvs.append(bes_to_blender_uv(vert.uvs[0]))
                        has_original_uvs = True
                    else:
                        all_uvs.append((0.0, 0.0))

            vert_offset += len(bes_mesh.vertices)

        # Build mesh
        mesh.from_pydata(all_verts, [], all_faces)

        # Create UV layer only if original mesh had UV data
        if has_original_uvs and all_uvs:
            uv_layer = mesh.uv_layers.new(name='UVMap')
            for poly in mesh.polygons:
                for loop_idx in poly.loop_indices:
                    vert_idx = mesh.loops[loop_idx].vertex_index
                    if vert_idx < len(all_uvs):
                        uv_layer.data[loop_idx].uv = all_uvs[vert_idx]

        # Assign materials
        unique_mat_indices = list(set(face_materials))
        for mat_idx in unique_mat_indices:
            if mat_idx in self._materials:
                mesh.materials.append(self._materials[mat_idx])

        # Set material indices for faces
        if len(unique_mat_indices) > 1:
            for poly_idx, mat_idx in enumerate(face_materials):
                if mat_idx in unique_mat_indices:
                    mesh.polygons[poly_idx].material_index = unique_mat_indices.index(mat_idx)

        mesh.update()
        mesh.validate()

        # Create object
        obj = bpy.data.objects.new(bes_node.get_visible_name(), mesh)
        self.context.collection.objects.link(obj)

        return obj

    def _apply_transform(self, obj, transform):
        """Apply BES transform to Blender object.

        Args:
            obj: Blender object
            transform: BES transform data

        Note: The old Blender plugin applied transforms directly without
        coordinate system conversion, and it worked. We do the same here.
        """
        from ..core.bes_types import BESTransform

        if not isinstance(transform, BESTransform):
            return

        # Apply translation directly (no conversion)
        obj.location = transform.translation

        # Apply rotation directly (BES uses radians, XYZ order)
        obj.rotation_euler = Euler(transform.rotation, 'XYZ')

        # Apply scale directly
        obj.scale = transform.scale

        # Store original BES matrix for roundtrip export
        if transform.matrix:
            obj['bes_original_matrix'] = json.dumps(transform.matrix)

    def _apply_transform_safe(self, obj, transform):
        """Apply BES transform with protection against zero scale.

        Args:
            obj: Blender object
            transform: BES transform data
        """
        from ..core.bes_types import BESTransform

        if not isinstance(transform, BESTransform):
            return

        # Apply translation
        obj.location = transform.translation

        # Apply rotation (BES uses radians, XYZ order)
        obj.rotation_euler = Euler(transform.rotation, 'XYZ')

        # Apply scale with zero-protection
        # v0005 skeletal models have placeholder transforms with zero scale
        scale = transform.scale
        if scale[0] == 0 and scale[1] == 0 and scale[2] == 0:
            # All zeros - use identity scale (common in v0005 skeletal models)
            scale = (1.0, 1.0, 1.0)
        elif scale[0] == 0 or scale[1] == 0 or scale[2] == 0:
            # Some zeros - replace only zero components
            scale = (
                scale[0] if scale[0] != 0 else 1.0,
                scale[1] if scale[1] != 0 else 1.0,
                scale[2] if scale[2] != 0 else 1.0,
            )
        obj.scale = scale

    def _import_skeleton(self, skeleton: BESSkeleton):
        """Import skeletal data with proper bone hierarchy.

        For v0006+ (ISKE): Creates Blender armature with parent-child hierarchy.
        For v0005 (SKELETON_MARKER): Uses flat structure with bone parts.

        Args:
            skeleton: BES skeleton data
        """
        from ..core.bone_hierarchy import get_bone_parent, is_known_bone

        # PRIORITY 1: Use bone_parts if available
        # (populated from ISKE OBJECT children or v0005 SKELETON_MARKER)
        if skeleton.bone_parts:
            self._import_skeleton_flat(skeleton)
            return

        # PRIORITY 2: Fall back to ISKE scene tree traversal
        # (for v0006+ models where bone_parts wasn't populated)
        iske_bone_names = self._collect_iske_bone_names()
        iske_bone_transforms = self._collect_iske_bone_transforms()

        if iske_bone_names:
            # v0006+ with ISKE - create armature with hierarchy
            armature = bpy.data.armatures.new(skeleton.name)
            arm_obj = bpy.data.objects.new(skeleton.name, armature)
            self.context.collection.objects.link(arm_obj)
            self.context.view_layer.objects.active = arm_obj

            # Store original skeleton name for export (Blender may rename with .001 suffix)
            arm_obj['bes_skeleton_name'] = skeleton.name

            bpy.ops.object.mode_set(mode='EDIT')

            # Create bones with proper hierarchy
            created_bones = {}

            # First pass: create all bones
            for name in iske_bone_names:
                edit_bone = armature.edit_bones.new(name)

                # Set bone position from transform if available
                if name in iske_bone_transforms:
                    transform = iske_bone_transforms[name]
                    edit_bone.head = transform.translation
                    # Tail points up by default (will be adjusted by hierarchy)
                    edit_bone.tail = (
                        transform.translation[0],
                        transform.translation[1],
                        transform.translation[2] + 0.1
                    )
                else:
                    edit_bone.head = (0, 0, 0)
                    edit_bone.tail = (0, 0.1, 0)

                created_bones[name] = edit_bone

            # Second pass: set parent relationships
            for name in iske_bone_names:
                parent_name = get_bone_parent(name)
                if parent_name and parent_name in created_bones:
                    created_bones[name].parent = created_bones[parent_name]
                    # Connect to parent if same position
                    created_bones[name].use_connect = False

            bpy.ops.object.mode_set(mode='OBJECT')

            # Import ISKE meshes parented to armature
            self._import_iske_meshes(arm_obj)

        else:
            # v0005 fallback - flat structure with bone parts
            self._import_skeleton_flat(skeleton)

    def _import_skeleton_flat(self, skeleton: BESSkeleton):
        """Import skeletal bone parts with armature.

        Creates an armature with bones and mesh objects with Armature modifier.

        Args:
            skeleton: BES skeleton data
        """
        import math
        from mathutils import Euler, Vector

        # Create armature
        armature = bpy.data.armatures.new(skeleton.name)
        arm_obj = bpy.data.objects.new(skeleton.name, armature)
        self.context.collection.objects.link(arm_obj)
        self.context.view_layer.objects.active = arm_obj

        # Store original skeleton name for export (Blender may rename with .001 suffix)
        arm_obj['bes_skeleton_name'] = skeleton.name

        # BES skeletal models use Z-up coordinate system (same as Blender)
        # No armature rotation needed - vertices are already in world space

        # Enter edit mode to create bones
        bpy.ops.object.mode_set(mode='EDIT')

        # Create one bone per unique body part (use LOD 0, damage A only for bones)
        bone_positions = {}  # Store bone head/tail positions
        created_bones = {}

        for bone_part in skeleton.bone_parts:
            # Only create bones for LOD 0, damage state A (highest quality)
            if bone_part.lod_level != 0 or bone_part.damage_state != 'A':
                continue

            bone_name = bone_part.body_part
            if bone_name in created_bones:
                continue

            # Calculate bone position from mesh center
            if bone_part.meshes:
                positions = []
                for mesh in bone_part.meshes:
                    for vert in mesh.vertices:
                        positions.append(vert.position)

                if positions:
                    # Calculate center of mesh vertices
                    cx = sum(p[0] for p in positions) / len(positions)
                    cy = sum(p[1] for p in positions) / len(positions)
                    cz = sum(p[2] for p in positions) / len(positions)

                    # Use raw coordinates (BES uses Z-up like Blender)
                    head = Vector((cx, cy, cz))
                    tail = Vector((cx, cy, cz + 0.1))  # Small offset for bone length

                    edit_bone = armature.edit_bones.new(bone_name)
                    edit_bone.head = head
                    edit_bone.tail = tail
                    created_bones[bone_name] = edit_bone
                    bone_positions[bone_name] = head

        bpy.ops.object.mode_set(mode='OBJECT')

        # Import each bone part as a mesh, parented to armature
        for bone_part in skeleton.bone_parts:
            obj = self._import_bone_part(bone_part, arm_obj)
            if obj:
                # Parent to armature
                obj.parent = arm_obj
                obj.matrix_parent_inverse.identity()

                # Ensure valid scale after parenting
                # v0005 bone parts have zero-scale transforms (placeholder data)
                # Vertices are already in world space, so identity transform is correct
                if obj.scale[0] == 0 and obj.scale[1] == 0 and obj.scale[2] == 0:
                    obj.scale = (1.0, 1.0, 1.0)

                # Add Armature modifier
                mod = obj.modifiers.new(name="Armature", type='ARMATURE')
                mod.object = arm_obj

                # Create vertex group for this bone part's body part
                bone_name = bone_part.body_part
                if bone_name in created_bones:
                    if bone_name not in obj.vertex_groups:
                        vg = obj.vertex_groups.new(name=bone_name)
                        # Add all vertices with weight 1.0
                        vg.add(list(range(len(obj.data.vertices))), 1.0, 'REPLACE')

    def _collect_iske_bone_names(self) -> list:
        """Collect bone names from ISKE nodes in scene tree.

        Returns:
            List of bone names from ISKE nodes
        """
        names = []

        def traverse(node):
            if node.node_type == NodeType.ISKE:
                names.append(node.name)
            for child in node.children:
                traverse(child)

        if self.bes_file.root_node:
            traverse(self.bes_file.root_node)

        return names

    def _collect_iske_bone_transforms(self) -> dict:
        """Collect transforms from ISKE nodes.

        Returns:
            Dict mapping bone name to transform
        """
        transforms = {}

        def traverse(node):
            if node.node_type == NodeType.ISKE and node.transform:
                transforms[node.name] = node.transform
            for child in node.children:
                traverse(child)

        if self.bes_file.root_node:
            traverse(self.bes_file.root_node)

        return transforms

    def _import_iske_meshes(self, armature_obj):
        """Import meshes from ISKE nodes, parented to armature.

        Args:
            armature_obj: Blender armature object
        """
        def traverse(node, depth=0):
            if node.node_type == NodeType.ISKE and node.meshes:
                # Create mesh object for this ISKE node
                mesh_obj = self._create_mesh_object(node)
                if mesh_obj:
                    mesh_obj.parent = armature_obj
                    mesh_obj['bes_is_iske_mesh'] = True
                    mesh_obj['bes_bone_name'] = node.name

                    # Create vertex group for this bone
                    vg = mesh_obj.vertex_groups.new(name=node.name)
                    # Add all vertices with weight 1.0
                    vg.add(list(range(len(mesh_obj.data.vertices))), 1.0, 'REPLACE')

                    # Add Armature modifier
                    mod = mesh_obj.modifiers.new(name="Armature", type='ARMATURE')
                    mod.object = armature_obj

            for child in node.children:
                traverse(child, depth + 1)

        if self.bes_file.root_node:
            traverse(self.bes_file.root_node)

    def _import_bone_part(self, bone_part: BESBonePart, parent_obj) -> Optional[bpy.types.Object]:
        """Import a single bone part as a mesh object.

        BES transform matrix is in WORLD SPACE. We must NOT set parent
        and apply transform together - that would cause double transformation.
        Instead, use matrix_world directly.

        Args:
            bone_part: BES bone part data
            parent_obj: Parent Blender object (stored as reference only)

        Returns:
            Created Blender mesh object
        """
        if not bone_part.meshes:
            return None

        # Create mesh object
        obj = self._create_bone_mesh_object(bone_part)

        if obj:
            # v0005 skeletal models: Transform handling
            # Most v0005 TRANSFORM chunks contain zeros (placeholder data),
            # meaning vertices are already in world space.
            #
            # If transform has a valid matrix, use matrix_world directly.
            # This avoids issues with parent transforms and coordinate systems.
            if bone_part.transform and bone_part.transform.matrix:
                # Check if matrix has valid rotation/scale (upper-left 3x3 not all zeros)
                mat = bone_part.transform.matrix
                is_identity = all(
                    mat[i][j] == (1.0 if i == j else 0.0)
                    for i in range(4) for j in range(4)
                )
                # Check if the 3x3 rotation/scale part is all zeros (invalid)
                has_zero_scale = all(
                    mat[i][j] == 0.0
                    for i in range(3) for j in range(3)
                )

                if not is_identity and not has_zero_scale:
                    # Apply non-trivial matrix transform
                    from ..utils.math_utils import bes_matrix_to_blender
                    blender_mat = bes_matrix_to_blender(mat)
                    obj.matrix_world = Matrix(blender_mat)

            # Ensure valid scale (v0005 models may have zero scale in transforms)
            # Vertices are already in world space, so we just need identity transform
            if obj.scale[0] == 0 and obj.scale[1] == 0 and obj.scale[2] == 0:
                obj.scale = (1.0, 1.0, 1.0)

            # Store bone part properties
            obj['bes_is_bone_part'] = True
            obj['bes_bone_name'] = bone_part.name
            obj['bes_lod_level'] = bone_part.lod_level
            obj['bes_damage_state'] = bone_part.damage_state
            obj['bes_body_part'] = bone_part.body_part
            obj['bes_part_index'] = bone_part.part_index

            # Store original transform for roundtrip export
            if bone_part.transform:
                obj['bes_original_matrix'] = json.dumps(bone_part.transform.matrix)
                obj['bes_original_translation'] = list(bone_part.transform.translation)
                obj['bes_original_rotation'] = list(bone_part.transform.rotation)
                obj['bes_original_scale'] = list(bone_part.transform.scale)

            # Hide non-LOD0 and non-A (undamaged) parts by default
            if bone_part.lod_level > 0 or bone_part.damage_state != 'A':
                obj.hide_viewport = True
                obj.hide_render = True

        return obj

    def _create_bone_mesh_object(self, bone_part: BESBonePart) -> bpy.types.Object:
        """Create Blender mesh object from bone part.

        Handles BESBoneVertex format (36-52 byte skeletal vertices with 1-5 bone weights).

        Bone weight preservation:
        - All weights per vertex are stored as JSON in mesh custom property 'bes_bone_weights'
        - Bone indices per vertex stored as JSON in 'bes_bone_indices'
        - Vertex flags stored in 'bes_vertex_flags' for export format detection

        Args:
            bone_part: BES bone part with mesh data

        Returns:
            Created Blender mesh object
        """
        # Create mesh data
        mesh = bpy.data.meshes.new(bone_part.name)

        # Combine all meshes in bone part
        all_verts = []
        all_faces = []
        all_uvs = []
        all_bone_weights = []  # Store ALL bone weights per vertex (list of lists)
        all_bone_indices = []  # Store ALL bone indices per vertex (list of lists)
        all_vertex_flags = []  # Store vertex flags for export
        face_materials = []
        vert_offset = 0

        for bes_mesh in bone_part.meshes:
            # Add vertices - use raw coordinates (no conversion needed)
            # Consistent with regular mesh import which uses raw coords
            for vert in bes_mesh.vertices:
                all_verts.append(vert.position)

                # Handle BESBoneVertex (has .uv) vs BESVertex (has .uvs)
                if isinstance(vert, BESBoneVertex):
                    all_uvs.append(bes_to_blender_uv(vert.uv))
                    # Store ALL weights and bone indices, not just the first one
                    all_bone_weights.append(list(vert.weights))
                    all_bone_indices.append(list(vert.bone_indices))
                    all_vertex_flags.append(vert.flags)
                elif hasattr(vert, 'uvs') and vert.uvs:
                    all_uvs.append(bes_to_blender_uv(vert.uvs[0]))
                    all_bone_weights.append([1.0])
                    all_bone_indices.append([])
                    all_vertex_flags.append(vert.flags if hasattr(vert, 'flags') else 0)
                else:
                    all_uvs.append((0.0, 0.0))
                    all_bone_weights.append([1.0])
                    all_bone_indices.append([])
                    all_vertex_flags.append(0)

            # Add faces with offset
            for face in bes_mesh.faces:
                all_faces.append((
                    face.a + vert_offset,
                    face.b + vert_offset,
                    face.c + vert_offset,
                ))
                face_materials.append(bes_mesh.material_index)

            vert_offset += len(bes_mesh.vertices)

        # Build mesh
        mesh.from_pydata(all_verts, [], all_faces)

        # Create UV layer
        if all_uvs:
            uv_layer = mesh.uv_layers.new(name='UVMap')
            for poly in mesh.polygons:
                for loop_idx in poly.loop_indices:
                    vert_idx = mesh.loops[loop_idx].vertex_index
                    if vert_idx < len(all_uvs):
                        uv_layer.data[loop_idx].uv = all_uvs[vert_idx]

        # Assign materials
        unique_mat_indices = list(set(face_materials))
        for mat_idx in unique_mat_indices:
            if mat_idx in self._materials:
                mesh.materials.append(self._materials[mat_idx])

        # Set material indices for faces
        if len(unique_mat_indices) > 1:
            for poly_idx, mat_idx in enumerate(face_materials):
                if mat_idx in unique_mat_indices:
                    mesh.polygons[poly_idx].material_index = unique_mat_indices.index(mat_idx)

        mesh.update()
        mesh.validate()

        # Create object
        obj = bpy.data.objects.new(bone_part.name, mesh)
        self.context.collection.objects.link(obj)

        # Store bone weight data for roundtrip export
        # Using JSON to preserve the full weight structure per vertex
        if all_bone_weights:
            obj['bes_bone_weights'] = json.dumps(all_bone_weights)
        if all_bone_indices:
            obj['bes_bone_indices'] = json.dumps(all_bone_indices)
        if all_vertex_flags:
            # Store first vertex flags (all should be same for mesh)
            obj['bes_vertex_flags'] = all_vertex_flags[0] if all_vertex_flags else 0

        # Create vertex group for visualization (using first weight only)
        vg = obj.vertex_groups.new(name=bone_part.name)
        for vert_idx, weights in enumerate(all_bone_weights):
            if weights and weights[0] > 0:
                vg.add([vert_idx], weights[0], 'REPLACE')

        return obj
