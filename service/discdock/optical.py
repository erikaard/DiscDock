"""Direct optical-drive access for damaged-disc rescue.

Windows' volume API (``ReadFile`` on ``\\\\.\\D:``) works, but it hides the
drive's sense data and cannot bound a read. SCSI pass-through gives DiscDock the
exact reason a read failed (medium error, copy protection, removed disc) and a
per-command timeout, while remaining available to a standard user account.
"""

from __future__ import annotations

import ctypes
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

SECTOR_SIZE = 2048
DVD_ECC_BLOCK_SECTORS = 16
BLURAY_ECC_CLUSTER_SECTORS = 32

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000
PAGE_READWRITE = 0x04
FILE_BEGIN = 0
IOCTL_SCSI_PASS_THROUGH_DIRECT = 0x0004D014
IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C
SCSI_IOCTL_DATA_IN = 1

ERROR_ACCESS_DENIED = 5
ERROR_INVALID_FUNCTION = 1
ERROR_NOT_READY = 21
ERROR_CRC = 23
ERROR_SECTOR_NOT_FOUND = 27
ERROR_READ_FAULT = 30
ERROR_GEN_FAILURE = 31
ERROR_DEV_NOT_EXIST = 55
ERROR_INVALID_PARAMETER = 87
ERROR_SEM_TIMEOUT = 121
ERROR_IO_DEVICE = 1117
ERROR_MEDIA_CHANGED = 1110
ERROR_NO_MEDIA_IN_DRIVE = 1112
ERROR_DEVICE_NOT_CONNECTED = 1167
ERROR_NOT_SUPPORTED = 50

SENSE_MEDIUM_ERROR = 0x3
SENSE_HARDWARE_ERROR = 0x4
SENSE_ABORTED_COMMAND = 0xB

_DEVICE_GONE_ERRORS = {ERROR_DEV_NOT_EXIST, ERROR_DEVICE_NOT_CONNECTED, ERROR_NO_MEDIA_IN_DRIVE}
# Windows reports a MEDIUM ERROR sense as a CRC/data error. A HARDWARE ERROR
# sense becomes a generic device I/O error; that says nothing about the disc.
_MEDIUM_ERRORS = {ERROR_CRC, ERROR_SECTOR_NOT_FOUND, ERROR_READ_FAULT}
_FAULT_ERRORS = {ERROR_GEN_FAILURE, ERROR_IO_DEVICE}


class OpticalError(OSError):
    """Base class for drive-level failures."""


class UnreadableSectorError(OpticalError):
    """The drive reported that the requested sectors cannot be read."""

    def __init__(self, message: str, *, sense: tuple[int, int, int] = (0, 0, 0), timed_out: bool = False):
        super().__init__(message)
        self.sense = sense
        self.timed_out = timed_out


class DriveFaultError(OpticalError):
    """The drive itself failed (for example sense 4/3E/01), not the requested sectors.

    A drive in this state rejects every read instantly, including sectors it
    read successfully a moment earlier, so its errors must never be recorded
    as damage on the disc.
    """

    def __init__(self, message: str, *, sense: tuple[int, int, int] = (0, 0, 0)):
        super().__init__(message)
        self.sense = sense


class MediaUnavailableError(OpticalError):
    """The disc was removed or the drive disappeared."""


class CopyProtectionError(OpticalError):
    """The drive refused scrambled DVD sectors because CSS authentication is missing."""


def _require_windows() -> None:
    if os.name != "nt":
        raise OpticalError("Direct optical-drive access is only available on Windows")


def _drive_path(letter: str) -> str:
    drive = letter.rstrip(":\\/").upper()
    if len(drive) != 1 or not drive.isalpha():
        raise ValueError("Invalid Windows drive letter")
    return rf"\\.\{drive}:"


def _kernel32():
    from ctypes import wintypes

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
    kernel32.VirtualAlloc.argtypes = [wintypes.LPVOID, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
    kernel32.VirtualAlloc.restype = wintypes.LPVOID
    kernel32.VirtualFree.argtypes = [wintypes.LPVOID, ctypes.c_size_t, wintypes.DWORD]
    kernel32.VirtualFree.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.ReadFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    kernel32.ReadFile.restype = wintypes.BOOL
    kernel32.SetFilePointerEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_longlong,
        ctypes.POINTER(ctypes.c_longlong),
        wintypes.DWORD,
    ]
    kernel32.SetFilePointerEx.restype = wintypes.BOOL
    return kernel32


class _ScsiPassThroughDirect(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_ushort),
        ("ScsiStatus", ctypes.c_ubyte),
        ("PathId", ctypes.c_ubyte),
        ("TargetId", ctypes.c_ubyte),
        ("Lun", ctypes.c_ubyte),
        ("CdbLength", ctypes.c_ubyte),
        ("SenseInfoLength", ctypes.c_ubyte),
        ("DataIn", ctypes.c_ubyte),
        ("DataTransferLength", ctypes.c_ulong),
        ("TimeOutValue", ctypes.c_ulong),
        ("DataBuffer", ctypes.c_void_p),
        ("SenseInfoOffset", ctypes.c_ulong),
        ("Cdb", ctypes.c_ubyte * 16),
    ]


class _ScsiPassThroughDirectWithSense(ctypes.Structure):
    _fields_ = [
        ("sptd", _ScsiPassThroughDirect),
        ("Filler", ctypes.c_ulong),
        ("Sense", ctypes.c_ubyte * 32),
    ]


def parse_sense(sense: bytes) -> tuple[int, int, int]:
    """Return (sense key, ASC, ASCQ) from fixed or descriptor-format sense data."""
    if len(sense) < 4:
        return 0, 0, 0
    response = sense[0] & 0x7F
    if response in {0x72, 0x73}:
        return sense[1] & 0x0F, sense[2], sense[3]
    if len(sense) < 14:
        return sense[2] & 0x0F, 0, 0
    return sense[2] & 0x0F, sense[12], sense[13]


def classify_sense(lba: int, sense: tuple[int, int, int]) -> OpticalError:
    """Turn a CHECK CONDITION that is not handled by retrying into an exception."""
    key, asc, ascq = sense
    text = f"{key:X}/{asc:02X}/{ascq:02X}"
    if key == 5 and asc == 0x6F:
        return CopyProtectionError(
            "The drive refused encrypted DVD sectors because the disc is not authenticated"
        )
    if key in {SENSE_HARDWARE_ERROR, SENSE_ABORTED_COMMAND}:
        return DriveFaultError(f"The drive reported a hardware fault (sense {text})", sense=sense)
    return UnreadableSectorError(f"Sector {lba} is unreadable (sense {text})", sense=sense)


class _AlignedBuffer:
    def __init__(self, kernel32, size: int):
        self._kernel32 = kernel32
        self.size = size
        self.address = kernel32.VirtualAlloc(None, size, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE)
        if not self.address:
            raise ctypes.WinError(ctypes.get_last_error())

    def bytes(self, length: int) -> bytes:
        return ctypes.string_at(self.address, length)

    def close(self) -> None:
        if self.address:
            self._kernel32.VirtualFree(self.address, 0, MEM_RELEASE)
            self.address = None


class OpticalDevice:
    """Common interface used by the rescue engine and tests."""

    method = "unknown"
    total_sectors = 0
    max_transfer_sectors = 32

    def read(self, lba: int, count: int) -> bytes:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - interface
        return None

    def reopen(self) -> None:
        return None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


class SptiDevice(OpticalDevice):
    """Read sectors with SCSI READ(12) through IOCTL_SCSI_PASS_THROUGH_DIRECT."""

    method = "scsi_pass_through"
    MAXIMUM_TRANSFER_SECTORS = 256

    def __init__(self, letter: str, *, timeout_seconds: int = 30):
        _require_windows()
        self.letter = letter
        self.path = _drive_path(letter)
        self.timeout_seconds = max(5, int(timeout_seconds))
        self._kernel32 = _kernel32()
        self._handle = None
        self._buffer = _AlignedBuffer(self._kernel32, self.MAXIMUM_TRANSFER_SECTORS * SECTOR_SIZE)
        self.max_transfer_sectors = self.MAXIMUM_TRANSFER_SECTORS
        try:
            self._open()
            self.total_sectors = self._read_capacity()
            self._detect_transfer_limit()
        except BaseException:
            self.close()
            raise

    def _open(self) -> None:
        handle = self._kernel32.CreateFileW(
            self.path,
            GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            0,
            None,
        )
        if handle in (None, ctypes.c_void_p(-1).value):
            error = ctypes.get_last_error()
            if error in _DEVICE_GONE_ERRORS or error == ERROR_NOT_READY:
                raise MediaUnavailableError(f"The optical drive is not available (Windows error {error})")
            raise ctypes.WinError(error)
        self._handle = handle

    def reopen(self) -> None:
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None
        self._open()

    def close(self) -> None:
        if getattr(self, "_handle", None):
            self._kernel32.CloseHandle(self._handle)
            self._handle = None
        buffer = getattr(self, "_buffer", None)
        if buffer:
            buffer.close()

    def _command(self, cdb: bytes, length: int, timeout: int | None = None) -> tuple[int, bytes, int]:
        """Run one data-in command. Returns (SCSI status, sense bytes, transferred)."""
        from ctypes import wintypes

        if not self._handle:
            self._open()
        if length > self._buffer.size:
            raise ValueError("SCSI transfer exceeds the aligned buffer")
        request = _ScsiPassThroughDirectWithSense()
        request.sptd.Length = ctypes.sizeof(_ScsiPassThroughDirect)
        request.sptd.CdbLength = len(cdb)
        request.sptd.SenseInfoLength = len(request.Sense)
        request.sptd.DataIn = SCSI_IOCTL_DATA_IN
        request.sptd.DataTransferLength = length
        request.sptd.TimeOutValue = int(timeout or self.timeout_seconds)
        request.sptd.DataBuffer = self._buffer.address if length else None
        request.sptd.SenseInfoOffset = _ScsiPassThroughDirectWithSense.Sense.offset
        for index, value in enumerate(cdb):
            request.sptd.Cdb[index] = value
        returned = wintypes.DWORD()
        ok = self._kernel32.DeviceIoControl(
            self._handle,
            IOCTL_SCSI_PASS_THROUGH_DIRECT,
            ctypes.byref(request),
            ctypes.sizeof(request),
            ctypes.byref(request),
            ctypes.sizeof(request),
            ctypes.byref(returned),
            None,
        )
        if not ok:
            error = ctypes.get_last_error()
            raise ctypes.WinError(error)
        return request.sptd.ScsiStatus, bytes(request.Sense), int(request.sptd.DataTransferLength)

    def _read_capacity(self) -> int:
        cdb = bytes([0x25, 0, 0, 0, 0, 0, 0, 0, 0, 0])
        for attempt in range(20):
            status, sense, transferred = self._command(cdb, 8, timeout=20)
            if status == 0 and transferred >= 8:
                data = self._buffer.bytes(8)
                last_lba = int.from_bytes(data[0:4], "big")
                block = int.from_bytes(data[4:8], "big")
                if block not in {0, SECTOR_SIZE}:
                    raise OpticalError(f"Unexpected optical sector size {block}")
                return last_lba + 1
            key, asc, ascq = parse_sense(sense)
            if key == 2 and asc == 0x3A:
                raise MediaUnavailableError("No disc is loaded")
            if key in {SENSE_HARDWARE_ERROR, SENSE_ABORTED_COMMAND}:
                raise DriveFaultError(
                    f"The drive reported a hardware fault (sense {key:X}/{asc:02X}/{ascq:02X})",
                    sense=(key, asc, ascq),
                )
            if attempt < 19 and key in {2, 6}:
                time.sleep(1)
                continue
            break
        raise OpticalError("The drive did not report the disc capacity")

    def _detect_transfer_limit(self) -> None:
        size = self.MAXIMUM_TRANSFER_SECTORS
        while size >= DVD_ECC_BLOCK_SECTORS:
            try:
                self._read_raw(0, size)
                self.max_transfer_sectors = size
                return
            except _TransferTooLarge:
                size //= 2
            except OpticalError:
                # An unreadable lead-in must not prevent the rescue itself.
                self.max_transfer_sectors = min(size, 32)
                return
        self.max_transfer_sectors = DVD_ECC_BLOCK_SECTORS

    def _read_raw(self, lba: int, count: int) -> bytes:
        cdb = bytes(
            [
                0xA8,
                0,
                (lba >> 24) & 0xFF,
                (lba >> 16) & 0xFF,
                (lba >> 8) & 0xFF,
                lba & 0xFF,
                (count >> 24) & 0xFF,
                (count >> 16) & 0xFF,
                (count >> 8) & 0xFF,
                count & 0xFF,
                0,
                0,
            ]
        )
        length = count * SECTOR_SIZE
        unit_attention = 0
        not_ready = 0
        while True:
            started = time.monotonic()
            try:
                status, sense, transferred = self._command(cdb, length)
            except OSError as error:
                code = getattr(error, "winerror", 0) or 0
                if code == ERROR_INVALID_PARAMETER and count > 1:
                    raise _TransferTooLarge() from error
                if code in _DEVICE_GONE_ERRORS:
                    raise MediaUnavailableError(f"The optical drive disappeared (Windows error {code})") from error
                if code == ERROR_SEM_TIMEOUT or time.monotonic() - started >= self.timeout_seconds:
                    raise UnreadableSectorError(
                        f"The drive timed out reading sector {lba}", timed_out=True
                    ) from error
                if code in _MEDIUM_ERRORS:
                    raise UnreadableSectorError(f"Sector {lba} could not be read (Windows error {code})") from error
                if code in _FAULT_ERRORS:
                    raise DriveFaultError(f"The drive stopped responding (Windows error {code})") from error
                raise OpticalError(f"SCSI pass-through failed (Windows error {code})") from error
            if status == 0:
                if transferred != length:
                    raise UnreadableSectorError(f"The drive returned a short read at sector {lba}")
                return self._buffer.bytes(length)
            key, asc, ascq = parse_sense(sense)
            if key == 6 and unit_attention < 3:
                unit_attention += 1
                continue
            if key == 2:
                if asc == 0x3A:
                    raise MediaUnavailableError("The disc was removed from the drive")
                if not_ready < 30:
                    not_ready += 1
                    time.sleep(1)
                    continue
                raise MediaUnavailableError("The drive stayed not ready")
            raise classify_sense(lba, (key, asc, ascq))

    def read(self, lba: int, count: int) -> bytes:
        if count <= 0:
            return b""
        if lba < 0 or (self.total_sectors and lba + count > self.total_sectors):
            raise ValueError("Read beyond the end of the disc")
        output = bytearray()
        position = lba
        remaining = count
        while remaining:
            take = min(remaining, self.max_transfer_sectors)
            try:
                output += self._read_raw(position, take)
            except _TransferTooLarge:
                self.max_transfer_sectors = max(1, self.max_transfer_sectors // 2)
                continue
            position += take
            remaining -= take
        return bytes(output)


class _TransferTooLarge(Exception):
    pass


class VolumeHandleDevice(OpticalDevice):
    """Fallback reader through Windows' raw volume handle."""

    method = "windows_volume"

    def __init__(self, letter: str, *, total_sectors: int = 0):
        _require_windows()
        self.letter = letter
        self.path = _drive_path(letter)
        self._kernel32 = _kernel32()
        self._handle = None
        self.max_transfer_sectors = 32
        self._buffer = _AlignedBuffer(self._kernel32, 256 * SECTOR_SIZE)
        try:
            self._open()
            self.total_sectors = total_sectors or self._length_sectors()
        except BaseException:
            self.close()
            raise

    def _open(self) -> None:
        handle = self._kernel32.CreateFileW(
            self.path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None
        )
        if handle in (None, ctypes.c_void_p(-1).value):
            error = ctypes.get_last_error()
            if error in _DEVICE_GONE_ERRORS or error == ERROR_NOT_READY:
                raise MediaUnavailableError(f"The optical drive is not available (Windows error {error})")
            raise ctypes.WinError(error)
        self._handle = handle

    def reopen(self) -> None:
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None
        self._open()

    def close(self) -> None:
        if getattr(self, "_handle", None):
            self._kernel32.CloseHandle(self._handle)
            self._handle = None
        buffer = getattr(self, "_buffer", None)
        if buffer:
            buffer.close()

    def _length_sectors(self) -> int:
        from ctypes import wintypes

        length = ctypes.c_longlong()
        returned = wintypes.DWORD()
        ok = self._kernel32.DeviceIoControl(
            self._handle,
            IOCTL_DISK_GET_LENGTH_INFO,
            None,
            0,
            ctypes.byref(length),
            ctypes.sizeof(length),
            ctypes.byref(returned),
            None,
        )
        if not ok or length.value <= 0:
            raise OpticalError("Windows could not determine the disc size")
        return length.value // SECTOR_SIZE

    def read(self, lba: int, count: int) -> bytes:
        from ctypes import wintypes

        if count <= 0:
            return b""
        if lba < 0 or (self.total_sectors and lba + count > self.total_sectors):
            raise ValueError("Read beyond the end of the disc")
        output = bytearray()
        position = lba
        remaining = count
        while remaining:
            take = min(remaining, self.max_transfer_sectors)
            moved = ctypes.c_longlong()
            if not self._kernel32.SetFilePointerEx(
                self._handle, position * SECTOR_SIZE, ctypes.byref(moved), FILE_BEGIN
            ):
                raise OpticalError(f"Could not seek to sector {position}")
            done = wintypes.DWORD()
            ok = self._kernel32.ReadFile(
                self._handle, self._buffer.address, take * SECTOR_SIZE, ctypes.byref(done), None
            )
            if not ok:
                error = ctypes.get_last_error()
                if error in _DEVICE_GONE_ERRORS or error in {ERROR_NOT_READY, ERROR_MEDIA_CHANGED}:
                    raise MediaUnavailableError(f"The disc is not available (Windows error {error})")
                if error in _FAULT_ERRORS:
                    raise DriveFaultError(f"The drive stopped responding (Windows error {error})")
                raise UnreadableSectorError(f"Sector {position} could not be read (Windows error {error})")
            if done.value != take * SECTOR_SIZE:
                raise UnreadableSectorError(f"The drive returned a short read at sector {position}")
            output += self._buffer.bytes(done.value)
            position += take
            remaining -= take
        return bytes(output)


def open_optical_device(letter: str, *, timeout_seconds: int = 30) -> OpticalDevice:
    """Prefer SCSI pass-through, falling back to Windows' raw volume reader."""
    try:
        return SptiDevice(letter, timeout_seconds=timeout_seconds)
    except (MediaUnavailableError, DriveFaultError):
        raise
    except OSError as error:
        # Pass-through can be denied by policy or unsupported by a bridge; the
        # volume reader still works there, only with less detailed errors.
        code = getattr(error, "winerror", 0) or 0
        fallback_codes = {ERROR_ACCESS_DENIED, ERROR_INVALID_FUNCTION, ERROR_NOT_SUPPORTED, ERROR_INVALID_PARAMETER}
        if code not in fallback_codes and not isinstance(error, OpticalError):
            raise
    return VolumeHandleDevice(letter)


# --- DVD-Video layout ----------------------------------------------------------------------------


@dataclass
class DiscFile:
    name: str
    extents: list[tuple[int, int]] = field(default_factory=list)  # (first sector, byte length)

    @property
    def size(self) -> int:
        return sum(length for _, length in self.extents)

    def sector_ranges(self) -> list[tuple[int, int]]:
        return [
            (start, start + (length + SECTOR_SIZE - 1) // SECTOR_SIZE)
            for start, length in self.extents
            if length > 0
        ]


@dataclass
class DvdLayout:
    files: dict[str, DiscFile]
    title_set: int | None
    priority_ranges: list[tuple[int, int]]
    verified: bool
    note: str = ""
    # Start sectors of the title set's video units (VOBUs, about half a second
    # each). A player skips to the next one after a read error; so does the rescue.
    unit_starts: list[int] = field(default_factory=list)
    movie_sectors: int = 0
    # Filesystem and IFO/BUP navigation data: tiny, and without it MakeMKV
    # cannot open the image at all ("Signature is invalid in IFO file").
    structure_ranges: list[tuple[int, int]] = field(default_factory=list)


ReadSectors = Callable[[int, int], bytes]


def merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted((start, end) for start, end in ranges if end > start):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def parse_segment_cells(segment_map: str) -> set[int]:
    """Cell numbers from MakeMKV's segment map, for example ``"7-16,17-28"``."""
    cells: set[int] = set()
    for part in str(segment_map or "").split(","):
        part = part.strip()
        if not part:
            continue
        low, _, high = part.partition("-")
        try:
            first = int(low)
            last = int(high) if high else first
        except ValueError:
            return set()
        if first <= 0 or last < first or last - first > 999:
            return set()
        cells.update(range(first, last + 1))
    return cells


def _be16(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 2], "big")


def _be32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "big")


def _read_file(read: ReadSectors, entry: DiscFile) -> bytes:
    start, length = entry.extents[0]
    sectors = (length + SECTOR_SIZE - 1) // SECTOR_SIZE
    if sectors > 4096:
        raise OpticalError(f"{entry.name} is implausibly large")
    return read(start, max(1, sectors))[:length]


def _iso_directory(read: ReadSectors, lba: int, size: int) -> list[tuple[str, int, int, int]]:
    sectors = max(1, (size + SECTOR_SIZE - 1) // SECTOR_SIZE)
    if sectors > 64:
        raise OpticalError("ISO 9660 directory is implausibly large")
    data = read(lba, sectors)[:size]
    entries: list[tuple[str, int, int, int]] = []
    offset = 0
    while offset < len(data):
        length = data[offset]
        if length == 0:
            offset = (offset // SECTOR_SIZE + 1) * SECTOR_SIZE
            continue
        record = data[offset : offset + length]
        offset += length
        if len(record) < 34:
            break
        name_length = record[32]
        raw_name = record[33 : 33 + name_length]
        if raw_name in {b"\x00", b"\x01"}:
            continue
        name = raw_name.decode("ascii", "replace").split(";", 1)[0].rstrip(".").upper()
        entries.append(
            (
                name,
                int.from_bytes(record[2:6], "little"),
                int.from_bytes(record[10:14], "little"),
                record[25],
            )
        )
    return entries


def read_video_ts_files(read: ReadSectors, total_sectors: int) -> dict[str, DiscFile]:
    """List VIDEO_TS files with sector extents from the ISO 9660 bridge filesystem."""
    root: tuple[int, int] | None = None
    for sector in range(16, 32):
        descriptor = read(sector, 1)
        if descriptor[1:6] != b"CD001":
            break
        if descriptor[0] == 1:
            record = descriptor[156:190]
            root = (int.from_bytes(record[2:6], "little"), int.from_bytes(record[10:14], "little"))
            break
        if descriptor[0] == 255:
            break
    if not root:
        raise OpticalError("The disc has no ISO 9660 directory")
    video_ts = next(
        (
            (lba, size)
            for name, lba, size, flags in _iso_directory(read, *root)
            if name == "VIDEO_TS" and flags & 0x02
        ),
        None,
    )
    if not video_ts:
        raise OpticalError("VIDEO_TS was not found in the ISO 9660 directory")
    files: dict[str, DiscFile] = {}
    for name, lba, size, flags in _iso_directory(read, *video_ts):
        if flags & 0x02:
            continue
        if lba <= 0 or lba >= total_sectors or lba + (size + SECTOR_SIZE - 1) // SECTOR_SIZE > total_sectors:
            raise OpticalError(f"{name} points outside the disc")
        files.setdefault(name, DiscFile(name)).extents.append((lba, size))
    return files


def dvd_title_entry(read: ReadSectors, files: dict[str, DiscFile], title_number: int) -> tuple[int, int, int] | None:
    """Return (title set, title within that set, chapter count) for a DVD title."""
    ifo = files.get("VIDEO_TS.IFO")
    if not ifo or not ifo.extents or title_number <= 0:
        return None
    ifo_lba = ifo.extents[0][0]
    header = read(ifo_lba, 1)
    if header[:12] != b"DVDVIDEO-VMG":
        return None
    table_sector = _be32(header, 0xC4)
    if table_sector <= 0:
        return None
    table = read(ifo_lba + table_sector, 1)
    count = _be16(table, 0)
    if not 1 <= title_number <= min(count, 99):
        return None
    entry = table[8 + 12 * (title_number - 1) : 8 + 12 * title_number]
    if len(entry) != 12 or not 1 <= entry[6] <= 99:
        return None
    return entry[6], entry[7], _be16(entry, 2)


def dvd_title_set_for_title(read: ReadSectors, files: dict[str, DiscFile], title_number: int) -> int | None:
    entry = dvd_title_entry(read, files, title_number)
    return entry[0] if entry else None


def dvd_title_cells(
    read: ReadSectors,
    files: dict[str, DiscFile],
    title_set: int,
    vts_title: int,
    chapters: int,
    wanted_cells: set[int] | None = None,
) -> list[tuple[int, int]]:
    """Sector ranges of the cells a DVD title plays, from its title set's IFO."""
    ifo = files.get(f"VTS_{title_set:02d}_0.IFO")
    video = files.get(f"VTS_{title_set:02d}_1.VOB")
    if not ifo or not video or not ifo.extents or not video.extents:
        return []
    data = _read_file(read, ifo)
    if data[:12] != b"DVDVIDEO-VTS":
        return []
    ptt = _be32(data, 0xC8) * SECTOR_SIZE
    pgci = _be32(data, 0xCC) * SECTOR_SIZE
    if not ptt or not pgci or ptt + 8 > len(data) or pgci + 8 > len(data):
        return []
    if not 1 <= vts_title <= _be16(data, ptt):
        return []
    title_offset = ptt + _be32(data, ptt + 8 + 4 * (vts_title - 1))
    programs = sorted(
        {
            _be16(data, title_offset + 4 * chapter)
            for chapter in range(max(1, chapters))
            if title_offset + 4 * chapter + 2 <= len(data)
        }
    )
    program_count = _be16(data, pgci)
    base = video.extents[0][0]
    # MakeMKV's cell numbers refer to one program chain; ignore them otherwise.
    filter_cells = bool(wanted_cells) and len(programs) == 1
    ranges: list[tuple[int, int]] = []
    for number in programs:
        if not 1 <= number <= program_count or pgci + 16 + 8 * number > len(data):
            return []
        pgc = pgci + _be32(data, pgci + 8 + 8 * (number - 1) + 4)
        if pgc + 0xEC > len(data):
            return []
        table = pgc + _be16(data, pgc + 0xE8)
        for cell in range(1, data[pgc + 3] + 1):
            entry = table + 24 * (cell - 1)
            if entry + 24 > len(data):
                return []
            if filter_cells and cell not in (wanted_cells or set()):
                continue
            first, last = _be32(data, entry + 8), _be32(data, entry + 20)
            if last >= first:
                ranges.append((base + first, base + last + 1))
    return merge_ranges(ranges)


def dvd_vobu_starts(read: ReadSectors, files: dict[str, DiscFile], title_set: int) -> list[int]:
    """Start sector of every VOBU in a title set, from the IFO's VOBU address map."""
    ifo = files.get(f"VTS_{title_set:02d}_0.IFO")
    video = files.get(f"VTS_{title_set:02d}_1.VOB")
    if not ifo or not video or not ifo.extents or not video.extents:
        return []
    data = _read_file(read, ifo)
    if data[:12] != b"DVDVIDEO-VTS":
        return []
    table = _be32(data, 0xE4) * SECTOR_SIZE
    if not table or table + 4 > len(data):
        return []
    count = max(0, (_be32(data, table) + 1 - 4) // 4)
    base = video.extents[0][0]
    return sorted(
        {base + _be32(data, table + 4 + 4 * index) for index in range(count) if table + 8 + 4 * index <= len(data)}
    )


def dvd_title_video_ranges(
    read: ReadSectors, total_sectors: int, title_number: int, segment_map: str = ""
) -> list[tuple[int, int]]:
    """The sectors one DVD title plays, so the movie can be read without the disc's navigation.

    A player follows the navigation in the IFO files to the movie. When damage
    leaves that navigation in a state MakeMKV refuses, these ranges still point
    straight at the title's own video. Falls back to the whole title set when the
    cell table cannot be read, as ``read_dvd_layout`` does.
    """
    files = read_video_ts_files(read, total_sectors)
    title = dvd_title_entry(read, files, title_number)
    if not title:
        return []
    prefix = f"VTS_{title[0]:02d}_"
    video = merge_ranges(
        [
            span
            for name, entry in files.items()
            if name.startswith(prefix) and name.endswith(".VOB") and name != f"{prefix}0.VOB"
            for span in entry.sector_ranges()
        ]
    )
    if not video:
        return []
    low, high = video[0][0], video[-1][1]
    try:
        cells = dvd_title_cells(read, files, title[0], title[1], title[2], parse_segment_cells(segment_map))
    except (OSError, ValueError):
        cells = []
    if cells and all(low <= start and end <= high for start, end in cells):
        return cells
    return video


def dvd_video_scrambled(read: ReadSectors, ranges: list[tuple[int, int]], samples: int = 24) -> bool:
    """Whether the video in these sectors is still CSS-scrambled.

    Scrambled video can only be read by a player that decrypts DVDs, not copied
    out of a rescued image as it is. Every pack says whether it is scrambled in
    its PES header, so a handful of packs spread over the movie answer this.
    """
    total = sum(max(0, end - start) for start, end in ranges)
    if total <= 0:
        return False
    for index in range(max(1, samples)):
        offset = total * index // max(1, samples)
        sector = -1
        for start, end in ranges:
            if offset < end - start:
                sector = start + offset
                break
            offset -= end - start
        if sector < 0:
            continue
        try:
            pack = read(sector, 1)
        except OpticalError:
            continue
        # A pack holds one PES packet; only audio, video and private streams carry the header with the
        # scrambling bits. Navigation packs (0xBF) and padding (0xBE) never do.
        if len(pack) < 21 or pack[:4] != b"\x00\x00\x01\xba" or pack[14:17] != b"\x00\x00\x01":
            continue
        if pack[17] in {0xBE, 0xBF} or pack[20] & 0xC0 != 0x80:
            continue
        if (pack[20] >> 4) & 0x03:
            return True
    return False


def read_dvd_layout(
    read: ReadSectors,
    total_sectors: int,
    title_number: int,
    *,
    windows_video_ts: Path | None = None,
    segment_map: str = "",
) -> DvdLayout:
    """Find the sectors that matter for one DVD title.

    The ISO 9660 bridge is cross-checked against the sizes Windows reports
    through UDF. Any disagreement disables prioritization rather than risking a
    wrong guess about which sectors belong to the movie.
    """
    files = read_video_ts_files(read, total_sectors)
    title = dvd_title_entry(read, files, title_number)
    title_set = title[0] if title else None
    if windows_video_ts is not None:
        try:
            for path in windows_video_ts.iterdir():
                entry = files.get(path.name.upper())
                if entry is None or entry.size != path.stat().st_size:
                    return DvdLayout(files, title_set, [], False, f"{path.name} differs between filesystems")
        except OSError as error:
            return DvdLayout(files, title_set, [], False, f"Windows could not list VIDEO_TS: {error}")
    if not title or not title_set:
        return DvdLayout(files, None, [], False, "The DVD title was not found in VIDEO_TS.IFO")
    prefix = f"VTS_{title_set:02d}_"
    structures: list[tuple[int, int]] = []
    title_video: list[tuple[int, int]] = []
    for name, entry in files.items():
        if name.endswith((".IFO", ".BUP")):
            structures.extend(entry.sector_ranges())
        elif name.startswith(prefix) and name.endswith(".VOB") and name != f"{prefix}0.VOB":
            title_video.extend(entry.sector_ranges())
    if not title_video:
        return DvdLayout(files, title_set, [], False, f"No video files were found for title set {title_set}")
    video_low = min(start for start, _ in title_video)
    video_high = max(end for _, end in title_video)
    try:
        cells = dvd_title_cells(read, files, title_set, title[1], title[2], parse_segment_cells(segment_map))
    except (OSError, ValueError):
        cells = []
    note = ""
    if cells and all(video_low <= start and end <= video_high for start, end in cells):
        movie = cells
    else:
        movie = merge_ranges(title_video)
        note = "The title's cell table could not be read; using all of its video files"
    try:
        units = [start for start in dvd_vobu_starts(read, files, title_set) if video_low <= start < video_high]
    except (OSError, ValueError):
        units = []
    first_file = min((start for entry in files.values() for start, _ in entry.extents), default=0)
    # Filesystem descriptors live before the first file and at the end of UDF discs.
    filesystem = [
        (0, min(total_sectors, max(first_file, 512))),
        (max(0, total_sectors - 512), total_sectors),
    ]
    return DvdLayout(
        files,
        title_set,
        merge_ranges([*structures, *movie, *filesystem]),
        True,
        note,
        units,
        sum(end - start for start, end in movie),
        merge_ranges([*structures, *filesystem]),
    )


@dataclass
class DvdTitle:
    """One title as the disc's own tables describe it, without asking MakeMKV."""

    number: int
    title_set: int
    duration_seconds: int
    chapters: int


def dvd_titles(read: ReadSectors, files: dict[str, DiscFile]) -> list[DvdTitle]:
    """Every title the disc's navigation lists, with the playback time it declares.

    MakeMKV refuses a disc whose navigation damage makes its titles look fake, and
    then reports none at all. The tables themselves still say what is on the disc.
    """
    ifo = files.get("VIDEO_TS.IFO")
    if not ifo or not ifo.extents:
        return []
    ifo_lba = ifo.extents[0][0]
    header = read(ifo_lba, 1)
    if header[:12] != b"DVDVIDEO-VMG" or _be32(header, 0xC4) <= 0:
        return []
    count = min(99, _be16(read(ifo_lba + _be32(header, 0xC4), 1), 0))
    titles: list[DvdTitle] = []
    for number in range(1, count + 1):
        entry = dvd_title_entry(read, files, number)
        if not entry:
            continue
        title_set, vts_title, chapters = entry
        vts = files.get(f"VTS_{title_set:02d}_0.IFO")
        if not vts or not vts.extents:
            continue
        try:
            data = _read_file(read, vts)
        except OpticalError:
            continue
        ptt, pgci = _be32(data, 0xC8) * SECTOR_SIZE, _be32(data, 0xCC) * SECTOR_SIZE
        if data[:12] != b"DVDVIDEO-VTS" or not ptt or not pgci or not 1 <= vts_title <= _be16(data, ptt):
            continue
        program_chain = _be16(data, ptt + _be32(data, ptt + 8 + 4 * (vts_title - 1)))
        if not 1 <= program_chain <= _be16(data, pgci):
            continue
        pgc = pgci + _be32(data, pgci + 8 + 8 * (program_chain - 1) + 4)
        if pgc + 8 > len(data):
            continue
        hours, minutes, seconds = (
            int(f"{value:02x}") if (value >> 4) < 10 and (value & 15) < 10 else 0
            for value in data[pgc + 4 : pgc + 7]
        )
        titles.append(DvdTitle(number, title_set, hours * 3600 + minutes * 60 + seconds, max(1, chapters)))
    return titles


def dvd_longest_title(read: ReadSectors, files: dict[str, DiscFile]) -> int:
    """The DVD title with the longest playback time, for a disc MakeMKV could not open."""
    titles = dvd_titles(read, files)
    if not titles:
        return 0
    return max(titles, key=lambda title: (title.duration_seconds, -title.number)).number


# --- UDF and Blu-ray layout ----------------------------------------------------------------------

UDF_AVDP = 2
UDF_PARTITION = 5
UDF_LOGICAL_VOLUME = 6
UDF_TERMINATOR = 8
UDF_FILE_SET = 256
UDF_FILE_IDENTIFIER = 257
UDF_ALLOCATION_EXTENT = 258
UDF_FILE_ENTRY = 261
UDF_EXTENDED_FILE_ENTRY = 266
UDF_MAX_FILES = 50000


def udf_tag(data: bytes, offset: int = 0) -> int:
    """The identifier of a UDF descriptor tag, or 0 when the bytes are not a valid tag."""
    if len(data) < offset + 16:
        return 0
    if (sum(data[offset : offset + 4]) + sum(data[offset + 5 : offset + 16])) & 0xFF != data[offset + 4]:
        return 0
    return int.from_bytes(data[offset : offset + 2], "little")


def _le16(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 2], "little")


def _le32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "little")


def _udf_name(raw: bytes) -> str:
    if not raw:
        return ""
    if raw[0] == 16:
        return raw[1:].decode("utf-16-be", "replace")
    return raw[1:].decode("latin-1")


@dataclass
class UdfVolume:
    """A UDF file system read sector by sector, including the metadata partition of Blu-ray discs."""

    read: ReadSectors
    total_sectors: int
    partition_starts: dict[int, int]
    # One entry per partition reference: ("physical", partition) or ("metadata", partition, extents).
    maps: list[tuple[Any, ...]]
    file_set: tuple[int, int]
    structure_sectors: set[int] = field(default_factory=set)
    note: str = ""

    @classmethod
    def open(cls, read: ReadSectors, total_sectors: int) -> UdfVolume:
        anchor = None
        for sector in (256, total_sectors - 1, total_sectors - 257):
            try:
                data = read(sector, 1)
            except OpticalError:
                continue
            if udf_tag(data) == UDF_AVDP:
                anchor = data
                break
        if anchor is None:
            raise OpticalError("The disc has no readable UDF anchor")
        partitions: dict[int, int] = {}
        logical_volume: bytes | None = None
        for length_offset in (16, 24):
            length, location = _le32(anchor, length_offset), _le32(anchor, length_offset + 4)
            for sector in range(location, location + min(64, max(1, length // SECTOR_SIZE))):
                try:
                    data = read(sector, 1)
                except OpticalError:
                    continue
                kind = udf_tag(data)
                if kind == UDF_PARTITION:
                    partitions.setdefault(_le16(data, 22), _le32(data, 188))
                elif kind == UDF_LOGICAL_VOLUME and logical_volume is None:
                    logical_volume = data
                elif kind == UDF_TERMINATOR:
                    break
            if partitions and logical_volume is not None:
                break
        if not partitions or logical_volume is None:
            raise OpticalError("The UDF volume descriptors could not be read")
        if _le32(logical_volume, 212) != SECTOR_SIZE:
            raise OpticalError("The UDF block size is not 2048 bytes")
        file_set = (_le32(logical_volume, 252), _le16(logical_volume, 256))
        volume = cls(read, total_sectors, partitions, [], file_set)
        offset = 440
        for _ in range(min(8, _le32(logical_volume, 268))):
            kind, size = logical_volume[offset], logical_volume[offset + 1]
            if kind == 1:
                volume.maps.append(("physical", _le16(logical_volume, offset + 4)))
            elif kind == 2:
                identifier = logical_volume[offset + 5 : offset + 28].rstrip(b"\x00")
                partition = _le16(logical_volume, offset + 38)
                if identifier == b"*UDF Metadata Partition":
                    extents = volume._metadata_extents(
                        partition, _le32(logical_volume, offset + 40), _le32(logical_volume, offset + 44)
                    )
                    volume.maps.append(("metadata", partition, extents))
                elif identifier == b"*UDF Sparable Partition":
                    volume.maps.append(("physical", partition))
                else:
                    raise OpticalError(f"Unsupported UDF partition type {identifier.decode('ascii', 'replace')}")
            else:
                raise OpticalError("Unknown UDF partition map")
            offset += max(6, size)
        return volume

    def _metadata_extents(self, partition: int, location: int, mirror: int) -> list[tuple[int, int]]:
        start = self.partition_starts.get(partition)
        if start is None:
            raise OpticalError("The UDF metadata partition refers to a missing partition")
        for candidate in (location, mirror):
            try:
                data = self.read(start + candidate, 1)
            except OpticalError:
                continue
            if udf_tag(data) not in {UDF_FILE_ENTRY, UDF_EXTENDED_FILE_ENTRY}:
                continue
            self.structure_sectors.add(start + candidate)
            extents = [
                (position, length // SECTOR_SIZE)
                for position, length, ref, kind in self._allocation(data, ("physical", partition), 0)
                if kind == 0 and ref == 0
            ]
            if extents:
                return extents
        raise OpticalError("The UDF metadata file entry could not be read")

    def physical(self, block: int, reference: int) -> int:
        if not 0 <= reference < len(self.maps):
            raise OpticalError("A UDF address refers to an unknown partition")
        mapping = self.maps[reference]
        base = self.partition_starts[mapping[1]]
        if mapping[0] == "physical":
            return base + block
        remaining = block
        for position, blocks in mapping[2]:
            if remaining < blocks:
                return base + position + remaining
            remaining -= blocks
        raise OpticalError("A UDF address lies outside the metadata partition")

    def _allocation(
        self, entry: bytes, mapping_or_reference: tuple[Any, ...] | int, depth: int
    ) -> list[tuple[int, int, int, int]]:
        """(block, byte length, partition reference, extent type) of each allocation descriptor."""
        kind = udf_tag(entry)
        if kind == UDF_FILE_ENTRY:
            base, ea_length, ad_length = 176, _le32(entry, 168), _le32(entry, 172)
        elif kind == UDF_EXTENDED_FILE_ENTRY:
            base, ea_length, ad_length = 216, _le32(entry, 208), _le32(entry, 212)
        elif kind == UDF_ALLOCATION_EXTENT:
            base, ea_length, ad_length = 24, 0, _le32(entry, 20)
        else:
            raise OpticalError("Not a UDF file entry")
        own_reference = mapping_or_reference if isinstance(mapping_or_reference, int) else 0
        flags = _le16(entry, 34) if kind != UDF_ALLOCATION_EXTENT else 0
        style = flags & 7
        data = entry[base + ea_length : base + ea_length + ad_length]
        result: list[tuple[int, int, int, int]] = []
        size = {0: 8, 1: 16, 2: 20}.get(style)
        if size is None:
            return result
        for offset in range(0, len(data) - size + 1, size):
            raw_length = _le32(data, offset)
            length, extent_type = raw_length & 0x3FFFFFFF, raw_length >> 30
            if length == 0:
                break
            if style == 0:
                block, reference = _le32(data, offset + 4), own_reference
            elif style == 1:
                block, reference = _le32(data, offset + 4), _le16(data, offset + 8)
            else:
                block, reference = _le32(data, offset + 12), _le16(data, offset + 16)
            if extent_type == 3:
                if depth >= 8:
                    raise OpticalError("UDF allocation descriptors continue too deep")
                sector = self.physical(block, reference)
                self.structure_sectors.add(sector)
                result.extend(self._allocation(self.read(sector, 1), reference, depth + 1))
                break
            result.append((block, length, reference, extent_type))
        return result

    def _entry(self, block: int, reference: int) -> tuple[bytes, int, int, list[tuple[int, int]], bytes | None]:
        """File entry bytes, file type, size, (sector, bytes) extents, and embedded data."""
        sector = self.physical(block, reference)
        data = self.read(sector, 1)
        kind = udf_tag(data)
        if kind not in {UDF_FILE_ENTRY, UDF_EXTENDED_FILE_ENTRY}:
            raise OpticalError(f"UDF file entry at sector {sector} is not valid")
        self.structure_sectors.add(sector)
        size = int.from_bytes(data[56:64], "little")
        file_type = data[27]
        if _le16(data, 34) & 7 == 3:
            base, ea_length, ad_length = (176, _le32(data, 168), _le32(data, 172)) if kind == UDF_FILE_ENTRY else (
                216,
                _le32(data, 208),
                _le32(data, 212),
            )
            return data, file_type, size, [], data[base + ea_length : base + ea_length + min(ad_length, size)]
        extents: list[tuple[int, int]] = []
        remaining = size
        for position, length, extent_reference, extent_type in self._allocation(data, reference, 0):
            used = min(length, remaining)
            if extent_type == 0 and used > 0:
                extents.append((self.physical(position, extent_reference), used))
            remaining -= length
            if remaining <= 0:
                break
        return data, file_type, size, extents, None

    def _read_extents(self, extents: list[tuple[int, int]], limit: int) -> bytes:
        chunks: list[bytes] = []
        total = 0
        for sector, length in extents:
            count = (length + SECTOR_SIZE - 1) // SECTOR_SIZE
            if total + length > limit:
                raise OpticalError("A UDF directory is implausibly large")
            for first in range(sector, sector + count, 32):
                self.structure_sectors.update(range(first, first + min(32, sector + count - first)))
                chunks.append(self.read(first, min(32, sector + count - first)))
            total += length
        data = b"".join(chunks)
        return data[: sum(length for _, length in extents)]

    def files(self) -> dict[str, DiscFile]:
        """Every file on the volume keyed by its upper-case path, with the sectors of its data."""
        file_set_sector = self.physical(*self.file_set)
        data = self.read(file_set_sector, 1)
        if udf_tag(data) != UDF_FILE_SET:
            raise OpticalError("The UDF file set descriptor is not valid")
        self.structure_sectors.add(file_set_sector)
        result: dict[str, DiscFile] = {}
        pending: list[tuple[str, int, int, int]] = [("", _le32(data, 404), _le16(data, 408), 0)]
        seen: set[tuple[int, int]] = set()
        while pending:
            prefix, block, reference, depth = pending.pop()
            if (block, reference) in seen or depth > 8:
                continue
            seen.add((block, reference))
            _, _, size, extents, embedded = self._entry(block, reference)
            directory = embedded if embedded is not None else self._read_extents(extents, 8 * 1024 * 1024)
            offset = 0
            while offset + 38 <= len(directory):
                if udf_tag(directory, offset) != UDF_FILE_IDENTIFIER:
                    break
                characteristics = directory[offset + 18]
                name_length = directory[offset + 19]
                child_block, child_reference = _le32(directory, offset + 24), _le16(directory, offset + 28)
                use_length = _le16(directory, offset + 36)
                name_start = offset + 38 + use_length
                name = _udf_name(directory[name_start : name_start + name_length])
                offset += (38 + use_length + name_length + 3) & ~3
                if characteristics & 0x0C or not name:
                    continue
                path = f"{prefix}{name}"
                if characteristics & 0x02:
                    pending.append((path + "/", child_block, child_reference, depth + 1))
                    continue
                if len(result) >= UDF_MAX_FILES:
                    raise OpticalError("The disc lists implausibly many files")
                _, _, _, child_extents, child_embedded = self._entry(child_block, child_reference)
                # Looked up by upper-case path; the name keeps the disc's own spelling,
                # which MakeMKV needs when it reads the files from a folder.
                entry = DiscFile(path, child_extents)
                if child_embedded is not None:
                    entry.extents = []
                result[path.upper()] = entry
            del size
        return result


def mpls_clips(data: bytes) -> tuple[list[str], float]:
    """Clip names (for example "00055") a Blu-ray playlist plays, and its length in seconds."""
    if data[:4] != b"MPLS" or len(data) < 20:
        raise OpticalError("Not a Blu-ray playlist")
    start = _be32(data, 8)
    if start + 10 > len(data):
        raise OpticalError("The Blu-ray playlist is truncated")
    items, subpaths = _be16(data, start + 6), _be16(data, start + 8)
    offset = start + 10
    clips: list[str] = []
    seconds = 0.0
    for _ in range(items):
        length = _be16(data, offset)
        item = data[offset + 2 : offset + 2 + length]
        if len(item) < 20:
            raise OpticalError("A Blu-ray play item is truncated")
        clips.append(item[0:5].decode("ascii", "replace"))
        seconds += max(0, _be32(item, 16) - _be32(item, 12)) / 45000
        if (_be16(item, 9) >> 4) & 1 and len(item) > 34:
            for angle in range(1, item[32]):
                name = item[34 + 10 * (angle - 1) : 39 + 10 * (angle - 1)]
                if len(name) == 5:
                    clips.append(name.decode("ascii", "replace"))
        offset += 2 + length
    for _ in range(subpaths):
        if offset + 4 > len(data):
            break
        length = _be32(data, offset)
        sub = data[offset + 4 : offset + 4 + length]
        position = 6
        for _ in range(sub[5] if len(sub) > 5 else 0):
            item_length = _be16(sub, position)
            name = sub[position + 2 : position + 7]
            if len(name) == 5:
                clips.append(name.decode("ascii", "replace"))
            position += 2 + item_length
        offset += 4 + length
    return list(dict.fromkeys(clip for clip in clips if clip.isdigit())), seconds


# Stream coding types in a playlist's stream table.
MPLS_VIDEO_CODINGS = frozenset({0x01, 0x02, 0x1B, 0x20, 0x24, 0xEA})
MPLS_AUDIO_CODINGS = frozenset({0x03, 0x04, 0x80, 0x81, 0x82, 0x83, 0x84, 0x85, 0x86, 0xA1, 0xA2})
MPLS_SUBTITLE_CODINGS = frozenset({0x90})


@dataclass(frozen=True)
class PlaylistStream:
    """A stream of a playlist's main clip: its transport-stream PID, kind and language."""

    pid: int
    kind: str
    language: str = ""


def _mpls_items(data: bytes) -> list[bytes]:
    if data[:4] != b"MPLS" or len(data) < 20:
        raise OpticalError("Not a Blu-ray playlist")
    start = _be32(data, 8)
    if start + 10 > len(data):
        raise OpticalError("The Blu-ray playlist is truncated")
    items: list[bytes] = []
    offset = start + 10
    for _ in range(_be16(data, start + 6)):
        length = _be16(data, offset)
        item = data[offset + 2 : offset + 2 + length]
        if len(item) < 20:
            raise OpticalError("A Blu-ray play item is truncated")
        items.append(item)
        offset += 2 + length
    return items


def mpls_play_items(data: bytes) -> list[tuple[str, float, float]]:
    """The clips a playlist plays one after another, with their in and out times in seconds."""
    return [
        (item[0:5].decode("ascii", "replace"), _be32(item, 12) / 45000, _be32(item, 16) / 45000)
        for item in _mpls_items(data)
    ]


def mpls_streams(data: bytes) -> list[PlaylistStream]:
    """Video, audio and subtitle streams of the playlist's main clips, in the playlist's order."""
    items = _mpls_items(data)
    if not items:
        return []
    item = items[0]
    angles = item[32] if (_be16(item, 9) >> 4) & 1 and len(item) > 32 else 1
    table = 32 if angles <= 1 else 34 + 10 * (angles - 1)
    if len(item) < table + 16:
        return []
    streams: list[PlaylistStream] = []
    position = table + 16
    # Primary video, primary audio and presentation graphics come first in the table.
    for kind, count in zip(("video", "audio", "subtitle"), item[table + 4 : table + 7], strict=True):
        for _ in range(count):
            if position >= len(item):
                return streams
            entry = item[position + 1 : position + 1 + item[position]]
            position += 1 + item[position]
            if position >= len(item):
                return streams
            attributes = item[position + 1 : position + 1 + item[position]]
            position += 1 + item[position]
            # Type 1 streams are in the main clip; the others live in sub-clips.
            if len(entry) < 3 or entry[0] != 1 or not attributes:
                continue
            coding = attributes[0]
            if coding in MPLS_AUDIO_CODINGS:
                language = attributes[2:5]
            elif coding in MPLS_SUBTITLE_CODINGS:
                language = attributes[1:4]
            elif coding in MPLS_VIDEO_CODINGS:
                language = b""
            else:
                continue
            text = language.decode("ascii", "replace").strip("\x00 ").lower()
            streams.append(PlaylistStream(_be16(entry, 1), kind, text if len(text) == 3 and text.isalpha() else ""))
    return streams


def mpls_chapters(data: bytes) -> list[float]:
    """Chapter start times, in seconds from the start of the playlist."""
    items = _mpls_items(data)
    starts: list[tuple[int, int]] = []
    elapsed = 0
    for item in items:
        starts.append((elapsed, _be32(item, 12)))
        elapsed += max(0, _be32(item, 16) - _be32(item, 12))
    marks = _be32(data, 12)
    if not marks or marks + 6 > len(data):
        return []
    chapters: set[float] = set()
    for index in range(_be16(data, marks + 4)):
        mark = data[marks + 6 + 14 * index : marks + 20 + 14 * index]
        # Mark type 1 is an entry mark, which players show as a chapter.
        if len(mark) < 14 or mark[1] != 1 or _be16(mark, 2) >= len(items):
            continue
        offset, in_time = starts[_be16(mark, 2)]
        chapters.add(round((offset + max(0, _be32(mark, 4) - in_time)) / 45000, 3))
    return sorted(chapters)


@dataclass
class BlurayLayout:
    files: dict[str, DiscFile]
    playlist: str
    clips: list[str]
    priority_ranges: list[tuple[int, int]]
    verified: bool
    note: str = ""
    movie_sectors: int = 0
    structure_ranges: list[tuple[int, int]] = field(default_factory=list)


def read_bluray_layout(
    read: ReadSectors,
    total_sectors: int,
    playlist: str = "",
    *,
    windows_root: Path | None = None,
    segment_map: str = "",
) -> BlurayLayout:
    """Find the sectors of a Blu-ray's file system, navigation files and one playlist's video clips.

    Without a playlist name the longest playlist is taken as the movie. The
    file sizes are cross-checked against what Windows reports; any
    disagreement disables prioritization.
    """
    volume = UdfVolume.open(read, total_sectors)
    files = volume.files()
    playlists = {
        name.rsplit("/", 1)[1].removesuffix(".MPLS"): entry
        for name, entry in files.items()
        if name.startswith("BDMV/PLAYLIST/") and name.endswith(".MPLS")
    }
    streams = {
        name.rsplit("/", 1)[1].removesuffix(".M2TS"): entry
        for name, entry in files.items()
        if name.startswith("BDMV/STREAM/") and name.endswith(".M2TS")
    }
    if not playlists or not streams:
        return BlurayLayout(files, "", [], [], False, "The disc has no Blu-ray playlists or video clips")
    if windows_root is not None:
        try:
            for folder, suffix in (("PLAYLIST", ".MPLS"), ("STREAM", ".M2TS")):
                for path in (windows_root / "BDMV" / folder).iterdir():
                    if path.suffix.upper() != suffix:
                        continue
                    entry = files.get(f"BDMV/{folder}/{path.name.upper()}")
                    if entry is None or entry.size != path.stat().st_size:
                        return BlurayLayout(files, "", [], [], False, f"{path.name} differs between file systems")
        except OSError as error:
            return BlurayLayout(files, "", [], [], False, f"Windows could not list the Blu-ray folders: {error}")
    wanted = Path(playlist).stem.upper() if playlist else ""
    clips: list[str] = []
    chosen = ""
    note = ""
    if wanted and wanted in playlists:
        try:
            clips, _ = mpls_clips(_read_file(read, playlists[wanted]))
            chosen = wanted
        except OpticalError as error:
            note = f"Playlist {wanted} could not be read: {error}"
    if not clips and segment_map:
        numbers = [part.strip() for part in segment_map.split(",") if part.strip().isdigit()]
        clips = [f"{int(number):05d}" for number in numbers]
        chosen = wanted
    if not clips and not wanted:
        longest = -1.0
        for name, entry in playlists.items():
            try:
                names, seconds = mpls_clips(_read_file(read, entry))
            except OpticalError:
                continue
            if seconds > longest and names:
                chosen, clips, longest = name, names, seconds
    missing = [clip for clip in clips if clip not in streams]
    if not clips or missing:
        return BlurayLayout(
            files, chosen, clips, [], False, note or f"The playlist's clips were not found: {', '.join(missing)}"
        )
    movie = merge_ranges([span for clip in clips for span in streams[clip].sector_ranges()])
    video_names = {f"BDMV/STREAM/{clip}.M2TS" for clip in streams}
    navigation = [
        span
        for name, entry in files.items()
        if name not in video_names and "/SSIF/" not in name
        for span in entry.sector_ranges()
    ]
    data_start = min((start for entry in files.values() for start, _ in entry.sector_ranges()), default=0)
    structure = merge_ranges(
        [
            (0, min(total_sectors, max(512, data_start))),
            *navigation,
            *[(sector, sector + 1) for sector in volume.structure_sectors],
            *[
                (volume.partition_starts[mapping[1]] + position, volume.partition_starts[mapping[1]] + position + blocks)
                for mapping in volume.maps
                if mapping[0] == "metadata"
                for position, blocks in mapping[2]
            ],
        ]
    )
    return BlurayLayout(
        files,
        chosen,
        clips,
        merge_ranges([*structure, *movie]),
        True,
        note,
        sum(end - start for start, end in movie),
        structure,
    )
