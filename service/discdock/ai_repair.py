from __future__ import annotations

import asyncio
import base64
import json
import math
import uuid
from bisect import bisect_left
from itertools import pairwise
from pathlib import Path
from typing import Any

import httpx

from .damage_screens import MoviePatcher, loading_screen_worthy
from .database import utc_now
from .processes import ProcessFailure, ProcessRunner, run_capture

OPENAI_API_ROOT = "https://api.openai.com/v1"
SUPPORTED_MODELS = {"gpt-image-2.5-sunburst", "gpt-image-2.5-flare"}
MAX_REPAIR_SEGMENT_SECONDS = 4.0
MAX_TOTAL_REPAIR_SECONDS = 8.0
MAX_AI_KEYFRAMES = 24
# Damage closer together than this is repaired as one bridge: the few frames
# between two holes are usually too broken to serve as anchors.
MERGE_DAMAGE_SECONDS = 1.0
# Video decoded around each missing interval to find pictures broken by it.
DAMAGE_WINDOW_SECONDS = 6.0
GENERATION_HEIGHT = 720


def _fraction(value: str) -> float:
    try:
        numerator, denominator = value.replace(":", "/").split("/", 1)
        return float(numerator) / float(denominator) if float(denominator) else 0.0
    except (ValueError, ZeroDivisionError):
        try:
            return float(value)
        except ValueError:
            return 0.0


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f} seconds"
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes} min {rest:02d} s"


def _run_capture(args: list[str], timeout: int = 300, owner: str = "") -> tuple[int, str, str]:
    # A scan of a whole movie can run for hours. It ends with DiscDock, and
    # cancelling the job stops it (see processes.cancel_captures).
    return run_capture(args, timeout=timeout, owner=owner)


def _video_info(source: Path, ffprobe_path: str, owner: str = "") -> dict[str, Any]:
    code, stdout, stderr = _run_capture(
        [
            ffprobe_path,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            (
                "stream=width,height,avg_frame_rate,r_frame_rate,sample_aspect_ratio,field_order,codec_name"
                ":format=duration"
            ),
            "-of",
            "json",
            str(source),
        ],
        owner=owner,
    )
    if code != 0:
        raise RuntimeError(f"FFprobe could not inspect the movie: {stderr.strip()[-500:]}")
    payload = json.loads(stdout)
    streams = payload.get("streams") or []
    if not streams:
        raise RuntimeError("The movie has no readable video stream")
    stream = streams[0]
    fps_text = str(stream.get("avg_frame_rate") or "0")
    fps = _fraction(fps_text)
    if not fps:
        fps_text = str(stream.get("r_frame_rate") or "0")
        fps = _fraction(fps_text)
    if not 1 <= fps <= 120:
        raise RuntimeError("DiscDock could not determine the movie frame rate")
    sar_text = str(stream.get("sample_aspect_ratio") or "1:1")
    sar = _fraction(sar_text)
    if not 0.2 <= sar <= 5:
        sar_text, sar = "1:1", 1.0
    return {
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "fps": fps,
        "fps_text": fps_text,
        "duration_seconds": float((payload.get("format") or {}).get("duration") or 0),
        "sar": sar,
        "sar_text": sar_text,
        "field_order": str(stream.get("field_order") or "unknown"),
        "codec": str(stream.get("codec_name") or ""),
    }


def video_info(source: Path, ffprobe_path: str, owner: str = "") -> dict[str, Any]:
    """Size, frame rate, pixel shape and duration of a movie's video."""
    return _video_info(source, ffprobe_path, owner)


def _probe_packets(source: Path, ffprobe_path: str, owner: str = "") -> tuple[list[float], list[float]]:
    """Return sorted presentation times of all video packets and of keyframes."""
    code, stdout, stderr = _run_capture(
        [
            ffprobe_path,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "packet=pts_time,flags",
            "-of",
            "csv=p=0",
            str(source),
        ],
        timeout=1800,
        owner=owner,
    )
    if code != 0:
        raise RuntimeError(f"FFprobe could not map the movie timeline: {stderr.strip()[-500:]}")
    times: list[float] = []
    keyframes: list[float] = []
    for line in stdout.splitlines():
        parts = line.strip().split(",")
        try:
            value = float(parts[0])
        except (ValueError, IndexError):
            continue
        times.append(value)
        if len(parts) > 1 and "K" in parts[1]:
            keyframes.append(value)
    times.sort()
    keyframes.sort()
    return times, keyframes


def packet_gaps(times: list[float], fps: float) -> list[tuple[float, float]]:
    """Find presentation-time jumps where video frames are missing."""
    frame = 1.0 / fps
    threshold = max(0.12, frame * 3.2)
    return [
        (previous + frame, current)
        for previous, current in pairwise(times)
        if current - previous > threshold
    ]


def _decoder_damage(
    source: Path, ffprobe_path: str, windows: list[tuple[float, float]] | None, owner: str = ""
) -> list[float]:
    """Return timestamps of frames the decoder reported as damaged or concealed."""
    args = [
        ffprobe_path,
        "-v",
        "error",
        # Single-threaded decoding keeps each decoder message attached to its frame.
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
    ]
    if windows:
        args.extend(["-read_intervals", ",".join(f"{start:.3f}%{end:.3f}" for start, end in windows)])
    args.append(str(source))
    code, stdout, stderr = _run_capture(args, timeout=4 * 3600, owner=owner)
    if code != 0 and not stdout.strip():
        raise RuntimeError(f"FFprobe could not decode the damaged video: {stderr.strip()[-500:]}")
    try:
        frames = json.loads(stdout or "{}").get("frames") or []
    except ValueError as error:
        raise RuntimeError("FFprobe returned unreadable frame information") from error
    damaged: list[float] = []
    for frame in frames:
        if not frame.get("logs"):
            continue
        try:
            damaged.append(float(frame.get("best_effort_timestamp_time")))
        except (TypeError, ValueError):
            continue
    return sorted(damaged)


def _merge(intervals: list[tuple[float, float]], distance: float = 0.0) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for start, end in sorted(intervals):
        if merged and start - merged[-1][1] <= distance:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def plan_segments(
    gaps: list[tuple[float, float]],
    damaged_times: list[float],
    keyframes: list[float],
    fps: float,
    duration: float,
) -> list[tuple[float, float]]:
    """Turn missing and broken frames into repair windows on the frame grid.

    Each window ends on the next keyframe: pictures between the damage and that
    keyframe are predicted from damaged references and cannot be trusted.
    """
    frame = 1.0 / fps
    intervals = list(gaps) + [(time, time + frame) for time in damaged_times]
    if not intervals:
        return []
    padded = [(max(0.0, start - 2 * frame), min(duration, end + 2 * frame)) for start, end in intervals]
    windows: list[tuple[float, float]] = []
    for start, end in _merge(padded, MERGE_DAMAGE_SECONDS):
        index = bisect_left(keyframes, end - frame / 2)
        if index < len(keyframes) and keyframes[index] - end <= 2.0:
            end = keyframes[index]
        first_frame = math.floor(start * fps + 1e-6)
        last_frame = max(first_frame + 1, math.ceil(end * fps - 1e-6))
        windows.append((first_frame / fps, last_frame / fps))
    return _merge(windows)


def generation_size(width: int, height: int, sar: float) -> tuple[int, int]:
    """Choose an image-model size with the movie's display shape (divisible by 16)."""
    display = (width * (sar or 1.0)) / max(1, height)
    display = min(3.0, max(1 / 3, display))
    generated_width = round(GENERATION_HEIGHT * display / 16) * 16
    return max(480, min(1920, generated_width)), GENERATION_HEIGHT


def find_repair_source(folder: Path) -> Path:
    if not folder.exists():
        raise RuntimeError("The incomplete MakeMKV output is no longer available")
    candidates = [
        path
        for path in folder.rglob("*.mkv")
        if path.is_file() and ".discdock-repair" not in path.parts and path.stat().st_size > 1024 * 1024
    ]
    if not candidates:
        raise RuntimeError(
            "MakeMKV did not leave a playable partial MKV. AI cannot reconstruct a movie without readable video on both sides of the damaged interval."
        )
    return max(candidates, key=lambda path: (path.stat().st_size, path.name.casefold()))


def estimate_request_ceiling(quality: str) -> float:
    # GPT Image 2.5 is token billed and reports exact usage only after each
    # request. These deliberately generous per-request ceilings include two
    # reference images, the prompt, and low/medium image output.
    return 0.08 if quality == "low" else 0.30


def calculate_usage_cost(usage: dict[str, Any]) -> float:
    details = usage.get("input_tokens_details") or {}
    image_input = float(details.get("image_tokens") or 0)
    text_input = float(details.get("text_tokens") or 0)
    total_input = float(usage.get("input_tokens") or 0)
    if not image_input and not text_input:
        image_input = total_input
    output = float(usage.get("output_tokens") or 0)
    return image_input * 8 / 1_000_000 + text_input * 5 / 1_000_000 + output * 30 / 1_000_000


def measure_damage(
    source: Path, ffprobe_path: str, *, scan_without_gaps: bool = True, owner: str = ""
) -> dict[str, Any]:
    """Find missing and broken video in a movie, as repair windows on its frame grid.

    Only the video around missing intervals is decoded. When nothing is missing,
    ``scan_without_gaps`` decodes the whole movie for concealed pictures; that
    takes as long as encoding it, so the best-effort check skips it.
    """
    info = _video_info(source, ffprobe_path, owner)
    fps = float(info["fps"])
    duration = float(info["duration_seconds"])
    times, keyframes = _probe_packets(source, ffprobe_path, owner)
    if not times:
        raise RuntimeError("The movie has no readable video frames")
    gaps = packet_gaps(times, fps)
    windows = (
        _merge(
            [
                (max(0.0, start - DAMAGE_WINDOW_SECONDS), min(duration, end + DAMAGE_WINDOW_SECONDS))
                for start, end in gaps
            ]
        )
        if gaps
        else None
    )
    damaged = _decoder_damage(source, ffprobe_path, windows, owner) if gaps or scan_without_gaps else []
    segments = plan_segments(gaps, damaged, keyframes, fps, duration or times[-1] + 1.0 / fps)
    return {
        "info": info,
        "times": times,
        "keyframes": keyframes,
        "gaps": gaps,
        "damaged": damaged,
        "segments": segments,
    }


def damage_moments(measured: dict[str, Any]) -> list[dict[str, Any]]:
    """The damaged moments of a measured movie, for the library and loading screens."""
    gaps = measured["gaps"]
    return [
        {
            "start_seconds": round(start, 3),
            "end_seconds": round(end, 3),
            "duration_seconds": round(end - start, 3),
            "missing_seconds": round(
                sum(max(0.0, min(gap_end, end) - max(gap_start, start)) for gap_start, gap_end in gaps), 3
            ),
        }
        for start, end in measured["segments"]
    ]


def analyze_repair(
    source: Path,
    ffprobe_path: str,
    *,
    model: str,
    quality: str,
    keyframes_per_second: float,
    expected_duration_seconds: float = 0,
    owner: str = "",
) -> dict[str, Any]:
    if model not in SUPPORTED_MODELS:
        raise RuntimeError("The configured OpenAI image model is not supported")
    measured = measure_damage(source, ffprobe_path, owner=owner)
    info = measured["info"]
    fps = float(info["fps"])
    frame = 1.0 / fps
    duration = float(info["duration_seconds"])
    times = measured["times"]
    gaps = measured["gaps"]
    damaged = measured["damaged"]
    segments = measured["segments"]
    notes: list[str] = []
    if expected_duration_seconds and duration + 60 < expected_duration_seconds:
        notes.append(
            f"The movie is {_format_duration(expected_duration_seconds - duration)} shorter than the disc title, "
            "so some damage was cut out entirely; AI can only fill gaps inside the movie."
        )
    generated_width, generated_height = generation_size(int(info["width"]), int(info["height"]), info["sar"])
    planned: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    keyframe_total = 0
    frame_total = 0
    ai_seconds = 0.0
    for start, end in segments:
        seconds = end - start
        missing = sum(max(0.0, min(gap_end, end) - max(gap_start, start)) for gap_start, gap_end in gaps)
        keyframe_count = max(1, math.ceil(seconds * keyframes_per_second - 1e-9))
        # Damage that is too long, or at the very start or end, is left as it is.
        # It is listed as skipped instead of ending the analysis.
        if start < 2 * frame:
            reason = "at the very start of the movie, with no intact picture before it"
        elif end > times[-1] - frame / 2:
            reason = "at the very end of the movie, with no intact picture after it"
        elif seconds > MAX_REPAIR_SEGMENT_SECONDS:
            reason = f"longer than {MAX_REPAIR_SEGMENT_SECONDS:.0f} seconds, too long for generated frames"
        elif ai_seconds + seconds > MAX_TOTAL_REPAIR_SECONDS or keyframe_total + keyframe_count > MAX_AI_KEYFRAMES:
            reason = f"over the limit of {MAX_TOTAL_REPAIR_SECONDS:.0f} seconds of generated frames per movie"
        else:
            reason = ""
        if reason:
            skipped.append(
                {
                    "start_seconds": round(start, 6),
                    "end_seconds": round(end, 6),
                    "duration_seconds": round(seconds, 6),
                    "missing_seconds": round(missing, 3),
                    "reason": reason,
                }
            )
            continue
        frames = max(1, round(seconds * fps))
        index = len(planned) + 1
        keyframe_total += keyframe_count
        frame_total += frames
        ai_seconds += seconds
        planned.append(
            {
                "index": index,
                "start_seconds": round(start, 6),
                "end_seconds": round(end, 6),
                "duration_seconds": round(seconds, 6),
                "before_seconds": round(max(0.0, start - frame), 6),
                "after_seconds": round(end, 6),
                "missing_seconds": round(missing, 3),
                "damaged_frame_count": sum(1 for time in damaged if start <= time < end),
                "frame_count": frames,
                "ai_keyframe_count": keyframe_count,
                "preview": f".discdock-repair/preview-{index:03d}.mp4",
                "applied": False,
            }
        )
    estimate = round(keyframe_total * estimate_request_ceiling(quality), 2)
    damaged_seconds = sum(end - start for start, end in segments)
    if planned and skipped:
        summary = (
            f"AI can replace {frame_total} broken frames in {len(planned)} "
            f"{'place' if len(planned) == 1 else 'places'}. {len(skipped)} longer damaged "
            f"{'stretch stays' if len(skipped) == 1 else 'stretches stay'} skipped."
        )
    elif planned:
        summary = f"AI can replace all {frame_total} broken frames in {len(planned)} {'place' if len(planned) == 1 else 'places'}."
    elif skipped:
        summary = (
            f"The damage ({_format_duration(damaged_seconds)} in {len(skipped)} "
            f"{'place' if len(skipped) == 1 else 'places'}) is too long for AI. Keep the movie with those "
            "moments skipped."
        )
    else:
        summary = "No missing or broken frames were found. The movie can be kept as it is."
    return {
        "estimate_id": uuid.uuid4().hex,
        "status": "awaiting_confirmation",
        "mode": "openai_frame_bridge",
        "model": model,
        "quality": quality,
        "prepared_at": utc_now(),
        "source_path": str(source),
        "source_duration_seconds": round(duration, 3),
        "fps": round(fps, 6),
        "fps_text": str(info.get("fps_text") or ""),
        "width": int(info["width"]),
        "height": int(info["height"]),
        "sample_aspect_ratio": str(info["sar_text"]),
        "field_order": str(info["field_order"]),
        "generation_width": generated_width,
        "generation_height": generated_height,
        "frame_count": frame_total,
        "ai_keyframe_count": keyframe_total,
        "estimated_max_cost_usd": estimate,
        "estimate_is_ceiling": True,
        "segments": planned,
        "skipped": skipped,
        "damaged_seconds": round(damaged_seconds, 3),
        "summary": summary,
        "notes": notes,
        "disclaimer": "Generated frames, not recovered original footage.",
    }


class OpenAIFrameRepair:
    def __init__(
        self,
        api_key: str,
        model: str,
        quality: str,
        ffmpeg_path: str,
        runner: ProcessRunner,
        ffprobe_path: str = "",
    ):
        if model not in SUPPORTED_MODELS:
            raise ValueError("Unsupported OpenAI image model")
        self.api_key = api_key
        self.model = model
        self.quality = quality
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path
        self.runner = runner

    @staticmethod
    async def test_key(api_key: str, model: str) -> tuple[bool, str]:
        if not api_key:
            return False, "Enter an OpenAI API key first"
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{OPENAI_API_ROOT}/models/{model}",
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if response.status_code == 200:
            return True, "OpenAI key works and the AI repair model is available"
        if response.status_code in {401, 403}:
            return False, "OpenAI rejected the key or this project cannot use the image model"
        return False, f"OpenAI could not verify the key (HTTP {response.status_code})"

    async def _run_ffmpeg(
        self,
        job_id: str,
        args: list[str],
        *,
        timeout: int,
        callback=None,
    ) -> None:
        result = await self.runner.run(
            job_id,
            [self.ffmpeg_path, "-hide_banner", "-nostdin", *args],
            timeout=timeout,
            no_output_timeout=max(600, timeout),
            on_line=callback,
        )
        if result.return_code != 0 or result.cancelled:
            tail = " ".join(result.lines[-3:])
            raise ProcessFailure(f"FFmpeg could not build the AI repair. {tail}".strip(), result)

    async def _extract_frame(
        self, job_id: str, source: Path, at: float, target: Path, width: int, height: int, fps: float
    ) -> None:
        # Seek a quarter frame early so floating-point timestamps cannot skip
        # the intended intact frame.
        await self._run_ffmpeg(
            job_id,
            [
                "-y",
                "-ss",
                f"{max(0.0, at - 0.25 / fps):.6f}",
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-vf",
                (
                    "scale=trunc(iw*sar/2)*2:ih,setsar=1,"
                    f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                    f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1"
                ),
                str(target),
            ],
            timeout=180,
        )
        if not target.is_file():
            raise RuntimeError("Could not extract an intact boundary frame")

    async def _generate_keyframe(
        self,
        before: Path,
        after: Path,
        target: Path,
        fraction: float,
        size: str = "1280x720",
    ) -> tuple[float, dict[str, Any]]:
        prompt = (
            "You are restoring a damaged film. The first reference is the last intact frame before a short "
            "unreadable moment and the second reference is the first intact frame after it. Create the single "
            f"in-between frame at {fraction * 100:.1f}% of the way from the first to the second reference. Keep "
            "the exact same shot: framing, camera angle, lens, lighting, colours, grain, people, faces, "
            "clothing, and background. Continue only the motion implied by the two references. If the "
            "references are from two different shots, return the closer reference unchanged instead of "
            "blending them. Do not add captions, logos, borders, or new subjects."
        )
        files = [
            ("image[]", ("before.png", before.read_bytes(), "image/png")),
            ("image[]", ("after.png", after.read_bytes(), "image/png")),
        ]
        data = {
            "model": self.model,
            "prompt": prompt,
            "size": size,
            "quality": self.quality,
            "output_format": "png",
            "n": "1",
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(360)) as client:
            response = await client.post(
                f"{OPENAI_API_ROOT}/images/edits",
                headers={"Authorization": f"Bearer {self.api_key}"},
                data=data,
                files=files,
            )
        if response.status_code >= 400:
            try:
                message = (response.json().get("error") or {}).get("message")
            except (ValueError, AttributeError):
                message = ""
            raise RuntimeError(message or f"OpenAI image repair failed (HTTP {response.status_code})")
        payload = response.json()
        images = payload.get("data") or []
        encoded = images[0].get("b64_json") if images else ""
        if not encoded:
            raise RuntimeError("OpenAI returned no reconstructed frame")
        target.write_bytes(base64.b64decode(encoded))
        usage = payload.get("usage") or {}
        return calculate_usage_cost(usage), usage

    async def _build_preview(
        self,
        job_id: str,
        before: Path,
        generated: list[Path],
        after: Path,
        target: Path,
        duration: float,
        fps: float,
        width: int,
        height: int,
        sar: str = "1/1",
    ) -> None:
        sequence = [before, *generated, after]
        frame_duration = duration / max(1, len(sequence) - 1)
        concat_path = target.with_suffix(".ffconcat")
        lines = ["ffconcat version 1.0"]
        for image in sequence[:-1]:
            escaped = image.resolve().as_posix().replace("'", "'\\''")
            lines.extend([f"file '{escaped}'", f"duration {frame_duration:.9f}"])
        escaped = sequence[-1].resolve().as_posix().replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
        concat_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        await self._run_ffmpeg(
            job_id,
            [
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_path),
                "-t",
                f"{duration:.6f}",
                "-vf",
                # Interpolate at display shape, then return to the movie's own
                # storage size and pixel shape (for example anamorphic DVDs).
                f"minterpolate=fps={fps:.6f}:mi_mode=mci,scale={width}:{height},setsar={sar},format=yuv420p",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "17",
                str(target),
            ],
            timeout=900,
        )

    async def generate_and_apply(
        self,
        job_id: str,
        plan: dict[str, Any],
        destination: Path,
        accepted_max_cost_usd: float,
        progress_callback=None,
    ) -> tuple[Path, dict[str, Any]]:
        source = Path(str(plan["source_path"]))
        if not source.is_file():
            raise RuntimeError("The prepared movie is no longer available")
        if not plan.get("segments"):
            raise RuntimeError("This movie has no damage short enough for AI to replace")
        destination.mkdir(parents=True, exist_ok=True)
        assets = destination / ".discdock-repair"
        assets.mkdir(parents=True, exist_ok=True)
        actual_cost = 0.0
        usage_records: list[dict[str, Any]] = []
        completed_keyframes = 0
        segments = [dict(segment) for segment in plan.get("segments") or []]
        fps = float(plan["fps"])
        width = int(plan["width"])
        height = int(plan["height"])
        sar = str(plan.get("sample_aspect_ratio") or "1:1").replace(":", "/")
        generated_width = int(plan.get("generation_width") or 1280)
        generated_height = int(plan.get("generation_height") or 720)
        total_keyframes = int(plan["ai_keyframe_count"])

        for segment in segments:
            index = int(segment["index"])
            start = float(segment["start_seconds"])
            end = float(segment["end_seconds"])
            duration = end - start
            before = assets / f"boundary-{index:03d}-before.png"
            after = assets / f"boundary-{index:03d}-after.png"
            await self._extract_frame(
                job_id,
                source,
                float(segment.get("before_seconds", start - 1 / fps)),
                before,
                generated_width,
                generated_height,
                fps,
            )
            await self._extract_frame(
                job_id,
                source,
                float(segment.get("after_seconds", end)),
                after,
                generated_width,
                generated_height,
                fps,
            )
            keyframes: list[Path] = []
            count = int(segment["ai_keyframe_count"])
            for keyframe_index in range(1, count + 1):
                if actual_cost >= accepted_max_cost_usd:
                    raise RuntimeError("The approved OpenAI cost ceiling was reached before the repair finished")
                target = assets / f"segment-{index:03d}-ai-{keyframe_index:03d}.png"
                fraction = keyframe_index / (count + 1)
                cost, usage = await self._generate_keyframe(
                    before, after, target, fraction, f"{generated_width}x{generated_height}"
                )
                actual_cost += cost
                usage_records.append(usage)
                keyframes.append(target)
                completed_keyframes += 1
                if progress_callback:
                    response = progress_callback(
                        {
                            "type": "progress",
                            "percent": completed_keyframes * 70 / max(1, total_keyframes),
                            "message": f"OpenAI reconstructed keyframe {completed_keyframes} of {total_keyframes}",
                        }
                    )
                    if asyncio.iscoroutine(response):
                        await response
            preview = assets / f"preview-{index:03d}.mp4"
            await self._build_preview(
                job_id, before, keyframes, after, preview, duration, fps, width, height, sar
            )
            segment["applied"] = True

        patcher = MoviePatcher(self.ffmpeg_path, self.ffprobe_path, self.runner)
        info = {
            "width": width,
            "height": height,
            "fps": fps,
            "fps_text": str(plan.get("fps_text") or ""),
            "sar": _fraction(sar),
            "sar_text": sar,
            "field_order": str(plan.get("field_order") or ""),
            "duration_seconds": float(plan.get("source_duration_seconds") or 0),
        }
        patches: list[dict[str, Any]] = [
            {
                "start": float(segment["start_seconds"]),
                "end": float(segment["end_seconds"]),
                "clip": assets / f"preview-{int(segment['index']):03d}.mp4",
            }
            for segment in segments
        ]
        # Damage too long for AI gets a loading screen instead of a frozen picture.
        screens = (
            [moment for moment in plan.get("skipped") or [] if loading_screen_worthy(moment)]
            if plan.get("loading_screens")
            else []
        )
        chapters = None
        if screens:
            patches += await patcher.loading_screen_patches(
                job_id,
                assets,
                screens,
                info=info,
                disc=str(plan.get("disc_name") or "disc"),
                progress_callback=progress_callback,
            )
            chapters = await asyncio.to_thread(
                patcher.write_chapters, source, screens, info["duration_seconds"], assets / "chapters.txt", job_id
            )
        repaired = destination / "repaired.mkv"
        method = await patcher.stitch(
            job_id,
            source,
            patches,
            repaired,
            info=info,
            chapters_file=chapters,
            progress_callback=progress_callback,
            progress_start=70,
            progress_span=29,
            message="Adding the generated frames to the movie",
        )
        for pattern in ("boundary-*.png", "segment-*-ai-*.png", "*.ffconcat", "loading-screen-*.mkv", "chapters.txt"):
            for path in assets.glob(pattern):
                path.unlink(missing_ok=True)

        finished = {
            **plan,
            "status": "applied",
            "applied_at": utc_now(),
            "actual_cost_usd": round(actual_cost, 6),
            "usage": usage_records,
            "segments": segments,
            "loading_screens_added": [patch["moment"] for patch in patches if "moment" in patch],
            # "splice": only the damaged moments were encoded again; "reencode": the whole video.
            "patch_method": method,
        }
        (assets / "repair.json").write_text(
            json.dumps(finished, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return repaired, finished
