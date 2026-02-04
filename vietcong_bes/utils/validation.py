# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2024 Vietcong Blender Tools Contributors

"""
BES Export Validation

Validates Blender scenes before BES export, checking for common
issues that could cause problems in the game engine.
"""

import bpy
import math
from typing import List, Tuple, Set, Optional
from dataclasses import dataclass, field


@dataclass
class ValidationResult:
    """Result of validation check."""
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    info: List[str] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return len(self.errors) > 0

    @property
    def is_valid(self) -> bool:
        return not self.has_errors


# Object naming prefixes and their meanings
VALID_PREFIXES = {
    '^K': 'Collision mesh (uses physics)',
    '^SF': 'Sector/Fake sector',
    '^SK': 'Skeleton bone',
    '^SL': 'Slot',
    '!': 'Hidden object',
    '@': 'Target/Helper',
    '>': 'LOD parent',
}

# Maximum limits for BES format
MAX_VERTICES_PER_MESH = 65535
MAX_FACES_PER_MESH = 65535
MAX_MATERIALS = 256
MAX_NAME_LENGTH = 64


def get_exportable_objects(context) -> List[bpy.types.Object]:
    """Get list of objects that will be exported."""
    objects = []

    # If objects are selected, export those
    if context.selected_objects:
        for obj in context.selected_objects:
            if obj.type in {'MESH', 'EMPTY', 'ARMATURE'}:
                objects.append(obj)
    else:
        # Export all visible objects
        for obj in context.scene.objects:
            if obj.visible_get() and obj.type in {'MESH', 'EMPTY', 'ARMATURE'}:
                objects.append(obj)

    return objects


def get_used_materials(context) -> Set[bpy.types.Material]:
    """Get set of materials used by exportable objects."""
    materials = set()

    for obj in get_exportable_objects(context):
        if obj.type == 'MESH' and obj.data.materials:
            for mat in obj.data.materials:
                if mat is not None:
                    materials.add(mat)

    return materials


def validate_mesh(obj: bpy.types.Object, result: ValidationResult) -> None:
    """Validate a mesh object."""
    if obj.type != 'MESH':
        return

    mesh = obj.data
    name = obj.name

    # Check for empty mesh
    if len(mesh.vertices) == 0:
        result.errors.append(f"'{name}': Mesh has no vertices")
        return

    if len(mesh.polygons) == 0:
        result.errors.append(f"'{name}': Mesh has no faces")
        return

    # Check vertex limits
    vert_count = len(mesh.vertices)
    if vert_count > MAX_VERTICES_PER_MESH:
        result.errors.append(
            f"'{name}': Too many vertices ({vert_count}), max is {MAX_VERTICES_PER_MESH}"
        )

    # Check face limits
    face_count = len(mesh.polygons)
    if face_count > MAX_FACES_PER_MESH:
        result.errors.append(
            f"'{name}': Too many faces ({face_count}), max is {MAX_FACES_PER_MESH}"
        )

    # Check for NaN/Inf in vertex positions
    for i, vert in enumerate(mesh.vertices):
        for j, coord in enumerate(vert.co):
            if math.isnan(coord) or math.isinf(coord):
                result.errors.append(
                    f"'{name}': Vertex {i} has invalid coordinate (NaN/Inf)"
                )
                break

    # Check for UV maps
    if not mesh.uv_layers:
        result.warnings.append(f"'{name}': No UV map found")
    elif not mesh.uv_layers.active:
        result.warnings.append(f"'{name}': No active UV map")

    # Check for degenerate faces (zero area)
    mesh.calc_loop_triangles()
    for tri in mesh.loop_triangles:
        if tri.area < 1e-8:
            result.warnings.append(f"'{name}': Contains degenerate (zero-area) faces")
            break

    # Check for loose vertices
    used_verts = set()
    for poly in mesh.polygons:
        used_verts.update(poly.vertices)

    loose_count = len(mesh.vertices) - len(used_verts)
    if loose_count > 0:
        result.warnings.append(f"'{name}': Has {loose_count} loose vertices")


def validate_object_name(obj: bpy.types.Object, result: ValidationResult) -> None:
    """Validate object naming conventions."""
    name = obj.name

    # Check name length
    if len(name) > MAX_NAME_LENGTH:
        result.errors.append(
            f"'{name}': Name too long ({len(name)} chars), max is {MAX_NAME_LENGTH}"
        )

    # Check for Blender's duplicate naming
    if name.endswith(('.001', '.002', '.003')):
        result.warnings.append(
            f"'{name}': Duplicate name (Blender added suffix)"
        )

    # Check for invalid characters
    invalid_chars = set('<>:"/\\|?*')
    found_invalid = [c for c in name if c in invalid_chars]
    if found_invalid:
        result.warnings.append(
            f"'{name}': Contains invalid characters: {found_invalid}"
        )

    # Validate prefixes
    for prefix in VALID_PREFIXES.keys():
        if name.startswith(prefix):
            # Valid prefix found
            return

    # Check for unknown special prefix
    if name.startswith(('^', '!', '@', '>')):
        first_char = name[0]
        if name.startswith('^'):
            # Get the full prefix (up to 3 chars)
            prefix_end = min(3, len(name))
            prefix = name[:prefix_end]
            if prefix not in VALID_PREFIXES:
                result.warnings.append(
                    f"'{name}': Unknown prefix '{prefix}'"
                )


def validate_material(mat: bpy.types.Material, result: ValidationResult) -> None:
    """Validate a material."""
    name = mat.name

    if not hasattr(mat, 'bes'):
        result.info.append(f"Material '{name}': No BES properties")
        return

    bes = mat.bes

    # Check collision material code
    if bes.collision_material:
        from ..materials.collision_materials import COLLISION_MATERIALS
        code = bes.collision_material
        if code not in COLLISION_MATERIALS and code != '- ':
            result.warnings.append(
                f"Material '{name}': Unknown collision material code '{code}'"
            )

    # Check color values
    def check_color(color_name, color_value):
        for i, c in enumerate(color_value):
            if c < 0 or c > 1:
                result.warnings.append(
                    f"Material '{name}': {color_name} color out of range (0-1)"
                )
                break

    check_color("Diffuse", bes.diffuse_color)
    check_color("Ambient", bes.ambient_color)
    check_color("Specular", bes.specular_color)

    # Check texture files if pteromat
    if bes.material_type == 'pteromat':
        from .texture_utils import texture_exists

        texture_props = [
            'tex_diffuse_1', 'tex_diffuse_2', 'tex_diffuse_3',
            'tex_environment_1', 'tex_environment_2',
            'tex_lightmap', 'tex_overlay_multi'
        ]

        for prop_name in texture_props:
            tex_name = getattr(bes, prop_name, '')
            if tex_name and not texture_exists(tex_name):
                result.warnings.append(
                    f"Material '{name}': Texture '{tex_name}' not found in game directory"
                )


def validate_hierarchy(context, result: ValidationResult) -> None:
    """Validate object hierarchy."""
    objects = get_exportable_objects(context)

    # Check for circular parent references (shouldn't happen in Blender, but check anyway)
    for obj in objects:
        visited = set()
        current = obj
        while current is not None:
            if current in visited:
                result.errors.append(
                    f"'{obj.name}': Circular parent reference detected"
                )
                break
            visited.add(current)
            current = current.parent

    # Check hierarchy depth
    MAX_DEPTH = 32
    for obj in objects:
        depth = 0
        current = obj
        while current.parent is not None:
            depth += 1
            current = current.parent
            if depth > MAX_DEPTH:
                result.warnings.append(
                    f"'{obj.name}': Very deep hierarchy ({depth} levels)"
                )
                break


def validate_transforms(context, result: ValidationResult) -> None:
    """Validate object transforms."""
    for obj in get_exportable_objects(context):
        # Check for non-uniform scale
        scale = obj.scale
        if abs(scale.x - scale.y) > 0.001 or abs(scale.y - scale.z) > 0.001:
            result.warnings.append(
                f"'{obj.name}': Non-uniform scale {tuple(round(s, 3) for s in scale)}"
            )

        # Check for negative scale
        if scale.x < 0 or scale.y < 0 or scale.z < 0:
            result.warnings.append(
                f"'{obj.name}': Negative scale may cause issues"
            )

        # Check for very large coordinates
        MAX_COORD = 100000.0
        loc = obj.location
        if abs(loc.x) > MAX_COORD or abs(loc.y) > MAX_COORD or abs(loc.z) > MAX_COORD:
            result.warnings.append(
                f"'{obj.name}': Very large location values"
            )


def validate_bes_export(context) -> ValidationResult:
    """
    Perform full validation of scene for BES export.

    Returns:
        ValidationResult with errors, warnings, and info
    """
    result = ValidationResult()

    objects = get_exportable_objects(context)
    materials = get_used_materials(context)

    # Basic checks
    if not objects:
        result.errors.append("No objects to export")
        return result

    # Count statistics
    total_verts = 0
    total_faces = 0
    mesh_count = 0

    for obj in objects:
        if obj.type == 'MESH':
            mesh_count += 1
            total_verts += len(obj.data.vertices)
            total_faces += len(obj.data.polygons)

    # Add statistics as info
    result.info.append(f"Objects: {len(objects)} ({mesh_count} meshes)")
    result.info.append(f"Total vertices: {total_verts}")
    result.info.append(f"Total faces: {total_faces}")
    result.info.append(f"Materials: {len(materials)}")

    # Check material limit
    if len(materials) > MAX_MATERIALS:
        result.errors.append(
            f"Too many materials ({len(materials)}), max is {MAX_MATERIALS}"
        )

    # Validate each object
    for obj in objects:
        validate_mesh(obj, result)
        validate_object_name(obj, result)

    # Validate materials
    for mat in materials:
        validate_material(mat, result)

    # Validate hierarchy
    validate_hierarchy(context, result)

    # Validate transforms
    validate_transforms(context, result)

    return result


def format_validation_report(result: ValidationResult) -> str:
    """Format validation result as human-readable report."""
    lines = []

    lines.append("=" * 50)
    lines.append("BES Export Validation Report")
    lines.append("=" * 50)

    if result.errors:
        lines.append(f"\nERRORS ({len(result.errors)}):")
        for error in result.errors:
            lines.append(f"  [!] {error}")

    if result.warnings:
        lines.append(f"\nWARNINGS ({len(result.warnings)}):")
        for warning in result.warnings:
            lines.append(f"  [?] {warning}")

    if result.info:
        lines.append(f"\nINFO:")
        for info in result.info:
            lines.append(f"  [i] {info}")

    lines.append("")
    if result.is_valid:
        lines.append("RESULT: VALID - Ready for export")
    else:
        lines.append("RESULT: INVALID - Fix errors before export")

    lines.append("=" * 50)

    return "\n".join(lines)
