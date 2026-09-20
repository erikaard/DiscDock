from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import unicodedata
import uuid
from pathlib import Path

from .damage_screens import UNREPAIRED_SUFFIXES, is_unrepaired_copy
from .models import MediaKind
from .processes import start_external_process

INVALID_COMPONENT = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
MEDIA_EXTENSIONS = {
    ".mkv",
    ".mp4",
    ".m4v",
    ".avi",
    ".mov",
    ".ts",
    ".m2ts",
    ".flac",
    ".mp3",
    ".opus",
    ".wav",
    ".iso",
}
# A CD track can be a few seconds long and far smaller than a megabyte; a video file or disc image cannot.
AUDIO_EXTENSIONS = {".flac", ".mp3", ".opus", ".wav"}
MIN_VIDEO_BYTES = 1024 * 1024


def _too_small(path: Path, size: int) -> bool:
    return size <= 0 if path.suffix.lower() in AUDIO_EXTENSIONS else size <= MIN_VIDEO_BYTES


def safe_component(value: str, fallback: str = "Unidentified") -> str:
    cleaned = unicodedata.normalize("NFKC", value or "")
    cleaned = INVALID_COMPONENT.sub("_", cleaned).strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        cleaned = fallback
    if cleaned.upper() in RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned[:150]


def ensure_within(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    root_resolved = root.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise ValueError(f"Path escapes configured root: {resolved}")
    return resolved


def output_folder(
    completed: Path,
    media_kind: MediaKind,
    title: str,
    year: str,
    fingerprint: str,
    *,
    duplicate_policy: str = "keep_both",
    include_category: bool = True,
) -> Path:
    if duplicate_policy not in {"replace", "keep_both"}:
        raise ValueError("Output creation requires replace or keep_both duplicate handling")
    category = {
        MediaKind.MOVIE: "movies",
        MediaKind.SERIES: "tv",
        MediaKind.MUSIC: "music",
        MediaKind.OTHER: "other",
        MediaKind.DATA: "data",
    }.get(media_kind, "unidentified")
    name = safe_component(title)
    if year and year[:4].isdigit():
        name = f"{name} ({year[:4]})"
    parent = completed / category if include_category else completed
    canonical = ensure_within(parent / name, completed)
    if duplicate_policy == "replace" or not canonical.exists():
        return canonical

    suffix = safe_component(fingerprint[:8] or uuid.uuid4().hex[:8])
    candidate = ensure_within(canonical.with_name(f"{canonical.name} [{suffix}]"), completed)
    sequence = 2
    while candidate.exists():
        candidate = ensure_within(canonical.with_name(f"{canonical.name} [{suffix}-{sequence}]"), completed)
        sequence += 1
    return candidate


def _next_number(folder: Path | None, base: str, label: str) -> int:
    """The number after the highest "<base> - <label> NN" file already in ``folder``."""
    if folder is None or not folder.is_dir():
        return 1
    prefix = f"{base} - {label} ".casefold()
    highest = 0
    for path in folder.glob("*.mkv"):
        stem = path.stem.casefold()
        if stem.startswith(prefix) and stem[len(prefix) :].isdigit():
            highest = max(highest, int(stem[len(prefix) :]))
    return highest + 1


def rename_video_outputs(
    folder: Path, folder_name: str, media_kind: MediaKind, *, existing: Path | None = None
) -> list[Path]:
    """Give MKV files stable library names before the staging tree is finalized.

    With ``existing``, the files join a library folder that already holds some:
    episode and extra numbers continue after the ones there, and a folder that
    already has its movie gets only extras.
    """
    everything = [path for path in folder.rglob("*") if path.is_file() and path.suffix.lower() == ".mkv"]
    # The copy kept without loading screens or AI frames follows the movie's name.
    kept = [path for path in everything if is_unrepaired_copy(path)]
    files = [path for path in everything if path not in kept]
    if not files:
        return []
    base = safe_component(folder_name)
    if media_kind == MediaKind.SERIES:
        ordered = sorted(files, key=lambda path: path.name.casefold())
        first = _next_number(existing, base, "Episode")
        names = [f"{base} - Episode {index:02d}.mkv" for index in range(first, first + len(ordered))]
    else:
        main = max(files, key=lambda path: (path.stat().st_size, path.name.casefold()))
        extras = sorted(
            (path for path in files if path != main),
            key=lambda path: (-path.stat().st_size, path.name.casefold()),
        )
        ordered = [main, *extras]
        first = _next_number(existing, base, "Extra")
        if existing is not None and (existing / f"{base}.mkv").exists():
            names = [f"{base} - Extra {index:02d}.mkv" for index in range(first, first + len(ordered))]
        else:
            names = [f"{base}.mkv", *[f"{base} - Extra {index:02d}.mkv" for index in range(first, first + len(extras))]]
        for path in sorted(kept, key=lambda item: item.name.casefold()):
            suffix = next(suffix for suffix in UNREPAIRED_SUFFIXES if path.stem.endswith(suffix))
            ordered.append(path)
            names.append(f"{base}{suffix}.mkv")

    moves = [(source, source.with_name(name)) for source, name in zip(ordered, names, strict=True)]
    source_paths = set(ordered)
    for _, target in moves:
        if target.exists() and target not in source_paths:
            raise FileExistsError(target)

    staged: list[tuple[Path, Path, Path]] = []
    completed: list[tuple[Path, Path]] = []
    try:
        for source, target in moves:
            if source == target:
                continue
            temporary = source.with_name(f".discdock-rename-{uuid.uuid4().hex}.tmp")
            os.replace(source, temporary)
            staged.append((temporary, target, source))
        for temporary, target, source in staged:
            os.replace(temporary, target)
            completed.append((target, source))
    except BaseException:
        for target, source in reversed(completed):
            if target.exists() and not source.exists():
                os.replace(target, source)
        for temporary, _, source in staged:
            if temporary.exists() and not source.exists():
                os.replace(temporary, source)
        raise
    return [target for _, target in moves]


def disk_space_ok(path: Path, required_bytes: int) -> bool:
    path.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(path).free >= max(required_bytes, 2 * 1024**3)


def _run_ffprobe(path: Path, ffprobe_path: str) -> bool:
    process = start_external_process(
        [
            ffprobe_path,
            "-v",
            "error",
            "-show_entries",
            "format=duration,size",
            "-of",
            "default=nw=1",
            str(path),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=0x08000000 if os.name == "nt" else 0,
    )
    try:
        stdout, _ = process.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        return False
    return process.returncode == 0 and bool(stdout.strip())


async def _ffprobe(path: Path, ffprobe_path: str) -> bool:
    if (
        not ffprobe_path
        or not Path(ffprobe_path).is_file()
        or path.suffix.lower() not in MEDIA_EXTENSIONS - {".iso"}
    ):
        return not _too_small(path, path.stat().st_size)
    return await asyncio.to_thread(_run_ffprobe, path, ffprobe_path)


async def verify_media_file(path: Path, ffprobe_path: str) -> None:
    if not path.is_file() or path.stat().st_size <= 1024 * 1024:
        raise RuntimeError("The repaired movie file is incomplete")
    size = path.stat().st_size
    await asyncio.sleep(2)
    if not path.is_file() or path.stat().st_size != size:
        raise RuntimeError("The repaired movie file is still changing")
    if not await _ffprobe(path, ffprobe_path):
        raise RuntimeError("The repaired movie failed media verification")


async def verify_outputs(folder: Path, ffprobe_path: str) -> list[Path]:
    files = [path for path in folder.rglob("*") if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS]
    if not files:
        raise RuntimeError("No media files were produced")
    sizes_before = {path: path.stat().st_size for path in files}
    await asyncio.sleep(2)
    if any(path.stat().st_size != size or _too_small(path, size) for path, size in sizes_before.items()):
        raise RuntimeError("Output files are incomplete or still changing")
    results = await asyncio.gather(*(_ffprobe(path, ffprobe_path) for path in files))
    if not all(results):
        invalid = [str(path.name) for path, ok in zip(files, results, strict=True) if not ok]
        raise RuntimeError("Output verification failed: " + ", ".join(invalid))
    return files


async def _copy_file_cancellable(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb", buffering=0) as input_handle, destination.open("xb", buffering=0) as output_handle:
        while True:
            block = await asyncio.to_thread(input_handle.read, 4 * 1024 * 1024)
            if not block:
                break
            await asyncio.to_thread(output_handle.write, block)
        await asyncio.to_thread(os.fsync, output_handle.fileno())
    await asyncio.to_thread(shutil.copystat, source, destination)


async def _copy_tree_cancellable(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        target = ensure_within(destination / relative, destination)
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            await _copy_file_cancellable(path, target)


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


async def finalize_directory(
    source: Path,
    destination: Path,
    keep_source: bool,
    *,
    replace_existing: bool = False,
) -> Path:
    source = source.resolve()
    destination = destination.resolve()
    if source == destination:
        raise ValueError("Source and destination must be different directories")
    if not source.is_dir():
        raise FileNotFoundError(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not replace_existing:
        raise FileExistsError(destination)

    incoming = destination.with_name(f".{destination.name}.incoming-{uuid.uuid4().hex}")
    if incoming.exists():
        raise FileExistsError(incoming)

    source_was_moved = False
    backup: Path | None = None
    try:
        if not keep_source and source.drive.casefold() == destination.drive.casefold():
            os.replace(source, incoming)
            source_was_moved = True
        else:
            await _copy_tree_cancellable(source, incoming)

        if destination.exists():
            if not replace_existing:
                raise FileExistsError(destination)
            backup = destination.with_name(f".{destination.name}.replaced-{uuid.uuid4().hex}")
            os.replace(destination, backup)

        try:
            os.replace(incoming, destination)
        except BaseException:
            if backup and backup.exists() and not destination.exists():
                os.replace(backup, destination)
                backup = None
            raise
    except BaseException:
        if incoming.exists():
            if source_was_moved and not source.exists():
                try:
                    os.replace(incoming, source)
                except OSError:
                    # Keep the complete staged tree under an explicit recovery name.
                    recovery = incoming.with_name(f".{destination.name}.recovery-{uuid.uuid4().hex[:8]}")
                    os.replace(incoming, recovery)
            elif not source_was_moved:
                cancelled = incoming.with_name(f".{destination.name}.cancelled-{uuid.uuid4().hex[:8]}")
                os.replace(incoming, cancelled)
        raise

    if backup and backup.exists():
        try:
            await asyncio.to_thread(_remove_path, backup)
        except OSError:
            # The new output is already in place. Leaving the hidden backup is
            # safer than reporting a failed rip and attempting the replacement again.
            pass
    if not keep_source and source.exists():
        # Cross-volume source cleanup happens only after a complete durable copy.
        await asyncio.to_thread(shutil.rmtree, source)
    return destination


async def merge_into_directory(source: Path, destination: Path, keep_source: bool) -> Path:
    """Move the files of ``source`` into the existing library folder ``destination``.

    Files already in the folder are never replaced: when a name is taken, nothing
    moves. Should a move fail halfway, the files moved so far go back to ``source``.
    """
    source = source.resolve()
    destination = destination.resolve()
    if source == destination:
        raise ValueError("Source and destination must be different directories")
    if not source.is_dir():
        raise FileNotFoundError(source)
    if not destination.is_dir():
        raise FileNotFoundError(destination)
    moves = [(path, destination / path.relative_to(source)) for path in sorted(source.rglob("*")) if path.is_file()]
    taken = [target for _, target in moves if target.exists()]
    if taken:
        raise FileExistsError(taken[0])
    same_volume = not keep_source and source.drive.casefold() == destination.drive.casefold()
    done: list[tuple[Path, Path]] = []
    try:
        for path, target in moves:
            target.parent.mkdir(parents=True, exist_ok=True)
            if same_volume:
                os.replace(path, target)
            else:
                incoming = target.with_name(f".{target.name}.incoming-{uuid.uuid4().hex}")
                await asyncio.to_thread(shutil.copy2, path, incoming)
                os.replace(incoming, target)
            done.append((path, target))
    except BaseException:
        for path, target in reversed(done):
            if same_volume and target.exists() and not path.exists():
                os.replace(target, path)
            elif not same_volume:
                target.unlink(missing_ok=True)
        raise
    if not keep_source and source.exists():
        await asyncio.to_thread(shutil.rmtree, source)
    return destination


async def move_failed_staging(
    staging: Path,
    failed_root: Path,
    job_id: str,
    *,
    raw_root: Path,
) -> Path | None:
    """Move raw and transcode staging trees into one collision-safe failed-job folder."""
    staging = staging.resolve()
    failed_root = failed_root.resolve()
    raw_root = raw_root.resolve()
    if not staging.exists():
        transcode_only = staging.with_name(staging.name.removesuffix(".partial") + ".transcode.partial")
        if not transcode_only.exists():
            return None

    if staging == failed_root or failed_root in staging.parents:
        return staging
    ensure_within(staging, raw_root)

    transcode = staging.with_name(staging.name.removesuffix(".partial") + ".transcode.partial")
    sources = [path for path in (staging, transcode) if path.exists()]
    for source in sources:
        ensure_within(source, raw_root)

    base = ensure_within(failed_root / safe_component(job_id, "failed-job"), failed_root)
    bundle = base
    sequence = 2
    while bundle.exists():
        bundle = ensure_within(base.with_name(f"{base.name}-{sequence}"), failed_root)
        sequence += 1
    bundle.mkdir(parents=True, exist_ok=False)

    primary: Path | None = None
    for source in sources:
        destination = ensure_within(bundle / source.name, failed_root)
        moved = await finalize_directory(source, destination, keep_source=False)
        if source == staging or primary is None:
            primary = moved
    return primary


def open_folder(path: Path) -> None:
    if os.name != "nt":
        raise RuntimeError("Folder opening is only supported on Windows")
    start_external_process(
        ["explorer.exe", str(path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=0x08000000,
    )
