#!/bin/bash
# Launcher for Sider under Steam / Proton (e.g. eFootball PES 2021).
#
# 1. Copy this file anywhere you like (e.g. next to sider.dll).
# 2. Edit the 3 values below.
# 3. In Steam, right-click the game -> Properties -> General -> Launch
#    Options, and set it to:
#
#      "/full/path/to/run-steam.sh" %command%
#
# Steam substitutes %command% with its normal Proton launch command. This
# script starts the injector in the background (to catch the game once it's
# ready) and then runs that command normally -- Steam's own process
# tracking, overlay, and playtime tracking all keep working as usual.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ↓↓↓ EDIT THESE THREE LINES ↓↓↓ --------------------------------------------
APPID="1259970"                                                          # the game's Steam AppID (1259970 = eFootball PES 2021)
GAME_EXE="/path/to/steamapps/common/eFootball PES 2021/PES2021.exe"      # the game's main .exe
SIDER_DLL="/path/to/steamapps/common/eFootball PES 2021/sider/sider.dll" # sider.dll (see main README for where to get this)
# ↑↑↑ ------------------------------------------------------------------- ↑↑↑

# Standard Steam library layout: steamapps/compatdata/<appid>/pfx
STEAM_LIBRARY="$(cd "$(dirname "$GAME_EXE")/../.." && pwd)"
PREFIX="$STEAM_LIBRARY/compatdata/$APPID/pfx"

"$DIR/.venv/bin/sider-inject-python" -u "$DIR/sider_inject.py" \
  --no-launch \
  --prefix "$PREFIX" \
  --exe "$GAME_EXE" \
  --dll "$SIDER_DLL" \
  --timeout 120 \
  >"$DIR/inject.log" 2>&1 &

exec "$@"
