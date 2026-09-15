"""Copy a Blu-ray movie from rescued files with FFmpeg, past damaged spots.

MakeMKV stops extracting a movie in a stretch of dense damage. MakeMKV's own
library for Blu-ray players (libmmbd) still decrypts the movie there, one aligned
unit at a time, in a helper process (``discdock.bluray_decrypt``); DiscDock
decrypts nothing itself. FFmpeg then copies the plain movie, skipping what it
cannot use. The copy keeps the playlist's streams in their order with their
languages, and the playlist's chapters.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

from .bluray_decrypt import EVENT_PREFIX as DECRYPT_EVENT_PREFIX
from .bluray_decrypt import EXIT_CANNOT_OPEN
from .bluray_folder import ALIGNED_UNIT, SOURCE_PACKET
from .disc_rescue import _extend_file, _set_sparse
from .optical import OpticalError, PlaylistStream, mpls_chapters, mpls_clips, mpls_play_items, mpls_streams
from .processes import ProcessFailure, ProcessRunner, run_capture

ProgressCallback = Callable[[dict], Awaitable[None] | None]
# A copy shorter than this share of the playlist stopped early.
MIN_COPIED_SHARE = 0.9
OUTPUT_NAME = "title_t00.mkv"
# Decrypting takes about as long as copying; progress runs 0-60 % for it, then 60-100 %.
DECRYPT_SHARE = 60.0
MAX_DECRYPT_ATTEMPTS = 3
# Small files are copied when a hard link is not possible.
_COPY_MAX_BYTES = 64 * 1024 * 1024
_PROGRESS_LINE = re.compile(r"^[a-z_]+=")


class BlurayCopyError(RuntimeError):
    """FFmpeg cannot copy this movie."""


def libmmbd_library(make_mkv_path: str) -> str:
    """MakeMKV's library for Blu-ray players next to its executable, without ".dll"."""
    if not make_mkv_path:
        return ""
    folder = Path(make_mkv_path).parent
    for name in ("libmmbd64", "libmmbd"):
        if (folder / f"{name}.dll").is_file():
            return str(folder / name)
    return ""


def decrypt_helper_command(arguments: list[str]) -> tuple[list[str], Path | None]:
    """Return the command that runs the decrypting helper for this installation."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--bluray-decrypt", *arguments], None
    return [sys.executable, "-m", "discdock.bluray_decrypt", *arguments], Path(__file__).resolve().parents[1]


def find_path(folder: Path, *parts: str) -> Path | None:
    """A file below ``folder``, with names compared without regard to case."""
    current = folder
    for part in parts:
        try:
            names = os.listdir(current)
        except OSError:
            return None
        match = next((name for name in names if name.casefold() == part.casefold()), None)
        if match is None:
            return None
        current = current / match
    return current


def _movie_clips(folder: Path, playlist: str) -> list[str]:
    playlist_path = find_path(folder, "BDMV", "PLAYLIST", playlist)
    if playlist_path is None:
        raise BlurayCopyError(f"The rescued disc files have no playlist {playlist}")
    try:
        return mpls_clips(playlist_path.read_bytes())[0]
    except OpticalError as error:
        raise BlurayCopyError(f"The movie's playlist cannot be read: {error}") from error


def movie_decryption_started(folder: Path, playlist: str) -> bool:
    """Whether an earlier copy already decrypted the start of the movie in ``folder``.

    MakeMKV stopped at the damage that time, so it need not be asked again.
    """
    try:
        clips = _movie_clips(folder, playlist)
    except BlurayCopyError:
        return False
    clip = find_path(folder, "BDMV", "STREAM", f"{clips[0]}.m2ts") if clips else None
    if clip is None:
        return False
    with clip.open("rb") as source:
        unit = source.read(ALIGNED_UNIT)
    # Clear and valid, and not an empty unit standing in for an unread spot.
    return (
        len(unit) == ALIGNED_UNIT
        and unit[0] >> 6 == 0
        and unit[4::SOURCE_PACKET] == b"\x47" * (ALIGNED_UNIT // SOURCE_PACKET)
        and unit[5:7] != b"\x1f\xff"
    )


def build_key_folder(folder: Path, target: Path, playlist: str) -> int:
    """Prepare the folder MakeMKV's library opens to decrypt the movie in ``folder``.

    It holds hard links to the disc's files, without the content hash tables,
    which the units of unread spots fail. The library needs the movie's clips to
    be there, but it locks every clip it opens against writing, so here they are
    empty sparse files of the same size and the real clips stay free to be
    decrypted in place. Other clips are left out. Leftovers of an earlier
    attempt are reused. Returns the number of disc files linked.
    """
    clips = {f"{clip}.m2ts".casefold() for clip in _movie_clips(folder, playlist)}
    linked = 0
    for root, _directories, names in os.walk(folder):
        relative = Path(root).relative_to(folder)
        parts = [part.casefold() for part in relative.parts]
        for name in names:
            source, destination = Path(root) / name, target / relative / name
            if parts[:2] == ["bdmv", "stream"]:
                if len(parts) == 2 and name.casefold() in clips:
                    size = source.stat().st_size
                    if not (destination.is_file() and destination.stat().st_size == size):
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        with destination.open("wb") as placeholder:
                            _set_sparse(placeholder)
                            _extend_file(placeholder, size)
                continue
            if name.casefold().startswith("contenthash"):
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                try:
                    os.link(source, destination)
                except OSError:
                    if source.stat().st_size > _COPY_MAX_BYTES:
                        raise
                    shutil.copy2(source, destination)
            linked += 1
    return linked


def chapter_metadata(chapters: list[float], duration: float) -> str:
    """An FFmpeg metadata file with one chapter per start time."""
    lines = [";FFMETADATA1"]
    for number, start in enumerate(chapters, 1):
        end = chapters[number] if number < len(chapters) else max(duration, start)
        lines += [
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={round(start * 1000)}",
            f"END={round(end * 1000)}",
            f"title=Chapter {number:02d}",
        ]
    return "\n".join(lines) + "\n"


def concat_listing(items: list[tuple[Path, float, float]]) -> str:
    """An FFmpeg concat list that plays each clip from its in time to its out time."""
    lines = ["ffconcat version 1.0"]
    for path, start, end in items:
        quoted = str(path).replace("'", "'\\''")
        lines += [f"file '{quoted}'", f"inpoint {start:.6f}", f"outpoint {end:.6f}"]
    return "\n".join(lines) + "\n"


def copy_arguments(
    ffmpeg_path: str,
    source: list[str],
    output: Path,
    streams: list[PlaylistStream],
    chapters: Path | None,
) -> list[str]:
    """FFmpeg arguments that copy the movie's streams, skipping damaged packets instead of stopping."""
    args = [
        ffmpeg_path,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-nostats",
        "-progress",
        "pipe:1",
        "-loglevel",
        "error",
        "-fflags",
        "+discardcorrupt+genpts",
        "-err_detect",
        "ignore_err",
        "-probesize",
        "50M",
        "-analyzeduration",
        "30M",
        *source,
    ]
    if chapters is not None:
        args += ["-f", "ffmetadata", "-i", str(chapters)]
    if streams:
        # Transport-stream PIDs, as the playlist names them.
        for stream in streams:
            args += ["-map", f"0:i:{stream.pid}"]
    else:
        args += ["-map", "0:v:0", "-map", "0:a?", "-map", "0:s?"]
    args += [
        "-map_metadata",
        "-1",
        "-map_chapters",
        "1" if chapters is not None else "-1",
        "-c",
        "copy",
        # A packet cut off by damage can lose its time stamp, and MKV cannot store it.
        "-bsf",
        r"noise=drop=eq(pts\,nopts)",
        "-max_muxing_queue_size",
        "4096",
    ]
    for index, stream in enumerate(streams):
        if stream.language:
            args += [f"-metadata:s:{index}", f"language={stream.language}"]
    args.append(str(output))
    return args


async def _notify(callback: ProgressCallback | None, event: dict) -> None:
    if callback is None:
        return
    response = callback(event)
    if asyncio.iscoroutine(response):
        await response


class BlurayMovieCopy:
    def __init__(self, ffmpeg_path: str, ffprobe_path: str, runner: ProcessRunner, aacs_library: str):
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path
        self.runner = runner
        self.aacs_library = aacs_library

    def _probe(self, owner: str, args: list[str]) -> dict:
        code, stdout, _ = run_capture([self.ffprobe_path, "-v", "error", *args, "-of", "json"], timeout=300, owner=owner)
        if code != 0:
            return {}
        try:
            return json.loads(stdout or "{}")
        except ValueError:
            return {}

    async def decrypt(
        self, job_id: str, keys: Path, clips: list[Path], timeout: int, callback: ProgressCallback | None = None
    ) -> dict:
        """Let MakeMKV's library, opened on ``keys``, decrypt the clips in place; start the helper again when it stops."""
        arguments = ["--library", self.aacs_library, "--disc", str(keys)]
        for clip in clips:
            arguments += ["--clip", str(clip)]
        command, cwd = decrypt_helper_command(arguments)
        furthest = -1
        for _attempt in range(MAX_DECRYPT_ATTEMPTS):
            state: dict = {}

            async def on_line(line: str, state: dict = state) -> None:
                if not line.startswith(DECRYPT_EVENT_PREFIX):
                    return
                try:
                    event = json.loads(line[len(DECRYPT_EVENT_PREFIX) :])
                except ValueError:
                    return
                kind = event.get("type")
                if kind == "progress":
                    state["done"] = int(event.get("done") or 0)
                    total = int(event.get("total") or 0)
                    if total > 0:
                        await _notify(
                            callback,
                            {
                                "type": "progress",
                                "percent": min(100.0, state["done"] * 100 / total) * DECRYPT_SHARE / 100,
                                "message": "Decrypting the rescued movie with MakeMKV's library",
                            },
                        )
                elif kind == "done":
                    state["finished"] = event
                elif kind == "error":
                    state["error"] = str(event.get("message") or "")

            # Opening the library takes MakeMKV a minute or two before the first progress line.
            result = await self.runner.run(
                job_id, command, cwd=cwd, timeout=timeout, no_output_timeout=900, on_line=on_line
            )
            if "finished" in state:
                return state["finished"]
            if result.cancelled or result.timed_out:
                raise ProcessFailure("Decrypting the rescued movie was stopped", result)
            if result.return_code == EXIT_CANNOT_OPEN:
                raise BlurayCopyError(state.get("error") or "MakeMKV's library could not open the rescued disc files")
            # Units already decrypted stay decrypted, so another run continues where this one stopped.
            reached = int(state.get("done") or 0)
            if reached <= furthest:
                break
            furthest = reached
        raise ProcessFailure("MakeMKV's library keeps stopping while decrypting the rescued movie", result)

    async def copy(
        self,
        job_id: str,
        folder: Path,
        keys: Path,
        playlist: str,
        destination: Path,
        timeout: int,
        callback: ProgressCallback | None = None,
    ) -> Path:
        """Decrypt the movie of ``playlist`` in ``folder`` in place and copy it into ``destination``.

        ``keys`` is the folder from ``build_key_folder`` that MakeMKV's library opens.
        """
        for tool in (self.ffmpeg_path, self.ffprobe_path):
            if not tool or not Path(tool).is_file():
                raise FileNotFoundError("FFmpeg is required to copy the movie past the damaged spots")
        if not self.aacs_library or not Path(f"{self.aacs_library}.dll").is_file():
            raise FileNotFoundError("MakeMKV's libmmbd library is required to copy the movie past the damaged spots")
        playlist_path = find_path(folder, "BDMV", "PLAYLIST", playlist)
        if playlist_path is None:
            raise BlurayCopyError(f"The rescued disc files have no playlist {playlist}")
        data = await asyncio.to_thread(playlist_path.read_bytes)
        try:
            items = mpls_play_items(data)
            streams = mpls_streams(data)
            chapters = mpls_chapters(data)
        except OpticalError as error:
            raise BlurayCopyError(f"The movie's playlist cannot be read: {error}") from error
        if not items:
            raise BlurayCopyError(f"The playlist {playlist} plays nothing")
        duration = sum(max(0.0, end - start) for _, start, end in items)
        clips: dict[str, Path] = {}
        for name, _, _ in items:
            clip = find_path(folder, "BDMV", "STREAM", f"{name}.m2ts")
            if clip is None:
                raise BlurayCopyError(f"The rescued disc files have no clip {name}.m2ts")
            clips[name] = clip

        await self.decrypt(job_id, keys, list(clips.values()), timeout, callback)

        destination.mkdir(parents=True, exist_ok=True)
        first = next(iter(clips.values()))
        listing = destination / "clips.ffconcat"
        if len(items) == 1:
            source = ["-i", str(first)]
        else:
            listing.write_text(
                concat_listing([(clips[name], start, end) for name, start, end in items]), encoding="utf-8"
            )
            source = ["-f", "concat", "-safe", "0", "-i", str(listing)]
        info = await asyncio.to_thread(self._probe, job_id, ["-show_entries", "stream=id", str(first)])
        present: set[int] = set()
        for stream in info.get("streams") or []:
            try:
                present.add(int(str(stream.get("id")), 0))
            except ValueError:
                continue
        if not present:
            raise BlurayCopyError("FFmpeg could not read the decrypted movie")
        mapped = [stream for stream in streams if stream.pid in present]
        if not any(stream.kind == "video" for stream in mapped):
            mapped = []
        partial = destination / f"{Path(OUTPUT_NAME).stem}.part.mkv"
        metadata = destination / "chapters.ffmetadata" if chapters else None
        if metadata is not None:
            metadata.write_text(chapter_metadata(chapters, duration), encoding="utf-8")
        # FFmpeg reports no time while some subtitle stream has no packet yet; the bytes written still count up.
        clip_bytes = sum(clip.stat().st_size for clip in clips.values())

        async def on_line(line: str) -> None:
            key, _, value = line.partition("=")
            try:
                if key == "out_time_us" and duration > 0:
                    percent = int(value) / 1_000_000 * 100 / duration
                elif key == "total_size" and clip_bytes > 0:
                    percent = int(value) * 100 / clip_bytes
                else:
                    return
            except ValueError:
                return
            await _notify(
                callback,
                {
                    "type": "progress",
                    "percent": DECRYPT_SHARE + max(0.0, min(99.0, percent)) * (100 - DECRYPT_SHARE) / 100,
                    "message": "Copying the movie past the damaged spots",
                },
            )

        try:
            result = await self.runner.run(
                job_id,
                copy_arguments(self.ffmpeg_path, source, partial, mapped, metadata),
                timeout=timeout,
                no_output_timeout=600,
                on_line=on_line,
            )
        finally:
            for helper_file in (metadata, listing):
                if helper_file is not None:
                    helper_file.unlink(missing_ok=True)
        if result.cancelled or result.timed_out or result.return_code != 0:
            errors = [line for line in result.lines if line.strip() and not _PROGRESS_LINE.match(line)]
            raise ProcessFailure(f"FFmpeg could not copy the movie. {' '.join(errors[-3:])}".strip(), result)
        info = await asyncio.to_thread(self._probe, job_id, ["-show_entries", "format=duration", str(partial)])
        try:
            copied = float((info.get("format") or {}).get("duration") or 0)
        except ValueError:
            copied = 0.0
        if duration > 0 and copied < duration * MIN_COPIED_SHARE:
            raise ProcessFailure(
                f"FFmpeg copied only {copied / 60:.0f} of {duration / 60:.0f} minutes of the movie", result
            )
        target = destination / OUTPUT_NAME
        os.replace(partial, target)
        return target
