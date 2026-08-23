#!/bin/bash
# Launcher for Sider under Lutris / plain Wine (e.g. Football Life).
#
# 1. Copy this file next to your game (or anywhere you like).
# 2. Edit the 4 values below.
# 3. In Lutris, set this script as the game's executable
#    (Configure -> Game options -> Executable), instead of the game's own
#    .exe or its .bat file.
#
# This replaces sider.exe entirely -- it starts the game itself and injects
# sider.dll into it once the game is ready. No more sider.exe window, no
# more waiting on a log file before starting the game.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ↓↓↓ EDIT THESE FOUR LINES ↓↓↓ ---------------------------------------------
PREFIX="/path/to/your/wineprefix"                            # the Wine prefix (contains a drive_c folder)
GAME_EXE="$PREFIX/drive_c/Games/YourGame/game.exe"            # the game's main .exe
SIDER_DLL="$PREFIX/drive_c/Games/YourGame/sider/sider.dll"    # sider.dll (see main README for where to get this)
WINE_BIN="wine"                                               # leave as "wine", or point at a specific build
# ↑↑↑ ------------------------------------------------------------------- ↑↑↑

exec "$DIR/.venv/bin/sider-inject-python" -u "$DIR/sider_inject.py" \
  --prefix "$PREFIX" \
  --exe "$GAME_EXE" \
  --dll "$SIDER_DLL" \
  --wine "$WINE_BIN" \
  --timeout 120 \
  --wait-exit
