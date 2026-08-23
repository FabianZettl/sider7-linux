#!/usr/bin/env python3
"""
Native Linux replacement for sider.exe's injection mechanism.

sider.exe installs a global SetWindowsHookEx(WH_CBT, ...) hook purely as a
delivery vehicle to get sider.dll mapped into the game process -- the hook
callback itself (meconnect) does nothing but CallNextHookEx. All real work
happens in sider.dll's DllMain(DLL_PROCESS_ATTACH), which runs regardless of
how the DLL got loaded.

This tool replaces the fragile Wine-emulated global hook with a direct,
native LoadLibraryW injection: we launch the game (needs CAP_SYS_PTRACE,
since Wine detaches the actual game process from whatever launched it), wait
for it to map kernel32.dll, then hijack one of its threads via ptrace to
call LoadLibraryW(sider.dll) with the correct Win64 calling convention.

(An earlier version used Frida for the injection step, but Frida's own
agent-bootstrap fails against Wine processes -- see ptrace_inject.py's
docstring. ptrace_inject.py does the minimal equivalent by hand.)
"""
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil

import ptrace_inject


def to_windows_path(linux_path: Path, prefix: Path) -> str:
    """Convert a Linux path to the Windows path Wine/Proton will see it as.

    Paths under the prefix's drive_c become C:\\...; anything else (e.g. a
    Steam game's install dir under steamapps/common, which lives outside
    the Proton compatdata prefix entirely) falls back to Wine's default Z:
    drive, which maps the whole Linux filesystem root.
    """
    resolved = linux_path.resolve()
    drive_c = (prefix / "drive_c").resolve()
    try:
        rel = resolved.relative_to(drive_c)
        return "C:\\" + "\\".join(rel.parts)
    except ValueError:
        return "Z:\\" + "\\".join(resolved.parts[1:])


DEBUG_LOG = None


def dbg(msg):
    if DEBUG_LOG:
        DEBUG_LOG.write(f"{time.time():.2f} {msg}\n")
        DEBUG_LOG.flush()


def find_process_by_name(exe_name: str, timeout: float, min_start_time: float) -> psutil.Process | None:
    """Scan all processes (not just our own descendants) for exe_name.

    Wine detaches the actual game process from the launcher that started it
    (re-parented to init), so it is *not* our descendant -- this only works
    because the injector binary carries CAP_SYS_PTRACE, which bypasses the
    Yama ptrace_scope=1 ancestry requirement. min_start_time filters out
    stale processes from a previous run that happen to share the name
    (with slack, since psutil's create_time is derived from CLOCK_BOOTTIME
    while min_start_time comes from time.time()/CLOCK_REALTIME -- these two
    clocks can disagree by a second or so).
    """
    CLOCK_SKEW_SLACK = 5.0
    deadline = time.time() + timeout
    target = exe_name.lower()
    target15 = target[:15]  # Linux comm is truncated to 15 chars
    dbg(f"find_process_by_name: target={target!r} target15={target15!r} min_start_time={min_start_time:.2f}")
    n = 0
    while time.time() < deadline:
        n += 1
        for p in psutil.process_iter(["name", "create_time"]):
            try:
                name = p.info["name"]
                ct = p.info["create_time"]
            except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                continue
            if name.lower() in (target, target15):
                passes = ct >= min_start_time - CLOCK_SKEW_SLACK
                dbg(f"iter={n} CANDIDATE pid={p.pid} name={name!r} create_time={ct:.2f} "
                    f"(min_start_time={min_start_time:.2f}, passes={passes})")
                if not passes:
                    continue
                return p
        if n % 20 == 1:
            dbg(f"iter={n} no match yet, {deadline - time.time():.1f}s left")
        time.sleep(0.25)
    dbg("deadline exceeded, giving up")
    return None


def find_module_base(pid: int, module_filename: str, timeout: float) -> int | None:
    deadline = time.time() + timeout
    target = module_filename.lower()
    while time.time() < deadline:
        try:
            maps_text = Path(f"/proc/{pid}/maps").read_text()
        except (FileNotFoundError, ProcessLookupError):
            return None
        candidates = []
        for line in maps_text.splitlines():
            parts = line.split(maxsplit=5)
            if len(parts) < 6:
                continue
            addr_range, _perms, offset, _dev, _inode, path = parts
            if path.lower().endswith(target):
                base = int(addr_range.split("-")[0], 16)
                off = int(offset, 16)
                candidates.append((off, base))
        if candidates:
            candidates.sort()
            return candidates[0][1]
        time.sleep(0.25)
    return None


def is_module_mapped(pid: int, module_filename: str) -> bool:
    try:
        maps_text = Path(f"/proc/{pid}/maps").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return False
    target = module_filename.lower()
    return any(line.lower().endswith(target) for line in maps_text.splitlines())


def wait_for_window(pid: int, timeout: float) -> bool:
    """Wait for the target process to have created at least one X11 window.

    The original sider.exe mechanism (SetWindowsHookEx(WH_CBT, ...)) only
    actually fires -- loading sider.dll and running its DllMain -- on
    window-related events, i.e. well into the game's own bootstrap.
    Injecting as soon as kernel32.dll/the exe module are merely *mapped* can
    be much earlier than that: sider.dll's own Lua init can still complete
    fine at that point, but the memory patches it applies to the *game's*
    code/data (memlib.cpp/patcher.cpp) may target state the game hasn't
    finished setting up yet, corrupting it in a way that only crashes later
    (this caused a real crash -- a null-pointer write inside the game's own
    code -- a little while after an apparently-successful injection).
    Waiting for a real window is a much closer match to the original
    trigger point.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            result = subprocess.run(
                ["xdotool", "search", "--pid", str(pid)],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return True
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        time.sleep(0.25)
    return False


def wait_and_inject(exe_name: str, dll_windows_path: str, launch_time: float, timeout: float) -> int:
    """Wait for exe_name to appear and be ready, then inject dll_windows_path into it.

    Returns the HMODULE (never 0 -- raises SystemExit on failure).
    """
    print(f"[*] waiting for {exe_name} process (requires CAP_SYS_PTRACE, since Wine detaches "
          f"it from whatever launched it)...")
    target = find_process_by_name(exe_name, timeout, launch_time)
    if target is None:
        sys.exit(f"[!] never saw {exe_name} appear as a running process")
    print(f"[+] found target: pid={target.pid} name={target.name()}")

    print("[*] waiting for kernel32.dll to be mapped...")
    k32_base = find_module_base(target.pid, "kernel32.dll", timeout)
    if k32_base is None:
        sys.exit("[!] kernel32.dll never appeared in /proc/<pid>/maps")
    print(f"[+] kernel32.dll base = {hex(k32_base)}")

    # kernel32.dll being mapped only means Wine's own bootstrap has started --
    # the target's own PE image (and the PEB process-parameters that
    # sider.dll's DllMain relies on, e.g. GetModuleFileName(NULL, ...)) may
    # not be fully set up yet. Wait for the game's own module too, as a
    # stronger readiness signal.
    print(f"[*] waiting for {exe_name} module (the game's own PE image) to be mapped...")
    deadline = time.time() + timeout
    while not is_module_mapped(target.pid, exe_name):
        if time.time() > deadline:
            sys.exit(f"[!] {exe_name} module never appeared in /proc/<pid>/maps")
        time.sleep(0.25)
    print(f"[+] {exe_name} module is mapped")

    # The original sider.exe mechanism only actually fires on window-related
    # events (see wait_for_window's docstring) -- much later in the game's
    # bootstrap than "modules are mapped". Match that timing before we let
    # sider.dll patch the game's own memory, or those patches can target
    # state the game hasn't finished setting up yet.
    print(f"[*] waiting for {exe_name} to create a window (mirrors when the original hook would fire)...")
    if not wait_for_window(target.pid, timeout):
        sys.exit(f"[!] {exe_name} never created a window")
    print(f"[+] {exe_name} has a window")

    # Even so, DllMain can still legitimately return FALSE (-> LoadLibraryW
    # returns NULL) if we race Wine's own process setup, and ptrace itself
    # can transiently fail (EPERM) right around that same window -- Wine
    # doing internal housekeeping seems to briefly affect traceability.
    # Both are harmless to retry: a FALSE return auto-unloads the DLL, and a
    # ptrace error just means the process wasn't ready for us yet.
    hmodule = 0
    last_error = None
    attempts = 10
    for attempt in range(1, attempts + 1):
        print(f"[*] ptrace-attaching to pid {target.pid} and calling LoadLibraryW(sider.dll) "
              f"(attempt {attempt}/{attempts}) ...")
        try:
            hmodule = ptrace_inject.inject_dll(target.pid, k32_base, dll_windows_path)
        except (OSError, ptrace_inject.InjectError) as e:
            last_error = e
            print(f"[!] ptrace call failed ({e}); retrying...")
            time.sleep(1.5)
            continue
        if hmodule != 0:
            break
        print("[!] LoadLibraryW returned NULL, DllMain likely raced Wine's process setup; retrying...")
        time.sleep(1.5)

    if hmodule == 0:
        sys.exit(f"[!] injection did not succeed after {attempts} attempts (last error: {last_error})")

    print(f"[+] sider.dll injected successfully, HMODULE={hex(hmodule)}")
    return hmodule


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--prefix", required=True, type=Path, help="WINEPREFIX / Proton compatdata pfx directory")
    ap.add_argument("--exe", required=True, type=Path,
                     help="Path to the game .exe (Linux path; used to find its basename when --no-launch)")
    ap.add_argument("--dll", required=True, type=Path, help="Path to sider.dll (Linux path)")
    ap.add_argument("--wine", default="wine", help="wine binary to use to launch the game (default: wine)")
    ap.add_argument("--timeout", type=float, default=60.0, help="seconds to wait for process/module")
    ap.add_argument("--wait-exit", action="store_true", help="wait for the game to exit before quitting")
    ap.add_argument("--debug-log", type=Path, help="write detailed diagnostics to this file")
    ap.add_argument("--no-launch", action="store_true",
                     help="don't launch the exe ourselves -- just wait for it to appear "
                          "(e.g. when Steam/Proton is launching it) and inject into it")
    args = ap.parse_args()

    global DEBUG_LOG
    if args.debug_log:
        DEBUG_LOG = open(args.debug_log, "w")
        ptrace_inject.set_logger(dbg)

    dll = args.dll.resolve()
    prefix = args.prefix.resolve()

    if not dll.exists():
        sys.exit(f"dll not found: {dll}")

    dll_windows_path = to_windows_path(dll, prefix)
    print(f"[*] sider.dll as Windows path: {dll_windows_path}")

    proc = None
    if args.no_launch:
        exe_name = args.exe.name
        # No "our own freshly-launched instance" to disambiguate against here
        # -- the target may legitimately have started well before us (e.g.
        # Steam/Proton was already mid-launch when we got going). Accept any
        # matching process regardless of age.
        launch_time = 0.0
        print(f"[*] not launching anything -- waiting for an already-launched/soon-to-appear {exe_name}")
    else:
        exe = args.exe.resolve()
        if not exe.exists():
            sys.exit(f"exe not found: {exe}")
        exe_name = exe.name

        env = os.environ.copy()
        env["WINEPREFIX"] = str(prefix)

        launch_time = time.time()
        print(f"[*] launching: {args.wine} {exe_name}  (cwd={exe.parent})")
        proc = subprocess.Popen([args.wine, str(exe)], cwd=str(exe.parent), env=env)

    wait_and_inject(exe_name, dll_windows_path, launch_time, args.timeout)

    if args.wait_exit and proc is not None:
        proc.wait()


if __name__ == "__main__":
    main()
