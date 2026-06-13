"""Inject a DLL into the running FF7 (FF7_EN.exe) process.

7th Heaven / FFNx never loads an arbitrary gameplay DLL as a "mod" — that's why
dropping a MinHook DLL in as a mod does nothing. The robust path is to inject it
into the live process after launch. The AP client already attaches to FF7 via
pymem, so it's the natural injector.

FF7_EN.exe is 32-bit; the AP client's Python is usually 64-bit, so we CANNOT use
the injector's own ``GetProcAddress(LoadLibraryA)`` (wrong bitness/address).
Instead we read ``LoadLibraryA`` straight out of the TARGET process's kernel32
export table, which is correct regardless of injector bitness.

NOTE: this is untested against a live FF7 in this dev environment — verify in
game (the client logs success/failure). It is opt-in: only runs if a hook DLL
path is configured AND the file exists.
"""
from __future__ import annotations

import ctypes
import logging
import struct
from ctypes import wintypes
from pathlib import Path
from typing import Optional

logger = logging.getLogger("FF7Client")

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.VirtualAllocEx.restype = wintypes.LPVOID
_k32.VirtualAllocEx.argtypes = [wintypes.HANDLE, wintypes.LPVOID, ctypes.c_size_t,
                                wintypes.DWORD, wintypes.DWORD]
_k32.CreateRemoteThread.restype = wintypes.HANDLE
_k32.CreateRemoteThread.argtypes = [wintypes.HANDLE, wintypes.LPVOID, ctypes.c_size_t,
                                    wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD,
                                    wintypes.LPVOID]

_MEM_COMMIT_RESERVE = 0x3000
_PAGE_READWRITE = 0x04
_PROCESS_ALL = 0x1FFFFF


def _target_loadlibrarya(pm) -> Optional[int]:
    """Return the VA of LoadLibraryA inside the TARGET process's kernel32."""
    try:
        import pymem.process
        mod = pymem.process.module_from_name(pm.process_handle, "kernel32.dll")
        if mod is None:
            return None
        base = mod.lpBaseOfDll
        # Walk PE headers from the target's mapped kernel32 to find the export
        # directory, then resolve "LoadLibraryA".
        e_lfanew = struct.unpack("<I", pm.read_bytes(base + 0x3C, 4))[0]
        opt = base + e_lfanew + 0x18              # IMAGE_OPTIONAL_HEADER
        export_rva = struct.unpack("<I", pm.read_bytes(opt + 0x60, 4))[0]  # DataDir[0]
        exp = base + export_rva
        num_names = struct.unpack("<I", pm.read_bytes(exp + 0x18, 4))[0]
        addr_funcs = base + struct.unpack("<I", pm.read_bytes(exp + 0x1C, 4))[0]
        addr_names = base + struct.unpack("<I", pm.read_bytes(exp + 0x20, 4))[0]
        addr_ords  = base + struct.unpack("<I", pm.read_bytes(exp + 0x24, 4))[0]
        for i in range(num_names):
            name_rva = struct.unpack("<I", pm.read_bytes(addr_names + i * 4, 4))[0]
            name = pm.read_string(base + name_rva, 32)
            if name == "LoadLibraryA":
                ordinal = struct.unpack("<H", pm.read_bytes(addr_ords + i * 2, 2))[0]
                func_rva = struct.unpack("<I", pm.read_bytes(addr_funcs + ordinal * 4, 4))[0]
                return base + func_rva
    except Exception as exc:
        logger.debug(f"resolve LoadLibraryA failed: {exc}")
    return None


def inject_dll(pm, dll_path: Path) -> bool:
    """Inject dll_path into the FF7 process behind ``pm``. Idempotent-ish: callers
    should guard against re-injecting (track a flag on the context)."""
    dll_path = Path(dll_path)
    if not dll_path.is_file():
        logger.debug(f"hook DLL not found: {dll_path}")
        return False
    load_lib = _target_loadlibrarya(pm)
    if not load_lib:
        logger.warning("Could not resolve LoadLibraryA in FF7; cannot inject hook DLL.")
        return False
    path_bytes = str(dll_path.resolve()).encode("ascii") + b"\x00"
    h = pm.process_handle
    remote = _k32.VirtualAllocEx(h, None, len(path_bytes), _MEM_COMMIT_RESERVE, _PAGE_READWRITE)
    if not remote:
        logger.warning("VirtualAllocEx failed; cannot inject hook DLL.")
        return False
    pm.write_bytes(remote, path_bytes, len(path_bytes))
    thread = _k32.CreateRemoteThread(h, None, 0, load_lib, remote, 0, None)
    if not thread:
        logger.warning(f"CreateRemoteThread failed (err={ctypes.get_last_error()}).")
        return False
    _k32.WaitForSingleObject(thread, 5000)
    _k32.CloseHandle(thread)
    logger.info(f"Injected hook DLL: {dll_path.name}")
    return True
