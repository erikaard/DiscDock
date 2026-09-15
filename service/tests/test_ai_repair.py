from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from discdock import ai_repair
from discdock.ai_repair import (
    OpenAIFrameRepair,
    analyze_repair,
    calculate_usage_cost,
    estimate_request_ceiling,
    find_repair_source,
    generation_size,
    packet_gaps,
    plan_segments,
)
from discdock.processes import ProcessRunner


def test_calculate_usage_cost_uses_image_text_and_output_rates() -> None:
    usage = {
        "input_tokens": 1100,
        "input_tokens_details": {"image_tokens": 1000, "text_tokens": 100},
        "output_tokens": 200,
    }

    assert calculate_usage_cost(usage) == pytest.approx(0.0145)


def test_repair_estimates_are_deliberate_per_request_ceiling() -> None:
    assert estimate_request_ceiling("low") == 0.08
    assert estimate_request_ceiling("medium") == 0.30


def _timeline(duration: float, fps: float, missing: list[tuple[float, float]]) -> list[float]:
    frames = [index / fps for index in range(int(duration * fps))]
    return [time for time in frames if not any(start - 1e-9 <= time < end - 1e-9 for start, end in missing)]


def _stub_movie(
    monkeypatch: pytest.MonkeyPatch,
    *,
    duration: float,
    fps: float = 25.0,
    missing: tuple[tuple[float, float], ...] = (),
    damaged: tuple[float, ...] = (),
    sar: str = "64:45",
) -> list:
    times = _timeline(duration, fps, list(missing))
    keyframes = [index * 12 / fps for index in range(int(duration * fps / 12) + 1)]
    decoded_windows: list = []
    monkeypatch.setattr(
        ai_repair,
        "_video_info",
        lambda path, probe, owner="": {
            "width": 720,
            "height": 576,
            "fps": fps,
            "duration_seconds": duration,
            "sar": ai_repair._fraction(sar),
            "sar_text": sar,
            "field_order": "progressive",
            "codec": "mpeg2video",
        },
    )
    monkeypatch.setattr(ai_repair, "_probe_packets", lambda path, probe, owner="": (times, keyframes))

    def decoder(path, probe, windows, owner=""):
        decoded_windows.append(windows)
        return list(damaged)

    monkeypatch.setattr(ai_repair, "_decoder_damage", decoder)
    return decoded_windows


def test_packet_gaps_find_missing_presentation_time() -> None:
    gaps = packet_gaps(_timeline(30, 25.0, [(10.0, 10.4)]), 25.0)

    assert len(gaps) == 1
    assert gaps[0][0] == pytest.approx(10.0)
    assert gaps[0][1] == pytest.approx(10.4)


def test_plan_segments_merge_nearby_damage_and_end_on_a_keyframe() -> None:
    keyframes = [index * 12 / 25 for index in range(200)]

    segments = plan_segments([(10.0, 10.4), (10.9, 11.0)], [11.52], keyframes, 25.0, 60.0)

    assert len(segments) == 1
    assert segments[0][0] == pytest.approx(9.92)
    assert segments[0][1] == pytest.approx(12.0)


def test_generation_size_keeps_the_display_shape() -> None:
    assert generation_size(720, 576, 64 / 45) == (1280, 720)
    assert generation_size(720, 576, 16 / 15) == (960, 720)
    assert generation_size(1920, 1080, 1.0) == (1280, 720)


def test_analyze_repair_counts_native_frames_and_ai_keyframes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "partial.mkv"
    source.write_bytes(b"partial movie")
    windows = _stub_movie(monkeypatch, duration=600, missing=((10.0, 10.4), (20.0, 20.2)))

    plan = analyze_repair(
        source,
        "ffprobe.exe",
        model="gpt-image-2.5-sunburst",
        quality="low",
        keyframes_per_second=2,
        expected_duration_seconds=600,
    )

    bounds = [(segment["start_seconds"], segment["end_seconds"]) for segment in plan["segments"]]
    assert bounds == [(pytest.approx(9.92), pytest.approx(10.56)), (pytest.approx(19.92), pytest.approx(20.64))]
    assert plan["frame_count"] == 34
    assert plan["ai_keyframe_count"] == 4
    assert plan["estimated_max_cost_usd"] == 0.32
    assert plan["generation_width"] == 1280
    assert plan["sample_aspect_ratio"] == "64:45"
    assert plan["segments"][0]["missing_seconds"] == pytest.approx(0.4)
    assert plan["segments"][0]["preview"].endswith("preview-001.mp4")
    assert windows and windows[0] is not None, "only the video around each gap is decoded"


def test_analyze_repair_finds_broken_frames_without_missing_timestamps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "partial.mkv"
    source.write_bytes(b"partial movie")
    windows = _stub_movie(monkeypatch, duration=600, damaged=(100.0, 100.04))

    plan = analyze_repair(
        source,
        "ffprobe.exe",
        model="gpt-image-2.5-sunburst",
        quality="low",
        keyframes_per_second=2,
    )

    assert windows == [None], "without timestamp gaps the whole movie is decoded"
    assert len(plan["segments"]) == 1
    assert plan["segments"][0]["damaged_frame_count"] == 2
    assert plan["segments"][0]["end_seconds"] == pytest.approx(100.32)


def test_a_short_movie_is_noted_without_ending_the_analysis(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "truncated-bluray.mkv"
    source.write_bytes(b"partial movie")
    _stub_movie(monkeypatch, duration=900)

    plan = analyze_repair(
        source,
        "ffprobe.exe",
        model="gpt-image-2.5-sunburst",
        quality="low",
        keyframes_per_second=2,
        expected_duration_seconds=5400,
    )

    assert plan["segments"] == [] and plan["skipped"] == []
    assert "shorter than the disc title" in plan["notes"][0]


def test_damage_too_long_for_ai_is_skipped_while_short_damage_is_still_planned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "partial.mkv"
    source.write_bytes(b"partial movie")
    _stub_movie(monkeypatch, duration=500, missing=((30.0, 34.1), (100.0, 100.4)))

    plan = analyze_repair(
        source,
        "ffprobe.exe",
        model="gpt-image-2.5-flare",
        quality="medium",
        keyframes_per_second=2,
    )

    assert [segment["index"] for segment in plan["segments"]] == [1]
    assert plan["segments"][0]["start_seconds"] == pytest.approx(99.92)
    assert len(plan["skipped"]) == 1
    assert "longer than 4 seconds" in plan["skipped"][0]["reason"]
    assert plan["skipped"][0]["missing_seconds"] > 4
    assert "1 longer damaged stretch stays skipped" in plan["summary"]
    assert plan["ai_keyframe_count"] == plan["segments"][0]["ai_keyframe_count"]


def test_damage_at_the_very_end_is_skipped_instead_of_invented(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "partial.mkv"
    source.write_bytes(b"partial movie")
    _stub_movie(monkeypatch, duration=60, damaged=(59.92,))

    plan = analyze_repair(
        source,
        "ffprobe.exe",
        model="gpt-image-2.5-flare",
        quality="low",
        keyframes_per_second=2,
    )

    assert plan["segments"] == []
    assert "very end" in plan["skipped"][0]["reason"]
    assert plan["estimated_max_cost_usd"] == 0


def test_find_repair_source_uses_largest_movie_and_ignores_repair_previews(
    tmp_path: Path,
) -> None:
    small = tmp_path / "title-1.mkv"
    large = tmp_path / "title-2.mkv"
    preview = tmp_path / ".discdock-repair" / "preview.mkv"
    preview.parent.mkdir()
    small.write_bytes(b"s" * (2 * 1024 * 1024))
    large.write_bytes(b"l" * (3 * 1024 * 1024))
    preview.write_bytes(b"p" * (4 * 1024 * 1024))

    assert find_repair_source(tmp_path) == large


@pytest.mark.asyncio
async def test_ffmpeg_builds_preview_and_stitches_an_anamorphic_dvd_movie(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg is not installed")
    source = tmp_path / "source.mkv"
    await asyncio.to_thread(
        subprocess.run,
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=720x576:rate=25:duration=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=10",
            "-vf",
            "setsar=64/45",
            "-c:v",
            "mpeg2video",
            "-b:v",
            "5M",
            "-g",
            "12",
            "-c:a",
            "aac",
            "-shortest",
            str(source),
        ],
        check=True,
        timeout=60,
    )
    repair = OpenAIFrameRepair(
        "unused-test-key", "gpt-image-2.5-flare", "low", ffmpeg, ProcessRunner()
    )
    sizes: list[str] = []

    async def fake_generate(before: Path, after: Path, target: Path, fraction: float, size: str = ""):
        del after, fraction
        sizes.append(size)
        shutil.copyfile(before, target)
        return 0.001, {"output_tokens": 34}

    monkeypatch.setattr(repair, "_generate_keyframe", fake_generate)
    plan = {
        "estimate_id": "test-estimate",
        "status": "awaiting_confirmation",
        "source_path": str(source),
        "source_duration_seconds": 10.0,
        "fps": 25.0,
        "width": 720,
        "height": 576,
        "sample_aspect_ratio": "64:45",
        "field_order": "progressive",
        "generation_width": 1280,
        "generation_height": 720,
        "frame_count": 12,
        "ai_keyframe_count": 1,
        "segments": [
            {
                "index": 1,
                "start_seconds": 2.0,
                "end_seconds": 2.48,
                "duration_seconds": 0.48,
                "before_seconds": 1.96,
                "after_seconds": 2.48,
                "frame_count": 12,
                "ai_keyframe_count": 1,
                "preview": ".discdock-repair/preview-001.mp4",
                "applied": False,
            }
        ],
    }

    repaired, finished = await repair.generate_and_apply(
        "ffmpeg-smoke", plan, tmp_path / "output", accepted_max_cost_usd=0.08
    )

    assert repaired.stat().st_size > 1024
    assert (tmp_path / "output" / ".discdock-repair" / "preview-001.mp4").is_file()
    assert finished["status"] == "applied"
    assert finished["actual_cost_usd"] == 0.001
    assert sizes == ["1280x720"]
    result = await asyncio.to_thread(
        subprocess.run,
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,sample_aspect_ratio:format=duration",
            "-of",
            "default=nw=1",
            str(repaired),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    details = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    assert float(details["duration"]) >= 9.8
    assert details["width"] == "720" and details["height"] == "576"
    assert details["sample_aspect_ratio"] == "64:45"
