"""Loading screens for the moments a damaged disc could not be read.

A movie rescued from a damaged disc has holes where no video was readable, and
players freeze on the last picture until the video continues. DiscDock can put
an animated loading screen over each longer hole: it says the disc is damaged
there, shows the movie time where the film continues, counts down to it, and
adds chapter marks so a player's "next chapter" button skips straight past it.

Only a few seconds around each hole are encoded again, in the movie's own
video format. Everything else is copied bit for bit, so the rest of the movie
keeps its original quality and a Blu-ray takes minutes instead of hours.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import shutil
import uuid
from bisect import bisect_left, bisect_right
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from fractions import Fraction
from itertools import pairwise
from pathlib import Path
from typing import Any

from .processes import ProcessFailure, ProcessRunner, run_capture

LOADING_SCREEN_MIN_SECONDS = 2.0
BACKGROUND_COLOR = "0x0B1416"
ACCENT_COLOR = "0x40DDC6"
MUTED_COLOR = "0x9FB3B8"
SPINNER_DOTS = 8
FONT_DIRECTORY = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
REGULAR_FONTS = ("segoeui.ttf", "arial.ttf")
BOLD_FONTS = ("segoeuib.ttf", "arialbd.ttf")
# Video formats DiscDock can encode short pieces in that play on inside the copied movie.
SPLICE_CODECS = frozenset({"mpeg2video", "h264"})
# A copied stretch shorter than this between two damaged moments is encoded with them.
MIN_COPY_SECONDS = 2.0
# Characters the packet filter and cut points may use on one FFmpeg command line.
MAX_COPY_ARGUMENTS = 12000
# Filter graphs longer than this are passed to FFmpeg in a file.
MAX_INLINE_FILTER_GRAPH = 6000
# The encoded pieces use their own H.264 parameter set number, so the decoder
# keeps the movie's own parameter sets for the copied video that follows.
SPLICE_H264_PARAMETER_SET_ID = 31
UNREPAIRED_SUFFIX_LOADING_SCREENS = " - without loading screens"
UNREPAIRED_SUFFIX_AI = " - without AI frames"
UNREPAIRED_SUFFIXES = (UNREPAIRED_SUFFIX_LOADING_SCREENS, UNREPAIRED_SUFFIX_AI)

ProgressCallback = Callable[[dict], Awaitable[None] | None]


class SpliceUnavailable(RuntimeError):
    """The movie cannot be updated piece by piece; it has to be encoded as a whole."""


def is_unrepaired_copy(path: Path) -> bool:
    """Whether a movie file is the copy kept without loading screens or AI frames."""
    return path.stem.endswith(UNREPAIRED_SUFFIXES)


def unrepaired_copy_path(movie: Path, suffix: str = UNREPAIRED_SUFFIX_LOADING_SCREENS) -> Path:
    return movie.with_name(movie.stem + suffix + movie.suffix)


def format_timestamp(seconds: float, *, round_up: bool = True) -> str:
    """Movie time as HH:MM:SS, by default rounded up so "skip to" lands after the damage."""
    total = max(0, math.ceil(seconds - 1e-6) if round_up else math.floor(seconds))
    hours, rest = divmod(total, 3600)
    minutes, remainder = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{remainder:02d}"


def disc_display_name(disc_type: str) -> str:
    return {"dvd": "DVD", "bluray": "Blu-ray"}.get(str(disc_type), "disc")


def loading_screen_worthy(moment: dict[str, Any]) -> bool:
    return float(moment["end_seconds"]) - float(moment["start_seconds"]) >= LOADING_SCREEN_MIN_SECONDS


def _font(names: tuple[str, ...]) -> str:
    for name in names:
        path = FONT_DIRECTORY / name
        if path.is_file():
            return path.as_posix()
    return ""


def _quoted(value: str) -> str:
    """Quote a filter option value: FFmpeg removes the quotes, then unescapes ':'."""
    return "'" + value.replace("'", "").replace(":", "\\:") + "'"


def display_width(width: int, sar: float) -> int:
    return max(2, round(width * (sar or 1.0) / 2) * 2)


def loading_screen_filter(
    *,
    width: int,
    height: int,
    sar: float,
    sar_text: str,
    duration: float,
    resume_at: float,
    disc: str,
    regular_font: str = "",
    bold_font: str = "",
) -> str:
    """Draw the loading screen on a canvas with the movie's display shape.

    Drawing at display shape keeps text and spinner round on anamorphic DVDs;
    the result is then scaled back to the movie's own storage size and pixel shape.
    """
    height_f = float(height)
    regular = f"fontfile={_quoted(regular_font)}:" if regular_font else ""
    bold = f"fontfile={_quoted(bold_font)}:" if bold_font else regular
    canvas_width = display_width(width, sar)
    filters: list[str] = []
    center_x = canvas_width / 2
    center_y = height_f * 0.32
    radius = height_f * 0.085
    dot_size = max(10, round(height_f * 0.09))
    for index in range(SPINNER_DOTS):
        angle = 2 * math.pi * index / SPINNER_DOTS - math.pi / 2
        x = center_x + radius * math.cos(angle)
        y = center_y + radius * math.sin(angle)
        # The bright dot circles once a second and fades out behind itself.
        alpha = f"0.2+0.8*pow(1-mod(t*{SPINNER_DOTS}-{index},{SPINNER_DOTS})/{SPINNER_DOTS},3)"
        filters.append(
            f"drawtext={regular}text={_quoted(chr(0x2022))}:fontsize={dot_size}:fontcolor=white:"
            f"alpha={_quoted(alpha)}:x={x:.1f}-text_w/2:y={y:.1f}-text_h/2"
        )
    remaining = f"max(0,{duration:.3f}-t)"
    countdown = f"%{{eif:floor({remaining}/60):d}}:%{{eif:mod(floor({remaining}),60):d:2}}"
    lines = (
        (f"The {disc} is scratched here", bold, 0.052, "white", 0.52),
        (f"Skip to {format_timestamp(resume_at)}", bold, 0.08, ACCENT_COLOR, 0.61),
        (f"The movie continues in {countdown}", regular, 0.042, MUTED_COLOR, 0.74),
    )
    for text, font, size, color, top in lines:
        filters.append(
            f"drawtext={font}text={_quoted(text)}:fontsize={max(12, round(height_f * size))}:"
            f"fontcolor={color}:x=(w-text_w)/2:y={height_f * top:.1f}"
        )
    filters.extend([f"scale={width}:{height}", f"setsar={sar_text.replace(':', '/')}", "format=yuv420p"])
    return ",".join(filters)


def _escape_metadata(value: str) -> str:
    for character in ("\\", "=", ";", "#", "\n"):
        value = value.replace(character, "\\" + character)
    return value


def build_chapters(existing: list[dict[str, Any]], screens: list[dict[str, Any]], duration: float) -> str:
    """FFMETADATA chapters: the disc's own chapters plus a mark around every loading screen."""
    marks: list[tuple[float, str]] = []
    for moment in screens:
        start, end = float(moment["start_seconds"]), float(moment["end_seconds"])
        marks.append((start, f"Disc damage - the movie continues at {format_timestamp(end)}"))
        marks.append((end, "Movie continues"))
    for chapter in existing:
        start = float(chapter.get("start_time") or 0)
        title = str((chapter.get("tags") or {}).get("title") or "").strip() or "Chapter"
        # A disc chapter right at a loading screen edge would only duplicate it.
        if not any(abs(start - mark) < 1.0 for mark, _ in marks):
            marks.append((start, title))
    marks = sorted((start, title) for start, title in marks if 0 <= start < duration)
    lines = [";FFMETADATA1"]
    for index, (start, title) in enumerate(marks):
        end = marks[index + 1][0] if index + 1 < len(marks) else duration
        lines.extend(
            [
                "[CHAPTER]",
                "TIMEBASE=1/1000",
                f"START={round(start * 1000)}",
                f"END={max(round(start * 1000) + 1, round(end * 1000))}",
                f"title={_escape_metadata(title)}",
            ]
        )
    return "\n".join(lines) + "\n"


def read_chapters(source: Path, ffprobe_path: str, owner: str = "") -> list[dict[str, Any]] | None:
    """The movie's chapters, or None when they cannot be read (then they are copied as they are)."""
    if not ffprobe_path or not Path(ffprobe_path).is_file():
        return None
    code, stdout, _ = run_capture(
        [ffprobe_path, "-v", "error", "-show_chapters", "-of", "json", str(source)], timeout=120, owner=owner
    )
    if code != 0:
        return None
    try:
        return list(json.loads(stdout or "{}").get("chapters") or [])
    except ValueError:
        return None


def _fraction(value: str, default: Fraction = Fraction(0)) -> Fraction:
    try:
        numerator, _, denominator = str(value).replace(":", "/").partition("/")
        result = Fraction(int(numerator), int(denominator or 1))
        return result if result > 0 else default
    except (ValueError, ZeroDivisionError):
        return default


def probe_video_stream(source: Path, ffprobe_path: str, owner: str = "") -> dict[str, Any]:
    """Format details of a movie's video that a matching encoder needs."""
    code, stdout, stderr = run_capture(
        [
            ffprobe_path,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            (
                "stream=codec_name,profile,pix_fmt,width,height,avg_frame_rate,r_frame_rate,time_base,"
                "sample_aspect_ratio,field_order,color_range,color_space,color_transfer,color_primaries"
                ":format=duration"
            ),
            "-of",
            "json",
            str(source),
        ],
        timeout=300,
        owner=owner,
    )
    if code != 0:
        raise RuntimeError(f"FFprobe could not inspect the movie: {stderr.strip()[-300:]}")
    payload = json.loads(stdout or "{}")
    streams = payload.get("streams") or []
    if not streams:
        raise RuntimeError("The movie has no video stream")
    stream = streams[0]
    fps_text = str(stream.get("avg_frame_rate") or "0/0")
    fps = _fraction(fps_text)
    if not 1 <= fps <= 120:
        fps_text = str(stream.get("r_frame_rate") or "0/0")
        fps = _fraction(fps_text)
    if not 1 <= fps <= 120:
        raise RuntimeError("DiscDock could not determine the movie frame rate")
    return {
        "codec": str(stream.get("codec_name") or ""),
        "profile": str(stream.get("profile") or ""),
        "pix_fmt": str(stream.get("pix_fmt") or ""),
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "fps": float(fps),
        "fps_text": fps_text,
        "time_base": _fraction(str(stream.get("time_base") or "1/1000"), Fraction(1, 1000)),
        "sar_text": str(stream.get("sample_aspect_ratio") or "1:1"),
        "field_order": str(stream.get("field_order") or "unknown"),
        "color_range": str(stream.get("color_range") or ""),
        "color_space": str(stream.get("color_space") or ""),
        "color_transfer": str(stream.get("color_transfer") or ""),
        "color_primaries": str(stream.get("color_primaries") or ""),
        "duration_seconds": float((payload.get("format") or {}).get("duration") or 0),
    }


def probe_packets(source: Path, ffprobe_path: str, owner: str = "") -> list[tuple[int, bool]]:
    """Presentation time (in the stream's time base) and keyframe flag of every video packet, in file order."""
    code, stdout, stderr = run_capture(
        [ffprobe_path, "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts,flags", "-of", "csv=p=0", str(source)],
        timeout=3 * 3600,
        owner=owner,
    )
    if code != 0:
        raise RuntimeError(f"FFprobe could not list the movie's video packets: {stderr.strip()[-300:]}")
    packets: list[tuple[int, bool]] = []
    for line in stdout.splitlines():
        value, _, flags = line.strip().partition(",")
        try:
            packets.append((int(value), "K" in flags))
        except ValueError:
            continue
    return packets


def _stream_counts(path: Path, ffprobe_path: str, owner: str = "") -> dict[str, int]:
    code, stdout, _ = run_capture(
        [ffprobe_path, "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
        timeout=300,
        owner=owner,
    )
    counts: dict[str, int] = {}
    if code == 0:
        for line in stdout.splitlines():
            kind = line.strip().strip(",")
            if kind:
                counts[kind] = counts.get(kind, 0) + 1
    return counts


def _first_packet_time(path: Path, ffprobe_path: str, owner: str = "") -> float:
    _, stdout, stderr = run_capture(
        [
            ffprobe_path,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-read_intervals",
            "%+#1",
            "-show_entries",
            "packet=pts_time",
            "-of",
            "csv=p=0",
            str(path),
        ],
        timeout=120,
        owner=owner,
    )
    try:
        return float(stdout.strip().splitlines()[0].strip(","))
    except (IndexError, ValueError) as error:
        raise RuntimeError(f"FFprobe could not read the first packet of {path.name}: {stderr.strip()[-300:]}") from error


def _keyframe_times(path: Path, ffprobe_path: str, owner: str = "", seconds: float = 6.0) -> list[float]:
    """Presentation times of the keyframes in the first seconds of a movie file."""
    code, stdout, _ = run_capture(
        [
            ffprobe_path,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-read_intervals",
            f"%+{seconds:g}",
            "-show_entries",
            "packet=pts_time,flags",
            "-of",
            "csv=p=0",
            str(path),
        ],
        timeout=300,
        owner=owner,
    )
    times: list[float] = []
    if code == 0:
        for line in stdout.splitlines():
            value, _, flags = line.strip().partition(",")
            try:
                if "K" in flags:
                    times.append(float(value))
            except ValueError:
                continue
    return times


def _start_time(path: Path, ffprobe_path: str, owner: str = "") -> float:
    code, stdout, stderr = run_capture(
        [ffprobe_path, "-v", "error", "-show_entries", "format=start_time,duration", "-of", "json", str(path)],
        timeout=300,
        owner=owner,
    )
    if code != 0:
        raise RuntimeError(f"FFprobe could not read {path.name}: {stderr.strip()[-300:]}")
    value = (json.loads(stdout or "{}").get("format") or {}).get("start_time")
    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"{path.name} has no start time") from error


@dataclass
class SpliceWindow:
    """A stretch of the movie that is encoded again: from the first picture of a
    keyframe's group up to the keyframe where the copied movie resumes."""

    start_pts: int
    end_pts: int | None
    seek_seconds: float
    patches: list[dict[str, Any]] = field(default_factory=list)


def _display_start(packets: list[tuple[int, bool]], index: int) -> int:
    """Earliest picture a keyframe's group shows: pictures stored after the
    keyframe but shown before it depend on the group before, so they go with it."""
    pts = packets[index][0]
    lowest = pts
    for later, _ in packets[index + 1 : index + 65]:
        if later > pts:
            break
        lowest = min(lowest, later)
    return lowest


def plan_splice(
    packets: list[tuple[int, bool]], time_base: Fraction, fps: float, patches: list[dict[str, Any]]
) -> list[SpliceWindow]:
    """Choose the stretches to encode again so every copied packet keeps its references.

    Pictures shown before a keyframe but stored after it are left out of the
    copy that starts there, so the copied movie never needs a picture that was
    replaced. Everything shown before the first re-encoded picture is stored
    before its keyframe and is copied unchanged.
    """
    tick = float(time_base)
    if not packets or tick <= 0 or fps <= 0:
        raise SpliceUnavailable("The movie's video packets could not be listed")
    keys = sorted((pts, index) for index, (pts, key) in enumerate(packets) if key)
    if not keys:
        raise SpliceUnavailable("The movie has no keyframes")
    key_pts = [pts for pts, _ in keys]
    quarter = 0.25 / fps
    first_display = min(pts for pts, _ in packets[: min(len(packets), 64)])
    windows: list[SpliceWindow] = []
    for patch in sorted(patches, key=lambda item: float(item["start"])):
        start_ticks = round((float(patch["start"]) - quarter) / tick)
        end_ticks = round((float(patch["end"]) - quarter) / tick)
        before = bisect_right(key_pts, start_ticks) - 1
        if before >= 0:
            start_pts = _display_start(packets, keys[before][1])
            seek = key_pts[before - 1] * tick if before >= 1 else 0.0
        else:
            start_pts, seek = first_display, 0.0
        after = bisect_left(key_pts, max(end_ticks, start_pts + 1))
        end_pts = key_pts[after] if after < len(key_pts) else None
        windows.append(SpliceWindow(start_pts, end_pts, max(0.0, seek), [patch]))
    return merge_windows(windows, MIN_COPY_SECONDS / tick)


def merge_windows(windows: list[SpliceWindow], min_copy_ticks: float) -> list[SpliceWindow]:
    """Join encoded stretches separated by less than ``min_copy_ticks`` of copied video."""
    merged: list[SpliceWindow] = []
    for window in sorted(windows, key=lambda item: item.start_pts):
        previous = merged[-1] if merged else None
        if previous is not None and (previous.end_pts is None or window.start_pts - previous.end_pts < min_copy_ticks):
            previous.end_pts = (
                None if previous.end_pts is None or window.end_pts is None else max(previous.end_pts, window.end_pts)
            )
            previous.seek_seconds = min(previous.seek_seconds, window.seek_seconds)
            previous.patches.extend(window.patches)
        else:
            merged.append(SpliceWindow(window.start_pts, window.end_pts, window.seek_seconds, list(window.patches)))
    return merged


def fit_windows(windows: list[SpliceWindow], tick: float) -> list[SpliceWindow]:
    """Merge nearby stretches until the copy command stays well inside Windows' 32,767-character limit.

    A disc with scattered damage can have hundreds of damaged moments; each
    one adds to the packet filter and the list of cut points.
    """
    seconds = MIN_COPY_SECONDS
    while len(windows) > 1 and len(drop_expression(windows)) + 12 * len(windows) > MAX_COPY_ARGUMENTS:
        seconds *= 2
        windows = merge_windows(windows, seconds / tick)
    return windows


def copy_start_times(packets: list[tuple[int, bool]], windows: list[SpliceWindow]) -> list[int]:
    """Presentation time of the keyframe each copied stretch around the encoded ones starts with, in movie order."""
    spans: list[tuple[float, float]] = []
    low: float = -math.inf
    for window in windows:
        spans.append((low, window.start_pts))
        low = math.inf if window.end_pts is None else window.end_pts
        if window.end_pts is None:
            break
    spans.append((low, math.inf))
    starts: list[int] = []
    for span_low, span_high in spans:
        # A copy starts with its first keyframe in file order; FFmpeg drops what comes before it.
        first = next((pts for pts, key in packets if key and span_low <= pts < span_high), None)
        if first is not None:
            starts.append(first)
    return starts


def drop_expression(windows: list[SpliceWindow]) -> str:
    """A noise bitstream filter expression that drops every packet shown inside an encoded stretch."""
    parts = [
        f"gte(pts\\,{window.start_pts})" + ("" if window.end_pts is None else f"*lt(pts\\,{window.end_pts})")
        for window in windows
    ]
    return "+".join(parts)


def window_pieces(
    patches: list[dict[str, Any]], fps: float, start: float, end: float | None
) -> tuple[list[tuple[float, float | None]], list[tuple[str, int]], list[tuple[Path, int]]]:
    """Split one re-encoded stretch into pieces of the movie and clips, in playing order.

    Damage leaves pictures at uneven distances, so what is left of the movie
    between the stretch's edge and a clip can be shorter than a single picture.
    FFmpeg finds nothing to encode in such a sliver and writes an empty file, so
    the clip next to it covers those milliseconds instead.
    """
    ranges: list[tuple[float, float | None]] = []
    order: list[tuple[str, int]] = []
    clips: list[tuple[Path, int]] = []
    cursor = start
    for patch in sorted(patches, key=lambda item: float(item["start"])):
        patch_start, patch_end = float(patch["start"]), float(patch["end"])
        if patch_start - cursor >= 1.0 / fps:
            ranges.append((cursor, patch_start))
            order.append(("source", len(ranges) - 1))
        else:
            patch_start = cursor
        clips.append((Path(str(patch["clip"])), max(1, round((patch_end - patch_start) * fps))))
        order.append(("clip", len(clips) - 1))
        cursor = max(cursor, patch_end)
    if end is None or end - cursor >= 1.0 / fps:
        ranges.append((cursor, end))
        order.append(("source", len(ranges) - 1))
    elif clips:
        clip, frames = clips[-1]
        clips[-1] = (clip, frames + max(1, round((end - cursor) * fps)))
    return ranges, order, clips


def splice_encoder_args(info: dict[str, Any]) -> list[str]:
    """Encoder settings for pieces that decode as part of the copied movie."""
    codec = str(info.get("codec") or "")
    if codec not in SPLICE_CODECS:
        raise SpliceUnavailable(f"Pieces cannot be encoded in the movie's {codec or 'unknown'} video format")
    if str(info.get("pix_fmt") or "") != "yuv420p":
        raise SpliceUnavailable(f"The movie uses the {info.get('pix_fmt') or 'unknown'} pixel format")
    fps = float(info["fps"])
    field_order = str(info.get("field_order") or "")
    interlaced = field_order in {"tt", "tb", "bb", "bt"}
    # The encoder takes the frame rate from the filter graph; -r would force a constant-rate resync.
    args = ["-fps_mode", "passthrough", "-pix_fmt", "yuv420p"]
    for key, option in (
        ("color_primaries", "-color_primaries"),
        ("color_transfer", "-color_trc"),
        ("color_space", "-colorspace"),
        ("color_range", "-color_range"),
    ):
        value = str(info.get(key) or "")
        if key == "color_transfer":
            # FFprobe prints these transfer names, but the encoder option only knows the gamma names.
            value = {"bt470bg": "gamma28", "bt470m": "gamma22"}.get(value, value)
        if value and value not in {"unknown", "reserved", "unspecified"}:
            args.extend([option, value])
    top = ["-top", "1" if field_order in {"tt", "tb"} else "0"] if interlaced else []
    if codec == "mpeg2video":
        return [
            *args,
            "-c:v",
            "mpeg2video",
            "-q:v",
            "2",
            "-qmin",
            "1",
            "-g",
            str(max(6, round(fps / 2))),
            "-bf",
            "2",
            *(["-flags", "+ildct+ilme"] if interlaced else []),
            *top,
        ]
    return [
        *args,
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "14",
        "-profile:v",
        "high",
        "-g",
        str(max(12, round(fps))),
        "-bf",
        "3",
        "-x264-params",
        f"sps-id={SPLICE_H264_PARAMETER_SET_ID}:repeat-headers=1",
        *(["-flags", "+ildct+ilme"] if interlaced else []),
        *top,
    ]


def _concat_line(path: Path) -> str:
    return "file '" + path.resolve().as_posix().replace("'", "'\\''") + "'"


def decode_problems(
    path: Path, ffprobe_path: str, intervals: list[tuple[float, float]], fps: float, owner: str = ""
) -> list[float]:
    """Times of pictures inside ``intervals`` that decode with errors, or that are missing."""
    if not intervals:
        return []
    lead = 1.5
    code, stdout, stderr = run_capture(
        [
            ffprobe_path,
            "-v",
            "error",
            "-threads",
            "1",
            "-show_log",
            "24",
            "-select_streams",
            "v:0",
            "-show_entries",
            "frame=best_effort_timestamp_time:log=message",
            "-of",
            "json",
            "-read_intervals",
            ",".join(f"{max(0.0, start - lead):.3f}%{end:.3f}" for start, end in intervals),
            str(path),
        ],
        timeout=1800,
        owner=owner,
    )
    if code != 0 and not stdout.strip():
        raise RuntimeError(f"FFprobe could not decode {path.name}: {stderr.strip()[-300:]}")
    frames = json.loads(stdout or "{}").get("frames") or []
    times: list[float] = []
    problems: list[float] = []
    for frame in frames:
        try:
            time = float(frame.get("best_effort_timestamp_time"))
        except (TypeError, ValueError):
            continue
        times.append(time)
        if frame.get("logs") and any(start <= time < end for start, end in intervals):
            problems.append(time)
    times.sort()
    for previous, current in pairwise(times):
        if current - previous > 2.5 / fps and any(start <= previous < end for start, end in intervals):
            problems.append(previous)
    return sorted(problems)


async def _notify(callback: ProgressCallback | None, event: dict) -> None:
    if callback is None:
        return
    response = callback(event)
    if asyncio.iscoroutine(response):
        await response


def starts_within_half_a_frame(first: float, wanted: float, fps: float) -> bool:
    """Whether an encoded stretch starts close enough to its planned moment to be spliced in.

    In a damaged stretch the pictures are not always on the frame grid, and the
    container rounds times to milliseconds. Less than half a frame off, the
    stretch still shows the same pictures at the same moments.
    """
    return abs(first - wanted) <= (0.5 / fps if fps > 0 else 0.0015)


class MoviePatcher:
    """Replace stretches of a movie with generated clips."""

    def __init__(self, ffmpeg_path: str, ffprobe_path: str, runner: ProcessRunner):
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path
        self.runner = runner

    async def _run_ffmpeg(self, job_id: str, args: list[str], *, timeout: int, callback=None) -> None:
        result = await self.runner.run(
            job_id,
            [self.ffmpeg_path, "-hide_banner", "-nostdin", *args],
            timeout=timeout,
            no_output_timeout=max(600, timeout),
            on_line=callback,
        )
        if result.return_code != 0 or result.cancelled:
            tail = " ".join(result.lines[-3:])
            raise ProcessFailure(f"FFmpeg could not update the movie. {tail}".strip(), result)

    async def render_loading_screen(
        self,
        job_id: str,
        target: Path,
        *,
        info: dict[str, Any],
        duration: float,
        resume_at: float,
        disc: str,
    ) -> None:
        width, height = int(info["width"]), int(info["height"])
        fps = float(info["fps"])
        sar_text = str(info.get("sar_text") or info.get("sample_aspect_ratio") or "1:1")
        fps_text = str(info.get("fps_text") or f"{fps:.6f}")
        graph = loading_screen_filter(
            width=width,
            height=height,
            sar=float(info.get("sar") or 1.0),
            sar_text=sar_text,
            duration=duration,
            resume_at=resume_at,
            disc=disc,
            regular_font=_font(REGULAR_FONTS),
            bold_font=_font(BOLD_FONTS),
        )
        canvas = display_width(width, float(info.get("sar") or 1.0))
        await self._run_ffmpeg(
            job_id,
            [
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c={BACKGROUND_COLOR}:s={canvas}x{height}:r={fps_text}",
                "-frames:v",
                str(max(1, round(duration * fps))),
                "-vf",
                graph,
                "-an",
                # Lossless: the clip is encoded once more in the movie's own format.
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-qp",
                "0",
                str(target),
            ],
            timeout=1800,
        )
        if not target.is_file():
            raise RuntimeError("FFmpeg did not create the loading screen")

    def _progress_reader(
        self,
        callback: ProgressCallback | None,
        *,
        start: float,
        span: float,
        seconds_total: float,
        message: str,
        offset: float = 0.0,
    ) -> Callable[[str], Awaitable[None]]:
        async def on_line(line: str) -> None:
            if not line.startswith("out_time_ms="):
                return
            try:
                seconds = int(line.split("=", 1)[1]) / 1_000_000 - offset
            except ValueError:
                return
            fraction = min(0.99, max(0.0, seconds / max(1.0, seconds_total)))
            await _notify(callback, {"type": "progress", "percent": start + span * fraction, "message": message})

        return on_line

    async def stitch(
        self,
        job_id: str,
        source: Path,
        patches: list[dict[str, Any]],
        output: Path,
        *,
        info: dict[str, Any],
        chapters_file: Path | None = None,
        progress_callback: ProgressCallback | None = None,
        progress_start: float = 0.0,
        progress_span: float = 100.0,
        message: str = "Updating the movie",
    ) -> str:
        """Write ``output``: the movie with each patch clip in place of its stretch.

        Returns "splice" when only the stretches around the patches were encoded
        again, or "reencode" when the whole video had to be encoded.
        """
        workdir = output.parent / f".discdock-splice-{uuid.uuid4().hex[:8]}"
        try:
            try:
                await self._splice(
                    job_id,
                    source,
                    patches,
                    output,
                    workdir,
                    chapters_file=chapters_file,
                    progress_callback=progress_callback,
                    progress_start=progress_start,
                    progress_span=progress_span,
                    message=message,
                )
                return "splice"
            except SpliceUnavailable as reason:
                await _notify(
                    progress_callback,
                    {"type": "log", "message": f"Encoding the whole video instead of only the damaged moments: {reason}"},
                )
            output.unlink(missing_ok=True)
            await asyncio.to_thread(shutil.rmtree, workdir, ignore_errors=True)
            await self._reencode(
                job_id,
                source,
                patches,
                output,
                info=info,
                chapters_file=chapters_file,
                progress_callback=progress_callback,
                progress_start=progress_start,
                progress_span=progress_span,
                message=message,
            )
            return "reencode"
        finally:
            await asyncio.to_thread(shutil.rmtree, workdir, ignore_errors=True)

    async def _splice(
        self,
        job_id: str,
        source: Path,
        patches: list[dict[str, Any]],
        output: Path,
        workdir: Path,
        *,
        chapters_file: Path | None,
        progress_callback: ProgressCallback | None,
        progress_start: float,
        progress_span: float,
        message: str,
    ) -> None:
        if not self.ffprobe_path or not Path(self.ffprobe_path).is_file():
            raise SpliceUnavailable("FFprobe is needed to find where the movie can be cut")
        stream = await asyncio.to_thread(probe_video_stream, source, self.ffprobe_path, job_id)
        encoder = splice_encoder_args(stream)
        free = shutil.disk_usage(output.parent).free
        if free < source.stat().st_size * 2 + 1024**3:
            raise SpliceUnavailable("There is not enough free disk space to update the movie in pieces")
        await _notify(progress_callback, {"type": "progress", "percent": progress_start, "message": message})
        packets = await asyncio.to_thread(probe_packets, source, self.ffprobe_path, job_id)
        fps = float(stream["fps"])
        time_base: Fraction = stream["time_base"]
        tick = float(time_base)
        windows = fit_windows(plan_splice(packets, time_base, fps, patches), tick)
        duration = max(1.0, float(stream["duration_seconds"]) or packets[-1][0] * tick)
        workdir.mkdir(parents=True, exist_ok=True)

        # 1. Copy the video outside the encoded stretches, split where each copy resumes.
        cuts = [window.end_pts * tick - 0.0005 for window in windows if window.end_pts is not None]
        copy_args = [
            "-y",
            "-copyts",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-c",
            "copy",
            "-bsf:v",
            f"noise=drop={drop_expression(windows)}",
            # MKV stores presentation times only. Without this, a first decode time
            # below zero makes FFmpeg move every timestamp of the piece forward.
            "-avoid_negative_ts",
            "disabled",
            "-f",
            "segment",
            "-segment_format",
            "matroska",
            "-segment_format_options",
            "avoid_negative_ts=disabled",
            "-reset_timestamps",
            "0",
            "-segment_list",
            str(workdir / "copies.csv"),
            "-segment_list_type",
            "csv",
        ]
        if cuts:
            copy_args.extend(["-segment_times", ",".join(f"{cut:.6f}" for cut in cuts)])
        copy_args.extend(["-progress", "pipe:1", "-nostats", str(workdir / "copy-%03d.mkv")])
        await self._run_ffmpeg(
            job_id,
            copy_args,
            timeout=6 * 3600,
            callback=self._progress_reader(
                progress_callback, start=progress_start, span=progress_span * 0.35, seconds_total=duration, message=message
            ),
        )
        names = [line.split(",", 1)[0].strip() for line in (workdir / "copies.csv").read_text(encoding="utf-8").splitlines()]
        copies = [workdir / name for name in names if name and (workdir / name).is_file()]
        first_pictures = await asyncio.to_thread(copy_start_times, packets, windows)
        if len(copies) != len(first_pictures):
            raise SpliceUnavailable(f"FFmpeg wrote {len(copies)} copied pieces instead of {len(first_pictures)}")
        expected = {piece: pts * tick for piece, pts in zip(copies, first_pictures, strict=True)}

        # 2. Encode each stretch with its patches, decoding from a keyframe before it.
        encoded: set[Path] = set()
        for number, window in enumerate(windows, start=1):
            target = workdir / f"window-{number:03d}.mkv"
            await self._encode_window(job_id, source, window, stream, encoder, target)
            expected[target] = window.start_pts * tick
            encoded.add(target)
            await _notify(
                progress_callback,
                {
                    "type": "progress",
                    "percent": progress_start + progress_span * (0.35 + 0.25 * number / len(windows)),
                    "message": message,
                },
            )

        # 3. Join copies and encoded stretches in time order, with the audio and subtitles of the source.
        # A container that moved the timestamps would put a piece at the wrong moment.
        source_keys = {pts for pts, key in packets if key}
        ordered: list[tuple[float, Path]] = []
        for piece, wanted in expected.items():
            if piece in encoded:
                first = await asyncio.to_thread(_first_packet_time, piece, self.ffprobe_path, job_id)
                if not starts_within_half_a_frame(first, wanted, fps):
                    raise SpliceUnavailable(f"{piece.name} starts at {first:.3f} s instead of {wanted:.3f} s")
            else:
                # Copied keyframes must keep the source's times. A damaged first picture may
                # have been given a new timestamp by FFmpeg, so one mismatch is allowed.
                keys = await asyncio.to_thread(_keyframe_times, piece, self.ffprobe_path, job_id)
                matched = sum(1 for time in keys if round(time / tick) in source_keys)
                if not keys or matched < max(1, len(keys) - 1):
                    raise SpliceUnavailable(f"The keyframes of {piece.name} moved away from the movie's own times")
            ordered.append((await asyncio.to_thread(_start_time, piece, self.ffprobe_path, job_id), piece))
        ordered.sort()
        lines = ["ffconcat version 1.0"]
        for index, (start, piece) in enumerate(ordered):
            lines.append(_concat_line(piece))
            if index + 1 < len(ordered):
                lines.append(f"duration {ordered[index + 1][0] - start:.6f}")
        playlist = workdir / "pieces.ffconcat"
        playlist.write_text("\n".join(lines) + "\n", encoding="utf-8")
        first_start = ordered[0][0]
        inputs = [
            "-y",
            "-copyts",
            "-itsoffset",
            f"{first_start:.6f}",
            "-f",
            "concat",
            "-safe",
            "0",
            "-auto_convert",
            "0",
            "-i",
            str(playlist),
            "-i",
            str(source),
        ]
        chapter_input = "1"
        if chapters_file is not None:
            inputs.extend(["-f", "ffmetadata", "-i", str(chapters_file)])
            chapter_input = "2"
        await self._run_ffmpeg(
            job_id,
            [
                *inputs,
                "-map",
                "0:v:0",
                "-map",
                "1:a?",
                "-map",
                "1:s?",
                "-map",
                "1:t?",
                "-map_chapters",
                chapter_input,
                "-map_metadata",
                "1",
                "-map_metadata:s:v:0",
                "1:s:v:0",
                "-c",
                "copy",
                "-avoid_negative_ts",
                "disabled",
                "-max_muxing_queue_size",
                "4096",
                "-progress",
                "pipe:1",
                "-nostats",
                str(output),
            ],
            timeout=6 * 3600,
            callback=self._progress_reader(
                progress_callback,
                start=progress_start + progress_span * 0.6,
                span=progress_span * 0.38,
                seconds_total=duration,
                message=message,
            ),
        )

        # 4. Check the result before anything uses it.
        await self._verify_splice(job_id, source, output, windows, stream)

    async def _encode_window(
        self,
        job_id: str,
        source: Path,
        window: SpliceWindow,
        stream: dict[str, Any],
        encoder: list[str],
        target: Path,
    ) -> None:
        fps = float(stream["fps"])
        tick = float(stream["time_base"])
        quarter = 0.25 / fps
        start = window.start_pts * tick
        end = None if window.end_pts is None else window.end_pts * tick
        sar = str(stream.get("sar_text") or "1:1").replace(":", "/")
        normalize = f"format=yuv420p,setsar={sar}"
        ranges, order, clips = window_pieces(window.patches, fps, start, end)
        inputs: list[str] = ["-y", "-copyts"]
        filters: list[str] = []
        clip_offset = 0
        if ranges:
            inputs.extend(["-ss", f"{window.seek_seconds:.6f}", "-i", str(source)])
            clip_offset = 1
            filters.append("[0:v]split=" + str(len(ranges)) + "".join(f"[s{index}]" for index in range(len(ranges))))
            for index, (low, high) in enumerate(ranges):
                bounds = f"start={low - quarter:.6f}" + ("" if high is None else f":end={high - quarter:.6f}")
                filters.append(f"[s{index}]trim={bounds},setpts=PTS-STARTPTS,{normalize}[o{index}]")
        for index, (clip, frames) in enumerate(clips):
            inputs.extend(["-i", str(clip)])
            # Exactly as many frames as the stretch it replaces, so the copy after it lines up.
            filters.append(
                f"[{index + clip_offset}:v]setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop={frames},"
                f"trim=end_frame={frames},{normalize}[c{index}]"
            )
        labels = "".join(f"[o{index}]" if kind == "source" else f"[c{index}]" for kind, index in order)
        tail = f",trim=end={end - quarter:.6f}" if end is not None else ""
        filters.append(
            f"{labels}concat=n={len(order)}:v=1:a=0,settb=AVTB,setpts=PTS+{round(start * 1_000_000)}{tail}[vout]"
        )
        if start < 1.0:
            # Reordered frames would need timestamps below zero, which the container shifts.
            encoder = [*encoder, "-bf", "0"]
        graph = ";".join(filters)
        graph_args = ["-filter_complex", graph]
        if len(graph) > MAX_INLINE_FILTER_GRAPH:
            # Many loading screens in one stretch: keep the command line short.
            script = target.with_suffix(".filtergraph")
            script.write_text(graph, encoding="utf-8")
            graph_args = ["-/filter_complex", str(script)]
        if sum(len(argument) + 3 for argument in inputs) > 24000:
            raise SpliceUnavailable("Too many loading screens lie too close together for one FFmpeg command")
        await self._run_ffmpeg(
            job_id,
            [
                *inputs,
                *graph_args,
                "-map",
                "[vout]",
                "-an",
                "-sn",
                "-dn",
                *encoder,
                "-avoid_negative_ts",
                "disabled",
                str(target),
            ],
            timeout=4 * 3600,
        )
        if not target.is_file() or target.stat().st_size == 0:
            raise RuntimeError("FFmpeg did not encode the damaged stretch")

    async def _verify_splice(
        self,
        job_id: str,
        source: Path,
        output: Path,
        windows: list[SpliceWindow],
        stream: dict[str, Any],
    ) -> None:
        if not output.is_file() or output.stat().st_size < 1024 * 1024:
            raise RuntimeError("FFmpeg did not produce a usable movie")
        tick = float(stream["time_base"])
        fps = float(stream["fps"])
        result = await asyncio.to_thread(probe_video_stream, output, self.ffprobe_path, job_id)
        if abs(float(result["duration_seconds"]) - float(stream["duration_seconds"])) > 2.0:
            raise SpliceUnavailable(
                f"The joined movie runs {result['duration_seconds']:.1f} s instead of {stream['duration_seconds']:.1f} s"
            )
        source_streams, output_streams = await asyncio.gather(
            asyncio.to_thread(_stream_counts, source, self.ffprobe_path, job_id),
            asyncio.to_thread(_stream_counts, output, self.ffprobe_path, job_id),
        )
        if source_streams != output_streams:
            raise SpliceUnavailable(f"The joined movie has streams {output_streams} instead of {source_streams}")
        patched = [(float(patch["start"]), float(patch["end"])) for window in windows for patch in window.patches]
        joints: list[float] = []
        for window in windows:
            joints.append(window.start_pts * tick)
            if window.end_pts is not None:
                joints.append(window.end_pts * tick)
        intervals = [(max(0.0, joint - 1.0), joint + 2.5) for joint in joints]
        found = await asyncio.to_thread(decode_problems, output, self.ffprobe_path, intervals, fps, job_id)
        found = [time for time in found if not any(start - 0.1 <= time < end + 0.1 for start, end in patched)]
        if not found:
            return
        # The damaged source may already stumble right there; only new problems count.
        known = await asyncio.to_thread(decode_problems, source, self.ffprobe_path, intervals, fps, job_id)
        new = [time for time in found if not any(abs(time - other) <= 2.5 / fps for other in known)]
        if new:
            listing = ", ".join(format_timestamp(time, round_up=False) for time in new[:5])
            raise SpliceUnavailable(f"The joined movie does not play cleanly at {listing}")

    async def _reencode(
        self,
        job_id: str,
        source: Path,
        patches: list[dict[str, Any]],
        output: Path,
        *,
        info: dict[str, Any],
        chapters_file: Path | None,
        progress_callback: ProgressCallback | None,
        progress_start: float,
        progress_span: float,
        message: str,
    ) -> None:
        """Encode the whole video with each patch clip in place of its stretch; audio and subtitles are copied."""
        if shutil.disk_usage(output.parent).free < source.stat().st_size * 1.2 + 1024**3:
            raise RuntimeError("There is not enough free disk space to update the movie")
        sar = str(info.get("sar_text") or info.get("sample_aspect_ratio") or "1:1").replace(":", "/")
        # Every piece must share pixel format, pixel shape and time base for concat.
        normalize = f"format=yuv420p,setsar={sar},settb=AVTB"
        ordered = sorted(patches, key=lambda patch: float(patch["start"]))
        filter_parts: list[str] = []
        concat_inputs: list[str] = []
        cursor = 0.0
        for offset, patch in enumerate(ordered, start=1):
            start, end = float(patch["start"]), float(patch["end"])
            if start > cursor:
                label = f"source{offset}"
                filter_parts.append(
                    f"[0:v]trim=start={cursor:.6f}:end={start:.6f},setpts=PTS-STARTPTS,{normalize}[{label}]"
                )
                concat_inputs.append(f"[{label}]")
            filter_parts.append(f"[{offset}:v]setpts=PTS-STARTPTS,{normalize}[patch{offset}]")
            concat_inputs.append(f"[patch{offset}]")
            cursor = end
        filter_parts.append(f"[0:v]trim=start={cursor:.6f},setpts=PTS-STARTPTS,{normalize}[sourcetail]")
        concat_inputs.append("[sourcetail]")
        filter_parts.append("".join(concat_inputs) + f"concat=n={len(concat_inputs)}:v=1:a=0[vout]")
        inputs: list[str] = []
        for patch in ordered:
            inputs.extend(["-i", str(patch["clip"])])
        chapter_args = ["-map_chapters", "0"]
        if chapters_file is not None:
            inputs.extend(["-f", "ffmetadata", "-i", str(chapters_file)])
            chapter_args = ["-map_chapters", str(len(ordered) + 1)]
        field_order = str(info.get("field_order") or "")
        interlace: list[str] = []
        if field_order in {"tt", "tb"}:
            interlace = ["-flags", "+ildct+ilme", "-top", "1"]
        elif field_order in {"bb", "bt"}:
            interlace = ["-flags", "+ildct+ilme", "-top", "0"]
        movie_duration = max(1.0, float(info.get("duration_seconds") or info.get("source_duration_seconds") or 1))
        await self._run_ffmpeg(
            job_id,
            [
                "-y",
                "-i",
                str(source),
                *inputs,
                "-filter_complex",
                ";".join(filter_parts),
                "-map",
                "[vout]",
                "-map",
                "0:a?",
                "-map",
                "0:s?",
                *chapter_args,
                "-map_metadata",
                "0",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "18",
                *interlace,
                "-c:a",
                "copy",
                "-c:s",
                "copy",
                "-max_muxing_queue_size",
                "4096",
                "-progress",
                "pipe:1",
                "-nostats",
                str(output),
            ],
            timeout=24 * 3600,
            callback=self._progress_reader(
                progress_callback,
                start=progress_start,
                span=progress_span,
                seconds_total=movie_duration,
                message=message,
            ),
        )
        if not output.is_file() or output.stat().st_size < 1024 * 1024:
            raise RuntimeError("FFmpeg did not produce a usable movie")

    async def loading_screen_patches(
        self,
        job_id: str,
        workdir: Path,
        moments: list[dict[str, Any]],
        *,
        info: dict[str, Any],
        disc: str,
        progress_callback: ProgressCallback | None = None,
    ) -> list[dict[str, Any]]:
        workdir.mkdir(parents=True, exist_ok=True)
        screens = [moment for moment in moments if loading_screen_worthy(moment)]
        patches: list[dict[str, Any]] = []
        for index, moment in enumerate(screens, start=1):
            start, end = float(moment["start_seconds"]), float(moment["end_seconds"])
            clip = workdir / f"loading-screen-{index:03d}.mkv"
            await _notify(
                progress_callback,
                {"type": "progress", "percent": 0, "message": f"Drawing loading screen {index} of {len(screens)}"},
            )
            await self.render_loading_screen(
                job_id, clip, info=info, duration=end - start, resume_at=end, disc=disc
            )
            patches.append({"start": start, "end": end, "clip": clip, "moment": moment})
        return patches

    def write_chapters(
        self, source: Path, screens: list[dict[str, Any]], duration: float, target: Path, owner: str = ""
    ) -> Path | None:
        existing = read_chapters(source, self.ffprobe_path, owner)
        if existing is None:
            return None
        target.write_text(build_chapters(existing, screens, duration), encoding="utf-8")
        return target

    async def add_loading_screens(
        self,
        job_id: str,
        source: Path,
        workdir: Path,
        moments: list[dict[str, Any]],
        *,
        info: dict[str, Any],
        disc: str,
        progress_callback: ProgressCallback | None = None,
    ) -> tuple[Path, str]:
        """Write a new movie file with a loading screen over every damaged moment long enough for one.

        Returns the new file and how it was made ("splice" or "reencode").
        """
        patches = await self.loading_screen_patches(
            job_id, workdir, moments, info=info, disc=disc, progress_callback=progress_callback
        )
        if not patches:
            raise ValueError("No damaged moment is long enough for a loading screen")
        chapters = await asyncio.to_thread(
            self.write_chapters,
            source,
            [patch["moment"] for patch in patches],
            float(info.get("duration_seconds") or 0),
            workdir / "chapters.txt",
            job_id,
        )
        output = workdir / "movie-with-loading-screens.mkv"
        method = await self.stitch(
            job_id,
            source,
            patches,
            output,
            info=info,
            chapters_file=chapters,
            progress_callback=progress_callback,
            message="Adding loading screens where the disc was damaged",
        )
        return output, method
