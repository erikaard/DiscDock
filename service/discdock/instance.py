from __future__ import annotations

import ctypes
import os
from typing import Self

ERROR_ALREADY_EXISTS = 183


class SingleInstance:
    """Process-lifetime Windows mutex preventing two drive controllers."""

    def __init__(self, name: str = r"Local\DiscDockNativeService") -> None:
        self.name = name
        self.handle: int | None = None
        self._kernel32 = None

    def acquire(self) -> None:
        if os.name != "nt" or self.handle:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.CreateMutexW(None, 0, self.name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            raise RuntimeError("DiscDock is already running for this Windows session")
        self._kernel32 = kernel32
        self.handle = int(handle)

    def release(self) -> None:
        if self.handle and self._kernel32:
            self._kernel32.CloseHandle(self.handle)
        self.handle = None
        self._kernel32 = None

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()
