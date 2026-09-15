"""A MakeMKV backup folder filled from a rescued Blu-ray image.

MakeMKV decrypts some Blu-rays only with information it reads from the drive,
so it cannot read them from a plain disc image. A backup MakeMKV starts itself
saves that information first, in discatt.dat. Next to the files of the rescued
image, it lets MakeMKV extract the movie as it would from its own backup,
without reading the damaged disc again.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path

from .disc_rescue import _extend_file, _set_sparse, _subtract
from .optical import SECTOR_SIZE, OpticalError, UdfVolume, _read_file, mpls_clips, read_bluray_layout

DISC_ATTRIBUTES = "discatt.dat"
# Files at least this large that the movie does not use become empty placeholders.
PLACEHOLDER_MIN_BYTES = 64 * 1024 * 1024
# Which spots of the movie's clips are empty units, so they can be filled once the rescue reads them.
PADDED_SPANS = ".discdock-padded-spans.json"
_CHUNK = 16 * 1024 * 1024
# Blu-ray clips are encrypted in aligned units of 32 packets, each a 4-byte header and a transport packet.
ALIGNED_UNIT = 6144
SOURCE_PACKET = 192
_ARRIVAL_MASK = (1 << 30) - 1
# An unencrypted transport-stream null packet (PID 0x1FFF).
NULL_PACKET = bytes([0x47, 0x1F, 0xFF, 0x10]) + b"\xff" * 184


def _safe_parts(name: str) -> list[str] | None:
    parts = name.split("/")
    if any(part in {"", ".", ".."} or any(character in part for character in '\\:*?"<>|') for part in parts):
        return None
    return parts


def _match_case(target: Path, folder: Path) -> None:
    """Give an existing file and its folders the exact names the disc uses.

    Windows finds "UNIT_KEY_RO.INF" when asked for "Unit_Key_RO.inf", but MakeMKV
    needs the disc's own spelling, so a wrongly spelled copy is renamed.
    """
    current = folder
    for part in target.relative_to(folder).parts:
        try:
            names = os.listdir(current)
        except OSError:
            return
        actual = next((name for name in names if name.casefold() == part.casefold()), None)
        if actual is None:
            return
        if actual != part:
            temporary = current / f".{part}.rename-{uuid.uuid4().hex[:8]}"
            os.rename(current / actual, temporary)
            os.rename(temporary, current / part)
        current = current / part


def _image_reader(handle):
    def read(lba: int, count: int) -> bytes:
        handle.seek(lba * SECTOR_SIZE)
        data = handle.read(count * SECTOR_SIZE)
        if len(data) != count * SECTOR_SIZE:
            raise OpticalError(f"Sector {lba} lies outside the rescued image")
        return data

    return read


def movie_playlist(image: Path, duration_seconds: int = 0) -> str:
    """The playlist file of the movie in a rescued Blu-ray image, for example "00800.mpls".

    DiscDock releases before 1.7.0 did not record which playlist a title plays.
    The playlist whose length is closest to the title's is taken, or the longest
    when the length is not known. Returns "" when no playlist can be read.
    """
    with image.open("rb") as handle:
        read = _image_reader(handle)
        try:
            files = UdfVolume.open(read, image.stat().st_size // SECTOR_SIZE).files()
        except OpticalError:
            return ""
        clips_on_disc = {
            name.rsplit("/", 1)[1].removesuffix(".M2TS")
            for name in files
            if name.startswith("BDMV/STREAM/") and name.endswith(".M2TS")
        }
        best: tuple[float, float, str] | None = None
        for name, entry in files.items():
            if not (name.startswith("BDMV/PLAYLIST/") and name.endswith(".MPLS")):
                continue
            try:
                clips, seconds = mpls_clips(_read_file(read, entry))
            except OpticalError:
                continue
            if not clips or any(clip not in clips_on_disc for clip in clips):
                continue
            # Closest in length first, then the longer one, then by name.
            rank = (abs(seconds - duration_seconds) if duration_seconds > 0 else 0.0, -seconds, entry.name)
            if best is None or rank < best:
                best = rank
    return best[2].rsplit("/", 1)[1] if best else ""


def unread_spans(extents: list[tuple[int, int]], unread: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Byte ranges of a clip, widened to whole aligned units, that lie in unread sectors.

    ``extents`` are the clip's (sector, bytes) runs in file order; ``unread`` are
    sector ranges the rescue could not read.
    """
    holes: list[tuple[int, int]] = []
    position = 0
    for start, length in extents:
        end = start + (length + SECTOR_SIZE - 1) // SECTOR_SIZE
        for low, high in unread:
            if high <= start or low >= end:
                continue
            first, last = max(low, start), min(high, end)
            holes.append(
                (position + (first - start) * SECTOR_SIZE, min(position + length, position + (last - start) * SECTOR_SIZE))
            )
        position += length
    spans: list[tuple[int, int]] = []
    for low, high in sorted(holes):
        # Part of a unit is useless: it decrypts as a whole.
        first, last = low - low % ALIGNED_UNIT, min(position, high + -high % ALIGNED_UNIT)
        if spans and first <= spans[-1][1]:
            spans[-1] = (spans[-1][0], max(spans[-1][1], last))
        else:
            spans.append((first, last))
    return spans


def pad_unread_units(path: Path, spans: list[tuple[int, int]]) -> int:
    """Fill unread spots of a Blu-ray clip with empty, unencrypted transport-stream units.

    The rescue leaves zeros where the disc could not be read. MakeMKV works
    around a few damaged packets, but stops extracting at a longer run of zeros.
    Null packets keep the stream valid, so the movie just has a gap there. Their
    arrival time stamps run on from the unit before the spot to the unit after it.
    Returns the number of bytes filled.
    """
    filled = 0
    leading = ALIGNED_UNIT // SOURCE_PACKET
    with path.open("r+b") as output:
        size = output.seek(0, os.SEEK_END)

        def arrival(offset: int) -> int | None:
            if offset < 0 or offset + 5 > size:
                return None
            output.seek(offset)
            header = output.read(5)
            return int.from_bytes(header[:4], "big") & _ARRIVAL_MASK if header[4] == 0x47 else None

        for low, high in spans:
            high = min(high, size)
            if high <= low:
                continue
            before, after = arrival(low - ALIGNED_UNIT), arrival(high)
            packets = (high - low) // SOURCE_PACKET
            step = ((after - before) & _ARRIVAL_MASK) if before is not None and after is not None else 0
            output.seek(low)
            block = bytearray()
            for number in range(packets):
                if step:
                    stamp = (before + step * (leading + number) // (leading + packets)) & _ARRIVAL_MASK  # type: ignore[operator]
                else:
                    stamp = before if before is not None else after or 0
                block += stamp.to_bytes(4, "big") + NULL_PACKET
                if len(block) >= _CHUNK:
                    output.write(block)
                    block.clear()
            # A clip that does not end on a whole packet keeps zeros in its last bytes.
            block += bytes((high - low) % SOURCE_PACKET)
            output.write(block)
            filled += high - low
    return filled


def _copy_file_range(source, output, extents: list[tuple[int, int]], low: int, high: int) -> None:
    """Copy bytes ``low`` to ``high`` of a file whose data lies in ``extents`` of the image."""
    position = 0
    for start, length in extents:
        end = position + length
        if end > low and position < high:
            first, last = max(low, position), min(high, end)
            source.seek(start * SECTOR_SIZE + first - position)
            output.seek(first)
            remaining = last - first
            while remaining > 0:
                block = source.read(min(_CHUNK, remaining))
                if not block:
                    raise OSError("The clip ends early in the rescued image")
                output.write(block)
                remaining -= len(block)
        position = end
        if position >= high:
            break


def _load_padded_spans(folder: Path) -> dict[str, list[tuple[int, int]]]:
    try:
        data = json.loads((folder / PADDED_SPANS).read_text(encoding="utf-8"))
        return {str(name): [(int(low), int(high)) for low, high in spans] for name, spans in data.items()}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def fill_backup_folder(
    image: Path,
    folder: Path,
    playlist: str = "",
    *,
    placeholder_min_bytes: int = PLACEHOLDER_MIN_BYTES,
    unread: list[tuple[int, int]] | None = None,
) -> dict[str, int]:
    """Write the files of a rescued Blu-ray image into a MakeMKV backup folder.

    Files MakeMKV already wrote with the right size are kept. The movie's clips
    are copied; spots in the ``unread`` sector ranges become empty transport-stream
    units. Spots that were empty units last time and have been read since are
    copied from the image again. Other large files become empty placeholders of
    the right size, so the folder needs about as much space as the movie.
    """
    total_sectors = image.stat().st_size // SECTOR_SIZE
    counts = {"copied": 0, "placeholders": 0, "kept": 0, "padded_bytes": 0, "refreshed_bytes": 0}
    with image.open("rb") as handle:
        read = _image_reader(handle)
        files = UdfVolume.open(read, total_sectors).files()
        layout = read_bluray_layout(read, total_sectors, playlist)
        movie = {f"BDMV/STREAM/{clip}.M2TS" for clip in layout.clips} if layout.verified else set(files)
        work: list[tuple[Path, list[tuple[int, int]], int, bool]] = []
        clips: list[tuple[Path, list[tuple[int, int]]]] = []
        needed = 0
        for name, entry in sorted(files.items()):
            parts = _safe_parts(entry.name)
            if parts is None or (entry.size and not entry.extents):
                continue
            target = folder.joinpath(*parts)
            if target.exists():
                _match_case(target, folder)
            if name in movie and name.startswith("BDMV/STREAM/") and name.endswith(".M2TS"):
                clips.append((target, entry.extents))
            if target.is_file() and target.stat().st_size == entry.size:
                counts["kept"] += 1
                continue
            placeholder = entry.size >= placeholder_min_bytes and name not in movie
            if not placeholder:
                needed += entry.size
            work.append((target, entry.extents, entry.size, placeholder))
        folder.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(folder).free < needed + 1024**3:
            raise RuntimeError(
                f"There is not enough free disk space to prepare the rescued movie for MakeMKV "
                f"({needed / 1024**3:.1f} GB needed)"
            )
        copied = {target for target, _, _, _ in work}
        for target, extents, size, placeholder in work:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as output:
                if placeholder:
                    _set_sparse(output)
                    _extend_file(output, size)
                    counts["placeholders"] += 1
                    continue
                for start, length in extents:
                    handle.seek(start * SECTOR_SIZE)
                    remaining = length
                    while remaining > 0:
                        block = handle.read(min(_CHUNK, remaining))
                        if not block:
                            raise OSError(f"{target.name} ends early in the rescued image")
                        output.write(block)
                        remaining -= len(block)
            counts["copied"] += 1

        previous = _load_padded_spans(folder)
        padded: dict[str, list[list[int]]] = {}
        for target, extents in clips:
            if not target.is_file():
                continue
            key = target.relative_to(folder).as_posix().casefold()
            spans = unread_spans(extents, unread or [])
            # A clip kept from an earlier attempt still has empty units where the rescue has read since.
            readable_again = [] if target in copied else _subtract(previous.get(key, []), spans)
            if readable_again:
                with target.open("r+b") as output:
                    for low, high in readable_again:
                        _copy_file_range(handle, output, extents, low, high)
                counts["refreshed_bytes"] += sum(high - low for low, high in readable_again)
            if spans:
                # Filling a spot again writes the same units.
                counts["padded_bytes"] += pad_unread_units(target, spans)
                padded[key] = [[low, high] for low, high in spans]
    if padded or previous:
        (folder / PADDED_SPANS).write_text(json.dumps(padded), encoding="utf-8")
    return counts
