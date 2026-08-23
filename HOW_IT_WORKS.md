# How it works

## Why this exists

On Windows, `sider.exe` installs a **global Windows hook**
(`SetWindowsHookEx(WH_CBT, ...)`) purely as a delivery mechanism: Windows
maps the hook module (`sider.dll`) into any process that triggers the hook,
which is how `sider.dll` ends up loaded inside the game.

Under Wine/Proton, that global-hook mechanism is emulated (via wineserver)
and can be fragile — the trigger doesn't always fire reliably, and it's one
more moving part in an already complex compatibility layer.

**Key insight:** the hook callback itself (`meconnect` in Sider's source)
does *nothing but* `CallNextHookEx()` — it's a no-op. All of Sider's actual
work (pattern-scanning the game's memory, patching it, spinning up the Lua
module system) happens in `DllMain(DLL_PROCESS_ATTACH)`, which runs
regardless of *how* the DLL got loaded. A plain `LoadLibraryW(sider.dll)`,
executed by any means inside the game process, has the exact same effect as
the original hook mechanism — we just need a Linux-native way to trigger
that call, without going through Wine's hook emulation at all.

## The mechanism

1. **Find the target process.** Wine/Proton detaches the actual game
   process from whatever launched it (it gets reparented to `init`), so we
   can't rely on it being our own child — `sider_inject.py` scans all
   processes by name instead.
2. **Wait for it to be ready.** We wait for `kernel32.dll` and the game's
   own module to be mapped (visible in `/proc/<pid>/maps`), *and* for the
   game to create its first window (via `xdotool search --pid`). The window
   wait matters: it's roughly when the original `WH_CBT` hook would have
   fired. Injecting earlier can technically load `sider.dll` fine, but the
   *memory patches* Sider then applies to the game itself can target state
   the game hasn't finished setting up yet — this caused real, hard-to
   -diagnose crashes a moment after an apparently-successful injection
   during development.
3. **Resolve `LoadLibraryW`.** We manually parse the PE export table of the
   target's `kernel32.dll` (reading `/proc/<pid>/mem`) to find its address
   — no Windows API access needed on our side, since we're a plain Linux
   process.
4. **Hijack a thread via `ptrace`.** We attach (`PTRACE_ATTACH`), save the
   thread's full register state — **including FPU/XMM**, not just the
   integer registers. Any real code touches SSE (CRT functions, memcpy, a
   Lua VM), and skipping this reliably corrupted the thread and crashed the
   game shortly after injection during development. We then point the
   thread at `LoadLibraryW` with the DLL path as the argument, using the
   **Microsoft x64 calling convention** (the target is Windows ABI code,
   even though it's executing as native x86-64 instructions under Wine),
   let it run, catch the return via a verified breakpoint, read the result,
   and restore everything exactly as it was.
5. **Detach.** The thread resumes exactly where it left off, just with
   `sider.dll` now loaded and initialized.

This needs `CAP_SYS_PTRACE` on the Python interpreter running it, because
step 1 means we're not attaching to our own child process — Linux's default
`ptrace_scope=1` only allows ptrace between parent and child by default.
`setup.sh` handles this by granting the capability to a **private copy** of
the venv's Python interpreter, not any shared system Python, so the scope
stays limited to this tool.

## Manual usage / debugging

```sh
.venv/bin/sider-inject-python -u sider_inject.py \
  --prefix /path/to/wineprefix-or-proton-pfx \
  --exe /path/to/game.exe \
  --dll /path/to/sider.dll \
  --timeout 120 \
  --debug-log inject-debug.log
```

Add `--no-launch` if the game is already running or being launched by
something else (e.g. Steam) instead of by this script — that's what
`run-steam.sh` does.

## Known limitations

- Only saves/restores the legacy FPU/SSE (XMM0-15) register state via
  `PTRACE_GETFPREGS`/`SETFPREGS`, not the full AVX/AVX2/AVX-512 extended
  state (YMM/ZMM upper halves). No crash traced to this has been observed,
  but if you hit one, this is the next thing to extend (via
  `PTRACE_GETREGSET`/`NT_X86_XSTATE`).
- Assumes straightforward window-creation timing as the readiness signal;
  games with unusual startup sequences may need a different signal than
  "first `xdotool`-visible window".
- Tested against Football Life 2026 (Lutris/plain Wine) and the Steam
  release of eFootball PES 2021 (Proton). Other Wine/Proton versions or
  games should work the same way in principle, but haven't been verified.
