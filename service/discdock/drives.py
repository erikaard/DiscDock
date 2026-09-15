from __future__ import annotations

import asyncio
import ctypes
import hashlib
import os
import re
import subprocess
import winreg
from collections.abc import Awaitable, Callable
from ctypes import wintypes
from pathlib import Path

from .database import utc_now
from .models import DiscKind, DriveInfo
from .processes import start_external_process

CREATE_NO_WINDOW = 0x08000000
DRIVE_CDROM = 5
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
IOCTL_STORAGE_MEDIA_REMOVAL = 0x002D4804
IOCTL_STORAGE_EJECT_MEDIA = 0x002D4808
IOCTL_STORAGE_LOAD_MEDIA = 0x002D480C
IOCTL_STORAGE_CHECK_VERIFY2 = 0x002D0800


IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400


def _stable_drive_id(pnp_device_id: str, letter: str) -> str:
    source = pnp_device_id or letter.upper()
    return hashlib.sha256(source.encode("utf-8", "replace")).hexdigest()[:20]


def _storage_identity(letter: str) -> str:
    """Return "vendor|product|serial" reported by the drive itself.

    Windows renumbers a USB drive (\\Device\\CdRom1 becomes CdRom0) when it is
    reconnected, so identities derived from device numbers change and jobs
    lose their drive. The drive's own serial number does not.
    """
    if os.name != "nt":
        return ""

    class _Query(ctypes.Structure):
        _fields_ = [("PropertyId", ctypes.c_int), ("QueryType", ctypes.c_int), ("Extra", ctypes.c_ubyte * 1)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    handle = kernel32.CreateFileW(
        rf"\\.\{letter.rstrip(':').upper()}:", 0, FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None
    )
    if handle == INVALID_HANDLE_VALUE:
        return ""
    try:
        buffer = ctypes.create_string_buffer(4096)
        returned = wintypes.DWORD()
        if not kernel32.DeviceIoControl(
            handle,
            IOCTL_STORAGE_QUERY_PROPERTY,
            ctypes.byref(_Query(0, 0)),
            ctypes.sizeof(_Query),
            buffer,
            len(buffer),
            ctypes.byref(returned),
            None,
        ):
            return ""
    finally:
        kernel32.CloseHandle(handle)
    raw = buffer.raw[: returned.value]

    def text(field: int) -> str:
        offset = int.from_bytes(raw[field : field + 4], "little") if len(raw) >= field + 4 else 0
        if not offset or offset >= len(raw):
            return ""
        end = raw.find(b"\0", offset)
        value = raw[offset : end if end >= 0 else len(raw)].decode("ascii", "ignore")
        return "".join(character for character in value if character.isprintable()).strip()

    serial = text(24)
    if not serial:
        return ""
    return f"{text(12)}|{text(16)}|{serial}"


_IDENTITY_CACHE: dict[tuple[str, str, str], str] = {}


def _cached_storage_identity(letter: str, target: str, pnp_device_id: str) -> str:
    """Ask a connected drive for its identity once instead of on every poll.

    A drive stuck in a hardware fault can take a long time to answer, and the
    identity cannot change while the same device stays connected.
    """
    key = (letter.upper(), target, pnp_device_id)
    cached = _IDENTITY_CACHE.get(key)
    if cached:
        return cached
    identity = _storage_identity(letter)
    if identity:
        _IDENTITY_CACHE[key] = identity
    return identity


def _pnp_id_for_target(target: str) -> str:
    match = re.search(r"CdRom(\d+)$", target, re.IGNORECASE)
    if not match or os.name != "nt":
        return target
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Services\cdrom\Enum"
        ) as key:
            value, _ = winreg.QueryValueEx(key, match.group(1))
            return str(value) or target
    except OSError:
        return target


def classify_disc(letter: str, media_loaded: bool) -> DiscKind:
    if not media_loaded or not letter:
        return DiscKind.UNKNOWN
    root = Path(f"{letter}\\")
    try:
        if (root / "BDMV").is_dir():
            return DiscKind.BLURAY
        if (root / "VIDEO_TS").is_dir():
            return DiscKind.DVD
        if any(root.glob("*.cda")):
            return DiscKind.AUDIO_CD
        if root.exists():
            return DiscKind.DATA
    except OSError:
        pass
    return DiscKind.UNKNOWN


def _device_target(letter: str) -> str:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    buffer = ctypes.create_unicode_buffer(2048)
    if kernel32.QueryDosDeviceW(letter, buffer, len(buffer)):
        return buffer.value
    return letter


def _media_status(letter: str, root: str) -> tuple[bool, str]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    label_buffer = ctypes.create_unicode_buffer(256)
    serial = wintypes.DWORD()
    maximum_component = wintypes.DWORD()
    flags = wintypes.DWORD()
    filesystem = ctypes.create_unicode_buffer(64)
    has_volume = bool(
        kernel32.GetVolumeInformationW(
            root,
            label_buffer,
            len(label_buffer),
            ctypes.byref(serial),
            ctypes.byref(maximum_component),
            ctypes.byref(flags),
            filesystem,
            len(filesystem),
        )
    )
    path = rf"\\.\{letter}"
    handle = kernel32.CreateFileW(
        path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None
    )
    if handle == INVALID_HANDLE_VALUE:
        return has_volume, label_buffer.value if has_volume else ""
    try:
        returned = wintypes.DWORD()
        ready = bool(
            kernel32.DeviceIoControl(
                handle, IOCTL_STORAGE_CHECK_VERIFY2, None, 0, None, 0, ctypes.byref(returned), None
            )
        )
        return ready or has_volume, label_buffer.value if has_volume else ""
    finally:
        kernel32.CloseHandle(handle)


def _query_windows_drives() -> list[DriveInfo]:
    if os.name != "nt":
        return []
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    length = kernel32.GetLogicalDriveStringsW(0, None)
    buffer = ctypes.create_unicode_buffer(length + 1)
    kernel32.GetLogicalDriveStringsW(length, buffer)
    roots = [item for item in buffer[:length].split("\x00") if item]
    drives: list[DriveInfo] = []
    for root in roots:
        if kernel32.GetDriveTypeW(root) != DRIVE_CDROM:
            continue
        letter = root[:2].upper()
        target = _device_target(letter)
        pnp_device_id = _pnp_id_for_target(target)
        identity = _cached_storage_identity(letter, target, pnp_device_id)
        loaded, label = _media_status(letter, root)
        vendor, _, rest = identity.partition("|")
        product = rest.partition("|")[0]
        drives.append(
            DriveInfo(
                id=_stable_drive_id(identity or pnp_device_id, letter),
                letter=letter,
                name=f"{vendor} {product} ({letter})".strip() if product else f"Windows optical drive ({letter})",
                pnp_device_id=pnp_device_id,
                media_loaded=loaded,
                volume_label=label,
                disc_kind=classify_disc(letter, loaded),
                state="media_ready" if loaded else "empty",
                last_seen=utc_now(),
            )
        )
    return drives


async def enumerate_drives() -> list[DriveInfo]:
    return await asyncio.to_thread(_query_windows_drives)


class DriveControl:
    def __init__(self) -> None:
        self._preview_processes: dict[str, subprocess.Popen] = {}

    @staticmethod
    def _device_io(letter: str, control_code: int, input_buffer: ctypes.Structure | None = None) -> None:
        if os.name != "nt":
            raise RuntimeError("Optical-drive control is only available on Windows")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.DeviceIoControl.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        kernel32.DeviceIoControl.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        path = rf"\\.\{letter.rstrip(':').upper()}:"
        handle = kernel32.CreateFileW(
            path,
            GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            0,
            None,
        )
        if handle == INVALID_HANDLE_VALUE:
            handle = kernel32.CreateFileW(
                path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None
            )
        if handle == INVALID_HANDLE_VALUE:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            returned = wintypes.DWORD()
            pointer = ctypes.byref(input_buffer) if input_buffer is not None else None
            size = ctypes.sizeof(input_buffer) if input_buffer is not None else 0
            ok = kernel32.DeviceIoControl(
                handle, control_code, pointer, size, None, 0, ctypes.byref(returned), None
            )
            if not ok:
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel32.CloseHandle(handle)

    async def stop_preview(self, letter: str) -> None:
        key = letter.rstrip(":").upper()
        process = self._preview_processes.pop(key, None)
        if not process or process.poll() is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=5)
        except TimeoutError:
            process.kill()
            await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=5)

    async def eject(self, letter: str) -> None:
        class PREVENT_MEDIA_REMOVAL(ctypes.Structure):
            _fields_ = [("PreventMediaRemoval", wintypes.BOOLEAN)]

        def operation() -> None:
            try:
                self._device_io(letter, IOCTL_STORAGE_MEDIA_REMOVAL, PREVENT_MEDIA_REMOVAL(False))
            except OSError:
                pass
            self._device_io(letter, IOCTL_STORAGE_EJECT_MEDIA)

        await self.stop_preview(letter)
        await asyncio.to_thread(operation)

    async def close_tray(self, letter: str) -> None:
        await asyncio.to_thread(self._device_io, letter, IOCTL_STORAGE_LOAD_MEDIA)

    def preview(self, letter: str, vlc_path: str, disc_kind: DiscKind = DiscKind.UNKNOWN) -> None:
        if not vlc_path or not Path(vlc_path).is_file():
            raise FileNotFoundError("VLC is not installed")
        key = letter.rstrip(":").upper()
        previous = self._preview_processes.get(key)
        if previous and previous.poll() is None:
            raise OSError("This disc is already open in VLC")
        scheme = {DiscKind.BLURAY: "bluray", DiscKind.DVD: "dvd", DiscKind.AUDIO_CD: "cdda"}.get(
            disc_kind, "file"
        )
        target = f"{scheme}:///{key}:/"
        self._preview_processes[key] = start_external_process(
            [vlc_path, "--play-and-exit", target],
            creationflags=CREATE_NO_WINDOW,
        )

    def preview_pids(self) -> set[int]:
        return {process.pid for process in self._preview_processes.values() if process.poll() is None}


class DriveMonitor:
    def __init__(
        self,
        poll_interval: float,
        on_inserted: Callable[[DriveInfo], Awaitable[None]],
        on_removed: Callable[[DriveInfo], Awaitable[None]],
        on_snapshot: Callable[[list[DriveInfo]], Awaitable[None]],
        should_pause: Callable[[], bool] | None = None,
    ):
        self.poll_interval = max(1.0, poll_interval)
        self.on_inserted = on_inserted
        self.on_removed = on_removed
        self.on_snapshot = on_snapshot
        self.should_pause = should_pause or (lambda: False)
        self._known: dict[str, DriveInfo] = {}
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._reconcile_lock = asyncio.Lock()

    async def reconcile(self) -> list[DriveInfo]:
        async with self._reconcile_lock:
            drives = await enumerate_drives()
            current = {drive.id: drive for drive in drives}
            previous_drives = self._known
            # Publish the new snapshot before callbacks. An automatic insertion
            # callback may refresh drives; it must not observe the disc as new twice.
            self._known = current
            await self.on_snapshot(drives)
        for drive_id, drive in current.items():
            previous = previous_drives.get(drive_id)
            if drive.media_loaded and (previous is None or not previous.media_loaded):
                await self.on_inserted(drive)
            if previous and previous.media_loaded and not drive.media_loaded:
                await self.on_removed(drive)
        for drive_id, previous in previous_drives.items():
            if drive_id not in current and previous.media_loaded:
                previous.media_loaded = False
                previous.state = "disconnected"
                await self.on_removed(previous)
        return drives

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                if not self.should_pause():
                    await self.reconcile()
            except Exception:
                pass
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self.poll_interval)
            except TimeoutError:
                continue

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self._run(), name="drive-monitor")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            await self._task
