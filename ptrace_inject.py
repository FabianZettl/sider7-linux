#!/usr/bin/env python3
"""
Minimal ptrace-based LoadLibraryW injector for Wine-hosted Windows processes.

Frida's own injector fails against Wine targets ("Unable to locate the libc" /
ProcessNotRespondingError -- a known, unresolved upstream issue: see
https://github.com/frida/frida/issues/3339). Frida's agent-bootstrap needs to
locate and understand the target's libc/module layout to inject its whole
runtime; Wine's hybrid PE+ELF memory map defeats that heuristic.

We don't need any of that. We only need to make ONE function call
(LoadLibraryW) inside the target. So instead of injecting a full agent, we:

  1. PTRACE_ATTACH to the target's main thread and stop it.
  2. Save its register state; verify the real (via /proc/pid/maps) stack
     region below its RSP has enough room for a deep call chain, and place
     scratch space (the DLL path string, a temporary stack) safely within it.
  3. Write the DLL path (UTF-16LE) and a fake return address into that
     scratch space.
  4. Point RIP at LoadLibraryW and RCX at the path (Microsoft x64 calling
     convention -- the target is Windows ABI code, even though it's running
     as native x86-64 instructions under Wine).
  5. Plant an INT3 at the fake return address (the thread's own current
     RIP -- see call_win64_function()'s docstring for the caveat there),
     PTRACE_CONT, and wait for the resulting SIGTRAP verified to be at that
     exact address -- that's LoadLibraryW returning.
  6. Read RAX (the HMODULE result), restore everything, detach.

This is the classic manual-DLL-injection technique, just done via Linux
ptrace against a non-child process (which works: ptrace(2) documents that a
successfully attached non-child tracee behaves like a child for wait() for
the duration of the attach).

NOTE on a dead end: an earlier version tried to fix the "current RIP might be
a hot, frequently-revisited address" caveat below by bootstrapping a
dedicated VirtualAlloc'd trampoline page first. That added complexity
introduced its own bugs (crashes that hadn't happened before, even against
the previously 100%-reliable Football Life target) and was reverted -- see
project memory / conversation history from 2026-08-22 if picking this up
again. The current code is the simpler version that was proven reliable
across many real runs against both Football Life (Lutris/Wine) and PES2021
(Steam/Proton) before that detour.
"""
import ctypes
import ctypes.util
import os
import signal
import struct
import time

libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)

PTRACE_PEEKTEXT = 1
PTRACE_POKETEXT = 4
PTRACE_CONT = 7
PTRACE_GETFPREGS = 14
PTRACE_SETFPREGS = 15
PTRACE_ATTACH = 16
PTRACE_DETACH = 17
PTRACE_GETREGS = 12
PTRACE_SETREGS = 13

libc.ptrace.restype = ctypes.c_long
libc.ptrace.argtypes = [ctypes.c_long, ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p]


class UserRegsStruct(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "r15", "r14", "r13", "r12", "rbp", "rbx", "r11", "r10", "r9", "r8",
        "rax", "rcx", "rdx", "rsi", "rdi", "orig_rax", "rip", "cs", "eflags",
        "rsp", "ss", "fs_base", "gs_base", "ds", "es", "fs", "gs",
    )]


class UserFpregsStruct(ctypes.Structure):
    """The FXSAVE-format struct user_fpregs_struct from <sys/user.h> --
    x87 FPU state plus XMM0-15 (SSE). PTRACE_GETREGS/SETREGS only cover the
    integer registers; skipping this meant an injected call using any
    floating point / SSE (memcpy, CRT functions, a Lua VM -- essentially any
    real code) would leave the thread's XMM state clobbered after we resumed
    it, causing crashes shortly after a "successful" injection.
    """
    _fields_ = [
        ("cwd", ctypes.c_uint16),
        ("swd", ctypes.c_uint16),
        ("ftw", ctypes.c_uint16),
        ("fop", ctypes.c_uint16),
        ("rip", ctypes.c_uint64),
        ("rdp", ctypes.c_uint64),
        ("mxcsr", ctypes.c_uint32),
        ("mxcr_mask", ctypes.c_uint32),
        ("st_space", ctypes.c_uint32 * 32),
        ("xmm_space", ctypes.c_uint32 * 64),
        ("padding", ctypes.c_uint32 * 24),
    ]


class InjectError(RuntimeError):
    pass


_log = lambda msg: None  # noqa: E731 -- overridden via set_logger()


def set_logger(fn):
    global _log
    _log = fn


def _ptrace(request, pid, addr=0, data=0):
    ctypes.set_errno(0)
    res = libc.ptrace(request, pid, ctypes.c_void_p(addr), ctypes.c_void_p(data))
    if res == -1:
        errno = ctypes.get_errno()
        if errno != 0:
            raise OSError(errno, os.strerror(errno), f"ptrace(request={request}, pid={pid})")
    return res


def attach(pid: int):
    _ptrace(PTRACE_ATTACH, pid)
    os.waitpid(pid, 0)


def detach(pid: int):
    _ptrace(PTRACE_DETACH, pid)


def get_regs(pid: int) -> UserRegsStruct:
    regs = UserRegsStruct()
    _ptrace(PTRACE_GETREGS, pid, 0, ctypes.addressof(regs))
    return regs


def set_regs(pid: int, regs: UserRegsStruct):
    _ptrace(PTRACE_SETREGS, pid, 0, ctypes.addressof(regs))


def get_fpregs(pid: int) -> UserFpregsStruct:
    regs = UserFpregsStruct()
    _ptrace(PTRACE_GETFPREGS, pid, 0, ctypes.addressof(regs))
    return regs


def set_fpregs(pid: int, regs: UserFpregsStruct):
    _ptrace(PTRACE_SETFPREGS, pid, 0, ctypes.addressof(regs))


def read_mem(pid: int, addr: int, size: int) -> bytes:
    with open(f"/proc/{pid}/mem", "rb") as f:
        f.seek(addr)
        data = f.read(size)
    if len(data) != size:
        raise InjectError(f"short read at {hex(addr)}: got {len(data)}/{size} bytes")
    return data


def write_mem(pid: int, addr: int, data: bytes):
    with open(f"/proc/{pid}/mem", "wb") as f:
        f.seek(addr)
        f.write(data)


def read_u16(pid, addr): return struct.unpack("<H", read_mem(pid, addr, 2))[0]
def read_u32(pid, addr): return struct.unpack("<I", read_mem(pid, addr, 4))[0]


def read_cstring(pid: int, addr: int, max_len: int = 256) -> str:
    data = read_mem(pid, addr, max_len)
    return data.split(b"\x00", 1)[0].decode("ascii", errors="replace")


def find_containing_region(pid: int, addr: int) -> tuple[int, int]:
    """Return (start, end) of the /proc/pid/maps VMA containing addr."""
    with open(f"/proc/{pid}/maps") as f:
        for line in f:
            addr_range = line.split(maxsplit=1)[0]
            start_s, end_s = addr_range.split("-")
            start, end = int(start_s, 16), int(end_s, 16)
            if start <= addr < end:
                return start, end
    raise InjectError(f"no mapped region contains {hex(addr)}")


def find_export(pid: int, base: int, export_name: str) -> int:
    """Manually walk a PE32+ module's export table (same algorithm as inject.js)."""
    if read_u16(pid, base) != 0x5A4D:
        raise InjectError(f"no MZ signature at {hex(base)}")

    e_lfanew = read_u32(pid, base + 0x3C)
    pe_header = base + e_lfanew
    if read_u32(pid, pe_header) != 0x00004550:
        raise InjectError("no PE signature")

    coff = pe_header + 4
    opt_header = coff + 20
    magic = read_u16(pid, opt_header)
    if magic != 0x20B:
        raise InjectError(f"not PE32+ (magic={hex(magic)})")

    data_directory = opt_header + 112
    export_rva = read_u32(pid, data_directory)
    if export_rva == 0:
        raise InjectError("no export table")

    export_dir = base + export_rva
    number_of_names = read_u32(pid, export_dir + 24)
    address_of_functions = base + read_u32(pid, export_dir + 28)
    address_of_names = base + read_u32(pid, export_dir + 32)
    address_of_name_ordinals = base + read_u32(pid, export_dir + 36)

    for i in range(number_of_names):
        name_rva = read_u32(pid, address_of_names + i * 4)
        name = read_cstring(pid, base + name_rva, 64)
        if name == export_name:
            ordinal = read_u16(pid, address_of_name_ordinals + i * 2)
            func_rva = read_u32(pid, address_of_functions + ordinal * 4)
            return base + func_rva

    raise InjectError(f"export {export_name!r} not found")


def call_win64_function(pid: int, func_addr: int, arg1: int, new_rsp: int, timeout: float = 30.0) -> int:
    """Hijack the target thread to call func_addr(arg1) with Win64 ABI, return RAX.

    new_rsp must already be a 16-aligned address picked by the caller to sit
    safely within the target thread's real stack region, with enough room
    below it for func_addr's own call chain -- see inject_dll(), which
    verifies this against /proc/pid/maps rather than guessing a fixed offset
    (a fixed ~8KB offset previously caused a real-world hang: sider.dll's
    DllMain does much deeper work -- CPK scanning, spinning up a Lua VM,
    loading a dozen+ modules -- than a bare LoadLibraryW call, and 8KB of
    headroom was sometimes not enough).

    We plant the INT3 "call has returned" trap at the thread's own current
    RIP. In principle that's not perfectly safe -- if it happened to be a
    generic, frequently-revisited location (e.g. a syscall dispatch stub the
    thread was merely paused inside), a later syscall on this same thread
    could retrigger it before the real call returns. In practice, across
    many real runs against both Football Life (Lutris/Wine) and PES2021
    (Steam/Proton), this has been reliable; the RIP-match verification below
    (only accept the trap if it landed exactly one byte past our INT3, not
    just "some SIGTRAP happened") is what actually matters for correctness.
    A fancier dedicated-trampoline scheme was tried and reverted after it
    introduced new, worse failures -- see this module's top docstring.

    We also save/restore the FPU/SSE (XMM) register state, not just the
    integer registers: LoadLibraryW/DllMain runs real code (CRT functions,
    a Lua VM, memcpy/memmove) that's essentially guaranteed to touch XMM
    registers. Without restoring them, the hijacked thread resumes with
    whatever floating-point garbage the injected call left behind, which
    reliably crashed the target a fraction of a second after a
    "successful" injection.
    """
    orig_regs = get_regs(pid)
    orig_fpregs = get_fpregs(pid)

    new_rsp -= 8  # RSP % 16 == 8, matching "just after a call pushed the return address"
    return_trap_addr = orig_regs.rip  # we'll plant an INT3 here and trap on return

    write_mem(pid, new_rsp, struct.pack("<Q", return_trap_addr))

    new_regs = UserRegsStruct()
    ctypes.memmove(ctypes.addressof(new_regs), ctypes.addressof(orig_regs), ctypes.sizeof(new_regs))
    new_regs.rip = func_addr
    new_regs.rsp = new_rsp
    new_regs.rcx = arg1

    orig_byte_at_trap = read_mem(pid, return_trap_addr, 1)
    write_mem(pid, return_trap_addr, b"\xCC")

    _log(f"call_win64_function: func_addr={hex(func_addr)} arg1={hex(arg1)} "
         f"orig_rip={hex(orig_regs.rip)} orig_rsp={hex(orig_regs.rsp)} "
         f"new_rsp={hex(new_rsp)} return_trap_addr={hex(return_trap_addr)} "
         f"orig_byte_at_trap={orig_byte_at_trap.hex()}")

    try:
        set_regs(pid, new_regs)
        _ptrace(PTRACE_CONT, pid)

        # Non-blocking poll with an enforced deadline -- os.waitpid(pid, 0)
        # blocks forever if the tracee stops for a reason we don't handle
        # (e.g. it crashed with SIGSEGV instead of hitting our breakpoint):
        # the one available stop event gets consumed once, and without
        # PTRACE_CONT'ing again there is nothing further to wait for.
        deadline = time.time() + timeout
        while True:
            wpid, status = os.waitpid(pid, os.WNOHANG)
            if wpid == 0:
                if time.time() > deadline:
                    raise InjectError("timed out waiting for injected call to return")
                time.sleep(0.01)
                continue
            if os.WIFEXITED(status):
                raise InjectError(f"target process exited (code={os.WEXITSTATUS(status)}) during injected call")
            if os.WIFSIGNALED(status):
                raise InjectError(f"target process killed by signal {os.WTERMSIG(status)} during injected call")
            if os.WIFSTOPPED(status):
                sig = os.WSTOPSIG(status)
                if sig != signal.SIGTRAP:
                    try:
                        sig_name = signal.Signals(sig).name
                    except ValueError:
                        sig_name = str(sig)
                    raise InjectError(
                        f"target thread stopped with unexpected signal {sig} ({sig_name}) during injected "
                        f"call -- it likely crashed (bad calling convention / stack setup?)"
                    )
                # Genuine SIGTRAP -- but is it *our* breakpoint? INT3 advances
                # RIP to breakpoint_addr+1. A stray trap (e.g. from something
                # else in this heavily-threaded Wine process) would land
                # elsewhere; if so, this isn't our call returning yet, so
                # resume and keep waiting instead of reading a bogus RAX.
                stop_regs = get_regs(pid)
                _log(f"call_win64_function: SIGTRAP at rip={hex(stop_regs.rip)} rax={hex(stop_regs.rax)} "
                     f"(expecting rip={hex(return_trap_addr + 1)})")
                if stop_regs.rip == return_trap_addr + 1:
                    result_regs = stop_regs
                    break
                _log("call_win64_function: spurious trap, not ours -- continuing")
                _ptrace(PTRACE_CONT, pid)
                continue
            raise InjectError(f"unexpected waitpid status: {status}")

        return result_regs.rax
    finally:
        write_mem(pid, return_trap_addr, orig_byte_at_trap)
        set_regs(pid, orig_regs)
        set_fpregs(pid, orig_fpregs)


def inject_dll(pid: int, kernel32_base: int, dll_path_windows: str) -> int:
    """Attach to pid, call LoadLibraryW(dll_path_windows), detach. Returns HMODULE (0 = failure)."""
    attach(pid)
    try:
        load_library_w = find_export(pid, kernel32_base, "LoadLibraryW")

        orig_regs = get_regs(pid)
        stack_start, stack_end = find_containing_region(pid, orig_regs.rsp)
        available = orig_regs.rsp - stack_start
        _log(f"inject_dll: stack region {hex(stack_start)}-{hex(stack_end)}, rsp={hex(orig_regs.rsp)}, "
             f"available below rsp={available} bytes")
        if available < 16384:
            raise InjectError(
                f"only {available} bytes of stack below RSP ({hex(orig_regs.rsp)}, region "
                f"{hex(stack_start)}-{hex(stack_end)}) -- not enough headroom for a safe injected call"
            )

        # sider.dll's DllMain does substantial work on this same call chain
        # (CPK scanning, spinning up a Lua VM, loading a dozen+ modules), so
        # give it generous headroom -- up to 512KB -- rather than the ~8KB a
        # bare LoadLibraryW call would need. Never use more than half of
        # what's actually mapped, and always verified against the real VMA
        # rather than assumed.
        call_headroom = min(512 * 1024, available // 2)
        new_rsp = (orig_regs.rsp - call_headroom) & ~0xF

        path_headroom = min(65536, (new_rsp - stack_start) // 2)
        if path_headroom < 4096:
            raise InjectError("not enough remaining stack headroom left for the DLL path string")
        path_addr = (new_rsp - path_headroom) & ~0xF

        path_bytes = dll_path_windows.encode("utf-16-le") + b"\x00\x00"
        write_mem(pid, path_addr, path_bytes)

        return call_win64_function(pid, load_library_w, path_addr, new_rsp)
    finally:
        detach(pid)
