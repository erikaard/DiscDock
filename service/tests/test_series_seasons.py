"""Series discs: choosing the season folder their episodes go into, and numbering on from it."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_workflow_core import FakeNotifications, MemoryDatabase, make_job

import discdock.workflow as workflow_module
from discdock.files import SeriesSeason, rename_video_outputs, series_seasons
from discdock.makemkv import DiscScan
from discdock.models import DiscKind, DriveInfo, JobState, MediaKind, TitleInfo
from discdock.settings import AppSettings
from discdock.workflow import DiscDockService

SERIES = "Desperate Housewives (2004)"


def _episodes(folder: Path, names: list[str]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / name).write_bytes(b"episode")


def test_a_series_folder_lists_its_seasons_and_where_each_continues(tmp_path: Path) -> None:
    series = tmp_path / SERIES
    _episodes(series / "Season 01", [f"{SERIES} - S01E{number:02d}.mkv" for number in range(1, 17)])
    # Moved into a season folder by hand without renaming: still counted.
    _episodes(series / "Season 2", [f"{SERIES} - Episode 08.mkv"])
    (series / "Extras").mkdir()

    assert series_seasons(series) == [
        SeriesSeason(1, series / "Season 01", 16, 17),
        SeriesSeason(2, series / "Season 2", 1, 9),
    ]


def test_episodes_lying_loose_in_the_series_folder_count_as_no_season(tmp_path: Path) -> None:
    series = tmp_path / SERIES
    _episodes(series, [f"{SERIES} - Episode {number:02d}.mkv" for number in range(1, 17)])

    assert series_seasons(series) == [SeriesSeason(None, series, 16, 17)]
    assert series_seasons(tmp_path / "No such series") == []


def test_a_seasons_episodes_are_named_and_numbered_on_inside_its_folder(tmp_path: Path) -> None:
    season = tmp_path / "completed" / "tv" / "Noddy (1992)" / "Season 02"
    _episodes(season, ["Noddy (1992) - S02E01.mkv"])
    staging = tmp_path / "raw" / "more.partial"
    staging.mkdir(parents=True)
    (staging / "title_t02.mkv").write_bytes(b"third")
    (staging / "title_t01.mkv").write_bytes(b"second")

    renamed = rename_video_outputs(staging, "Noddy (1992)", MediaKind.SERIES, existing=season, season=2)

    assert [path.name for path in renamed] == ["Noddy (1992) - S02E02.mkv", "Noddy (1992) - S02E03.mkv"]
    assert (staging / "Noddy (1992) - S02E02.mkv").read_bytes() == b"second"


def test_a_new_season_starts_at_its_first_episode(tmp_path: Path) -> None:
    staging = tmp_path / "raw" / "new.partial"
    staging.mkdir(parents=True)
    (staging / "title_t00.mkv").write_bytes(b"one")

    renamed = rename_video_outputs(staging, "Noddy (1992)", MediaKind.SERIES, existing=tmp_path / "Season 03", season=3)

    assert [path.name for path in renamed] == ["Noddy (1992) - S03E01.mkv"]


class _Database(MemoryDatabase):
    def update_job(self, job_id: str, **changes: Any) -> dict[str, Any]:
        job = super().update_job(job_id, **changes)
        for field in ("settings", "metadata"):
            if f"{field}_json" in changes:
                job[field] = json.loads(changes[f"{field}_json"])
        return job


def _identified(media_kind: MediaKind) -> dict[str, Any]:
    return {
        "user_selected": True,
        "provider": "omdb",
        "provider_id": "tt0410975",
        "title": "Desperate Housewives",
        "year": "2004",
        "media_kind": media_kind.value,
    }


def _process_service(tmp_path: Path, media_kind: MediaKind) -> tuple[DiscDockService, _Database, list[str]]:
    settings = AppSettings(data_root=tmp_path, omdb_enabled=False, auto_eject=False, min_length_seconds=600)
    settings.resolved_directories()["logs"].mkdir(parents=True)
    job = make_job(
        settings, tmp_path / "raw" / "disc.partial", title="", year="", fingerprint="", metadata=_identified(media_kind)
    )
    database = _Database(job)
    drive = DriveInfo(
        id="drive-id", letter="D:", name="Test drive", media_loaded=True, volume_label="DH10EUT4", disc_kind=DiscKind.DVD
    )
    service = DiscDockService.__new__(DiscDockService)
    service.database = database
    service.settings = settings
    service.secret_store = SimpleNamespace(all=dict)
    service.drives = {drive.id: drive}
    service._drive_locks = defaultdict(asyncio.Lock)
    service.notifications = FakeNotifications()
    service.drive_control = SimpleNamespace()
    service._interruptions = {}
    titles = [TitleInfo(id=index, duration_seconds=2580, size_bytes=1_500_000_000, chapters=6) for index in range(4)]

    class Disc:
        async def inspect(self, *_args: Any) -> DiscScan:
            return DiscScan(drive_index=0, disc_name="DH10EUT4", title_count=4, titles=titles)

    ripped: list[str] = []

    async def rip_and_finish(job_id: str, *_args: Any, **_kwargs: Any) -> None:
        ripped.append(job_id)

    service._make_mkv = lambda active_settings=None: Disc()  # type: ignore[method-assign]
    service._rip_and_finish = rip_and_finish  # type: ignore[method-assign]
    return service, database, ripped


@pytest.mark.asyncio
async def test_a_series_disc_waits_to_be_told_which_season_it_belongs_to(tmp_path: Path) -> None:
    service, database, ripped = _process_service(tmp_path, MediaKind.SERIES)
    series = tmp_path / "completed" / "tv" / SERIES
    _episodes(series, [f"{SERIES} - Episode {number:02d}.mkv" for number in range(1, 17)])

    await service._process_job("job-id")

    assert ripped == [], "nothing is ripped before the season is chosen"
    assert database.job["state"] == JobState.AWAITING_INPUT
    assert database.job["status_detail"] == f"Choose where these episodes go in {SERIES}"
    offer = database.job["metadata"]["series_offer"]
    assert offer["series"] == SERIES and Path(offer["series_path"]) == series.resolve()
    assert offer["seasons"] == [{"season": None, "folder": SERIES, "episodes": 16, "next_episode": 17}]
    assert (offer["next_season"], offer["default_season"]) == (1, 0), "loose episodes continue where they are"
    assert all(track["selected"] for track in database.tracks), "the episodes are ticked as usual"


@pytest.mark.asyncio
async def test_a_series_with_season_folders_suggests_the_latest_one(tmp_path: Path) -> None:
    service, database, _ = _process_service(tmp_path, MediaKind.SERIES)
    series = tmp_path / "completed" / "tv" / SERIES
    _episodes(series / "Season 01", [f"{SERIES} - S01E{number:02d}.mkv" for number in range(1, 24)])
    _episodes(series / "Season 02", [f"{SERIES} - S02E{number:02d}.mkv" for number in range(1, 9)])

    await service._process_job("job-id")

    offer = database.job["metadata"]["series_offer"]
    assert [(season["season"], season["next_episode"]) for season in offer["seasons"]] == [(1, 24), (2, 9)]
    assert (offer["next_season"], offer["default_season"]) == (3, 2)


@pytest.mark.asyncio
async def test_the_first_disc_of_a_series_suggests_a_first_season(tmp_path: Path) -> None:
    service, database, _ = _process_service(tmp_path, MediaKind.SERIES)

    await service._process_job("job-id")

    offer = database.job["metadata"]["series_offer"]
    assert offer["seasons"] == [] and (offer["next_season"], offer["default_season"]) == (1, 1)


@pytest.mark.asyncio
async def test_a_movie_is_never_asked_about_seasons(tmp_path: Path) -> None:
    service, database, ripped = _process_service(tmp_path, MediaKind.MOVIE)
    (tmp_path / "completed" / "movies" / SERIES).mkdir(parents=True)

    await service._process_job("job-id")

    assert ripped == ["job-id"] and "series_offer" not in database.job["metadata"]


def _waiting_service(tmp_path: Path, metadata: dict[str, Any]) -> tuple[DiscDockService, _Database, list[str]]:
    settings = AppSettings(data_root=tmp_path)
    job = make_job(
        settings,
        tmp_path / "raw" / "disc.partial",
        title="Desperate Housewives",
        year="2004",
        media_kind=MediaKind.SERIES.value,
        state=JobState.AWAITING_INPUT,
        metadata=metadata,
    )
    database = _Database(job)
    database.tracks = [{"source_id": index, "selected": True} for index in range(4)]
    service = DiscDockService.__new__(DiscDockService)
    service.database = database
    service.settings = settings
    service.drives = {
        "drive-id": DriveInfo(
            id="drive-id", letter="D:", name="Drive", media_loaded=True, volume_label="DH10EUT4", disc_kind=DiscKind.DVD
        )
    }
    service._tasks = {}
    service._job_task_finished = lambda *args: None  # type: ignore[method-assign]
    resumed: list[str] = []

    async def resume(job_id: str, drive: DriveInfo) -> None:
        del drive
        resumed.append(job_id)

    service._resume_job = resume  # type: ignore[method-assign]
    return service, database, resumed


@pytest.mark.asyncio
async def test_choosing_a_season_rips_into_it(tmp_path: Path) -> None:
    series = tmp_path / "completed" / "tv" / SERIES
    service, database, resumed = _waiting_service(tmp_path, {"series_offer": {"series_path": str(series)}})

    with pytest.raises(RuntimeError, match="season"):
        await service.continue_job("job-id", [0, 1, 2, 3], series_season=100)
    await service.continue_job("job-id", [0, 1, 2, 3], series_season=2)
    await service._tasks["job-id"]

    assert resumed == ["job-id"] and database.job["state"] == JobState.QUEUED
    metadata = database.job["metadata"]
    assert metadata["series_destination"] == {"series_path": str(series), "season": 2}
    assert "series_offer" not in metadata


@pytest.mark.asyncio
async def test_a_series_named_by_hand_is_asked_about_its_season_before_ripping(tmp_path: Path) -> None:
    # The disc waited to be identified; the title chosen in the dashboard is a series.
    service, database, resumed = _waiting_service(tmp_path, {})

    await service.continue_job("job-id", [0, 1, 2, 3])

    assert resumed == [] and database.job["state"] == JobState.AWAITING_INPUT
    assert database.job["metadata"]["series_offer"]["default_season"] == 1
    assert database.job["status_detail"] == f"Choose where these episodes go in {SERIES}"


def _ripping_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, season: int) -> tuple[DiscDockService, _Database, DriveInfo, AppSettings]:
    series = tmp_path / "completed" / "tv" / SERIES
    settings = AppSettings(
        data_root=tmp_path, completed_directory=tmp_path / "completed", auto_eject=False, skip_transcode=True
    )
    job = make_job(
        settings,
        tmp_path / "raw" / "disc.partial",
        title="Desperate Housewives",
        year="2004",
        media_kind=MediaKind.SERIES.value,
        metadata={"series_destination": {"series_path": str(series), "season": season}},
    )
    database = _Database(job)
    database.tracks = [{"source_id": index, "selected": True, "size_bytes": 1_000} for index in (1, 2)]
    service = DiscDockService.__new__(DiscDockService)
    service.database = database
    service.runner = object()
    service.notifications = FakeNotifications()
    service.drive_control = SimpleNamespace()

    class FakeMakeMKV:
        async def rip(self, job_id: str, letter: str, destination: Path, selected_ids: list[int], timeout: int, **kwargs: Any) -> None:
            del job_id, letter, timeout, kwargs
            destination.mkdir(parents=True, exist_ok=True)
            for title in selected_ids:
                (destination / f"title_t{title:02d}.mkv").write_bytes(f"episode {title}".encode())

    async def verified(folder: Path, ffprobe_path: str) -> list[Path]:
        del ffprobe_path
        return sorted(folder.glob("*.mkv"))

    service._make_mkv = lambda active_settings=None: FakeMakeMKV()  # type: ignore[method-assign]
    monkeypatch.setattr(workflow_module, "verify_outputs", verified)
    monkeypatch.setattr(workflow_module, "disk_space_ok", lambda path, required: True)
    drive = DriveInfo(id="drive-id", letter="D:", name="Drive", media_loaded=True, volume_label="DH", disc_kind=DiscKind.DVD)
    return service, database, drive, settings


@pytest.mark.asyncio
async def test_a_new_season_gets_its_own_folder_in_the_series(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    series = tmp_path / "completed" / "tv" / SERIES
    _episodes(series / "Season 01", [f"{SERIES} - S01E01.mkv"])
    service, database, drive, settings = _ripping_service(tmp_path, monkeypatch, season=2)

    await service._rip_and_finish("job-id", drive, settings)

    assert Path(database.job["output_path"]) == (series / "Season 02").resolve()
    assert sorted(path.name for path in (series / "Season 02").glob("*.mkv")) == [
        f"{SERIES} - S02E01.mkv",
        f"{SERIES} - S02E02.mkv",
    ]
    assert [path.name for path in (series / "Season 01").glob("*.mkv")] == [f"{SERIES} - S01E01.mkv"]


@pytest.mark.asyncio
async def test_without_a_season_folder_the_episodes_continue_in_the_series_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    series = tmp_path / "completed" / "tv" / SERIES
    _episodes(series, [f"{SERIES} - Episode {number:02d}.mkv" for number in range(1, 17)])
    service, database, drive, settings = _ripping_service(tmp_path, monkeypatch, season=0)

    await service._rip_and_finish("job-id", drive, settings)

    assert Path(database.job["output_path"]) == series.resolve()
    names = sorted(path.name for path in series.glob("*.mkv"))
    assert names[-2:] == [f"{SERIES} - Episode 17.mkv", f"{SERIES} - Episode 18.mkv"] and len(names) == 18
    assert (series / f"{SERIES} - Episode 17.mkv").read_bytes() == b"episode 1"


@pytest.mark.asyncio
async def test_a_season_folder_named_by_hand_is_used_as_it_is(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    series = tmp_path / "completed" / "tv" / SERIES
    _episodes(series / "Season 2", [f"{SERIES} - Episode 08.mkv"])
    service, _, drive, settings = _ripping_service(tmp_path, monkeypatch, season=2)

    await service._rip_and_finish("job-id", drive, settings)

    assert not (series / "Season 02").exists(), "no second folder for the same season"
    assert sorted(path.name for path in (series / "Season 2").glob("*.mkv")) == [
        f"{SERIES} - Episode 08.mkv",
        f"{SERIES} - S02E09.mkv",
        f"{SERIES} - S02E10.mkv",
    ]
