from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import discdock.workflow as workflow_module
from discdock.makemkv import DiscScan, NoVideoTitles
from discdock.models import DiscKind, DriveInfo, JobState, MediaKind, TitleInfo
from discdock.processes import ProcessResult
from discdock.settings import AppSettings
from discdock.workflow import (
    DiscDockService,
    _is_disc_read_warning,
    _user_metadata_candidate,
    select_disc_titles,
)


class MemoryDatabase:
    def __init__(self, job: dict[str, Any], duplicate: dict[str, Any] | None = None):
        self.job = job
        self.duplicate = duplicate
        self.events: list[tuple[str | None, str, dict[str, Any]]] = []
        self.tracks: list[dict[str, Any]] = []

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return self.job if self.job["id"] == job_id else None

    def update_job(self, job_id: str, **changes: Any) -> dict[str, Any]:
        assert job_id == self.job["id"]
        self.job.update(changes)
        return self.job

    def list_tracks(self, job_id: str) -> list[dict[str, Any]]:
        assert job_id == self.job["id"]
        return self.tracks

    def list_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        del limit
        return [self.job] if "drive_id" in self.job else []

    def replace_tracks(self, job_id: str, tracks: list[dict[str, Any]]) -> None:
        assert job_id == self.job["id"]
        self.tracks = tracks

    def query(self, sql: str, parameters: tuple = ()) -> list[dict[str, Any]]:
        del parameters
        return [self.duplicate] if self.duplicate and "FROM jobs" in sql else []

    def upsert_drive(self, drive: dict[str, Any]) -> None:
        del drive

    def append_event(self, job_id: str | None, event_type: str, payload: dict[str, Any]) -> None:
        self.events.append((job_id, event_type, payload))


class FakeNotifications:
    def __init__(self) -> None:
        self.sent: list[tuple[str | None, str, str, str]] = []

    async def send(self, job_id: str | None, event_type: str, title: str, body: str) -> bool:
        self.sent.append((job_id, event_type, title, body))
        return True


def make_job(settings: AppSettings, staging: Path, **overrides: Any) -> dict[str, Any]:
    job: dict[str, Any] = {
        "id": "job-id",
        "drive_id": "drive-id",
        "drive_letter": "D:",
        "disc_label": "TEST_DISC",
        "disc_type": DiscKind.DVD.value,
        "fingerprint": "abcdef1234567890",
        "title": "Test title",
        "year": "2024",
        "media_kind": MediaKind.MOVIE.value,
        "state": JobState.DETECTED,
        "stage": "detected",
        "status_detail": "",
        "staging_path": str(staging),
        "settings": settings.public_dict(),
        "metadata": {},
    }
    job.update(overrides)
    return job


def test_main_feature_uses_metadata_runtime_and_collapses_alternate_angles(tmp_path: Path) -> None:
    settings = AppSettings(
        data_root=tmp_path,
        main_feature=True,
        extras=True,
        min_length_seconds=600,
        max_length_seconds=20_000,
    )
    titles = [
        TitleInfo(id=0, duration_seconds=4978, size_bytes=4_577_600_000, chapters=20),
        TitleInfo(id=1, duration_seconds=4978, size_bytes=4_577_317_000, chapters=20, angle=2),
        TitleInfo(id=2, duration_seconds=4978, size_bytes=4_577_442_000, chapters=20, angle=3),
        TitleInfo(id=3, duration_seconds=5860, size_bytes=4_826_879_000, chapters=24),
        TitleInfo(id=4, duration_seconds=5860, size_bytes=4_826_596_000, chapters=24, angle=2),
        TitleInfo(id=5, duration_seconds=5860, size_bytes=4_826_721_000, chapters=24, angle=3),
    ]

    selected = select_disc_titles(titles, settings, runtime_minutes=87)

    assert [title.id for title in selected] == [0]


def test_main_feature_selects_exactly_one_even_when_extras_is_enabled(tmp_path: Path) -> None:
    settings = AppSettings(
        data_root=tmp_path,
        main_feature=True,
        extras=True,
        min_length_seconds=600,
        max_length_seconds=20_000,
    )
    titles = [
        TitleInfo(id=1, duration_seconds=5113, size_bytes=3_000_000_000, chapters=25),
        TitleInfo(id=4, duration_seconds=995, size_bytes=700_000_000, chapters=4),
        TitleInfo(id=6, duration_seconds=684, size_bytes=500_000_000, chapters=3),
    ]

    selected = select_disc_titles(titles, settings)

    assert [title.id for title in selected] == [1]


def test_a_job_waiting_for_a_choice_does_not_keep_discdock_busy(tmp_path: Path) -> None:
    settings = AppSettings(data_root=tmp_path)
    job = make_job(settings, tmp_path / "raw" / "job.partial", state=JobState.RIPPING)
    service = DiscDockService.__new__(DiscDockService)
    service.database = MemoryDatabase(job)

    assert service.busy_jobs() == ["Test title"]

    job["state"] = JobState.AWAITING_INPUT
    assert service.busy_jobs() == []


def test_user_selected_metadata_is_recognized_during_a_running_job() -> None:
    candidate = _user_metadata_candidate(
        {
            "metadata": {
                "provider": "omdb",
                "provider_id": "tt0429589",
                "title": "The Ant Bully",
                "year": "2006",
                "media_kind": "movie",
                "poster_url": "https://example.test/poster.jpg",
                "plot": "",
                "runtime_minutes": 88,
                "user_selected": True,
            }
        }
    )

    assert candidate is not None
    assert candidate.title == "The Ant Bully"
    assert candidate.user_selected is True


def test_uncorrectable_medium_error_is_a_dashboard_warning() -> None:
    assert _is_disc_read_warning("Scsi error - MEDIUM ERROR:L-EC UNCORRECTABLE ERROR")
    assert _is_disc_read_warning("OS error - STATUS_DEVICE_DATA_ERROR")
    assert not _is_disc_read_warning("Saving to MKV file")


def test_dvd_recovery_title_number_is_restored_from_an_existing_job_log(
    tmp_path: Path,
) -> None:
    settings = AppSettings(data_root=tmp_path)
    logs = settings.resolved_directories()["logs"]
    logs.mkdir(parents=True)
    (logs / "old-job.log").write_text(
        '2026-09-12T11:02:19.626+00:00 TINFO:0,24,0,"31"\n',
        encoding="utf-8",
    )

    recovered = DiscDockService._dvd_recovery_title_number(
        "old-job", {"source_id": 0, "disc_title_number": 0}, settings
    )

    assert recovered == 31


def test_dvd_recovery_prefers_the_persisted_disc_title_number(tmp_path: Path) -> None:
    settings = AppSettings(data_root=tmp_path)

    recovered = DiscDockService._dvd_recovery_title_number(
        "new-job", {"source_id": 0, "disc_title_number": 42}, settings
    )

    assert recovered == 42


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route_mode", "error_code", "expected_target", "expected_route"),
    [
        ("vlc_best_effort", "damaged_disc_recovery_failed", "vlc", "sector_best_effort"),
        ("ai_repair", "ai_repair_preparation_failed", "ai", "ai_repair"),
    ],
)
async def test_retry_returns_to_the_selected_recovery_route(
    tmp_path: Path,
    route_mode: str,
    error_code: str,
    expected_target: str,
    expected_route: str,
) -> None:
    settings = AppSettings(data_root=tmp_path)
    job = make_job(
        settings,
        tmp_path / "failed" / "attempt",
        state=JobState.FAILED,
        stage="failed",
        error_code=error_code,
        metadata={
            "recovery_route": {"mode": route_mode, "status": "failed"},
            "recovery": {"mode": "vlc_best_effort"},
        },
    )
    database = MemoryDatabase(job)
    service = DiscDockService.__new__(DiscDockService)
    service.database = database
    service.drives = {
        "drive-id": DriveInfo(
            id="drive-id",
            letter="D:",
            name="Test DVD drive",
            media_loaded=True,
            volume_label="TEST_DISC",
            disc_kind=DiscKind.DVD,
        )
    }
    called: list[str] = []

    async def recover(job_id: str) -> dict[str, Any]:
        assert job_id == "job-id"
        called.append("vlc")
        return {"route": "vlc"}

    async def analyze(job_id: str) -> dict[str, Any]:
        assert job_id == "job-id"
        called.append("ai")
        return {"route": "ai"}

    service.recover_damaged_job = recover  # type: ignore[method-assign]
    service.prepare_ai_repair_job = analyze  # type: ignore[method-assign]

    result = await service.retry_job("job-id")

    assert called == [expected_target]
    assert result == {"route": expected_target}
    route = json.loads(database.job["metadata_json"])["recovery_route"]
    assert route["mode"] == expected_route
    assert route["status"] == "retrying"


@pytest.mark.asyncio
async def test_retry_scans_the_disc_again_with_the_settings_as_they_are_now(tmp_path: Path) -> None:
    first_attempt = AppSettings(data_root=tmp_path, min_length_seconds=600)
    job = make_job(
        first_attempt,
        tmp_path / "raw" / "first.partial",
        state=JobState.FAILED,
        stage="failed",
        error_code="media_tool_failed",
        metadata={"short_titles_only": True},
    )
    job["settings"]["manual"] = True
    database = MemoryDatabase(job)
    service = DiscDockService.__new__(DiscDockService)
    service.database = database
    service.settings = AppSettings(data_root=tmp_path, main_feature=False, extras=True, min_length_seconds=0)
    service.drives = {
        "drive-id": DriveInfo(
            id="drive-id", letter="D:", name="Test drive", media_loaded=True, volume_label="TEST_DISC", disc_kind=DiscKind.DVD
        )
    }
    service._tasks = {}
    service._job_task_finished = lambda *args: None  # type: ignore[method-assign]
    scanned_with: list[AppSettings] = []

    async def process(job_id: str, manual: bool = False) -> None:
        del manual
        scanned_with.append(AppSettings.model_validate(json.loads(database.job["settings_json"])))

    service._process_job = process  # type: ignore[method-assign]

    await service.retry_job("job-id")
    await service._tasks["job-id"]

    assert scanned_with[0].min_length_seconds == 0, "the first attempt's 10-minute minimum is not reused"
    assert scanned_with[0].extras and not scanned_with[0].main_feature
    assert json.loads(database.job["settings_json"])["manual"] is True
    assert "short_titles_only" not in json.loads(database.job["metadata_json"])


@pytest.mark.asyncio
async def test_a_disc_with_only_titles_shorter_than_the_minimum_lets_you_choose(tmp_path: Path) -> None:
    settings = AppSettings(data_root=tmp_path, min_length_seconds=600, omdb_enabled=False, auto_eject=False)
    settings.resolved_directories()["logs"].mkdir(parents=True)
    job = make_job(settings, tmp_path / "raw" / "short.partial", title="", year="", fingerprint="")

    class Database(MemoryDatabase):
        def update_job(self, job_id: str, **changes: Any) -> dict[str, Any]:
            job = super().update_job(job_id, **changes)
            for field in ("settings", "metadata"):
                if f"{field}_json" in changes:
                    job[field] = json.loads(changes[f"{field}_json"])
            return job

    database = Database(job)
    drive = DriveInfo(
        id="drive-id", letter="D:", name="Test drive", media_loaded=True, volume_label="NODDY", disc_kind=DiscKind.DVD
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
    episodes = [TitleInfo(id=index, duration_seconds=540 + index, size_bytes=300_000_000, chapters=1) for index in range(3)]
    scans: list[int] = []

    class ShortEpisodesDisc:
        async def inspect(self, owner_id: str, letter: str, min_length: int, *args: Any) -> DiscScan:
            del owner_id, letter, args
            scans.append(min_length)
            if min_length > 0:
                raise NoVideoTitles("MakeMKV found no video titles", ProcessResult(args=[], return_code=0), 3)
            return DiscScan(drive_index=0, disc_name="NODDY", title_count=3, titles=episodes)

    service._make_mkv = lambda active_settings=None: ShortEpisodesDisc()  # type: ignore[method-assign]

    await service._process_job("job-id")

    assert scans == [600, 0]
    assert database.job["state"] == JobState.AWAITING_INPUT
    assert "shorter than the minimum length" in database.job["status_detail"]
    assert len(database.tracks) == 3 and any(track["selected"] for track in database.tracks)
    # MakeMKV numbers titles within its length filter, so the rip must not use the minimum either.
    assert database.job["settings"]["min_length_seconds"] == 0
    assert database.job["metadata"]["short_titles_only"] is True


@pytest.mark.asyncio
async def test_ai_repair_uses_saved_bluray_type_after_drive_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = AppSettings(data_root=tmp_path, auto_eject=False)
    staging = tmp_path / "failed" / "bluray-partial"
    staging.mkdir(parents=True)
    (tmp_path / "logs").mkdir()
    source = staging / "partial.mkv"
    source.write_bytes(b"partial")
    job = make_job(
        settings,
        staging,
        disc_type=DiscKind.BLURAY.value,
        state=JobState.FAILED,
        stage="failed",
    )
    database = MemoryDatabase(job)
    database.tracks = [
        {
            "source_id": 0,
            "selected": True,
            "duration_seconds": 5400,
            "size_bytes": 20_000_000_000,
        }
    ]
    notifications = FakeNotifications()
    service = DiscDockService.__new__(DiscDockService)
    service.database = database
    service.settings = settings
    service.secret_store = SimpleNamespace(all=dict)
    service.notifications = notifications
    service.drive_control = SimpleNamespace()
    service._interruptions = {}
    drive = DriveInfo(
        id="drive-id",
        letter="D:",
        name="Empty test drive",
        media_loaded=False,
        volume_label="",
        disc_kind=DiscKind.UNKNOWN,
    )

    recovered: list[Path] = []

    async def archive(*_args: Any) -> None:
        return None

    async def recover(job_id, active_drive, active_settings, destination, main_track) -> None:
        recovered.append(destination)
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "movie.mkv").write_bytes(b"movie")

    service._archive_previous_attempt = archive  # type: ignore[method-assign]
    service._recover_disc_to_staging = recover  # type: ignore[method-assign]
    monkeypatch.setattr(workflow_module, "find_repair_source", lambda folder: folder / "movie.mkv")
    monkeypatch.setattr(
        workflow_module,
        "analyze_repair",
        lambda *args, **kwargs: {
            "estimate_id": "estimate-123",
            "status": "awaiting_confirmation",
            "frame_count": 6,
            "ai_keyframe_count": 1,
            "estimated_max_cost_usd": 0.08,
            "segments": [],
        },
    )

    await service._start_ai_repair_preparation(job["id"], drive, settings)

    stored_metadata = json.loads(database.job["metadata_json"])
    assert database.job["state"] == JobState.AWAITING_REPAIR
    # Blu-rays go through the rescue image as well, so the movie continues past
    # the damage instead of ending where MakeMKV stopped.
    assert recovered and Path(database.job["staging_path"]) == recovered[0]
    assert stored_metadata["ai_repair"]["disc_type"] == DiscKind.BLURAY.value


@pytest.mark.asyncio
async def test_insertion_during_active_job_starts_when_that_job_finishes() -> None:
    service = DiscDockService.__new__(DiscDockService)
    drive = DriveInfo(
        id="drive-id",
        letter="D:",
        name="Test drive",
        media_loaded=True,
        volume_label="NEXT_DISC",
        disc_kind=DiscKind.DVD,
    )
    database = MemoryDatabase({"id": "unused"})
    service.settings = SimpleNamespace(auto_rip=True)
    service.database = database
    service.drives = {drive.id: drive}
    service._pending_insertions = {}
    service._shutting_down = False
    service._tasks = {}
    active: dict[str, Any] | None = {
        "id": "old-job",
        "drive_id": drive.id,
        "state": JobState.TRANSCODING,
    }
    service.active_job_for_drive = lambda drive_id: active  # type: ignore[method-assign]
    started = asyncio.Event()

    async def create_job(
        drive_id: str,
        manual: bool = False,
        media_kind: MediaKind | None = None,
        *,
        refresh: bool = True,
    ) -> dict[str, Any]:
        del manual, media_kind, refresh
        assert drive_id == drive.id
        started.set()
        return {"id": "new-job"}

    service.create_job = create_job  # type: ignore[method-assign]

    await service._on_inserted(drive)
    assert drive.id in service._pending_insertions
    assert not started.is_set()

    active = None
    previous_task = asyncio.create_task(asyncio.sleep(0))
    service._track_job_task("old-job", drive.id, previous_task)
    await previous_task
    await asyncio.wait_for(started.wait(), timeout=1)

    assert drive.id not in service._pending_insertions


@pytest.mark.asyncio
async def test_auto_mode_resumes_instead_of_duplicating_a_recoverable_job() -> None:
    drive = DriveInfo(
        id="drive-id",
        letter="D:",
        name="Test drive",
        media_loaded=True,
        volume_label="DAMAGED_DISC",
        disc_kind=DiscKind.DVD,
    )
    job = {
        "id": "recoverable-job",
        "drive_id": drive.id,
        "disc_label": drive.volume_label,
        "state": JobState.FAILED,
        "recoverable": True,
    }
    service = DiscDockService.__new__(DiscDockService)
    service.settings = SimpleNamespace(auto_rip=True)
    service.database = MemoryDatabase(job)
    service.drives = {drive.id: drive}
    service._pending_insertions = {}
    service._shutting_down = False
    service._tasks = {}
    started = False
    resumed = False

    async def create_job(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal started
        del args, kwargs
        started = True
        return {"id": "duplicate"}

    service.create_job = create_job  # type: ignore[method-assign]

    async def retry_job(job_id: str) -> dict[str, Any]:
        nonlocal resumed
        assert job_id == job["id"]
        resumed = True
        return job

    service.retry_job = retry_job  # type: ignore[method-assign]

    await service._on_inserted(drive)

    assert started is False
    assert resumed is True
    assert drive.id not in service._pending_insertions
    assert service.database.events[-1][1] == "drive.recovery_autostarted"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy", "expected_state", "expected_stage", "notification_type"),
    [
        ("ask", JobState.BLOCKED, "duplicate", "attention"),
        ("skip", JobState.COMPLETED, "skipped_duplicate", "completed"),
    ],
)
async def test_duplicate_ask_blocks_while_skip_completes_without_ripping(
    tmp_path: Path,
    policy: str,
    expected_state: JobState,
    expected_stage: str,
    notification_type: str,
) -> None:
    settings = AppSettings(
        data_root=tmp_path,
        duplicate_policy=policy,
        omdb_enabled=False,
        auto_eject=False,
        skip_transcode=True,
    )
    job = make_job(settings, tmp_path / "raw" / "job.partial", title="", year="")
    duplicate = {
        "id": "completed-job",
        "title": "Existing rip",
        "year": "2024",
        "media_kind": MediaKind.MOVIE.value,
        "output_path": str(tmp_path / "completed" / "movies" / "Existing rip"),
    }
    database = MemoryDatabase(job, duplicate)
    notifications = FakeNotifications()
    service = DiscDockService.__new__(DiscDockService)
    drive = DriveInfo(
        id="drive-id",
        letter="D:",
        name="Test drive",
        media_loaded=True,
        volume_label="EXISTING_RIP",
        disc_kind=DiscKind.DVD,
    )
    service.database = database
    service.drives = {drive.id: drive}
    service._drive_locks = defaultdict(asyncio.Lock)
    service.notifications = notifications
    service.drive_control = SimpleNamespace()
    service._interruptions = {}

    scan = DiscScan(
        drive_index=0,
        disc_name="EXISTING_RIP",
        titles=[TitleInfo(id=0, duration_seconds=3600, size_bytes=10_000_000, chapters=8)],
    )

    class FakeMakeMKV:
        async def inspect(self, *args: Any, **kwargs: Any) -> DiscScan:
            del args, kwargs
            return scan

    service._make_mkv = lambda active_settings=None: FakeMakeMKV()  # type: ignore[method-assign]

    await service._process_job(job["id"])

    assert database.job["state"] == expected_state
    assert database.job["stage"] == expected_stage
    if policy == "ask":
        assert database.job["error_code"] == "duplicate"
    else:
        assert database.job["output_path"] == duplicate["output_path"]
    assert notifications.sent[0][1] == notification_type


@pytest.mark.asyncio
async def test_a_completed_disc_can_get_more_titles_for_its_folder(tmp_path: Path) -> None:
    settings = AppSettings(data_root=tmp_path, completed_directory=tmp_path / "completed")
    library = tmp_path / "completed" / "movies" / "Noddy (1992)"
    library.mkdir(parents=True)
    (library / "Noddy (1992).mkv").write_bytes(b"first episode")
    job = make_job(
        settings,
        tmp_path / "raw" / "again.partial",
        title="NODDY",
        year="",
        state=JobState.BLOCKED,
        stage="duplicate",
        error_code="duplicate",
        error_message="Previously completed as Noddy",
    )
    previous = {
        "id": "completed-job",
        "title": "Noddy",
        "year": "1992",
        "media_kind": MediaKind.MOVIE.value,
        "output_path": str(library),
    }

    class Database(MemoryDatabase):
        def list_tracks(self, job_id: str) -> list[dict[str, Any]]:
            if job_id == "completed-job":
                return [{"source_id": 0, "selected": True}, {"source_id": 1, "selected": False}]
            return super().list_tracks(job_id)

    database = Database(job, previous)
    database.tracks = [{"source_id": index, "selected": index == 0, "duration_seconds": 660} for index in range(3)]
    service = DiscDockService.__new__(DiscDockService)
    service.database = database

    await service.add_titles_to_completed_disc("job-id")

    assert database.job["state"] == JobState.AWAITING_INPUT
    assert (database.job["title"], database.job["year"]) == ("Noddy", "1992")
    assert [track["source_id"] for track in database.tracks if track["selected"]] == [1, 2], "the title ripped before stays unticked"
    assert json.loads(database.job["metadata_json"])["add_to_existing"] == {
        "job_id": "completed-job",
        "output_path": str(library),
        "ripped_titles": [0],
    }
    with pytest.raises(RuntimeError, match="not waiting"):
        await service.add_titles_to_completed_disc("job-id")


@pytest.mark.asyncio
async def test_added_titles_are_ripped_into_the_folder_of_the_completed_disc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    completed = tmp_path / "completed" / "tv" / "Noddy (1992)"
    completed.mkdir(parents=True)
    (completed / "Noddy (1992) - Episode 01.mkv").write_bytes(b"one")
    settings = AppSettings(
        data_root=tmp_path, completed_directory=tmp_path / "completed", auto_eject=False, skip_transcode=True
    )
    job = make_job(
        settings,
        tmp_path / "raw" / "more.partial",
        title="Noddy",
        year="1992",
        media_kind=MediaKind.SERIES.value,
        metadata={
            "duplicate_override": True,
            "add_to_existing": {"job_id": "completed-job", "output_path": str(completed), "ripped_titles": [0]},
        },
    )
    database = MemoryDatabase(job)
    database.tracks = [{"source_id": 1, "selected": True, "size_bytes": 1_000}, {"source_id": 2, "selected": True, "size_bytes": 1_000}]
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
    drive = DriveInfo(id="drive-id", letter="D:", name="Test drive", media_loaded=True, volume_label="NODDY", disc_kind=DiscKind.DVD)

    await service._rip_and_finish(job["id"], drive, settings)

    assert Path(database.job["output_path"]) == completed.resolve()
    assert sorted(path.name for path in completed.glob("*.mkv")) == [
        "Noddy (1992) - Episode 01.mkv",
        "Noddy (1992) - Episode 02.mkv",
        "Noddy (1992) - Episode 03.mkv",
    ]
    assert (completed / "Noddy (1992) - Episode 01.mkv").read_bytes() == b"one", "the files already there are untouched"
    assert (completed / "Noddy (1992) - Episode 02.mkv").read_bytes() == b"episode 1"


@pytest.mark.asyncio
async def test_completed_audio_uses_configured_music_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    music_root = tmp_path / "custom-music"
    settings = AppSettings(
        data_root=tmp_path,
        music_directory=music_root,
        completed_directory=tmp_path / "video-library",
        duplicate_policy="keep_both",
        auto_eject=False,
        skip_transcode=True,
    )
    staging = tmp_path / "raw" / "album.partial"
    job = make_job(
        settings,
        staging,
        disc_type=DiscKind.AUDIO_CD.value,
        media_kind=MediaKind.MUSIC.value,
        title="An Album",
        year="",
    )
    database = MemoryDatabase(job)
    notifications = FakeNotifications()
    service = DiscDockService.__new__(DiscDockService)
    service.database = database
    service.runner = object()
    service.notifications = notifications
    service.drive_control = SimpleNamespace()
    service.settings = settings
    service.secret_store = SimpleNamespace(all=dict)
    settings.resolved_directories()["logs"].mkdir(parents=True)

    class FakeAudioRipper:
        def __init__(self, executable: str, runner: object):
            del executable, runner

        async def rip(self, job_id: str, letter: str, destination: Path, **kwargs: Any) -> None:
            del job_id, letter, kwargs
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "track.flac").write_bytes(b"audio")

    async def verified(folder: Path, ffprobe_path: str) -> list[Path]:
        del ffprobe_path
        return [folder / "track.flac"]

    monkeypatch.setattr(workflow_module, "AudioRipper", FakeAudioRipper)
    monkeypatch.setattr(workflow_module, "verify_outputs", verified)
    monkeypatch.setattr(workflow_module, "disk_space_ok", lambda path, required: True)

    drive = DriveInfo(
        id="drive-id",
        letter="D:",
        name="Test drive",
        media_loaded=True,
        volume_label="AN_ALBUM",
        disc_kind=DiscKind.AUDIO_CD,
    )
    await service._rip_and_finish(job["id"], drive, settings)

    output = Path(database.job["output_path"])
    assert output == (music_root / "An Album").resolve()
    assert (output / "track.flac").read_bytes() == b"audio"
    assert not (tmp_path / "video-library" / "music").exists()


INTO_THE_GREAT_WIDE_OPEN = [
    {"id": "403427d8-6201-4831-a346-f7d910eead70", "title": "Into the Great Wide Open (BIEM / MCPS) (XE) (1991)"},
    {
        "id": "338e72dc-1cf5-4757-b874-a8cab8aa877a",
        "title": "Into the Great Wide Open (GEMA / BIEM, made by Sonopress) (XE) (1991-07)",
    },
]


@pytest.mark.asyncio
async def test_a_cd_that_waited_for_a_release_in_1_7_9_continues_with_it_as_its_album(tmp_path: Path) -> None:
    settings = AppSettings(data_root=tmp_path)
    job = make_job(
        settings,
        tmp_path / "raw" / "album.partial",
        disc_type=DiscKind.AUDIO_CD.value,
        state=JobState.AWAITING_INPUT,
        metadata={"musicbrainz_releases": INTO_THE_GREAT_WIDE_OPEN},
    )
    database = MemoryDatabase(job)
    service = DiscDockService.__new__(DiscDockService)
    service.database = database
    service.drives = {
        "drive-id": DriveInfo(
            id="drive-id", letter="E:", name="Drive", media_loaded=True, volume_label="", disc_kind=DiscKind.AUDIO_CD
        )
    }
    service._tasks = {}
    service._job_task_finished = lambda *args: None  # type: ignore[method-assign]
    resumed: list[str] = []

    async def resume(job_id: str, drive: DriveInfo) -> None:
        del drive
        resumed.append(job_id)

    service._resume_job = resume  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="Choose one of the releases"):
        await service.continue_job("job-id", musicbrainz_release="an-unknown-release")
    await service.continue_job("job-id", musicbrainz_release=INTO_THE_GREAT_WIDE_OPEN[0]["id"])
    await service._tasks["job-id"]

    assert resumed == ["job-id"]
    assert database.job["state"] == JobState.QUEUED
    metadata = json.loads(database.job["metadata_json"])
    assert metadata["album"]["id"] == INTO_THE_GREAT_WIDE_OPEN[0]["id"]
    assert "musicbrainz_releases" not in metadata


@pytest.mark.asyncio
async def test_replace_policy_replaces_the_previous_completed_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    completed = tmp_path / "completed" / "movies" / "Previous title"
    completed.mkdir(parents=True)
    (completed / "old.mkv").write_bytes(b"old")
    settings = AppSettings(
        data_root=tmp_path,
        completed_directory=tmp_path / "completed",
        duplicate_policy="replace",
        auto_eject=False,
        skip_transcode=True,
    )
    staging = tmp_path / "raw" / "replacement.partial"
    job = make_job(
        settings,
        staging,
        metadata={"duplicate": {"job_id": "previous-job", "output_path": str(completed)}},
    )
    database = MemoryDatabase(job)
    database.tracks = [
        {
            "source_id": 0,
            "selected": True,
            "size_bytes": 3_000_000_000,
        }
    ]
    notifications = FakeNotifications()
    service = DiscDockService.__new__(DiscDockService)
    service.database = database
    service.runner = object()
    service.notifications = notifications
    service.drive_control = SimpleNamespace()

    class FakeMakeMKV:
        async def rip(
            self,
            job_id: str,
            letter: str,
            destination: Path,
            selected_ids: list[int],
            timeout: int,
            **kwargs: Any,
        ) -> None:
            del job_id, letter, selected_ids, timeout, kwargs
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "new.mkv").write_bytes(b"new")

    async def verified(folder: Path, ffprobe_path: str) -> list[Path]:
        del ffprobe_path
        return [folder / "new.mkv"]

    service._make_mkv = lambda active_settings=None: FakeMakeMKV()  # type: ignore[method-assign]
    monkeypatch.setattr(workflow_module, "verify_outputs", verified)
    monkeypatch.setattr(workflow_module, "disk_space_ok", lambda path, required: True)

    drive = DriveInfo(
        id="drive-id",
        letter="D:",
        name="Test drive",
        media_loaded=True,
        volume_label="TEST_DISC",
        disc_kind=DiscKind.DVD,
    )
    await service._rip_and_finish(job["id"], drive, settings)

    assert Path(database.job["output_path"]) == completed.resolve()
    assert (completed / "Previous title.mkv").read_bytes() == b"new"
    assert not (completed / "old.mkv").exists()


@pytest.mark.asyncio
async def test_failure_moves_existing_staging_to_failed_directory(tmp_path: Path) -> None:
    settings = AppSettings(
        data_root=tmp_path,
        raw_directory=tmp_path / "raw",
        failed_directory=tmp_path / "quarantine",
    )
    staging = tmp_path / "raw" / "failed.partial"
    staging.mkdir(parents=True)
    (staging / "partial.mkv").write_bytes(b"partial")
    job = make_job(settings, staging)
    database = MemoryDatabase(job)
    notifications = FakeNotifications()
    service = DiscDockService.__new__(DiscDockService)
    service.database = database
    service.notifications = notifications
    service.settings = settings

    await service._fail(job["id"], "test_failure", "The rip failed")
    await asyncio.sleep(0)

    moved = Path(database.job["staging_path"])
    assert database.job["state"] == JobState.FAILED
    assert moved.is_relative_to((tmp_path / "quarantine").resolve())
    assert (moved / "partial.mkv").read_bytes() == b"partial"
    assert not staging.exists()


@pytest.mark.asyncio
async def test_failure_moves_staging_made_after_the_data_folder_was_changed(tmp_path: Path) -> None:
    created_with = AppSettings(data_root=tmp_path / "before")
    staging = tmp_path / "after" / "raw" / "job-id-6ad5f8c0.partial"
    staging.mkdir(parents=True)
    (staging / "track.flac").write_bytes(b"partial")
    job = make_job(created_with, staging)
    database = MemoryDatabase(job)
    service = DiscDockService.__new__(DiscDockService)
    service.database = database
    service.notifications = FakeNotifications()
    service.settings = AppSettings(data_root=tmp_path / "after")

    await service._fail(job["id"], "media_tool_failed", "Audio-CD ripping failed")
    await asyncio.sleep(0)

    moved = Path(database.job["staging_path"])
    assert moved.is_relative_to((tmp_path / "after" / "failed").resolve()), "not 'Path escapes configured root'"
    assert (moved / "track.flac").read_bytes() == b"partial"
