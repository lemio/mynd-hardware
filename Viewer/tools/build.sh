#!/bin/sh
# Rebuild the viewer data from the KiCad project (run from anywhere; takes about two minutes).
#   circuit:  Viewer/mynd_main.sch.json     sheets → areas, parts with Schematik kinds, nets with colour groups and purposes
#             (tools/build_schematic.py + tools/simplify.py + tools/annotations.py; asks kicad-cli for the netlist)
#   copper:   Viewer/mynd_main.copper.json  tracks, pads, pours and vias per net, all four layers (tools/build_board.py).
#             The Altium import's pours are stored unfilled, so a temporary copy of the board is refilled first
#             (kicad-cli pcb drc --refill-zones --save-board); the board file in the repo is not touched.
#   3D board: Viewer/mynd_main.glb          kicad-cli's GLB export: parts, board, mask and silkscreen (the page draws the copper)
set -e
cd "$(dirname "$0")/../.."
KICAD_CLI="${KICAD_CLI:-/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli}"
BOARD=PCB_Schematics/KiCad/Main/Mynd_Main
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
python3 Viewer/tools/build_schematic.py
cp "$BOARD.kicad_pcb" "$BOARD.kicad_pro" "$TMP/"
"$KICAD_CLI" pcb drc --refill-zones --save-board -o "$TMP/drc.rpt" "$TMP/Mynd_Main.kicad_pcb" 2>&1 | grep -v Fontconfig | grep -i -e saved -e error || true
MYND_PCB="$TMP/Mynd_Main.kicad_pcb" python3 Viewer/tools/build_board.py
"$KICAD_CLI" pcb export glb -f --include-silkscreen --include-soldermask \
  -o Viewer/mynd_main.glb "$BOARD.kicad_pcb" 2>&1 | grep -v -e Fontconfig -e "without triangulation" || true
ls -la Viewer/mynd_main.*
