"""Resumable, bounded rescue imaging for damaged optical discs.

A damaged disc is read the way a player reads it:

1. Sweep: copy the disc in large reads. Where a read fails, go one
   error-correction block at a time, and after each unreadable block skip to the
   start of the next video unit (a DVD VOBU is about half a second of video). A
   player skips the same way, so a scratch costs one failed read per damaged
   half-second instead of the drive's retries for every block in it. Only when
   unit after unit fails does the sweep jump ahead in growing steps.
2. Retry: come back to what the sweep skipped. Each round reads forward in every
   damaged unit until its next unreadable block, so every damaged moment gets a
   turn before any gets a second one. Blocks that failed get one more attempt,
   because a marginal block often reads on a later try. Retrying stops at the
   time budget, as soon as little more is coming back, or when the user skips it.

Unread sectors stay zero in the image, so MakeMKV sees a complete, seekable
disc and resynchronises after each hole. The image is flushed before the map is
saved atomically, so a crash can never mark unwritten data as rescued.

A drive can also fail as a whole: after thousands of retries it may start
rejecting every read instantly, including sectors it read a moment earlier.
Those errors say nothing about the disc, so the engine never records them as
damage. It pauses, reconnects, and checks a sector it already rescued; if the
drive stays broken it stops and leaves the untested blocks for a later retry.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from bisect import bisect_left, bisect_right
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from .optical import (
    DVD_ECC_BLOCK_SECTORS,
    SECTOR_SIZE,
    CopyProtectionError,
    DriveFaultError,
    MediaUnavailableError,
    OpticalDevice,
    OpticalError,
    UnreadableSectorError,
    dvd_longest_title,
    merge_ranges,
    open_optical_device,
    read_bluray_layout,
    read_dvd_layout,
    read_video_ts_files,
)

NON_TRIED = "?"
NON_TRIMMED = "*"
NON_SCRAPED = "/"
BAD = "-"
FINISHED = "+"
STATUSES = {NON_TRIED, NON_TRIMMED, NON_SCRAPED, BAD, FINISHED}
PENDING = frozenset({NON_TRIED, NON_TRIMMED, NON_SCRAPED})
MAP_VERSION = 1
EVENT_PREFIX = "RESCUE "

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_COPY_PROTECTION = 3
EXIT_MEDIA_UNAVAILABLE = 4
EXIT_DRIVE_FAULT = 5
EXIT_WRONG_DISC = 6
# ProcessRunner treats Windows exit codes 1 and 255 as a cancellation.
EXIT_FAILED = 10
EXIT_INTERRUPTED = 130

DRIVE_RESET_ADVICE = (
    "Unplug and reconnect the drive (or eject and reinsert the disc), then choose Retry. "
    "Everything rescued so far is kept."
)

Emit = Callable[[dict[str, Any]], None]


class RescueFinishRequested(Exception):
    """The user asked DiscDock to continue with the data rescued so far."""


class DriveStoppedResponding(Exception):
    """The drive failed and did not recover; untested blocks remain pending."""


class WrongDiscError(Exception):
    """The disc in the drive is not the one the saved rescue image was made from."""


class _StructuresRead(Exception):
    """A structures-only run has read the file system and navigation."""


class RescueMap:
    """Contiguous sector ranges covering the whole disc, each with a status."""

    def __init__(
        self,
        total_sectors: int,
        ranges: list[tuple[int, int, str]] | None = None,
        meta: dict[str, Any] | None = None,
    ):
        if total_sectors <= 0:
            raise ValueError("A rescue map needs at least one sector")
        self.total_sectors = int(total_sectors)
        self.meta: dict[str, Any] = dict(meta or {})
        source = ranges if ranges is not None else [(0, self.total_sectors, NON_TRIED)]
        position = 0
        merged: list[tuple[int, int, str]] = []
        for start, end, status in source:
            if start != position or end <= start or status not in STATUSES:
                raise ValueError("The rescue map is not contiguous")
            if merged and merged[-1][2] == status:
                merged[-1] = (merged[-1][0], end, status)
            else:
                merged.append((int(start), int(end), status))
            position = end
        if position != self.total_sectors:
            raise ValueError("The rescue map does not cover the whole disc")
        self._ranges = merged
        self._starts = [item[0] for item in merged]

    def set(self, start: int, end: int, status: str) -> None:
        if status not in STATUSES:
            raise ValueError(f"Unknown rescue status {status!r}")
        start = max(0, int(start))
        end = min(self.total_sectors, int(end))
        if end <= start:
            return
        first = bisect_right(self._starts, start) - 1
        stop = bisect_left(self._starts, end)
        low = max(0, first - 1)
        high = min(len(self._ranges), stop + 1)
        segment = self._ranges[low:high]
        pieces = [(s, min(e, start), st) for s, e, st in segment if s < start]
        pieces.append((start, end, status))
        pieces.extend((max(s, end), e, st) for s, e, st in segment if e > end)
        merged: list[tuple[int, int, str]] = []
        for piece in pieces:
            if piece[1] <= piece[0]:
                continue
            if merged and merged[-1][2] == piece[2] and merged[-1][1] == piece[0]:
                merged[-1] = (merged[-1][0], piece[1], piece[2])
            else:
                merged.append(piece)
        self._ranges[low:high] = merged
        self._starts[low:high] = [piece[0] for piece in merged]

    def status_at(self, sector: int) -> str:
        index = bisect_right(self._starts, sector) - 1
        return self._ranges[index][2]

    def ranges(self, statuses: set[str] | frozenset[str] | None = None) -> Iterator[tuple[int, int, str]]:
        for item in self._ranges:
            if statuses is None or item[2] in statuses:
                yield item

    def count(self, statuses: set[str] | frozenset[str] | str) -> int:
        wanted = {statuses} if isinstance(statuses, str) else statuses
        return sum(end - start for start, end, status in self._ranges if status in wanted)

    def count_within(self, statuses: set[str] | frozenset[str], windows: list[tuple[int, int]]) -> int:
        total = 0
        for low, high in windows:
            index = max(0, bisect_right(self._starts, low) - 1)
            for start, end, status in self._ranges[index:]:
                if start >= high:
                    break
                if status in statuses:
                    total += max(0, min(end, high) - max(start, low))
        return total

    def next_range(self, statuses: set[str] | frozenset[str], from_sector: int) -> tuple[int, int, str] | None:
        index = max(0, bisect_right(self._starts, from_sector) - 1)
        for start, end, status in self._ranges[index:]:
            if end > from_sector and status in statuses:
                return max(start, from_sector), end, status
        return None

    def areas(self, statuses: set[str] | frozenset[str]) -> list[tuple[int, int]]:
        """Return maximal runs whose statuses are all in ``statuses``."""
        runs: list[tuple[int, int]] = []
        for start, end, status in self._ranges:
            if status not in statuses:
                continue
            if runs and runs[-1][1] == start:
                runs[-1] = (runs[-1][0], end)
            else:
                runs.append((start, end))
        return runs

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": MAP_VERSION,
            "sector_size": SECTOR_SIZE,
            "total_sectors": self.total_sectors,
            "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "meta": self.meta,
            "ranges": [[start, end, status] for start, end, status in self._ranges],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> RescueMap:
        if int(payload.get("version") or 0) != MAP_VERSION or int(payload.get("sector_size") or 0) != SECTOR_SIZE:
            raise ValueError("Unsupported rescue map")
        return cls(
            int(payload["total_sectors"]),
            [(int(start), int(end), str(status)) for start, end, status in payload["ranges"]],
            payload.get("meta") if isinstance(payload.get("meta"), dict) else None,
        )

    def save(self, path: Path) -> None:
        temporary = path.with_name(path.name + ".new")
        temporary.write_text(json.dumps(self.to_dict(), separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, path)

    @classmethod
    def load(cls, path: Path) -> RescueMap:
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))


def adopt_existing_image(image: Path, total_sectors: int, emit: Emit | None = None) -> RescueMap:
    """Build a map for an image written without one (older DiscDock releases).

    Earlier releases zero-filled skipped sectors and recorded them only in the
    job database. All-zero sectors are therefore treated as not yet read; the
    few genuinely blank sectors on a disc simply get read again quickly.
    """
    rescue_map = RescueMap(total_sectors)
    try:
        size = image.stat().st_size
    except OSError:
        return rescue_map
    usable = min(size, total_sectors * SECTOR_SIZE) // SECTOR_SIZE
    if usable <= 0:
        return rescue_map
    rescue_map.set(0, usable, FINISHED)
    zero_sector = bytes(SECTOR_SIZE)
    block_sectors = 512
    zero_block = bytes(block_sectors * SECTOR_SIZE)
    last_report = time.monotonic()
    with image.open("rb") as handle:
        sector = 0
        while sector < usable:
            count = min(block_sectors, usable - sector)
            block = handle.read(count * SECTOR_SIZE)
            if len(block) < count * SECTOR_SIZE:
                rescue_map.set(sector, usable, NON_TRIED)
                break
            if block == zero_block[: len(block)]:
                rescue_map.set(sector, sector + count, NON_TRIED)
            elif zero_sector in block:
                for index in range(count):
                    offset = index * SECTOR_SIZE
                    if block[offset : offset + SECTOR_SIZE] == zero_sector:
                        rescue_map.set(sector + index, sector + index + 1, NON_TRIED)
            sector += count
            now = time.monotonic()
            if emit and now - last_report >= 2:
                last_report = now
                emit({"type": "adopting", "percent": round(sector * 100 / usable, 2)})
    return rescue_map


def merge_rescue_images(target_image: Path, target_map: Path, donor_image: Path, donor_map: Path) -> int:
    """Copy sectors a donor rescue read that the target still lacks. Returns sectors copied.

    Reads from a damaged drive are not repeatable: a block that failed in one
    attempt often read in another. Two rescues of the same disc together
    therefore hold more of it than either alone.
    """
    target = RescueMap.load(target_map)
    donor = RescueMap.load(donor_map)
    if target.total_sectors != donor.total_sectors:
        raise ValueError("The rescue images are from discs of different sizes")
    donor_finished = donor.areas({FINISHED})
    overlap = _intersect(target.areas({FINISHED}), donor_finished)
    wanted = _intersect(target.areas(STATUSES - {FINISHED}), donor_finished)
    copied = 0
    with donor_image.open("rb") as source, target_image.open("r+b") as destination:
        size = target.total_sectors * SECTOR_SIZE
        destination.seek(0, os.SEEK_END)
        if destination.tell() < size:
            _extend_file(destination, size)
        # Refuse to mix discs: sectors both rescues read must be identical.
        step = max(1, len(overlap) // 64)
        for low, high in overlap[::step][:64]:
            sector = (low + high) // 2
            source.seek(sector * SECTOR_SIZE)
            destination.seek(sector * SECTOR_SIZE)
            if source.read(SECTOR_SIZE) != destination.read(SECTOR_SIZE):
                raise ValueError("The rescue images contain different data, so they are not the same disc")
        for start, end in wanted:
            position = start
            while position < end:
                count = min(512, end - position)
                source.seek(position * SECTOR_SIZE)
                data = source.read(count * SECTOR_SIZE)
                if len(data) != count * SECTOR_SIZE:
                    break
                destination.seek(position * SECTOR_SIZE)
                destination.write(data)
                position += count
            target.set(start, position, FINISHED)
            copied += position - start
        destination.flush()
        os.fsync(destination.fileno())
    if copied:
        target.meta["merged_sectors"] = int(target.meta.get("merged_sectors") or 0) + copied
    target.save(target_map)
    return copied


def _intersect(first: list[tuple[int, int]], second: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Intersection of two sorted lists of disjoint ranges."""
    result: list[tuple[int, int]] = []
    i = j = 0
    while i < len(first) and j < len(second):
        low = max(first[i][0], second[j][0])
        high = min(first[i][1], second[j][1])
        if high > low:
            result.append((low, high))
        if first[i][1] < second[j][1]:
            i += 1
        else:
            j += 1
    return result


def _subtract(ranges: list[tuple[int, int]], removed: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """The parts of ``ranges`` not covered by ``removed``."""
    result: list[tuple[int, int]] = []
    holes = merge_ranges(removed)
    for start, end in merge_ranges(ranges):
        position = start
        for low, high in holes:
            if high <= position or low >= end:
                continue
            if low > position:
                result.append((position, low))
            position = max(position, high)
            if position >= end:
                break
        if position < end:
            result.append((position, end))
    return result


def _reliable_reader(
    read: Callable[[int, int], bytes],
    attempts: int = 3,
    on_failure: Callable[[int, int], None] | None = None,
) -> Callable[[int, int], bytes]:
    """Retry layout reads a few times: one marginal navigation block must not disable movie-first reading."""

    def reader(lba: int, count: int) -> bytes:
        for attempt in range(1, attempts + 1):
            try:
                return read(lba, count)
            except UnreadableSectorError:
                if on_failure is not None:
                    on_failure(lba, attempt)
                if attempt == attempts:
                    raise
        raise UnreadableSectorError(f"Sector {lba} is unreadable")

    return reader


def _saved_image_reader(
    image: Path, map_path: Path, total_sectors: int
) -> tuple[Callable[[int, int], bytes] | None, BinaryIO | None]:
    """Read only sectors an earlier run already rescued, straight from its image.

    Resuming then finds the movie without the drive, which matters when the
    drive is struggling. The engine still checks that the disc is the same one.
    """
    try:
        rescued = RescueMap.load(map_path) if map_path.is_file() and image.is_file() else None
    except (OSError, ValueError, KeyError, TypeError):
        rescued = None
    if rescued is None or rescued.total_sectors != total_sectors:
        return None, None
    handle = image.open("rb")

    def read(lba: int, count: int) -> bytes:
        if rescued.count_within({FINISHED}, [(lba, lba + count)]) != count:
            raise UnreadableSectorError(f"Sector {lba} has not been rescued yet")
        handle.seek(lba * SECTOR_SIZE)
        data = handle.read(count * SECTOR_SIZE)
        if len(data) != count * SECTOR_SIZE:
            raise UnreadableSectorError(f"Sector {lba} is missing from the saved image")
        return data

    return read, handle


def _find_layout(
    arguments: argparse.Namespace, read: Callable[[int, int], bytes], total_sectors: int, *, check_windows: bool
) -> tuple[dict[str, Any], list[tuple[int, int]] | None, list[tuple[int, int]], list[int]]:
    """The layout event, movie ranges (None when not verified), navigation ranges and video units."""
    letter = arguments.drive.rstrip(":\\/").upper()
    if arguments.disc == "bluray":
        bluray = read_bluray_layout(
            read,
            total_sectors,
            arguments.playlist,
            windows_root=Path(f"{letter}:\\") if check_windows else None,
            segment_map=arguments.segment_map,
        )
        event = {
            "type": "layout",
            "verified": bluray.verified,
            "playlist": bluray.playlist,
            "clips": bluray.clips,
            "note": bluray.note,
            "priority_bytes": sum(end - start for start, end in bluray.priority_ranges) * SECTOR_SIZE,
            "movie_bytes": bluray.movie_sectors * SECTOR_SIZE,
            "units": 0,
        }
        if not bluray.verified:
            return event, None, [], []
        return event, bluray.priority_ranges, bluray.structure_ranges, []
    if arguments.dvd_title > 0 or arguments.auto_title:
        title = arguments.dvd_title or dvd_longest_title(read, read_video_ts_files(read, total_sectors))
        layout = read_dvd_layout(
            read,
            total_sectors,
            title,
            windows_video_ts=Path(f"{letter}:\\VIDEO_TS") if check_windows else None,
            segment_map=arguments.segment_map,
        )
        event = {
            "type": "layout",
            "verified": layout.verified,
            "title": title,
            "title_set": layout.title_set,
            "note": layout.note,
            "priority_bytes": sum(end - start for start, end in layout.priority_ranges) * SECTOR_SIZE,
            "movie_bytes": layout.movie_sectors * SECTOR_SIZE,
            "units": len(layout.unit_starts),
        }
        if not layout.verified:
            return event, None, [], []
        return event, layout.priority_ranges, layout.structure_ranges, layout.unit_starts
    return {}, None, [], []


def _set_sparse(handle: BinaryIO) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
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
        returned = wintypes.DWORD()
        kernel32.DeviceIoControl(
            msvcrt.get_osfhandle(handle.fileno()), 0x000900C4, None, 0, None, 0, ctypes.byref(returned), None
        )
    except (OSError, ValueError, AttributeError):
        # A non-sparse image is still correct; it only allocates space earlier.
        pass


def _extend_file(handle: BinaryIO, size: int) -> None:
    """Grow an image to the disc size without writing zeros.

    Python's truncate() fills the new space with zeros on Windows. That takes
    minutes for a Blu-ray and allocates the whole image even when it is sparse.
    """
    handle.flush()
    if os.name == "nt":
        try:
            import ctypes
            import msvcrt
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.SetFilePointerEx.argtypes = [
                wintypes.HANDLE,
                ctypes.c_longlong,
                ctypes.POINTER(ctypes.c_longlong),
                wintypes.DWORD,
            ]
            kernel32.SetFilePointerEx.restype = wintypes.BOOL
            kernel32.SetEndOfFile.argtypes = [wintypes.HANDLE]
            kernel32.SetEndOfFile.restype = wintypes.BOOL
            os_handle = msvcrt.get_osfhandle(handle.fileno())
            if kernel32.SetFilePointerEx(os_handle, size, None, 0) and kernel32.SetEndOfFile(os_handle):
                handle.seek(0)
                return
        except (OSError, ValueError, AttributeError):
            pass
    handle.truncate(size)


@dataclass
class RescueOptions:
    cluster_sectors: int = DVD_ECC_BLOCK_SECTORS
    extra_seconds: float = 900.0
    priority_ranges: list[tuple[int, int]] | None = None
    # Navigation and filesystem sectors, retried before anything else.
    critical_ranges: list[tuple[int, int]] | None = None
    # Start sectors of playback units (DVD VOBUs). Without them, damaged areas
    # are skipped in fixed steps of skip_sectors.
    unit_starts: list[int] | None = None
    skip_sectors: int = 256
    solid_damage_units: int = 8
    max_jump_sectors: int = 32768
    clean_exit_sectors: int = 2048
    save_interval_seconds: float = 5.0
    report_interval_seconds: float = 2.0
    device_wait_seconds: float = 60.0
    # A real medium error costs the drive seconds of retries. This many
    # failures in a row that each return almost immediately mean the drive,
    # not the disc, has stopped working.
    fast_failure_seconds: float = 0.5
    fast_failure_limit: int = 24
    fault_retries: int = 3
    fault_pauses: tuple[float, ...] = (5.0, 15.0, 30.0)
    # Retrying stops early once this window recovered less than stall_min_bytes.
    stall_window_seconds: float = 180.0
    stall_min_bytes: int = 2 * 1024 * 1024
    # Read only the file system and navigation (critical_ranges), for a disc
    # MakeMKV could not open: it then lists the titles from the image.
    structures_only: bool = False


class RescueEngine:
    def __init__(
        self,
        device: OpticalDevice,
        image_path: Path,
        map_path: Path,
        options: RescueOptions | None = None,
        *,
        emit: Emit | None = None,
        control_path: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.device = device
        self.image_path = image_path
        self.map_path = map_path
        self.options = options or RescueOptions()
        self.emit = emit or (lambda _event: None)
        self.control_path = control_path
        self.clock = clock
        self.sleep = sleep
        self.cluster = max(1, int(self.options.cluster_sectors))
        self.skip = max(self.cluster, int(self.options.skip_sectors))
        self.map: RescueMap | None = None
        self.phase = "starting"
        self.cursor = 0
        self.read_errors = 0
        self.drive_faults = 0
        self.retry_round = 0
        self.in_zone = False
        self.stop_reason = ""
        self.finish_requested = False
        self.started = clock()
        self.extra_started: float | None = None
        self._extra_used_before = 0.0
        self._image: BinaryIO | None = None
        self._last_save = clock()
        self._last_report = 0.0
        self._last_control_check = 0.0
        self._rate_window: deque[tuple[float, int]] = deque(maxlen=64)
        self._fast_failures: deque[tuple[int, int]] = deque()
        self._stall_samples: deque[tuple[float, int]] = deque()
        self._bytes_read = 0
        self._pending_at_extra_start = 0
        self.priority = merge_ranges(self.options.priority_ranges or []) or None
        self.critical = merge_ranges(self.options.critical_ranges or [])
        self.units = sorted(set(self.options.unit_starts or []))
        # What this run's sweep reads: the movie (or the whole disc) minus what earlier runs swept.
        self.sweep_ranges: list[tuple[int, int]] = []

    # --- setup ---------------------------------------------------------------------------------

    def _load_map(self) -> tuple[RescueMap, str]:
        total = int(self.device.total_sectors)
        if total <= 0:
            raise OpticalError("The drive did not report the disc size")
        loaded: RescueMap | None = None
        if self.map_path.is_file():
            try:
                loaded = RescueMap.load(self.map_path)
            except (OSError, ValueError, KeyError, TypeError):
                loaded = None
        if loaded is not None and self.image_path.is_file():
            if loaded.total_sectors != total:
                raise WrongDiscError(
                    "The disc in the drive has a different size than the saved rescue image, so it is another disc"
                )
            return loaded, "resumed"
        if self.image_path.is_file() and self.image_path.stat().st_size > 0:
            if self.image_path.stat().st_size > total * SECTOR_SIZE:
                raise WrongDiscError("The saved rescue image is larger than the disc in the drive")
            return adopt_existing_image(self.image_path, total, self.emit), "adopted"
        return RescueMap(total), "new"

    def _verify_same_disc(self) -> None:
        """Refuse to add sectors of another disc that merely shares a volume label."""
        assert self.map is not None and self._image is not None
        for sector in (16, 17, 18, 32, 64, 128, 256):
            if sector >= self.map.total_sectors or self.map.status_at(sector) != FINISHED:
                continue
            try:
                data = self.device.read(sector, 1)
            except OpticalError:
                continue
            self._image.seek(sector * SECTOR_SIZE)
            if self._image.read(SECTOR_SIZE) != data:
                raise WrongDiscError("The disc in the drive is not the disc this rescue image was made from")

    def _open_image(self) -> None:
        assert self.map is not None
        self.image_path.parent.mkdir(parents=True, exist_ok=True)
        mode = "r+b" if self.image_path.exists() else "w+b"
        handle = self.image_path.open(mode)
        _set_sparse(handle)
        size = self.map.total_sectors * SECTOR_SIZE
        handle.seek(0, os.SEEK_END)
        if handle.tell() != size:
            _extend_file(handle, size)
        self._image = handle

    # --- bookkeeping ---------------------------------------------------------------------------

    def _save(self, *, force: bool = False) -> None:
        now = self.clock()
        if not force and now - self._last_save < self.options.save_interval_seconds:
            return
        if self._image is not None:
            self._image.flush()
            os.fsync(self._image.fileno())
        if self.map is not None:
            self.map.meta["extra_used_seconds"] = round(self._extra_elapsed(), 1)
            # Unread sectors of the movie only, so the service can tell them from unread extras.
            self.map.meta["relevant_pending_sectors"] = self._pending_relevant()
            self.map.save(self.map_path)
        self._last_save = now

    def _extra_elapsed(self) -> float:
        running = 0.0 if self.extra_started is None else max(0.0, self.clock() - self.extra_started)
        return self._extra_used_before + running

    def _budget_exhausted(self) -> bool:
        return self.extra_started is not None and self._extra_elapsed() >= self.options.extra_seconds

    def _pending_relevant(self) -> int:
        assert self.map is not None
        if self.priority:
            return self.map.count_within(PENDING, self.priority)
        return self.map.count(PENDING)

    def stats(self) -> dict[str, Any]:
        assert self.map is not None
        total = self.map.total_sectors
        rescued = self.map.count(FINISHED)
        bad = self.map.count(BAD)
        pending = self.map.count(PENDING)
        movie_pending = self.map.count_within(PENDING, self.priority) if self.priority else pending
        movie_bad = self.map.count_within({BAD}, self.priority) if self.priority else bad
        now = self.clock()
        rate = 0
        if len(self._rate_window) >= 2:
            (first_time, first_bytes), (last_time, last_bytes) = self._rate_window[0], self._rate_window[-1]
            if last_time > first_time:
                rate = int((last_bytes - first_bytes) / (last_time - first_time))
        return {
            "phase": self.phase,
            "total_bytes": total * SECTOR_SIZE,
            "rescued_bytes": rescued * SECTOR_SIZE,
            "unreadable_bytes": bad * SECTOR_SIZE,
            "pending_bytes": pending * SECTOR_SIZE,
            "movie_unreadable_bytes": movie_bad * SECTOR_SIZE,
            "movie_pending_bytes": movie_pending * SECTOR_SIZE,
            "deferred_bytes": (
                self.map.count_within(
                    PENDING, _intersect([(0, self.cursor)], self.priority) if self.priority else [(0, self.cursor)]
                )
                * SECTOR_SIZE
                if self.phase == "sweep"
                else 0
            ),
            "scope": "movie" if self.priority else "disc",
            # Extras and menus a movie-first rescue leaves unread on purpose.
            "not_needed_bytes": (
                (self.map.count({NON_TRIED}) - self.map.count_within({NON_TRIED}, self.priority)) * SECTOR_SIZE
                if self.priority
                else 0
            ),
            "position_bytes": self.cursor * SECTOR_SIZE,
            "read_errors": self.read_errors,
            "drive_faults": self.drive_faults,
            "damaged_areas": sum(1 for _ in self.map.ranges({BAD})),
            "in_damaged_zone": self.in_zone,
            "retry_round": self.retry_round,
            "stop_reason": self.stop_reason,
            "rate_bytes_per_second": rate,
            "elapsed_seconds": round(now - self.started, 1),
            "extra_elapsed_seconds": round(self._extra_elapsed(), 1),
            "extra_budget_seconds": self.options.extra_seconds,
            "percent": round(self._percent(), 2),
            "method": getattr(self.device, "method", "unknown"),
        }

    def _percent(self) -> float:
        assert self.map is not None
        if self.phase in {"starting", "sweep"}:
            if self.sweep_ranges:
                size = sum(end - start for start, end in self.sweep_ranges)
                done = sum(max(0, min(end, self.cursor) - start) for start, end in self.sweep_ranges)
                return min(90.0, 90.0 * done / max(1, size))
            return min(90.0, 90.0 * self.cursor / self.map.total_sectors)
        if self.phase == "done":
            return 100.0
        pending_fraction = 1.0
        if self._pending_at_extra_start:
            pending_fraction = self._pending_relevant() / self._pending_at_extra_start
        budget_fraction = (
            min(1.0, self._extra_elapsed() / self.options.extra_seconds) if self.options.extra_seconds > 0 else 1.0
        )
        return min(99.9, 90.0 + 10.0 * max(1.0 - pending_fraction, budget_fraction))

    def _report(self, *, force: bool = False) -> None:
        now = self.clock()
        if not force and now - self._last_report < self.options.report_interval_seconds:
            return
        self._last_report = now
        self.emit({"type": "progress", **self.stats()})

    def _check_control(self) -> None:
        if self.finish_requested:
            raise RescueFinishRequested()
        if not self.control_path:
            return
        now = self.clock()
        if self._last_control_check and now - self._last_control_check < 1.0:
            return
        self._last_control_check = now
        try:
            command = self.control_path.read_text(encoding="utf-8").strip().casefold()
        except OSError:
            return
        if command == "finish":
            self.finish_requested = True
            raise RescueFinishRequested()

    def _checkpoint(self) -> None:
        self._check_control()
        self._report()
        self._save()

    # --- device access -------------------------------------------------------------------------

    def _read(self, lba: int, count: int) -> bytes:
        attempts = 0
        while True:
            started = self.clock()
            try:
                data = self.device.read(lba, count)
            except UnreadableSectorError:
                self.read_errors += 1
                if self.clock() - started >= self.options.fast_failure_seconds:
                    self._fast_failures.clear()
                    raise
                self._fast_failures.append((lba, lba + count))
                if len(self._fast_failures) < self.options.fast_failure_limit:
                    raise
                self._undo_fast_failures()
                attempts += 1
                if attempts > self.options.fault_retries or not self._recover_drive():
                    raise DriveStoppedResponding(
                        "The drive keeps rejecting reads instantly, which damage on a disc does not cause"
                    ) from None
                continue
            except DriveFaultError as error:
                attempts += 1
                if attempts > self.options.fault_retries or not self._recover_drive():
                    raise DriveStoppedResponding(str(error)) from error
                continue
            except MediaUnavailableError:
                attempts += 1
                if attempts > 2 or not self._wait_for_device():
                    raise
                continue
            except CopyProtectionError:
                raise
            except OpticalError as error:
                attempts += 1
                if attempts > 3:
                    self.read_errors += 1
                    raise UnreadableSectorError(str(error)) from error
                self.sleep(2)
                try:
                    self.device.reopen()
                except OSError:
                    pass
                continue
            self._fast_failures.clear()
            self._bytes_read += len(data)
            self._rate_window.append((self.clock(), self._bytes_read))
            return data

    def _undo_fast_failures(self) -> None:
        """Forget damage recorded from instant failures; those blocks were never really tested."""
        assert self.map is not None
        for start, end in self._fast_failures:
            if self.map.status_at(start) in {BAD, NON_TRIMMED, NON_SCRAPED}:
                self.map.set(start, end, NON_TRIED)
        self.read_errors = max(0, self.read_errors - len(self._fast_failures))
        self._fast_failures.clear()

    def _known_good_cluster(self) -> tuple[int, int] | None:
        assert self.map is not None
        for start, end, _ in self.map.ranges({FINISHED}):
            first = self._align_up(max(start, 16))
            if first + self.cluster <= end:
                return first, self.cluster
        return None

    def _recover_drive(self) -> bool:
        """Pause, reconnect, and prove the drive works again on a sector it already read."""
        self.drive_faults += 1
        self._fast_failures.clear()
        self._save(force=True)
        self.emit({"type": "waiting", "message": "The drive reported a fault; pausing to let it recover"})
        probe = self._known_good_cluster()
        for pause in self.options.fault_pauses:
            self.sleep(pause)
            try:
                self.device.reopen()
                if probe is not None:
                    self.device.read(*probe)
                return True
            except OSError:
                continue
        return False

    def _wait_for_device(self) -> bool:
        deadline = self.clock() + self.options.device_wait_seconds
        self.emit({"type": "waiting", "message": "Waiting for the optical drive to respond again"})
        while self.clock() < deadline:
            self._save(force=True)
            self.sleep(2)
            try:
                self.device.reopen()
                self.device.read(16, 1)
                return True
            except (UnreadableSectorError, CopyProtectionError):
                return True
            except OSError:
                continue
        return False

    def _write(self, lba: int, data: bytes) -> None:
        assert self._image is not None and self.map is not None
        self._image.seek(lba * SECTOR_SIZE)
        self._image.write(data)
        self.map.set(lba, lba + len(data) // SECTOR_SIZE, FINISHED)

    def _chunk_limit(self) -> int:
        maximum = max(self.cluster, int(self.device.max_transfer_sectors))
        return max(self.cluster, maximum - maximum % self.cluster)

    def _forward_count(self, start: int, end: int, limit: int) -> int:
        remainder = start % self.cluster
        if remainder:
            limit = self.cluster - remainder
        return max(1, min(end - start, limit))

    def _align_up(self, sector: int) -> int:
        return -(-sector // self.cluster) * self.cluster

    # --- playback units ------------------------------------------------------------------------

    def _unit_bounds(self, sector: int) -> tuple[int, int]:
        """The playback unit containing ``sector`` (a VOBU, or a fixed-size step)."""
        assert self.map is not None
        total = self.map.total_sectors
        if self.units and self.units[0] <= sector < self.units[-1] + 4 * self.skip:
            index = bisect_right(self.units, sector) - 1
            end = self.units[index + 1] if index + 1 < len(self.units) else self.units[index] + self.skip
            return self.units[index], min(total, end)
        low = sector - sector % self.skip
        return low, min(total, low + self.skip)

    def _next_unit_after(self, sector: int) -> int:
        return self._unit_bounds(sector)[1]

    # --- passes --------------------------------------------------------------------------------

    def _swept_ranges(self) -> list[tuple[int, int]]:
        assert self.map is not None
        swept = self.map.meta.get("swept_ranges")
        if swept is None:
            # Maps from before movie-first reading swept the whole disc.
            return [(0, self.map.total_sectors)] if self.map.meta.get("sweep_done") else []
        return [(int(start), int(end)) for start, end in swept]

    def _sweep_targets(self) -> list[tuple[int, int]]:
        """The movie (or the whole disc without a layout), minus what earlier runs already swept."""
        assert self.map is not None
        return _subtract(self.priority or [(0, self.map.total_sectors)], self._swept_ranges())

    def _next_sweep_range(self, cursor: int) -> tuple[int, int] | None:
        """The next never-read range at or after ``cursor`` inside this run's sweep ranges."""
        assert self.map is not None
        starts = [start for start, _ in self.sweep_ranges]
        while True:
            found = self.map.next_range({NON_TRIED}, cursor)
            if not found:
                return None
            start, end, _ = found
            index = bisect_right(starts, start) - 1
            if index >= 0 and start < self.sweep_ranges[index][1]:
                return start, min(end, self.sweep_ranges[index][1])
            if index + 1 >= len(self.sweep_ranges):
                return None
            low, high = self.sweep_ranges[index + 1]
            if low < end:
                return low, min(end, high)
            cursor = end

    def _sweep(self) -> None:
        assert self.map is not None
        self.phase = "sweep"
        self.emit({"type": "phase", "phase": self.phase, "scope": "movie" if self.priority else "disc"})
        options = self.options
        failed_units = 0
        jump = 0
        clean_run = 0
        read_since_skip = True
        self.cursor = self.sweep_ranges[0][0] if self.sweep_ranges else 0
        while True:
            self._checkpoint()
            found = self._next_sweep_range(self.cursor)
            if not found:
                break
            start, end = found
            limit = self.cluster if self.in_zone else self._chunk_limit()
            count = self._forward_count(start, end, limit)
            self.cursor = start
            try:
                data = self._read(start, count)
            except UnreadableSectorError:
                if count > self.cluster:
                    # Find the unreadable block itself before skipping anything.
                    if not self.in_zone:
                        self.emit({"type": "zone", "start_bytes": start * SECTOR_SIZE})
                    self.in_zone = True
                    clean_run = 0
                    continue
                self.map.set(start, start + count, BAD)
                if not self.in_zone:
                    self.emit({"type": "zone", "start_bytes": start * SECTOR_SIZE})
                self.in_zone = True
                clean_run = 0
                failed_units = failed_units + 1 if not read_since_skip else 1
                target = max(start + count, self._next_unit_after(start))
                if failed_units >= options.solid_damage_units:
                    # Unit after unit is unreadable: a solid damaged stretch.
                    jump = min(options.max_jump_sectors, max(jump * 2, self.skip * 4))
                    target = max(target, self._align_up(start + count + jump))
                self.cursor = min(self.map.total_sectors, target)
                read_since_skip = False
                continue
            self._write(start, data)
            self.cursor = start + count
            read_since_skip = True
            if self.in_zone:
                failed_units = 0
                jump = 0
                clean_run += count
                if clean_run >= options.clean_exit_sectors:
                    self.in_zone = False
        self.cursor = self.map.total_sectors
        self.in_zone = False

    def _relevant_areas(self, statuses: frozenset[str] | set[str]) -> list[tuple[int, int]]:
        assert self.map is not None
        areas = self.map.areas(statuses)
        if not self.priority:
            return areas
        clipped: list[tuple[int, int]] = []
        for start, end in areas:
            for low, high in self.priority:
                if min(end, high) > max(start, low):
                    clipped.append((max(start, low), min(end, high)))
        return clipped

    def _pending_units(self) -> list[tuple[int, int]]:
        """Playback units that still contain unread sectors, in disc order."""
        units: dict[int, tuple[int, int]] = {}
        for start, end in self._relevant_areas(PENDING):
            position = start
            while position < end:
                low, high = self._unit_bounds(position)
                high = max(high, position + 1)
                known = units.get(low)
                clipped = (max(low, start), min(high, end))
                units[low] = (min(known[0], clipped[0]), max(known[1], clipped[1])) if known else clipped
                position = high
        return [units[key] for key in sorted(units)]

    def _stall_detected(self) -> bool:
        assert self.map is not None and self.extra_started is not None
        now = self.clock()
        if not self._stall_samples or now - self._stall_samples[-1][0] >= 10:
            self._stall_samples.append((now, self.map.count(FINISHED)))
            window = self.options.stall_window_seconds
            while len(self._stall_samples) >= 2 and now - self._stall_samples[1][0] >= window:
                self._stall_samples.popleft()
        oldest_time, oldest_count = self._stall_samples[0]
        if now - oldest_time < self.options.stall_window_seconds:
            return False
        gained = (self._stall_samples[-1][1] - oldest_count) * SECTOR_SIZE
        return gained < self.options.stall_min_bytes

    def _should_stop_retrying(self) -> bool:
        if self._budget_exhausted():
            self.stop_reason = "budget"
            return True
        if self._stall_detected():
            self.stop_reason = "little_left_to_gain"
            return True
        return False

    def _read_unit_forward(self, low: int, high: int) -> bool | None:
        """Read the unread sectors of one unit up to its next unreadable block.

        Returns True when the unit is done, False after marking an unreadable
        block, and None when retrying has to stop.
        """
        assert self.map is not None
        position = self._first_status(PENDING, low, high)
        # Start with one block next to the damage and read more at a time as
        # the unit proves readable.
        size = self.cluster
        while position is not None:
            self._checkpoint()
            if self._should_stop_retrying():
                return None
            found = self.map.next_range(PENDING, position)
            pending_end = min(found[1], high) if found else high
            count = self._forward_count(position, pending_end, size)
            self.cursor = position
            try:
                data = self._read(position, count)
            except UnreadableSectorError:
                if count > self.cluster:
                    size = self.cluster
                    continue
                self.map.set(position, position + count, BAD)
                return False
            self._write(position, data)
            size = min(self._chunk_limit(), size * 2)
            position = self._first_status(PENDING, position + count, high)
        return True

    def _read_back_from_far_edges(self) -> bool:
        """Read each stretch the sweep jumped over back from its far end to the damage.

        Readable video just before where a jump landed would otherwise wait
        behind every damaged unit and could be lost when retrying stops early.
        """
        assert self.map is not None
        for low, high in self._relevant_areas({NON_TRIED}):
            if high - low <= 2 * self.skip:
                continue
            end = high
            while end > low:
                unit_low = max(low, self._unit_bounds(end - 1)[0])
                done = self._read_unit_forward(unit_low, end)
                if done is None:
                    return False
                if not done:
                    break
                end = unit_low
        return True

    def _retry(self) -> None:
        assert self.map is not None
        self.phase = "retry"
        self.emit({"type": "phase", "phase": self.phase})
        if not self._read_back_from_far_edges():
            return
        while True:
            units = self._pending_units()
            if not units:
                break
            self.retry_round += 1
            self.emit({"type": "round", "round": self.retry_round, "units": len(units)})
            # One attempt at each damaged moment per round, so each gets a turn
            # before any gets a second. Every attempt reads or marks a block.
            for low, high in units:
                if self._read_unit_forward(low, high) is None:
                    return
        # A marginal block often reads on a later attempt: try each failed block once more.
        for low, high in self._relevant_areas({BAD}):
            position = low
            while position < high:
                self._checkpoint()
                if self._should_stop_retrying():
                    return
                count = self._forward_count(position, high, self.cluster)
                if self.map.status_at(position) == BAD:
                    self.cursor = position
                    try:
                        self._write(position, self._read(position, count))
                    except UnreadableSectorError:
                        pass
                position += count
        self.stop_reason = self.stop_reason or "finished"

    def _retry_critical(self) -> None:
        """Read navigation and filesystem sectors first; a few failed blocks there make the image unusable."""
        assert self.map is not None
        for _attempt in range(3):
            areas = _intersect(self.map.areas(PENDING | {BAD}), self.critical)
            if not areas:
                return
            for low, high in areas:
                position = low
                while position < high:
                    self._checkpoint()
                    count = self._forward_count(position, high, self.cluster)
                    self.cursor = position
                    try:
                        self._write(position, self._read(position, count))
                    except UnreadableSectorError:
                        self.map.set(position, position + count, BAD)
                    position += count

    def _first_status(self, statuses: frozenset[str] | set[str], low: int, high: int) -> int | None:
        assert self.map is not None
        found = self.map.next_range(statuses, low)
        return found[0] if found and found[0] < high else None

    # --- entry point ---------------------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        self.map, origin = self._load_map()
        self._extra_used_before = float(self.map.meta.get("extra_used_seconds") or 0)
        self._open_image()
        if origin != "new":
            try:
                self._verify_same_disc()
            except WrongDiscError:
                assert self._image is not None
                self._image.close()
                self._image = None
                raise
        self.emit(
            {
                "type": "start",
                "origin": origin,
                "total_bytes": self.map.total_sectors * SECTOR_SIZE,
                "max_transfer_sectors": self.device.max_transfer_sectors,
                "cluster_sectors": self.cluster,
                "sweep_done": bool(self.map.meta.get("sweep_done")),
                "priority_bytes": (
                    sum(end - start for start, end in self.priority) * SECTOR_SIZE if self.priority else None
                ),
                "units": len(self.units),
                "method": getattr(self.device, "method", "unknown"),
            }
        )
        finished_early = False
        try:
            try:
                if self.options.structures_only:
                    self.phase = "structures"
                    self._retry_critical()
                    self.stop_reason = "structures"
                    raise _StructuresRead()
                self.sweep_ranges = self._sweep_targets()
                if self.sweep_ranges:
                    self._sweep()
                    swept = [*self._swept_ranges(), *(self.priority or [(0, self.map.total_sectors)])]
                    self.map.meta["swept_ranges"] = [list(span) for span in merge_ranges(swept)]
                    self.map.meta["sweep_done"] = True
                    self._save(force=True)
                if self.critical:
                    self.phase = "structures"
                    self._retry_critical()
                    self._save(force=True)
                if not self._pending_relevant():
                    self.stop_reason = "finished"
                elif self.options.extra_seconds <= self._extra_used_before:
                    self.stop_reason = "budget"
                else:
                    self.extra_started = self.clock()
                    self._pending_at_extra_start = self._pending_relevant()
                    self._retry()
            except RescueFinishRequested:
                finished_early = True
                self.stop_reason = "skipped"
            except _StructuresRead:
                pass
            self.phase = "done"
        finally:
            self._save(force=True)
            if self._image is not None:
                self._image.close()
                self._image = None
        summary = {"type": "done", "finished_early": finished_early, **self.stats()}
        summary["budget_exhausted"] = self.stop_reason == "budget"
        self.emit(summary)
        return summary


def _stdout_emitter() -> Emit:
    def emit(event: dict[str, Any]) -> None:
        stream = sys.stdout
        if stream is None:
            return
        stream.write(EVENT_PREFIX + json.dumps(event, separators=(",", ":"), default=str) + "\n")
        stream.flush()

    return emit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="discdock-disc-rescue", description="Rescue a damaged optical disc")
    parser.add_argument("--drive", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--map", required=True)
    parser.add_argument("--control", default="")
    parser.add_argument("--cluster", type=int, default=DVD_ECC_BLOCK_SECTORS)
    parser.add_argument("--skip-sectors", type=int, default=256)
    parser.add_argument("--extra-seconds", type=float, default=900.0)
    parser.add_argument("--dvd-title", type=int, default=0)
    parser.add_argument("--segment-map", default="")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--disc", choices=("dvd", "bluray"), default="dvd")
    parser.add_argument("--playlist", default="")
    parser.add_argument("--auto-title", action="store_true")
    parser.add_argument("--whole-disc", action="store_true")
    parser.add_argument("--structures-only", action="store_true")
    try:
        arguments = parser.parse_args(argv)
    except SystemExit:
        return EXIT_USAGE
    emit = _stdout_emitter()

    def interrupt(_signum, _frame) -> None:
        raise KeyboardInterrupt()

    for name in ("SIGBREAK", "SIGTERM", "SIGINT"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), interrupt)
            except (OSError, ValueError):
                pass

    try:
        device = open_optical_device(arguments.drive, timeout_seconds=arguments.timeout)
    except MediaUnavailableError as error:
        emit({"type": "error", "code": "media_unavailable", "message": str(error)})
        return EXIT_MEDIA_UNAVAILABLE
    except DriveFaultError as error:
        emit({"type": "error", "code": "drive_not_responding", "message": f"{error}. {DRIVE_RESET_ADVICE}"})
        return EXIT_DRIVE_FAULT
    except (OSError, ValueError) as error:
        emit({"type": "error", "code": "device_unavailable", "message": str(error)})
        return EXIT_FAILED
    try:
        priority: list[tuple[int, int]] | None = None
        critical: list[tuple[int, int]] = []
        units: list[int] = []

        def read_failed(lba: int, attempt: int) -> None:
            emit(
                {
                    "type": "waiting",
                    "message": f"The drive could not read sector {lba} while finding the movie (attempt {attempt} of 3)",
                }
            )

        saved, saved_image = _saved_image_reader(Path(arguments.image), Path(arguments.map), device.total_sectors)
        try:
            found = None
            if saved is not None:
                try:
                    found = _find_layout(arguments, saved, device.total_sectors, check_windows=False)
                except (OSError, ValueError):
                    found = None
                if found is not None and found[1] is None:
                    found = None
            if found is None:
                if arguments.disc == "bluray" or arguments.dvd_title > 0 or arguments.auto_title:
                    emit({"type": "waiting", "message": "Finding the movie on the disc"})
                found = _find_layout(
                    arguments,
                    _reliable_reader(device.read, on_failure=read_failed),
                    device.total_sectors,
                    check_windows=True,
                )
            event, priority, critical, units = found
            if event:
                emit(event)
        except (CopyProtectionError, DriveFaultError, MediaUnavailableError):
            raise
        except (OSError, ValueError) as error:
            emit({"type": "layout", "verified": False, "note": str(error)})
        finally:
            if saved_image is not None:
                saved_image.close()
        if arguments.whole_disc:
            # MakeMKV needed more of the disc than the movie and its navigation.
            priority = None
        engine = RescueEngine(
            device,
            Path(arguments.image),
            Path(arguments.map),
            RescueOptions(
                cluster_sectors=arguments.cluster,
                extra_seconds=max(0.0, arguments.extra_seconds),
                priority_ranges=priority,
                critical_ranges=critical,
                unit_starts=units,
                skip_sectors=max(arguments.cluster, arguments.skip_sectors),
                structures_only=arguments.structures_only,
            ),
            emit=emit,
            control_path=Path(arguments.control) if arguments.control else None,
        )
        engine.run()
        return EXIT_OK
    except CopyProtectionError as error:
        emit({"type": "error", "code": "copy_protection", "message": str(error)})
        return EXIT_COPY_PROTECTION
    except (DriveStoppedResponding, DriveFaultError) as error:
        emit({"type": "error", "code": "drive_not_responding", "message": f"{error}. {DRIVE_RESET_ADVICE}"})
        return EXIT_DRIVE_FAULT
    except WrongDiscError as error:
        emit({"type": "error", "code": "wrong_disc", "message": str(error)})
        return EXIT_WRONG_DISC
    except MediaUnavailableError as error:
        emit({"type": "error", "code": "media_unavailable", "message": str(error)})
        return EXIT_MEDIA_UNAVAILABLE
    except KeyboardInterrupt:
        emit({"type": "error", "code": "interrupted", "message": "Rescue stopped"})
        return EXIT_INTERRUPTED
    except Exception as error:
        emit({"type": "error", "code": "unexpected", "message": f"{type(error).__name__}: {error}"})
        return EXIT_FAILED
    finally:
        device.close()


if __name__ == "__main__":
    raise SystemExit(main())
