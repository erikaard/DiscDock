"""Look at what is on a data disc, and check that a backup of it is complete.

A disc that is not a movie or an audio CD is backed up as an image of itself.
Before that, DiscDock lists what the disc holds so it can be named and
recognised, and afterwards it reads the image's own file system back to prove
that every file came across.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .optical import SECTOR_SIZE, OpticalError, UdfVolume, _iso_directory

# A disc can hold far more files than anyone wants to read, and a scratched one can
# take a long time to answer. Both are capped so looking at a disc never hangs a job.
MAX_ENTRIES = 20000
SCAN_SECONDS = 120.0

# What the files say the disc is for.
GAME_MARKERS = {"autorun.inf", "setup.exe", "install.exe", "autorun.exe", "game.exe", "start.exe"}
PLAYABLE_EXTENSIONS = {".exe", ".msi", ".bat", ".cmd", ".jar", ".swf"}
MEDIA_EXTENSIONS = {
    ".avi", ".flac", ".m4a", ".m4v", ".mkv", ".mov", ".mp3", ".mp4", ".mpg", ".mpeg", ".ogg", ".wav", ".wmv",
}
PICTURE_EXTENSIONS = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff"}
DOCUMENT_EXTENSIONS = {".doc", ".docx", ".pdf", ".ppt", ".pptx", ".rtf", ".txt", ".xls", ".xlsx"}


@dataclass(frozen=True)
class DiscEntry:
    """One file on a disc: its path from the root of the disc, and its size."""

    path: str
    size: int


@dataclass
class DiscContents:
    """What a disc holds, as far as it could be read."""

    entries: list[DiscEntry]
    total_bytes: int
    truncated: bool = False
    note: str = ""

    @property
    def file_count(self) -> int:
        return len(self.entries)

    def top_level(self) -> list[tuple[str, int, int]]:
        """The first part of every path with how many files and bytes sit under it."""
        folders: dict[str, list[int]] = {}
        for entry in self.entries:
            # A file in the root of the disc stands for itself; anything else counts towards its folder.
            name = entry.path.replace("\\", "/").partition("/")[0]
            counts = folders.setdefault(name, [0, 0])
            counts[0] += 1
            counts[1] += entry.size
        return sorted(
            ((name, count, size) for name, (count, size) in folders.items()),
            key=lambda item: (-item[2], item[0].casefold()),
        )


def describe_contents(contents: DiscContents, label: str = "") -> dict[str, object]:
    """A short reading of what the disc is, to show next to its files.

    The guess only steers the wording and the suggested name; what is backed up
    never depends on it.
    """
    names = {Path(entry.path).name.casefold() for entry in contents.entries}
    suffixes = [Path(entry.path).suffix.casefold() for entry in contents.entries]
    programs = sum(1 for suffix in suffixes if suffix in PLAYABLE_EXTENSIONS)
    media = sum(1 for suffix in suffixes if suffix in MEDIA_EXTENSIONS)
    pictures = sum(1 for suffix in suffixes if suffix in PICTURE_EXTENSIONS)
    documents = sum(1 for suffix in suffixes if suffix in DOCUMENT_EXTENSIONS)
    markers = sorted(names & GAME_MARKERS)
    if markers and programs:
        kind, summary = "game", "Looks like a game or program disc: it starts itself from the disc."
    elif programs:
        kind, summary = "software", "Holds programs to install or run."
    elif media and media >= max(1, contents.file_count // 4):
        kind, summary = "media", "Holds music or video files."
    elif pictures and pictures >= max(1, contents.file_count // 4):
        kind, summary = "pictures", "Holds pictures."
    elif documents and documents >= max(1, contents.file_count // 4):
        kind, summary = "documents", "Holds documents."
    else:
        kind, summary = "files", "Holds files of no particular kind."
    return {
        "kind": kind,
        "summary": summary,
        "markers": markers,
        "suggested_title": suggested_title(label, contents),
        "file_count": contents.file_count,
        "total_bytes": contents.total_bytes,
        "program_count": programs,
        "media_count": media,
        "truncated": contents.truncated,
        "note": contents.note,
        "top_level": [
            {"name": name, "file_count": count, "total_bytes": size}
            for name, count, size in contents.top_level()[:40]
        ],
        "entries": [{"path": entry.path, "size": entry.size} for entry in contents.entries[:MAX_ENTRIES]],
    }


def suggested_title(label: str, contents: DiscContents) -> str:
    """A readable name from the disc's label, which is usually the product's own name."""
    del contents
    text = " ".join(part for part in label.replace("_", " ").replace("-", " ").split() if part)
    if not text:
        return ""
    if text.isupper() or text.islower():
        # Disc labels are shouted (SIMS2_EP1) or whispered; neither reads well as a folder name.
        text = " ".join(word.capitalize() for word in text.split())
    return text.strip()


def list_drive_files(
    letter: str, *, limit: int = MAX_ENTRIES, seconds: float = SCAN_SECONDS, clock: Callable[[], float] = time.monotonic
) -> DiscContents:
    """Every file Windows can see on the disc in ``letter``, with its size.

    Windows mounts a data disc itself, so its own file system is the most
    complete view: ISO 9660, Joliet and UDF all read the same way. Unreadable
    corners are skipped rather than failing the whole listing.
    """
    drive = letter.rstrip(":\\").upper()
    if len(drive) != 1 or not drive.isalpha():
        raise ValueError("Invalid Windows drive letter")
    return list_tree(Path(f"{drive}:\\"), limit=limit, seconds=seconds, clock=clock)


def list_tree(
    root: Path, *, limit: int = MAX_ENTRIES, seconds: float = SCAN_SECONDS, clock: Callable[[], float] = time.monotonic
) -> DiscContents:
    """Every file under ``root``, with its size, skipping whatever cannot be read."""
    entries: list[DiscEntry] = []
    total = 0
    truncated = False
    unreadable = 0
    deadline = clock() + seconds
    stack = [root]
    while stack:
        if clock() > deadline:
            truncated = True
            break
        folder = stack.pop()
        try:
            children = sorted(folder.iterdir(), key=lambda path: path.name.casefold())
        except OSError:
            unreadable += 1
            continue
        for child in children:
            try:
                if child.is_dir():
                    stack.append(child)
                    continue
                size = child.stat().st_size
            except OSError:
                unreadable += 1
                continue
            entries.append(DiscEntry(child.relative_to(root).as_posix(), size))
            total += size
            if len(entries) >= limit:
                truncated = True
                stack.clear()
                break
    entries.sort(key=lambda entry: entry.path.casefold())
    note = ""
    if unreadable:
        note = f"{unreadable} folder(s) or file(s) on the disc could not be listed."
    if truncated:
        note = (note + " " if note else "") + "Only the first part of the disc is listed here."
    return DiscContents(entries, total, truncated, note.strip())


def image_files(image: Path, *, limit: int = MAX_ENTRIES) -> DiscContents:
    """Every file inside a disc image, read from the image's own file system.

    Used to check a backup against the disc it came from, so a truncated or
    unreadable image is never mistaken for a complete one.
    """
    with image.open("rb") as handle:

        def read(lba: int, count: int) -> bytes:
            handle.seek(lba * SECTOR_SIZE)
            data = handle.read(count * SECTOR_SIZE)
            if len(data) < count * SECTOR_SIZE:
                data = data.ljust(count * SECTOR_SIZE, b"\0")
            return data

        total_sectors = image.stat().st_size // SECTOR_SIZE
        try:
            return _iso9660_files(read, total_sectors, limit)
        except OpticalError:
            return _udf_files(read, total_sectors, limit)


def _iso9660_files(read, total_sectors: int, limit: int) -> DiscContents:
    """The ISO 9660 listing of an image, following its directory records."""
    root: tuple[int, int] | None = None
    for sector in range(16, 32):
        if sector >= total_sectors:
            break
        descriptor = read(sector, 1)
        if descriptor[1:6] != b"CD001":
            break
        if descriptor[0] == 1 and root is None:
            record = descriptor[156:190]
            root = (int.from_bytes(record[2:6], "little"), int.from_bytes(record[10:14], "little"))
        if descriptor[0] == 255:
            break
    if not root:
        raise OpticalError("The image has no ISO 9660 directory")
    entries: list[DiscEntry] = []
    total = 0
    truncated = False
    stack: list[tuple[str, int, int]] = [("", *root)]
    seen: set[tuple[int, int]] = set()
    while stack:
        prefix, lba, size = stack.pop()
        if (lba, size) in seen or lba <= 0 or lba >= total_sectors:
            continue
        seen.add((lba, size))
        for name, child_lba, child_size, flags in _iso_directory(read, lba, size):
            path = f"{prefix}{name}"
            if flags & 0x02:
                stack.append((f"{path}/", child_lba, child_size))
                continue
            entries.append(DiscEntry(path, child_size))
            total += child_size
            if len(entries) >= limit:
                truncated = True
                stack.clear()
                break
    entries.sort(key=lambda entry: entry.path.casefold())
    return DiscContents(entries, total, truncated)


def _udf_files(read, total_sectors: int, limit: int) -> DiscContents:
    """The UDF listing of an image, for discs written without an ISO 9660 bridge."""
    volume = UdfVolume.open(read, total_sectors)
    entries = [
        DiscEntry(name.replace("\\", "/"), entry.size)
        for name, entry in sorted(volume.files().items(), key=lambda item: item[0].casefold())
    ]
    truncated = len(entries) > limit
    entries = entries[:limit]
    return DiscContents(entries, sum(entry.size for entry in entries), truncated)


@dataclass
class BackupCheck:
    """How an image compares with the disc it was made from."""

    complete: bool
    reason: str = ""
    files_in_image: int = 0
    files_on_disc: int = 0


def check_backup(disc: DiscContents, image: Path) -> BackupCheck:
    """Whether every file the disc showed is in the image, at the same size.

    The names a file system reports differ between Windows and a plain ISO 9660
    reader (upper case, ``;1`` versions, short names), so files are matched by
    path where possible and by size where the names were rewritten.
    """
    try:
        inside = image_files(image)
    except (OpticalError, OSError, ValueError) as error:
        return BackupCheck(False, f"the backup's file system could not be read ({error})", 0, disc.file_count)
    if not inside.entries:
        return BackupCheck(False, "the backup holds no files", 0, disc.file_count)
    if disc.truncated or inside.truncated:
        # Neither listing is complete, so only the obvious failures can be caught.
        if inside.file_count == 0:
            return BackupCheck(False, "the backup holds no files", 0, disc.file_count)
        return BackupCheck(True, "", inside.file_count, disc.file_count)
    missing = _missing_files(disc, inside)
    if missing:
        listing = ", ".join(missing[:3])
        return BackupCheck(
            False,
            f"{len(missing)} file(s) on the disc are not in the backup, for example {listing}",
            inside.file_count,
            disc.file_count,
        )
    return BackupCheck(True, "", inside.file_count, disc.file_count)


def _missing_files(disc: DiscContents, inside: DiscContents) -> list[str]:
    by_path = {entry.path.casefold(): entry.size for entry in inside.entries}
    sizes: dict[int, int] = {}
    for entry in inside.entries:
        sizes[entry.size] = sizes.get(entry.size, 0) + 1
    missing: list[str] = []
    for entry in disc.entries:
        wanted = entry.path.casefold()
        if by_path.get(wanted) == entry.size:
            continue
        # A file system rewrites names; an empty file has no size to match on either.
        if sizes.get(entry.size, 0) > 0 and entry.size > 0:
            sizes[entry.size] -= 1
            continue
        if entry.size == 0 and any(name.endswith(Path(wanted).name) for name in by_path):
            continue
        missing.append(entry.path)
    return missing
