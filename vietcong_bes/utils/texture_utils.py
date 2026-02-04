# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2024 Vietcong Blender Tools Contributors

"""
Texture Utilities

Functions for finding and managing textures from the Vietcong game directory.
"""

import os
from typing import Optional, List


# Supported texture formats (Vietcong uses TGA, DDS, BMP)
TEXTURE_EXTENSIONS = ('.tga', '.dds', '.bmp')
TEXTURE_EXTENSIONS_ALL_CASES = ['.dds', '.DDS', '.tga', '.TGA', '.bmp', '.BMP']


def get_addon_preferences():
    """Get the addon preferences object."""
    import bpy
    addon_name = "vietcong_bes"

    # Try to get preferences
    if hasattr(bpy.context, 'preferences'):
        prefs = bpy.context.preferences
        if hasattr(prefs, 'addons') and addon_name in prefs.addons:
            return prefs.addons[addon_name].preferences

    return None


def get_game_path() -> Optional[str]:
    """Get the game directory path from addon preferences."""
    prefs = get_addon_preferences()
    if prefs and prefs.game_path:
        path = prefs.game_path
        if os.path.isdir(path):
            return path
    return None


def get_game_texture_path() -> Optional[str]:
    """Get the game directory path for texture browsing.

    Note: In Vietcong, textures and models are mixed in the same folders.
    """
    return get_game_path()


def scan_textures(path: str, extensions: tuple = TEXTURE_EXTENSIONS) -> List[str]:
    """
    Recursively scan directory for texture files.

    Args:
        path: Directory to scan
        extensions: Tuple of valid extensions (lowercase)

    Returns:
        List of absolute file paths
    """
    textures = []

    if not os.path.isdir(path):
        return textures

    for root, dirs, files in os.walk(path):
        for filename in files:
            if filename.lower().endswith(extensions):
                textures.append(os.path.join(root, filename))

    return sorted(textures)


def find_texture_in_game(filename: str) -> Optional[str]:
    """
    Find a texture file in the game directories.

    Uses the same search logic as the BES importer:
    - Searches recursively in the game directory
    - Tries different extensions (DDS, TGA, BMP)
    - Case-insensitive matching

    Args:
        filename: Texture filename (can be just name or relative path)

    Returns:
        Absolute path if found, None otherwise
    """
    if not filename:
        return None

    # Get base name without extension
    base_name = os.path.splitext(os.path.basename(filename))[0]
    base_name_lower = base_name.lower()

    # Get game path
    game_path = get_game_path()
    if not game_path:
        return None

    # Search recursively in game directory
    for root, dirs, files in os.walk(game_path):
        # Build lowercase lookup for this directory
        files_lower = {f.lower(): f for f in files}

        # Try each extension
        for ext in TEXTURE_EXTENSIONS_ALL_CASES:
            candidate = base_name_lower + ext.lower()
            if candidate in files_lower:
                return os.path.join(root, files_lower[candidate])

    return None


def get_relative_texture_path(filepath: str) -> str:
    """
    Get texture path relative to game texture directory.

    Args:
        filepath: Absolute file path

    Returns:
        Relative path or just filename if not in game directory
    """
    texture_path = get_game_texture_path()
    if texture_path and filepath.startswith(texture_path):
        rel_path = os.path.relpath(filepath, texture_path)
        return rel_path

    return os.path.basename(filepath)


def list_textures_in_directory(directory: str = None, filter_pattern: str = "") -> List[dict]:
    """
    List textures in a directory with metadata.

    Args:
        directory: Directory to list (default: game texture path)
        filter_pattern: Optional filter pattern for filename

    Returns:
        List of dicts with 'name', 'path', 'size' keys
    """
    if directory is None:
        directory = get_game_texture_path()

    if not directory or not os.path.isdir(directory):
        return []

    textures = []
    filter_lower = filter_pattern.lower() if filter_pattern else ""

    for filename in os.listdir(directory):
        filepath = os.path.join(directory, filename)

        # Skip directories (for this flat listing)
        if os.path.isdir(filepath):
            continue

        # Check extension
        if not filename.lower().endswith(TEXTURE_EXTENSIONS):
            continue

        # Apply filter
        if filter_lower and filter_lower not in filename.lower():
            continue

        textures.append({
            'name': filename,
            'path': filepath,
            'size': os.path.getsize(filepath)
        })

    return sorted(textures, key=lambda x: x['name'].lower())


def texture_exists(filename: str) -> bool:
    """
    Check if a texture file exists in the game directories.

    Args:
        filename: Texture filename

    Returns:
        True if texture exists, False otherwise
    """
    return find_texture_in_game(filename) is not None
