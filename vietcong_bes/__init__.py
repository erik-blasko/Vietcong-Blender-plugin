# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2024 Vietcong Blender Tools Contributors

"""
Vietcong BES Format Import/Export Plugin for Blender 4.x

This plugin provides full import and export support for the BES (Binary Export Scene)
format used by Vietcong (2003) and Vietcong 2 (2005) games running on Ptero-Engine-II.

Features:
- Import: Geometry, materials (PteroMat), textures, hierarchy, LOD, transforms
- Export: Full scene export with materials, LOD, properties
- UI: Material panels, LOD management, collision material assignment
"""

bl_info = {
    "name": "Vietcong BES Format",
    "author": "Erik Blasko (fork of desintegrathor's plugin)",
    "version": (1, 0, 0),
    "blender": (4, 0, 0),
    "location": "File > Import/Export > Vietcong BES (.bes)",
    "description": "Import and Export Vietcong BES model files",
    "warning": "",
    "doc_url": "https://github.com/erik-blasko/Vietcong-Blender-plugin",
    "tracker_url": "https://github.com/erik-blasko/Vietcong-Blender-plugin/issues",
    "category": "Import-Export",
}

import bpy

# Import submodules
from . import core
from . import importers
from . import exporters
from . import ui
from . import materials
from . import utils


# Classes to register
classes = []


def menu_func_import(self, context):
    """Add import menu entry."""
    self.layout.operator(
        importers.IMPORT_OT_bes.bl_idname,
        text="Vietcong BES (.bes)"
    )
    self.layout.operator(
        importers.IMPORT_OT_stg.bl_idname,
        text="Vietcong STG Animation (.stg)"
    )


def menu_func_export(self, context):
    """Add export menu entry."""
    self.layout.operator(
        exporters.EXPORT_OT_bes.bl_idname,
        text="Vietcong BES (.bes)"
    )
    self.layout.operator(
        exporters.EXPORT_OT_stg.bl_idname,
        text="Vietcong STG Animation (.stg)"
    )


def register():
    """Register the addon."""
    # Register core modules first
    core.register()
    utils.register()
    materials.register()

    # Register importers/exporters
    importers.register()
    exporters.register()

    # Register UI
    ui.register()

    # Add menu entries
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)
    bpy.types.TOPBAR_MT_file_export.append(menu_func_export)

    print(f"Vietcong BES Plugin v{'.'.join(map(str, bl_info['version']))} registered")


def unregister():
    """Unregister the addon."""
    # Remove menu entries
    bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)

    # Unregister in reverse order
    ui.unregister()
    exporters.unregister()
    importers.unregister()
    materials.unregister()
    utils.unregister()
    core.unregister()

    print("Vietcong BES Plugin unregistered")


if __name__ == "__main__":
    register()
