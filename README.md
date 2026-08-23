# Sider 7 for Linux

Run [Sider](https://github.com/pes-modding/sider7) — the Lua-modding
companion tool for Pro Evolution Soccer 2021 / eFootball (Football Life,
livecpk mods, gameplay tweaks, etc.) — natively on Linux, via Lutris or
Steam Proton. No `sider.exe`, no waiting on batch files, no Wine hook
workarounds that sometimes just don't fire.

You already have everything you need if you're currently running Sider on
Windows or through Wine: this tool just changes *how `sider.dll` gets
loaded into the game*, not what Sider itself does.

## What this is (and isn't)

- ✅ A small Linux launcher that starts your game and loads your existing
  `sider.dll` into it — a drop-in replacement for `sider.exe`.
- ✅ Works with Football Life via Lutris, and the Steam release of
  eFootball PES 2021 via Proton.
- ❌ Not a copy of Sider itself. You still need your own `sider.dll` —
  the same one you'd use on Windows. Get it wherever you normally get
  Sider, or build it from [pes-modding/sider7](https://github.com/pes-modding/sider7)'s
  source. This project doesn't include or redistribute it.
- ❌ Not a mod, not game files, not affiliated with Konami/pes-modding.

## Requirements

- Linux with Python 3.10 or newer
- [`xdotool`](https://github.com/jordansissel/xdotool) — install via your
  package manager, e.g.:
  - Arch: `sudo pacman -S xdotool`
  - Debian/Ubuntu: `sudo apt install xdotool`
  - Fedora: `sudo dnf install xdotool`
- A working Lutris/Wine or Steam/Proton install of your game
- Your own `sider.dll` (see above)

## Setup (do this once)

```sh
git clone https://github.com/FabianZettl/sider7-linux.git
cd sider7-linux
./setup.sh
```

`setup.sh` creates an isolated Python environment and prints one more
command for you to run yourself (it needs your password, so the script
won't run it for you):

```sh
sudo setcap cap_sys_ptrace+eip "/path/it/prints/.venv/bin/sider-inject-python"
```

This grants the one specific permission the injector needs (to attach to
your game process) to its own private copy of the Python interpreter —
nothing system-wide.

## Using it with Football Life (Lutris)

1. Copy [`run-lutris.sh`](run-lutris.sh) somewhere convenient (next to your
   game folder is fine).
2. Open it in a text editor and fill in the 4 marked lines: your Wine
   prefix path, the game's `.exe`, your `sider.dll` path, and the Wine
   binary to use.
3. In Lutris, open the game's configuration → **Game options** → set
   **Executable** to your edited `run-lutris.sh` instead of the game's own
   `.exe` or `.bat`.
4. Launch the game from Lutris as usual. `run-lutris.sh` starts the game
   and injects Sider automatically — no separate `sider.exe` step.

## Using it with PES 2021 (Steam)

1. Copy [`run-steam.sh`](run-steam.sh) somewhere convenient (next to your
   `sider.dll` is fine).
2. Open it in a text editor and fill in the 3 marked lines: the game's
   Steam AppID (already set to PES 2021's `1259970`), the game's `.exe`
   path, and your `sider.dll` path.
3. In Steam, right-click **eFootball PES 2021** → **Properties** →
   **General** → **Launch Options**, and enter:
   ```
   "/full/path/to/run-steam.sh" %command%
   ```
4. Click **Play** as usual. Sider gets injected automatically in the
   background while the game starts normally.

## Troubleshooting

- **"ptrace(...): Operation not permitted"** — the `setcap` step from Setup
  wasn't done (or you re-ran `setup.sh`, which needs it redone each time).
- **It times out waiting for the process / a window** — some games take a
  while on first launch (shader cache warmup especially). Add
  `--timeout 300` (or more) to the `sider_inject.py` call in your script.
  For deeper debugging, add `--debug-log inject-debug.log` too.
- **A message about "LoadLibraryW returned NULL", then it retries and
  succeeds** — this is normal, not an error. It just means the first
  attempt raced the game's own startup; the tool retries automatically.
- Still stuck? Open an issue with your `--debug-log` output attached.

## How it works

Short version: Sider's actual work all happens inside `sider.dll`'s
`DllMain`, regardless of how the DLL gets loaded — the Windows-only hook
mechanism `sider.exe` normally uses to trigger that is just one way to get
there. This tool finds your game process directly and triggers the same
`LoadLibraryW(sider.dll)` call itself, natively, without needing Wine to
emulate that Windows hook mechanism at all.

For the full technical write-up (the `ptrace` mechanics, why timing matters,
register-state pitfalls and how they're handled) see
[HOW_IT_WORKS.md](HOW_IT_WORKS.md).

## Credits & license

All credit for Sider itself goes to the [pes-modding](https://github.com/pes-modding/sider7)
project. This launcher is independent, original code, released under the
[MIT license](LICENSE) — see the license file for the (short) disclaimer
about what this project does and doesn't include.
