"""Mend a rescued DVD image so MakeMKV can copy the movie past its damage.

A DVD's video comes in units of about half a second (VOBUs). Each opens with a
navigation pack that says where the unit ends, which cell it belongs to and what
stretch of time it covers, and MakeMKV follows those packs from one unit to the
next. A rescued image holds zeros wherever the disc could not be read. A zeroed
navigation pack breaks the chain, and MakeMKV then skips the whole movie "due to
navigation error". Zeroed sectors inside a unit cut pictures off half way, and in
a stretch of dense damage MakeMKV loses track of picture and sound and gives up
on the title.

Mending a copy of the image deals with both, without decrypting anything:

- every unread sector of the movie becomes an MPEG padding pack, which players
  and MakeMKV pass over by design;
- each lost navigation pack is rebuilt from the nearest one that survived, with
  its own address, length, cell and time;
- the video after a hole inside a unit becomes padding as well. Those pictures
  are predicted from the ones the hole took, so they could only show garbage.
  Without them the picture has a short gap, the sound carries on, and MakeMKV
  keeps going.

An IFO the disc would not give whole is put back together from its backup copy
(the .BUP), which holds the same bytes.

Scrambled video stays scrambled; MakeMKV decrypts it as usual. Mending only
replaces whole packs and rewrites navigation packs, which are never scrambled.
"""

from __future__ import annotations

import bisect
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .optical import (
    SECTOR_SIZE,
    DiscFile,
    OpticalError,
    ReadSectors,
    dvd_title_entry,
    dvd_vobu_starts,
    merge_ranges,
    read_video_ts_files,
)

# An MPEG-2 pack header (clock zero, the DVD mux rate, no stuffing) and one padding packet filling the sector.
_PACK_HEADER = bytes.fromhex("000001ba" "440004000401" "0189c3" "f8")
_PADDING_LENGTH = SECTOR_SIZE - len(_PACK_HEADER) - 6
_PADDING_HEADER = _PACK_HEADER + b"\x00\x00\x01\xbe" + _PADDING_LENGTH.to_bytes(2, "big")
PADDING_PACK = _PADDING_HEADER + b"\xff" * (SECTOR_SIZE - len(_PADDING_HEADER))

# Navigation pack fields, as byte offsets into the sector (DVD-Video PCI and DSI packets).
PCI_LBN = 0x2D
PCI_START_PTM = 0x39
PCI_END_PTM = 0x3D
PCI_SEQUENCE_END_PTM = 0x41
DSI_LBN = 0x40B
DSI_END_ADDRESS = 0x40F
DSI_REFERENCES = (0x413, 0x417, 0x41B)
DSI_VOB_ID = 0x41F
DSI_CELL_ID = 0x422
# Search information: next unit with video, 19 jumps forward, next unit, previous
# unit, 19 jumps back, previous unit with video. Offsets count sectors from this pack.
SRI = 0x4F1
SRI_NEXT_VIDEO = SRI
SRI_FORWARD = SRI + 4
SRI_NEXT = SRI + 80
SRI_PREVIOUS = SRI + 84
SRI_BACKWARD = SRI + 88
SRI_PREVIOUS_VIDEO = SRI + 164
SRI_JUMPS = 19
SRI_END_OF_CELL = 0x3FFFFFFF
SRI_HAS_VIDEO = 0x80000000

# 90 kHz clock ticks a unit lasts when nothing better is known: half a second.
DEFAULT_UNIT_TICKS = 45_000
COPY_CHUNK_SECTORS = 4096


class MendStopped(Exception):
    """The job stopped while the mended copy was being written."""


@dataclass
class DvdMend:
    """What mending a rescued DVD image changes, worked out without touching it."""

    title_set: int = 0
    # Sector ranges that become padding packs, then single sectors with new contents on top.
    padding: list[tuple[int, int]] = field(default_factory=list)
    sectors: dict[int, bytes] = field(default_factory=dict)
    unread_sectors: int = 0
    rebuilt_navigation: int = 0
    cleared_video_sectors: int = 0
    restored_sectors: int = 0

    @property
    def changes(self) -> bool:
        return bool(self.padding or self.sectors)


def _be16(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 2], "big")


def _be32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "big")


def _put32(pack: bytearray, offset: int, value: int) -> None:
    pack[offset : offset + 4] = (value & 0xFFFFFFFF).to_bytes(4, "big")


def is_navigation_pack(pack: bytes) -> bool:
    """Whether a sector is a DVD navigation pack: a PCI and a DSI packet behind a system header."""
    return (
        len(pack) >= SECTOR_SIZE
        and pack[:4] == b"\x00\x00\x01\xba"
        and pack[0x26:0x2A] == b"\x00\x00\x01\xbf"
        and pack[0x2C] == 0x00
        and pack[0x400:0x404] == b"\x00\x00\x01\xbf"
        and pack[0x406] == 0x01
    )


def _is_video_pack(pack: bytes) -> bool:
    if len(pack) < 18 or pack[:4] != b"\x00\x00\x01\xba":
        return False
    offset = 14 + (pack[13] & 0x07)
    return pack[offset : offset + 4] == b"\x00\x00\x01\xe0"


class _Holes:
    """Sorted, merged unread ranges with quick lookups."""

    def __init__(self, ranges: list[tuple[int, int]]):
        self.ranges = merge_ranges(ranges)
        self.lows = [low for low, _ in self.ranges]

    def contains(self, sector: int) -> bool:
        index = bisect.bisect_right(self.lows, sector) - 1
        return index >= 0 and sector < self.ranges[index][1]

    def first_in(self, low: int, high: int) -> int | None:
        """The first unread sector in ``[low, high)``."""
        index = bisect.bisect_right(self.lows, low) - 1
        if index >= 0 and low < self.ranges[index][1]:
            return low
        index += 1
        if index < len(self.ranges) and self.ranges[index][0] < high:
            return self.ranges[index][0]
        return None

    def within(self, spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
        found: list[tuple[int, int]] = []
        for low, high in spans:
            index = max(0, bisect.bisect_right(self.lows, low) - 1)
            while index < len(self.ranges) and self.ranges[index][0] < high:
                start, end = max(low, self.ranges[index][0]), min(high, self.ranges[index][1])
                if start < end:
                    found.append((start, end))
                index += 1
        return merge_ranges(found)


def _single_extent(entry: DiscFile) -> tuple[int, int] | None:
    spans = entry.sector_ranges()
    return spans[0] if len(spans) == 1 else None


def _restore_from_backups(read: ReadSectors, files: dict[str, DiscFile], holes: _Holes) -> dict[int, bytes]:
    """Sectors of each IFO the disc did not give, taken from its .BUP, and the other way round."""
    restored: dict[int, bytes] = {}
    for name, entry in files.items():
        if not name.endswith(".IFO"):
            continue
        backup = files.get(f"{name[:-4]}.BUP")
        if backup is None or backup.size != entry.size:
            continue
        first, second = _single_extent(entry), _single_extent(backup)
        if first is None or second is None or first[1] - first[0] != second[1] - second[0]:
            continue
        for offset in range(first[1] - first[0]):
            original, copy = first[0] + offset, second[0] + offset
            if holes.contains(original) and not holes.contains(copy):
                restored[original] = read(copy, 1)
            elif holes.contains(copy) and not holes.contains(original):
                restored[copy] = read(original, 1)
    return restored


def _cell_addresses(ifo: bytes) -> list[tuple[int, int, int, int]]:
    """(first sector, last sector, VOB id, cell id) of every cell, counted from the title set's video."""
    table = _be32(ifo, 0xE0) * SECTOR_SIZE
    if not table or table + 8 > len(ifo):
        return []
    end = min(len(ifo), table + _be32(ifo, table + 4) + 1)
    return sorted(
        (_be32(ifo, offset + 4), _be32(ifo, offset + 8), _be16(ifo, offset), ifo[offset + 2])
        for offset in range(table + 8, end - 11, 12)
    )


class _Cells:
    def __init__(self, cells: list[tuple[int, int, int, int]]):
        self.cells = cells
        self.firsts = [first for first, *_ in cells]

    def at(self, relative: int) -> tuple[int, int] | None:
        """(VOB id, cell id) of the cell holding a sector, counted from the title set's video."""
        index = bisect.bisect_right(self.firsts, relative) - 1
        if index >= 0 and relative <= self.cells[index][1]:
            return self.cells[index][2], self.cells[index][3]
        return None


def _read_ifo(read: ReadSectors, entry: DiscFile) -> bytes:
    start, length = entry.extents[0]
    sectors = (length + SECTOR_SIZE - 1) // SECTOR_SIZE
    if not 0 < sectors <= 4096:
        raise OpticalError(f"{entry.name} is implausibly large")
    return read(start, sectors)[:length]


def plan_dvd_mend(
    read: ReadSectors, total_sectors: int, unread: list[tuple[int, int]], title_number: int
) -> DvdMend:
    """Work out how to mend a rescued DVD image around the sectors in ``unread``.

    Only the title set that holds ``title_number`` is mended; that is what MakeMKV
    copies the movie from. Raises ``OpticalError`` when the image's filesystem or
    the title set's map of video units cannot be read, since then there is nothing
    to mend against.
    """
    holes = _Holes(unread)
    files = read_video_ts_files(read, total_sectors)
    mend = DvdMend()
    mend.sectors.update(_restore_from_backups(read, files, holes))
    mend.restored_sectors = len(mend.sectors)

    def mended_read(lba: int, count: int) -> bytes:
        data = bytearray(read(lba, count).ljust(count * SECTOR_SIZE, b"\0"))
        for offset in range(count):
            replacement = mend.sectors.get(lba + offset)
            if replacement is not None:
                data[offset * SECTOR_SIZE : (offset + 1) * SECTOR_SIZE] = replacement
        return bytes(data)

    entry = dvd_title_entry(mended_read, files, title_number)
    if not entry:
        raise OpticalError(f"DVD title {title_number} is not in the disc's title table")
    title_set = mend.title_set = entry[0]
    prefix = f"VTS_{title_set:02d}_"
    first_video, ifo_entry = files.get(f"{prefix}1.VOB"), files.get(f"{prefix}0.IFO")
    video = merge_ranges(
        [
            span
            for name, item in files.items()
            if name.startswith(prefix) and name.endswith(".VOB") and name != f"{prefix}0.VOB"
            for span in item.sector_ranges()
        ]
    )
    if not video or first_video is None or ifo_entry is None:
        raise OpticalError(f"Title set {title_set} has no video files")
    damaged = holes.within(video)
    mend.unread_sectors = sum(high - low for low, high in damaged)
    if not damaged:
        return mend
    base, video_end = first_video.extents[0][0], video[-1][1]
    starts = [
        start for start in dvd_vobu_starts(mended_read, files, title_set) if video[0][0] <= start < video_end
    ]
    if not starts:
        raise OpticalError(f"The map of title set {title_set}'s video units could not be read")
    cells = _Cells(_cell_addresses(_read_ifo(mended_read, ifo_entry)))

    navigation: dict[int, bytes] = {}
    lost: list[int] = []
    for index, start in enumerate(starts):
        pack = b"" if holes.contains(start) else mended_read(start, 1)
        if is_navigation_pack(pack):
            navigation[index] = pack
        else:
            lost.append(index)
    read_starts = sum(1 for start in starts if not holes.contains(start))
    if not navigation or read_starts - len(navigation) > read_starts // 2:
        # Most units the disc gave have no navigation pack where the map puts one:
        # the map does not describe this image, and rebuilding from it would do harm.
        raise OpticalError(f"The map of title set {title_set}'s video units does not match the image")
    survivors = sorted(navigation)

    def end_of(index: int) -> int:
        return starts[index + 1] if index + 1 < len(starts) else video_end

    def cell_of(index: int) -> tuple[int, int] | None:
        return cells.at(starts[index] - base) if 0 <= index < len(starts) else None

    for index in lost:
        position = bisect.bisect_left(survivors, index)
        before = survivors[position - 1] if position > 0 else None
        after = survivors[position] if position < len(survivors) else None
        template = navigation[before if before is not None else survivors[position]]
        start, end = starts[index], end_of(index)
        cell = cell_of(index) or (_be16(template, DSI_VOB_ID), template[DSI_CELL_ID])
        start_ptm, end_ptm = _unit_times(index, cell[0], before, after, starts, end_of, navigation)
        pack = bytearray(template)
        _put32(pack, PCI_LBN, start - base)
        _put32(pack, DSI_LBN, start - base)
        _put32(pack, DSI_END_ADDRESS, end - start - 1)
        _put32(pack, PCI_START_PTM, start_ptm)
        _put32(pack, PCI_END_PTM, end_ptm)
        _put32(pack, PCI_SEQUENCE_END_PTM, 0)
        # The unit's pictures are cleared below, so it has no reference pictures to point at.
        for offset in DSI_REFERENCES:
            _put32(pack, offset, 0)
        pack[DSI_VOB_ID : DSI_VOB_ID + 2] = cell[0].to_bytes(2, "big")
        pack[DSI_CELL_ID] = cell[1]
        same_cell_next = index + 1 < len(starts) and (cell_of(index + 1) or cell) == cell
        same_cell_previous = index > 0 and (cell_of(index - 1) or cell) == cell
        following = SRI_HAS_VIDEO | (end - start) if same_cell_next else SRI_END_OF_CELL
        preceding = SRI_HAS_VIDEO | (start - starts[index - 1]) if same_cell_previous else SRI_END_OF_CELL
        _put32(pack, SRI_NEXT_VIDEO, following)
        _put32(pack, SRI_NEXT, following)
        _put32(pack, SRI_PREVIOUS, preceding)
        _put32(pack, SRI_PREVIOUS_VIDEO, preceding)
        # Longer jumps would need times this unit's neighbours lost too; "none" is honest.
        for jump in range(SRI_JUMPS):
            _put32(pack, SRI_FORWARD + 4 * jump, SRI_END_OF_CELL)
            _put32(pack, SRI_BACKWARD + 4 * jump, SRI_END_OF_CELL)
        mend.sectors[start] = bytes(pack)
    mend.rebuilt_navigation = len(lost)

    cleared: list[tuple[int, int]] = []
    for index, start in enumerate(starts):
        end = end_of(index)
        first = holes.first_in(start, end)
        if first is None:
            continue
        data = mended_read(first, end - first)
        for offset in range(end - first):
            sector = first + offset
            pack = data[offset * SECTOR_SIZE : (offset + 1) * SECTOR_SIZE]
            if not holes.contains(sector) and _is_video_pack(pack):
                cleared.append((sector, sector + 1))
                mend.cleared_video_sectors += 1
    mend.padding = merge_ranges([*damaged, *cleared])
    return mend


def _unit_times(
    index: int,
    vob_id: int,
    before: int | None,
    after: int | None,
    starts: list[int],
    end_of: Callable[[int], int],
    navigation: dict[int, bytes],
) -> tuple[int, int]:
    """Presentation times for a unit that lost its navigation pack.

    Time runs evenly through the sectors between the surviving packs on either
    side when they belong to the same VOB. Each VOB keeps its own clock, so a
    neighbour from another VOB is no guide; the time is then carried on from the
    side that shares the unit's VOB.
    """
    start, end = starts[index], end_of(index)

    def times(other: int) -> tuple[int, int, int]:
        pack = navigation[other]
        return _be32(pack, PCI_START_PTM), _be32(pack, PCI_END_PTM), _be16(pack, DSI_VOB_ID)

    def ticks_per_sector(other: int) -> float:
        first, last, _ = times(other)
        sectors = max(1, end_of(other) - starts[other])
        return (last - first) / sectors if last > first else DEFAULT_UNIT_TICKS / sectors

    def onward_from(other: int) -> tuple[int, int]:
        origin, ticks = times(other)[1], ticks_per_sector(other)
        return origin + round((start - end_of(other)) * ticks), origin + round((end - end_of(other)) * ticks)

    def back_from(other: int) -> tuple[int, int]:
        origin, ticks = times(other)[0], ticks_per_sector(other)
        return origin - round((starts[other] - start) * ticks), origin - round((starts[other] - end) * ticks)

    left_vob = times(before)[2] if before is not None else None
    right_vob = times(after)[2] if after is not None else None
    if before is not None and after is not None and left_vob == right_vob == vob_id:
        gap_start, origin, target = end_of(before), times(before)[1], times(after)[0]
        ticks = (target - origin) / max(1, starts[after] - gap_start)
        first, last = origin + round((start - gap_start) * ticks), origin + round((end - gap_start) * ticks)
    elif after is not None and right_vob == vob_id and left_vob != vob_id:
        first, last = back_from(after)
    elif before is not None:
        first, last = onward_from(before)
    elif after is not None:
        first, last = back_from(after)
    else:
        first, last = 0, DEFAULT_UNIT_TICKS
    first = max(0, first)
    return first, max(first + 1, last)


def plan_image_mend(image: Path, unread: list[tuple[int, int]], title_number: int) -> DvdMend:
    """``plan_dvd_mend`` for a rescued image file, which is only read."""
    with image.open("rb") as handle:

        def read(lba: int, count: int) -> bytes:
            handle.seek(lba * SECTOR_SIZE)
            return handle.read(count * SECTOR_SIZE).ljust(count * SECTOR_SIZE, b"\0")

        return plan_dvd_mend(read, image.stat().st_size // SECTOR_SIZE, unread, title_number)


def write_mended_copy(source: Path, target: Path, mend: DvdMend, stop: threading.Event | None = None) -> None:
    """Write ``source`` with ``mend`` applied to ``target``. The rescued image itself stays as it is."""
    try:
        with source.open("rb") as reader, target.open("wb") as writer:
            while chunk := reader.read(COPY_CHUNK_SECTORS * SECTOR_SIZE):
                if stop is not None and stop.is_set():
                    raise MendStopped
                writer.write(chunk)
            for low, high in mend.padding:
                writer.seek(low * SECTOR_SIZE)
                for first in range(low, high, COPY_CHUNK_SECTORS):
                    writer.write(PADDING_PACK * (min(high, first + COPY_CHUNK_SECTORS) - first))
            for sector, data in sorted(mend.sectors.items()):
                writer.seek(sector * SECTOR_SIZE)
                writer.write(data)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
