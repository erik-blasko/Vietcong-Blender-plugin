# Vietcong Blender Plugin

Import/export plugin for Vietcong BES model files (Blender 4.x / 5.x).

## Features

- **BES Import/Export**: Full geometry, materials (Standard, PteroMat, PteroLayer), textures, hierarchy
- **LOD Management**: Import/export LOD levels with visibility controls
- **STG/STO Animations**: Import and export skeletal animations with events
- **User Properties**: Wobble, LOD distances, lighting, physics, doors, collision
- **Skeletal Models**: Bone hierarchy with damage states and body parts
- **Collision Materials**: Surface type assignment and management

## Installation

1. Pack the `vietcong_bes/` folder into a `.zip` — the zip must contain the folder itself (not just its contents)
2. Open Blender and drag-and-drop the `.zip` onto the Blender window
3. Or: Edit > Preferences > Add-ons > Install > select the `.zip`

## Usage

- **Import BES**: File > Import > Vietcong BES (.bes)
- **Export BES**: File > Export > Vietcong BES (.bes)
- **Import Animation**: File > Import > Vietcong STG Animation (.stg)
- **Export Animation**: File > Export > Vietcong STG Animation (.stg)

## Credits

- **Original plugin**: [desintegrathor](https://github.com/desintegrathor/Vietcong-Blender-plugin) — initial BES import/export implementation
- **Contributors**: [Erik Blasko](https://github.com/erik-blasko) — Blender 5.x compatibility, STG/STO support, export, materials
- **Vietcong game**: Ptero-Engine-II by Pterodon / Illusion Softworks (2003)

## License

GPL-3.0-or-later
