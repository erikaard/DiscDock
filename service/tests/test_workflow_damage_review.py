from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from test_workflow_recovery import make_job, make_service

import discdock.workflow as workflow_module
from discdock.models import JobState
from discdock.settings import AppSettings
from discdock.workflow import DiscDockService


def _finished_job(
    tmp_path: Path, *, ai_repair: bool = False, **overrides: Any
) -> tuple[DiscDockService, AppSettings, dict[str, Any], Path]:
    tool = tmp_path / "tool.exe"
    tool.touch()
    settings = AppSettings(
        data_root=tmp_path, ffmpeg_path=str(tool), ffprobe_path=str(tool), ai_repair_enabled=ai_repair
    )
    library = settings.resolved_directories()["completed"] / "Movies" / "Damaged movie (2009)"
    library.mkdir(parents=True)
    movie = library / "Damaged movie (2009).mkv"
    movie.write_bytes(b"movie" * 1000)
    job = make_job(
        settings,
        tmp_path / "raw" / "attempt.partial",
        **{
            "state": JobState.COMPLETED,
            "stage": "completed",
            "status_detail": "Completed",
            "output_path": str(library),
            "completed_at": "2026-09-19T20:00:00+00:00",
            **overrides,
        },
    )
    service, _ = make_service(settings, job)
    return service, settings, job, movie


async def _finish_background_tasks() -> None:
    for task in [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]:
        await asyncio.wait_for(task, timeout=10)


@pytest.mark.asyncio
async def test_a_finished_movie_can_be_looked_through_for_broken_parts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, job, movie = _finished_job(tmp_path)
    looked: list[Path] = []

    def measure(source: Path, ffprobe_path: str, **kwargs) -> dict[str, Any]:
        looked.append(source)
        assert kwargs.get("scan_without_gaps") is not False, "the whole movie is decoded, not only the gaps"
        return {
            "gaps": [(60.0, 64.0)],
            "segments": [(60.0, 64.0), (900.0, 900.5)],
            "info": {"width": 720, "height": 576, "fps": 25.0},
        }

    monkeypatch.setattr(workflow_module, "measure_damage", measure)

    await service.review_damaged_movie("job-id")
    assert job["state"] == JobState.TRANSCODING and job["stage"] == "damage_scan"
    await _finish_background_tasks()

    assert looked == [movie]
    moments = job["metadata"]["damage"]["moments"]
    assert [moment["duration_seconds"] for moment in moments] == [4.0, 0.5]
    assert [moment["treatment"] for moment in moments] == ["skipped", "brief_glitch"]
    assert job["metadata"]["damage"]["choice"] == "pending", "the movie now waits for a repair choice"
    assert job["state"] == JobState.COMPLETED and job["stage"] == "completed"
    assert "2 broken moments" in job["status_detail"]


@pytest.mark.asyncio
async def test_the_review_prices_ai_repair_without_sending_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, job, movie = _finished_job(tmp_path, ai_repair=True)

    def analyze(source: Path, ffprobe_path: str, **kwargs) -> dict[str, Any]:
        return {
            "estimate_id": "estimate-1",
            "frame_count": 100,
            "ai_keyframe_count": 4,
            "estimated_max_cost_usd": 0.42,
            "segments": [{"index": 1, "start_seconds": 60.0, "end_seconds": 64.0, "duration_seconds": 4.0}],
            "skipped": [],
        }

    monkeypatch.setattr(workflow_module, "analyze_repair", analyze)
    monkeypatch.setattr(
        workflow_module, "measure_damage", lambda *args, **kwargs: pytest.fail("one pass measures and prices")
    )

    await service.review_damaged_movie("job-id")
    await _finish_background_tasks()

    plan = job["metadata"]["ai_repair"]
    assert plan["status"] == "estimated", "priced, but nothing is offered for approval yet"
    assert plan["source_path"] == str(movie)
    assert plan["estimated_max_cost_usd"] == 0.42
    assert job["metadata"]["damage"]["choice"] == "pending"
    assert job["state"] == JobState.COMPLETED


@pytest.mark.asyncio
async def test_a_priced_finished_movie_goes_to_ai_review_without_the_disc(tmp_path: Path) -> None:
    plan = {
        "estimate_id": "estimate-1",
        "status": "estimated",
        "frame_count": 100,
        "ai_keyframe_count": 4,
        "estimated_max_cost_usd": 0.42,
        "segments": [{"index": 1, "start_seconds": 60.0, "end_seconds": 64.0, "duration_seconds": 4.0}],
    }
    service, _settings, job, movie = _finished_job(tmp_path, ai_repair=True)
    plan["source_path"] = str(movie)
    job["metadata"] = {
        "damage": {"moments": [{"start_seconds": 60.0, "end_seconds": 64.0, "duration_seconds": 4.0}]},
        "ai_repair": plan,
    }

    await service.prepare_ai_repair_job("job-id")

    assert job["state"] == JobState.AWAITING_REPAIR and job["stage"] == "ai_review"
    saved = job["metadata"]["ai_repair"]
    assert saved["status"] == "awaiting_confirmation"
    assert saved["library_path"] == str(movie)
    source = Path(saved["source_path"])
    assert source.is_file() and source.parent == Path(job["staging_path"])
    assert source.stat().st_size == movie.stat().st_size, "the copy is a hard link, not a second file"
    assert movie.is_file(), "the movie in the library is untouched until a repair is approved"


@pytest.mark.asyncio
async def test_declining_ai_leaves_the_finished_movie_exactly_as_it_is(tmp_path: Path) -> None:
    service, settings, job, movie = _finished_job(tmp_path)
    source_root = settings.resolved_directories()["raw"] / "job-id-ai-source-1234.partial"
    source_root.mkdir(parents=True)
    (source_root / movie.name).write_bytes(b"copy")
    job["state"] = JobState.AWAITING_REPAIR
    job["stage"] = "ai_review"
    job["metadata"] = {
        "damage": {"choice": "pending", "moments": [{"start_seconds": 60.0, "end_seconds": 64.0}]},
        "ai_repair": {
            "estimate_id": "estimate-1",
            "status": "awaiting_confirmation",
            "segments": [],
            "library_path": str(movie),
            "source_root": str(source_root),
            "source_path": str(source_root / movie.name),
        },
    }

    await service.keep_movie_without_ai("job-id")

    assert job["state"] == JobState.COMPLETED and job["stage"] == "completed"
    declined = job["metadata"]["ai_repair"]
    assert declined["status"] == "declined"
    assert declined["source_path"] == str(movie), "changing your mind later needs no new scan"
    assert "choice" not in job["metadata"]["damage"], "the question is answered"
    assert job["metadata"]["damage"]["moments"], "the damaged moments stay listed"
    assert not source_root.exists(), "the copy made for the repair is thrown away"
    assert movie.is_file()


def test_the_repaired_movie_replaces_the_one_in_its_library_folder(tmp_path: Path) -> None:
    library = tmp_path / "Damaged movie (2009)"
    library.mkdir()
    previous = library / "Damaged movie (2009).mkv"
    previous.write_bytes(b"as read from the disc")
    poster = library / "poster.jpg"
    poster.write_bytes(b"poster")
    staging = tmp_path / "raw" / "repaired.partial"
    staging.mkdir(parents=True)
    (staging / "Damaged movie (2009).mkv").write_bytes(b"repaired")
    (staging / "Damaged movie (2009) - without AI frames.mkv").write_bytes(b"as read from the disc")

    finalized = DiscDockService._replace_movie_in_library(staging, library, previous)

    assert finalized == library
    assert previous.read_bytes() == b"repaired"
    assert (library / "Damaged movie (2009) - without AI frames.mkv").read_bytes() == b"as read from the disc"
    assert poster.read_bytes() == b"poster", "everything else in the library folder is left alone"
    assert not staging.exists()


def test_repairing_a_finished_movie_does_not_hold_the_drive(tmp_path: Path) -> None:
    service, _, job, _ = _finished_job(tmp_path, state=JobState.TRANSCODING, stage="damage_scan")

    assert service.active_job_for_drive("drive-id") is None, "the disc is long gone; the next one can be ripped"

    job["stage"] = "ripping"
    assert service.active_job_for_drive("drive-id") is job
