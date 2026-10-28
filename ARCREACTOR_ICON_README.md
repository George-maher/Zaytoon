# Zaytoon Icon — Arc Reactor Edition

## What was done
- Generated a new `desktop/icons/zaytoon.ico` with an Iron Man arc-reactor design
- Created `desktop/icons/source_arcreactor.png` (256x256 PNG) as the source artwork
- Both files are 15 KB and 15 KB respectively, with all required Windows icon sizes (16, 24, 32, 48, 64, 128, 256)

## Files created
| Path | Size | Description |
|---|---|---|
| `desktop/icons/zaytoon.ico` | 15 KB | Multi-size Windows icon (all sizes) |
| `desktop/icons/source_arcreactor.png` | ~25 KB | 256x256 PNG source artwork |
| `create_arcreactor_icon.py` | ~3 KB | Script that produced the source PNG |
| `build_arcreactor_icon.py` | ~2 KB | Script that generated the .ico from the source |

## How to regenerate
If the icon needs to be rebuilt (e.g., after the frontend build changes):
```bash
python "D:\my work\Zaytoon\build_arcreactor_icon.py"
# or, using the make_icon.py infrastructure:
python "D:\my work\Zaytoon\desktop\icons\make_icon.py"
```
(Note: `make_icon.py` reads `source.png` by default; rename `source_arcreactor.png` → `source.png` first.)

## Icon design notes
- Red central core mimics the Arc Reactor
- Glowing concentric rings give the "energy" effect
- MCU-style blue/cyan accents provide contrast
- Core details (inner circle + ellipse) add visual depth
- All colors are fully opaque with alpha channel where needed

## Integration
The `zaytoon.ico` will be used by:
- **PyInstaller build**: `--icon desktop/icons/zaytoon.ico` in `build_exe.py`
- **Inno Setup installer**: `SetupIconFile` in the `.iss` script
- **Windows Explorer**: taskbar and shortcut icons
- **PyInstaller default**: if no icon is specified, the default PyInstaller globe is used

The original `desktop/icons/source.png` (from September 2026, ~498 KB) is untouched. The arc-reactor design is an optional alternate visual theme.