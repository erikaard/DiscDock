from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

import discdock.workflow as workflow_module
from discdock.disc_rescue import FINISHED, RescueMap
from discdock.makemkv import DiscScan
from discdock.media_tools import DvdSectorRescue
from discdock.models import DiscKind, DriveInfo, JobState, MediaKind, TitleInfo
from discdock.processes import ProcessFailure, ProcessResult
from discdock.settings import AppSettings
from discdock.workflow import DiscDockService


class MemoryDatabase:
    def __init__(self, job: dict[str, Any]):
        self.job = job
        self.tracks: list[dict[str, Any]] = []
        self.events: list[tuple[str | None, str, dict[str, Any]]] = []

    def append_event(self, job_id: str | None, event_type: str, payload: dict[str, Any]) -> int:
        self.events.append((job_id, event_type, payload))
        return len(self.events)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return self.job if self.job["id"] == job_id else None

    def update_job(self, job_id: str, **changes: Any) -> dict[str, Any]:
        assert job_id == self.job["id"]
        self.job.update(changes)
        if "metadata_json" in changes:
            self.job["metadata"] = json.loads(changes["metadata_json"])
        return self.job

    def list_tracks(self, job_id: str) -> list[dict[str, Any]]:
        assert job_id == self.job["id"]
        return self.tracks

    def list_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        del limit
        return [self.job]


class FakeNotifications:
    def __init__(self) -> None:
        self.sent: list[tuple[str | None, str, str, str]] = []

    async def send(self, job_id: str | None, event_type: str, title: str, body: str) -> bool:
        self.sent.append((job_id, event_type, title, body))
        return True


def make_service(settings: AppSettings, job: dict[str, Any]) -> tuple[DiscDockService, MemoryDatabase]:
    settings.resolved_directories()["logs"].mkdir(parents=True, exist_ok=True)
    database = MemoryDatabase(job)
    service = DiscDockService.__new__(DiscDockService)
    service.database = database
    service.settings = settings
    service.secret_store = SimpleNamespace(all=dict, get=lambda name: "test-key")
    service.notifications = FakeNotifications()
    service.drive_control = SimpleNamespace()
    service.runner = object()
    service.drives = {}
    service._interruptions = {}
    service._last_progress_write = {}
    service._last_progress_log = {}
    return service, database


def make_job(settings: AppSettings, staging: Path, **overrides: Any) -> dict[str, Any]:
    job: dict[str, Any] = {
        "id": "job-id",
        "drive_id": "drive-id",
        "drive_letter": "D:",
        "disc_label": "DAMAGED_DISC",
        "disc_type": DiscKind.DVD.value,
        "fingerprint": "abcdef1234567890",
        "title": "Damaged movie",
        "year": "2009",
        "media_kind": MediaKind.MOVIE.value,
        "state": JobState.RIPPING,
        "stage": "recovering",
        "status_detail": "",
        "staging_path": str(staging),
        "settings": settings.public_dict(),
        "metadata": {},
    }
    job.update(overrides)
    return job


def test_an_earlier_rescue_image_moves_into_the_jobs_rescue_folder(tmp_path: Path) -> None:
    settings = AppSettings(data_root=tmp_path)
    raw = settings.resolved_directories()["raw"]
    old = raw / "job-id-ai-source-1234.partial"
    old.mkdir(parents=True)
    (old / "rescued-disc.iso.part").write_bytes(b"x" * 4096)
    (old / "rescued-disc.iso.map.json").write_text("{}", encoding="utf-8")
    # A whole-disc rescue folder is left for _collect_rescue_images to combine.
    (raw / "job-id.rescue").mkdir()
    (raw / "job-id.rescue" / "rescued-disc.iso.part").write_bytes(b"y" * 8192)
    service, _ = make_service(settings, make_job(settings, old))
    image = service._rescue_image("job-id", settings)
    assert image.parent.name == "disc-abcdef1234567890.rescue", "one rescue image per disc, not per job"

    moved = DiscDockService._adopt_previous_rescue_image("job-id", settings, image, [])

    assert moved == old / "rescued-disc.iso.part"
    assert (image.parent / "rescued-disc.iso.part").read_bytes() == b"x" * 4096
    assert (image.parent / "rescued-disc.iso.map.json").is_file()
    assert not old.exists()
    assert DiscDockService._adopt_previous_rescue_image("job-id", settings, image, []) is None


@pytest.mark.asyncio
async def test_finish_now_signals_only_a_running_rescue(tmp_path: Path) -> None:
    settings = AppSettings(data_root=tmp_path)
    service, _ = make_service(settings, make_job(settings, tmp_path / "raw" / "attempt.partial"))

    with pytest.raises(RuntimeError, match="No damaged-disc rescue"):
        await service.finish_rescue_now("job-id")

    image = service._rescue_image("job-id", settings)
    image.parent.mkdir(parents=True)
    (image.parent / "rescued-disc.iso.part").write_bytes(b"x")
    await service.finish_rescue_now("job-id")

    assert (image.parent / "rescued-disc.iso.control").read_text(encoding="utf-8") == "finish"


@pytest.mark.asyncio
async def test_rescued_image_extraction_finds_the_movie_by_its_dvd_title_number(tmp_path: Path) -> None:
    settings = AppSettings(data_root=tmp_path, min_length_seconds=600)
    staging = tmp_path / "raw" / "attempt.partial"
    staging.mkdir(parents=True)
    image = tmp_path / "rescued-disc.iso"
    image.write_bytes(b"iso")
    service, _ = make_service(settings, make_job(settings, staging))
    calls: dict[str, Any] = {}

    class FakeMakeMKV:
        async def inspect_source(self, job_id, source, min_length, timeout, callback=None):
            calls["scan"] = (source, min_length)
            return DiscScan(
                titles=[
                    TitleInfo(id=0, disc_title_number=5, duration_seconds=300, chapters=2),
                    TitleInfo(id=1, disc_title_number=31, duration_seconds=8520, chapters=20),
                ]
            )

        async def rip_source(self, job_id, source, destination, title_ids, timeout, **kwargs):
            calls["rip"] = (source, title_ids, kwargs.get("min_length"))
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "title_t01.mkv").write_bytes(b"movie")
            return []

    service._make_mkv = lambda active_settings=None: FakeMakeMKV()  # type: ignore[method-assign]

    await service._extract_title_from_image(
        "job-id",
        "D:",
        settings,
        image,
        staging,
        {"source_id": 0, "disc_title_number": 31, "duration_seconds": 8614, "chapters": 20},
    )

    assert calls["scan"] == (f"iso:{image}", 120)
    assert calls["rip"] == (f"iso:{image}", [1], 120)
    assert (staging / "title_t01.mkv").read_bytes() == b"movie"
    assert not (staging / ".rescued-title.partial").exists()


@pytest.mark.asyncio
async def test_a_blu_ray_image_is_scanned_only_for_titles_about_as_long_as_the_movie(tmp_path: Path) -> None:
    settings = AppSettings(data_root=tmp_path, min_length_seconds=600)
    staging = tmp_path / "raw" / "attempt.partial"
    staging.mkdir(parents=True)
    image = tmp_path / "rescued-disc.iso"
    image.write_bytes(b"iso")
    service, _ = make_service(settings, make_job(settings, staging, disc_type=DiscKind.BLURAY.value))
    calls: dict[str, Any] = {}

    class FakeMakeMKV:
        no_output_timeout = 180

        async def inspect_source(self, job_id, source, min_length, timeout, callback=None):
            calls["scan"] = (min_length, self.no_output_timeout)
            return DiscScan(titles=[TitleInfo(id=0, duration_seconds=8976, chapters=24)])

        async def rip_source(self, job_id, source, destination, title_ids, timeout, **kwargs):
            calls["rip"] = (title_ids, kwargs.get("min_length"))
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "title_t00.mkv").write_bytes(b"movie")
            return []

    client = FakeMakeMKV()
    service._make_mkv = lambda active_settings=None: client  # type: ignore[method-assign]

    await service._extract_title_from_image(
        "job-id", "D:", settings, image, staging, {"source_id": 0, "duration_seconds": 8976, "chapters": 24}
    )

    assert calls["scan"] == (8078, 600), "unread extras are skipped, and MakeMKV may work silently for minutes"
    assert calls["rip"] == ([0], 8078)


@pytest.mark.asyncio
async def test_makemkv_stuck_on_one_spot_of_a_blu_ray_image_is_stopped_with_the_reason(tmp_path: Path) -> None:
    settings = AppSettings(data_root=tmp_path)
    staging = tmp_path / "raw" / "attempt.partial"
    staging.mkdir(parents=True)
    image = tmp_path / "rescued-disc.iso"
    image.write_bytes(b"iso")
    service, _ = make_service(settings, make_job(settings, staging, disc_type=DiscKind.BLURAY.value))
    cancelled: list[str] = []

    class Runner:
        async def cancel(self, owner_id: str) -> bool:
            cancelled.append(owner_id)
            return True

    service.runner = Runner()  # type: ignore[assignment]
    warning = (
        "MSG:4004,16777216,2,\"The source file '/BDMV/STREAM/00800.m2ts' is corrupt or invalid at offset 12288, "
        'attempting to work around"'
    )

    class StuckMakeMKV:
        async def inspect_source(self, job_id, source, min_length, timeout, callback=None):
            for _ in range(250):
                await callback({"type": "msg", "message": warning})
            raise ProcessFailure("MakeMKV could not inspect the disc", ProcessResult(args=[], return_code=1, cancelled=True))

    service._make_mkv = lambda active_settings=None: StuckMakeMKV()  # type: ignore[method-assign]

    with pytest.raises(workflow_module.ImageNotDecryptable, match="00800.m2ts at offset 12288"):
        await service._extract_title_from_image(
            "job-id", "D:", settings, image, staging, {"source_id": 0, "duration_seconds": 8976}
        )

    assert cancelled == ["job-id"], "MakeMKV is stopped once instead of working on one spot for hours"


@pytest.mark.asyncio
async def test_a_blu_ray_makemkv_cannot_decrypt_from_the_image_is_extracted_with_the_discs_attributes(
    tmp_path: Path,
) -> None:
    from test_disc_rescue import _bluray_image

    settings = AppSettings(data_root=tmp_path)
    staging = tmp_path / "raw" / "attempt.partial"
    staging.mkdir(parents=True)
    rescue = tmp_path / "raw" / "disc.rescue"
    rescue.mkdir(parents=True)
    image = rescue / "rescued-disc.iso"
    image.write_bytes(bytes(_bluray_image()))
    service, _ = make_service(settings, make_job(settings, staging, disc_type=DiscKind.BLURAY.value))
    service.drives = {"drive-id": _bluray_drive()}
    cancelled: list[str] = []

    class Runner:
        async def cancel(self, owner_id: str) -> bool:
            cancelled.append(owner_id)
            return True

    service.runner = Runner()  # type: ignore[assignment]
    warning = (
        "MSG:4004,16777216,2,\"The source file '/BDMV/STREAM/00800.m2ts' is corrupt or invalid at offset 0, "
        'attempting to work around"'
    )
    calls: dict[str, Any] = {}

    class ImageShyMakeMKV:
        async def inspect_source(self, job_id, source, min_length, timeout, callback=None):
            if source.startswith("iso:"):
                for _ in range(250):
                    await callback({"type": "msg", "message": warning})
                raise ProcessFailure("MakeMKV could not inspect the disc", ProcessResult(args=[], return_code=1))
            calls["scan"] = source
            return DiscScan(titles=[TitleInfo(id=0, duration_seconds=8976, chapters=24)])

        async def drive_index(self, owner_id, letter, timeout=900):
            return 0

        async def capture_disc_attributes(self, owner_id, drive_index, folder, timeout=1800):
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "discatt.dat").write_bytes(b"attributes")
            calls["capture"] = drive_index
            return folder / "discatt.dat"

        async def rip_source(self, job_id, source, destination, title_ids, timeout, **kwargs):
            calls["rip"] = (source, title_ids)
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "title_t00.mkv").write_bytes(b"movie")
            return []

    service._make_mkv = lambda active_settings=None: ImageShyMakeMKV()  # type: ignore[method-assign]
    track = {"source_id": 0, "duration_seconds": 8976, "chapters": 24, "source_filename": "00800.mpls"}

    await service._extract_title_from_image("job-id", "D:", settings, image, staging, track)

    folder = rescue / "makemkv-backup"
    assert cancelled == ["job-id"], "the stuck image scan is stopped"
    assert calls["capture"] == 0
    assert calls["scan"] == f"file:{folder}"
    assert calls["rip"] == (f"file:{folder}", [0])
    assert (folder / "BDMV" / "STREAM" / "00800.M2TS").is_file()
    assert (staging / "title_t00.mkv").read_bytes() == b"movie"


@pytest.mark.asyncio
async def test_a_retry_goes_straight_to_the_saved_decryption_information(tmp_path: Path) -> None:
    from test_disc_rescue import _bluray_image

    settings = AppSettings(data_root=tmp_path)
    staging = tmp_path / "raw" / "attempt.partial"
    staging.mkdir(parents=True)
    rescue = tmp_path / "raw" / "disc.rescue"
    folder = rescue / "makemkv-backup"
    folder.mkdir(parents=True)
    (folder / "discatt.dat").write_bytes(b"attributes")
    image = rescue / "rescued-disc.iso"
    image.write_bytes(bytes(_bluray_image()))
    service, _ = make_service(settings, make_job(settings, staging, disc_type=DiscKind.BLURAY.value))
    scans: list[str] = []

    class SavedFolderMakeMKV:
        async def inspect_source(self, job_id, source, min_length, timeout, callback=None):
            scans.append(source)
            return DiscScan(titles=[TitleInfo(id=0, duration_seconds=8976, chapters=24)])

        async def capture_disc_attributes(self, *args, **kwargs):
            raise AssertionError("the saved decryption information is reused")

        async def rip_source(self, job_id, source, destination, title_ids, timeout, **kwargs):
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "title_t00.mkv").write_bytes(b"movie")
            return []

    service._make_mkv = lambda active_settings=None: SavedFolderMakeMKV()  # type: ignore[method-assign]
    track = {"source_id": 0, "duration_seconds": 8976, "chapters": 24, "source_filename": "00800.mpls"}

    await service._extract_title_from_image("job-id", "D:", settings, image, staging, track)

    assert scans == [f"file:{folder}"], "the image MakeMKV could not decrypt is not scanned again"
    assert (staging / "title_t00.mkv").read_bytes() == b"movie"


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_a_blu_ray_makemkv_gives_up_on_is_copied_with_ffmpeg_past_the_damage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancelled: bool
) -> None:
    from test_disc_rescue import UDF_PARTITION, _bluray_image

    from discdock.disc_rescue import BAD

    settings = AppSettings(data_root=tmp_path)
    staging = tmp_path / "raw" / "attempt.partial"
    staging.mkdir(parents=True)
    rescue = tmp_path / "raw" / "disc.rescue"
    folder = rescue / "makemkv-backup"
    folder.mkdir(parents=True)
    (folder / "discatt.dat").write_bytes(b"attributes")
    image = rescue / "rescued-disc.iso"
    image.write_bytes(bytes(_bluray_image()))
    # The rescue could not read part of the movie clip (partition blocks 600 to 700 of its first extent).
    rescue_map = RescueMap(4096, [(0, 4096, FINISHED)])
    rescue_map.set(UDF_PARTITION + 600, UDF_PARTITION + 700, BAD)
    rescue_map.save(DvdSectorRescue.artifact_paths(image)["map"])
    service, _ = make_service(settings, make_job(settings, staging, disc_type=DiscKind.BLURAY.value))
    library = tmp_path / "MakeMKV" / "libmmbd64"
    monkeypatch.setattr(workflow_module, "libmmbd_library", lambda make_mkv_path: str(library))
    copies: list[tuple[Path, Path, str, str]] = []

    class FakeCopy:
        def __init__(self, ffmpeg_path, ffprobe_path, runner, aacs_library):
            self.aacs_library = aacs_library

        async def copy(self, job_id, backup_folder, keys, playlist, destination, timeout, callback=None):
            copies.append((backup_folder, keys, playlist, self.aacs_library))
            clip = backup_folder / "BDMV" / "STREAM" / "00800.m2ts"
            assert clip.is_file()
            assert (keys / "BDMV" / "STREAM" / "00800.m2ts").stat().st_size == clip.stat().st_size
            assert not (keys / "BDMV" / "STREAM" / "00001.m2ts").exists(), "only the movie's clips"
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "title_t00.mkv").write_bytes(b"copied movie")
            return destination / "title_t00.mkv"

    monkeypatch.setattr(workflow_module, "BlurayMovieCopy", FakeCopy)

    class GivesUpMakeMKV:
        async def inspect_source(self, job_id, source, min_length, timeout, callback=None):
            return DiscScan(titles=[TitleInfo(id=0, duration_seconds=8976, chapters=24)])

        async def rip_source(self, job_id, source, destination, title_ids, timeout, **kwargs):
            raise ProcessFailure(
                "MakeMKV reported success but produced no usable files",
                ProcessResult(args=[], return_code=0, cancelled=cancelled),
            )

    service._make_mkv = lambda active_settings=None: GivesUpMakeMKV()  # type: ignore[method-assign]
    track = {"source_id": 0, "duration_seconds": 8976, "chapters": 24, "source_filename": "00800.mpls"}

    if cancelled:
        with pytest.raises(RuntimeError, match="MakeMKV could not extract"):
            await service._extract_title_from_image("job-id", "D:", settings, image, staging, track)
        assert copies == [], "a cancelled job does not go on with FFmpeg"
        return

    await service._extract_title_from_image("job-id", "D:", settings, image, staging, track)

    assert copies == [(folder, rescue / "makemkv-keys", "00800.mpls", str(library))]
    assert (staging / "title_t00.mkv").read_bytes() == b"copied movie"
    assert not (rescue / "makemkv-keys").exists(), "the key folder is removed afterwards"
    movie = (folder / "BDMV" / "STREAM" / "00800.m2ts").read_bytes()
    unit = 133 * 6144  # the first aligned unit with unread sectors
    assert movie[unit + 4 : unit + 7] == b"\x47\x1f\xff", "the unread spot is empty packets for MakeMKV and FFmpeg"


@pytest.mark.asyncio
@pytest.mark.parametrize("source_filename", ["00800.mpls", ""], ids=["playlist recorded", "title from before 1.7.0"])
async def test_a_retry_after_decrypting_for_ffmpeg_does_not_ask_makemkv_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_filename: str
) -> None:
    from test_disc_rescue import UDF_PARTITION, _bluray_image

    from discdock.disc_rescue import BAD

    settings = AppSettings(data_root=tmp_path)
    staging = tmp_path / "raw" / "attempt.partial"
    staging.mkdir(parents=True)
    rescue = tmp_path / "raw" / "disc.rescue"
    folder = rescue / "makemkv-backup"
    folder.mkdir(parents=True)
    (folder / "discatt.dat").write_bytes(b"attributes")
    data = _bluray_image()
    # An earlier attempt decrypted the start of the movie clip: a clear unit of valid packets.
    decrypted = b"".join(
        (packet * 100).to_bytes(4, "big") + bytes([0x47, 0x10, 0x11, 0x10]) + bytes(184) for packet in range(32)
    )
    clip_start = (UDF_PARTITION + 200) * 2048
    data[clip_start : clip_start + len(decrypted)] = decrypted
    image = rescue / "rescued-disc.iso"
    image.write_bytes(bytes(data))
    rescue_map = RescueMap(4096, [(0, 4096, FINISHED)])
    rescue_map.set(UDF_PARTITION + 600, UDF_PARTITION + 700, BAD)
    rescue_map.save(DvdSectorRescue.artifact_paths(image)["map"])
    service, _ = make_service(settings, make_job(settings, staging, disc_type=DiscKind.BLURAY.value))
    library = tmp_path / "MakeMKV" / "libmmbd64"
    monkeypatch.setattr(workflow_module, "libmmbd_library", lambda make_mkv_path: str(library))
    copies: list[str] = []

    class FakeCopy:
        def __init__(self, ffmpeg_path, ffprobe_path, runner, aacs_library):
            pass

        async def copy(self, job_id, backup_folder, keys, playlist, destination, timeout, callback=None):
            copies.append(playlist)
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "title_t00.mkv").write_bytes(b"copied movie")
            return destination / "title_t00.mkv"

    monkeypatch.setattr(workflow_module, "BlurayMovieCopy", FakeCopy)

    class StopsAtTheDamageMakeMKV:
        async def inspect_source(self, *args, **kwargs):
            raise AssertionError("MakeMKV already stopped at this damage in the earlier attempt")

        async def rip_source(self, *args, **kwargs):
            raise AssertionError("MakeMKV already stopped at this damage in the earlier attempt")

    service._make_mkv = lambda active_settings=None: StopsAtTheDamageMakeMKV()  # type: ignore[method-assign]
    track = {"source_id": 0, "duration_seconds": 8976, "chapters": 24, "source_filename": source_filename}

    await service._extract_title_from_image("job-id", "D:", settings, image, staging, track)

    assert copies == ["00800.mpls"]
    assert (staging / "title_t00.mkv").read_bytes() == b"copied movie"


@pytest.mark.asyncio
async def test_a_failed_ai_repair_returns_to_review_without_a_new_analysis(tmp_path: Path) -> None:
    tool = tmp_path / "tool.exe"
    tool.write_bytes(b"tool")
    settings = AppSettings(data_root=tmp_path, ffmpeg_path=str(tool), ffprobe_path=str(tool))
    source = tmp_path / "raw" / "job-id-ai-source-1.partial" / "movie.mkv"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"movie")
    plan = {
        "estimate_id": "previous-estimate",
        "status": "awaiting_confirmation",
        "source_path": str(source),
        "source_root": str(source.parent),
        "frame_count": 12,
        "estimated_max_cost_usd": 0.16,
    }
    job = make_job(
        settings,
        tmp_path / "failed" / "repair-attempt",
        state=JobState.FAILED,
        stage="failed",
        error_code="ai_repair_failed",
        metadata={
            "warnings": [{"code": "disc_read_error"}],
            "ai_repair": plan,
            "recovery_route": {"mode": "ai_repair", "status": "failed"},
        },
    )
    service, database = make_service(settings, job)

    await service.prepare_ai_repair_job("job-id")

    assert database.job["state"] == JobState.AWAITING_REPAIR
    restored = database.job["metadata"]["ai_repair"]
    assert restored["status"] == "awaiting_confirmation"
    assert restored["estimate_id"] != "previous-estimate"
    assert database.job["staging_path"] == str(source.parent)


@pytest.mark.asyncio
async def test_normal_rip_uses_the_same_minimum_length_as_the_title_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = AppSettings(
        data_root=tmp_path,
        min_length_seconds=600,
        auto_eject=False,
        skip_transcode=True,
        duplicate_policy="keep_both",
    )
    staging = tmp_path / "raw" / "normal.partial"
    job = make_job(settings, staging, state=JobState.DETECTED, stage="detected")
    service, database = make_service(settings, job)
    database.tracks = [{"source_id": 0, "selected": True, "size_bytes": 10}]
    recorded: dict[str, Any] = {}

    class FakeMakeMKV:
        async def rip(self, job_id, letter, destination, selected_ids, timeout, **kwargs):
            recorded.update(kwargs)
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "title.mkv").write_bytes(b"movie")
            return []

    async def verified(folder: Path, ffprobe_path: str) -> list[Path]:
        del ffprobe_path
        return [folder / "title.mkv"]

    service._make_mkv = lambda active_settings=None: FakeMakeMKV()  # type: ignore[method-assign]
    monkeypatch.setattr(workflow_module, "verify_outputs", verified)
    monkeypatch.setattr(workflow_module, "disk_space_ok", lambda path, required: True)
    drive = DriveInfo(id="drive-id", letter="D:", name="Drive", media_loaded=True, disc_kind=DiscKind.DVD)

    await service._rip_and_finish("job-id", drive, settings)

    assert recorded["min_length"] == 600
    assert database.job["state"] == JobState.COMPLETED


@pytest.mark.asyncio
async def test_rescue_progress_is_saved_for_the_dashboard(tmp_path: Path) -> None:
    settings = AppSettings(data_root=tmp_path)
    job = make_job(settings, tmp_path / "raw" / "attempt.partial", metadata={"recovery": {"mode": "sector_best_effort"}})
    service, database = make_service(settings, job)
    status = {
        "phase": "sweep",
        "total_bytes": 7 * 1024**3,
        "rescued_bytes": 3 * 1024**3,
        "position_bytes": 3 * 1024**3,
        "in_damaged_zone": True,
        "not_a_rescue_field": "ignored",
    }

    await service._process_event("job-id", {"type": "rescue_status", "status": status})

    recovery = database.job["metadata"]["recovery"]
    assert recovery["mode"] == "sector_best_effort"
    assert recovery["rescued_bytes"] == 3 * 1024**3
    assert "not_a_rescue_field" not in recovery
    assert "skipping past damaged spots" in database.job["status_detail"]


@pytest.mark.asyncio
async def test_completed_best_effort_reports_skipped_data_and_removes_the_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = AppSettings(
        data_root=tmp_path, auto_eject=False, skip_transcode=True, duplicate_policy="keep_both"
    )
    staging = tmp_path / "raw" / "recovery.partial"
    job = make_job(
        settings,
        staging,
        metadata={"recovery": {"movie_unreadable_bytes": 3 * 1024**2, "movie_pending_bytes": 1024**2}},
    )
    service, database = make_service(settings, job)
    database.tracks = [{"source_id": 0, "selected": True, "size_bytes": 10, "duration_seconds": 5000}]
    image = service._rescue_image("job-id", settings)
    image.parent.mkdir(parents=True)
    image.write_bytes(b"iso")

    async def recover(job_id, drive, active_settings, destination, main_track) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "recovered.mkv").write_bytes(b"movie")

    async def verified(folder: Path, ffprobe_path: str) -> list[Path]:
        del ffprobe_path
        return [folder / "recovered.mkv"]

    service._recover_disc_to_staging = recover  # type: ignore[method-assign]
    monkeypatch.setattr(workflow_module, "verify_outputs", verified)
    monkeypatch.setattr(workflow_module, "disk_space_ok", lambda path, required: True)
    drive = DriveInfo(id="drive-id", letter="D:", name="Drive", media_loaded=False)

    await service._rip_and_finish("job-id", drive, settings, best_effort=True)

    assert database.job["state"] == JobState.COMPLETED
    assert "4.0 MB of unreadable disc data skipped" in database.job["status_detail"]
    assert not image.parent.exists()
    assert "may glitch" in service.notifications.sent[-1][3]


@pytest.mark.asyncio
async def test_ai_not_possible_keeps_the_salvaged_movie_for_best_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = AppSettings(
        data_root=tmp_path, auto_eject=False, skip_transcode=True, duplicate_policy="keep_both"
    )
    salvaged = tmp_path / "raw" / "job-id-ai-source-1.partial"
    salvaged.mkdir(parents=True)
    (salvaged / "movie.mkv").write_bytes(b"movie")
    job = make_job(
        settings,
        salvaged,
        state=JobState.FAILED,
        stage="failed",
        error_code="ai_repair_not_possible",
        metadata={"recovery": {"salvaged_movie_dir": str(salvaged)}},
    )
    service, database = make_service(settings, job)
    database.tracks = [{"source_id": 0, "selected": True, "size_bytes": 10, "duration_seconds": 5000}]

    async def must_not_extract(*_args, **_kwargs) -> None:
        pytest.fail("a salvaged movie must not be extracted again")

    async def verified(folder: Path, ffprobe_path: str) -> list[Path]:
        del ffprobe_path
        return [folder / "movie.mkv"]

    service._recover_disc_to_staging = must_not_extract  # type: ignore[method-assign]
    monkeypatch.setattr(workflow_module, "verify_outputs", verified)
    monkeypatch.setattr(workflow_module, "disk_space_ok", lambda path, required: True)
    drive = DriveInfo(id="drive-id", letter="D:", name="Drive", media_loaded=False)

    await service._start_best_effort_recovery("job-id", drive, settings)

    assert database.job["state"] == JobState.COMPLETED
    assert "salvaged_movie_dir" not in database.job["metadata"]["recovery"]


@pytest.mark.asyncio
async def test_retry_after_an_old_ai_not_possible_failure_returns_to_the_ai_review(tmp_path: Path) -> None:
    settings = AppSettings(data_root=tmp_path)
    job = make_job(
        settings,
        tmp_path / "raw" / "job-id-ai-source-1.partial",
        state=JobState.FAILED,
        stage="failed",
        error_code="ai_repair_not_possible",
        metadata={"recovery_route": {"mode": "ai_repair", "status": "failed"}},
    )
    service, database = make_service(settings, job)
    service.drives = {
        "drive-id": DriveInfo(id="drive-id", letter="D:", name="Drive", media_loaded=False)
    }
    routed: list[str] = []

    async def prepare(job_id: str) -> dict[str, Any]:
        routed.append(job_id)
        return database.job

    service.prepare_ai_repair_job = prepare  # type: ignore[method-assign]

    await service.retry_job("job-id")

    # Damage too long for AI no longer ends a job; the analysis now lands in
    # a review that can keep the movie, so a retry goes back there.
    assert routed == ["job-id"]
    assert database.job["metadata"]["recovery_route"]["mode"] == "ai_repair"


@pytest.mark.asyncio
@pytest.mark.parametrize(("action", "expected_calls"), [("best_effort", 1), ("ask", 0)])
async def test_unreadable_block_switches_to_best_effort_only_when_enabled(
    tmp_path: Path, action: str, expected_calls: int
) -> None:
    import asyncio

    settings = AppSettings(data_root=tmp_path, damaged_disc_action=action)
    job = make_job(settings, tmp_path / "raw" / "rip.partial", state=JobState.RIPPING, stage="ripping")
    service, _ = make_service(settings, job)
    calls: list[str] = []
    switched = asyncio.Event()

    async def recover(job_id: str) -> dict[str, Any]:
        calls.append(job_id)
        switched.set()
        return job

    service.recover_damaged_job = recover  # type: ignore[method-assign]
    error_line = (
        "MSG:2003,0,3,\"Error 'Scsi error - MEDIUM ERROR:L-EC UNCORRECTABLE ERROR' occurred while reading "
        "'/VIDEO_TS/VTS_06_1.VOB' at offset '1463154688'\""
    )

    await service._process_event("job-id", {"type": "log", "message": error_line})
    await service._process_event("job-id", {"type": "log", "message": error_line})
    # The switch runs as a background task; wait for it rather than for a fixed
    # number of loop turns. A setting of "ask" must never schedule it.
    if expected_calls:
        await asyncio.wait_for(switched.wait(), timeout=10)
    await asyncio.sleep(0.2)

    assert calls == ["job-id"] * expected_calls


def _saved_image_with_unread_blocks(service: DiscDockService, settings: AppSettings) -> Path:
    from discdock.disc_rescue import FINISHED, NON_SCRAPED, RescueMap
    from discdock.media_tools import DvdSectorRescue

    image = service._rescue_image("job-id", settings)
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(b"iso")
    rescue_map = RescueMap(64, [(0, 48, FINISHED), (48, 64, NON_SCRAPED)], {"sweep_done": True})
    rescue_map.save(DvdSectorRescue.artifact_paths(image)["map"])
    return image


@pytest.mark.asyncio
@pytest.mark.parametrize(("disc_inserted", "expected_reads"), [(True, 1), (False, 0)])
async def test_a_saved_image_with_unread_blocks_is_read_again_only_with_the_disc(
    tmp_path: Path, disc_inserted: bool, expected_reads: int
) -> None:
    settings = AppSettings(data_root=tmp_path, rescue_extra_minutes=30)
    staging = tmp_path / "raw" / "attempt.partial"
    staging.mkdir(parents=True)
    service, _ = make_service(settings, make_job(settings, staging))
    image = _saved_image_with_unread_blocks(service, settings)
    drive = DriveInfo(
        id="drive-id",
        letter="D:",
        name="Drive",
        media_loaded=disc_inserted,
        volume_label="DAMAGED_DISC" if disc_inserted else "",
        disc_kind=DiscKind.DVD,
    )
    service.drives = {drive.id: drive}
    calls: list[str] = []

    async def rescue(job_id, active_drive, active_settings, active_image, main_track) -> dict[str, Any]:
        assert active_image == image
        calls.append("rescue")
        return {"rescued_bytes": 1, "unreadable_bytes": 0, "pending_bytes": 0}

    async def extract(job_id, letter, active_settings, active_image, destination, main_track) -> None:
        calls.append("extract")

    service._run_sector_rescue = rescue  # type: ignore[method-assign]
    service._extract_title_from_image = extract  # type: ignore[method-assign]

    await service._recover_disc_to_staging("job-id", drive, settings, staging, {"source_id": 0})

    assert calls == ["rescue"] * expected_reads + ["extract"]


@pytest.mark.asyncio
async def test_a_salvaged_movie_is_not_kept_while_its_image_can_still_be_improved(tmp_path: Path) -> None:
    settings = AppSettings(data_root=tmp_path, rescue_extra_minutes=30)
    salvaged = tmp_path / "raw" / "job-id-ai-source-1.partial"
    salvaged.mkdir(parents=True)
    (salvaged / "movie.mkv").write_bytes(b"movie with holes from a failing drive")
    job = make_job(
        settings,
        salvaged,
        state=JobState.FAILED,
        stage="failed",
        error_code="ai_repair_not_possible",
        metadata={"recovery": {"salvaged_movie_dir": str(salvaged)}},
    )
    service, _ = make_service(settings, job)
    _saved_image_with_unread_blocks(service, settings)
    drive = DriveInfo(
        id="drive-id", letter="D:", name="Drive", media_loaded=True, volume_label="DAMAGED_DISC"
    )
    service.drives = {drive.id: drive}
    recorded: dict[str, Any] = {}

    async def rip_and_finish(job_id, active_drive, active_settings, **kwargs) -> None:
        recorded.update(kwargs)

    service._rip_and_finish = rip_and_finish  # type: ignore[method-assign]

    await service._start_best_effort_recovery("job-id", drive, settings)

    assert recorded == {"best_effort": True, "salvaged": False}


@pytest.mark.asyncio
async def test_damage_too_long_for_ai_lands_in_review_and_the_movie_can_be_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    from collections import defaultdict

    tool = tmp_path / "tool.exe"
    tool.write_bytes(b"tool")
    settings = AppSettings(data_root=tmp_path, auto_eject=False, ffmpeg_path=str(tool), ffprobe_path=str(tool))
    salvaged = tmp_path / "raw" / "job-id-ai-source-1.partial"
    salvaged.mkdir(parents=True)
    (salvaged / "movie.mkv").write_bytes(b"movie")
    job = make_job(
        settings,
        salvaged,
        state=JobState.QUEUED,
        stage="ai_analyzing",
        metadata={"warnings": [{"code": "disc_read_error"}], "recovery": {"salvaged_movie_dir": str(salvaged)}},
    )
    service, database = make_service(settings, job)
    database.tracks = [{"source_id": 0, "selected": True, "size_bytes": 10, "duration_seconds": 8614}]
    service._tasks = {}
    service._drive_locks = defaultdict(asyncio.Lock)
    service._pending_insertions = {}
    service._shutting_down = False
    plan = {
        "estimate_id": "estimate",
        "status": "awaiting_confirmation",
        "source_path": str(salvaged / "movie.mkv"),
        "frame_count": 0,
        "ai_keyframe_count": 0,
        "estimated_max_cost_usd": 0.0,
        "segments": [],
        "skipped": [
            {
                "start_seconds": 10.0,
                "end_seconds": 126.0,
                "duration_seconds": 116.0,
                "missing_seconds": 110.0,
                "reason": "longer than 4 seconds, too long to invent believably",
            }
        ],
        "summary": "The damage is too long for AI. Keep the movie with those moments skipped.",
    }
    monkeypatch.setattr(workflow_module, "find_repair_source", lambda root: root / "movie.mkv")
    monkeypatch.setattr(workflow_module, "analyze_repair", lambda *args, **kwargs: dict(plan))
    drive = DriveInfo(id="drive-id", letter="D:", name="Drive", media_loaded=False)

    await service._start_ai_repair_preparation("job-id", drive, settings)

    assert database.job["state"] == JobState.AWAITING_REPAIR, "damage too long for AI must not end the job"
    assert database.job["stage"] == "ai_review"
    assert "keep the movie" in database.job["status_detail"]
    with pytest.raises(RuntimeError, match="keep the movie instead"):
        await service.apply_ai_repair_job("job-id", "estimate", 0.0)

    recorded: dict[str, Any] = {}

    async def rip_and_finish(job_id, active_drive, active_settings, **kwargs) -> None:
        recorded.update(kwargs)

    service._rip_and_finish = rip_and_finish  # type: ignore[method-assign]
    service.active_job_for_drive = lambda drive_id: None  # type: ignore[method-assign]

    await service.keep_movie_without_ai("job-id")
    await asyncio.gather(*service._tasks.values())

    assert recorded == {"best_effort": True, "salvaged": True}, "the extracted movie is finished without re-reading"
    assert database.job["metadata"]["ai_repair"]["status"] == "declined"


def test_a_job_follows_its_drive_after_a_usb_reconnect(tmp_path: Path) -> None:
    settings = AppSettings(data_root=tmp_path)
    job = make_job(
        settings, tmp_path / "raw" / "rip.partial", state=JobState.FAILED, stage="failed", recoverable=1
    )
    service, database = make_service(settings, job)
    reconnected = DriveInfo(
        id="new-drive-id",
        letter="D:",
        name="Drive",
        media_loaded=True,
        volume_label="DAMAGED_DISC",
        disc_kind=DiscKind.DVD,
    )
    service.drives = {reconnected.id: reconnected}

    assert service.recoverable_job_for_disc(reconnected) is database.job
    assert service._drive_for_job(database.job) is reconnected
    assert database.job["drive_id"] == "new-drive-id"


def test_rescues_of_the_same_disc_by_other_jobs_are_combined_into_one_image(tmp_path: Path) -> None:
    from discdock.disc_rescue import FINISHED, RescueMap
    from discdock.optical import SECTOR_SIZE

    settings = AppSettings(data_root=tmp_path)
    raw = settings.resolved_directories()["raw"]
    total = 256

    def rescue(owner: str, finished: list[tuple[int, int]], name: str) -> None:
        folder = raw / f"{owner}.rescue"
        folder.mkdir(parents=True)
        data = bytearray(total * SECTOR_SIZE)
        rescue_map = RescueMap(total, meta={"sweep_done": True})
        for start, end in finished:
            for sector in range(start, end):
                data[sector * SECTOR_SIZE : (sector + 1) * SECTOR_SIZE] = bytes([sector % 250 + 1]) * SECTOR_SIZE
            rescue_map.set(start, end, FINISHED)
        (folder / name).write_bytes(bytes(data))
        rescue_map.save(folder / "rescued-disc.iso.map.json")

    # The drive got a new id, so a second job read the same disc into its own image.
    rescue("first-job", [(0, 200)], "rescued-disc.iso.part")
    rescue("second-job", [(0, 64), (180, 256)], "rescued-disc.iso")
    image = raw / "disc-abcdef1234567890.rescue" / "rescued-disc.iso"

    notes = DiscDockService._collect_rescue_images(settings, image, ["job-id", "first-job", "second-job"])

    combined = RescueMap.load(image.parent / "rescued-disc.iso.map.json")
    assert combined.count(FINISHED) == total
    assert len(notes) == 2
    assert not (raw / "first-job.rescue").exists() and not (raw / "second-job.rescue").exists()
    data = (image.parent / "rescued-disc.iso.part").read_bytes()
    assert data[230 * SECTOR_SIZE : 231 * SECTOR_SIZE] == bytes([231]) * SECTOR_SIZE


@pytest.mark.asyncio
async def test_a_blu_ray_rescue_reads_whole_clusters_without_dvd_navigation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = AppSettings(data_root=tmp_path)
    job = make_job(settings, tmp_path / "raw" / "rip.partial", disc_type=DiscKind.BLURAY.value)
    service, _ = make_service(settings, job)
    recorded: dict[str, Any] = {}

    class FakeRescue:
        def __init__(self, runner: object) -> None:
            del runner

        async def rip(self, job_id: str, letter: str, image: Path, **kwargs: Any) -> dict[str, Any]:
            recorded.update(kwargs)
            return {"rescued_bytes": 1}

    monkeypatch.setattr(workflow_module, "DvdSectorRescue", FakeRescue)
    drive = DriveInfo(id="drive-id", letter="E:", name="Drive", media_loaded=True, disc_kind=DiscKind.BLURAY)

    await service._run_sector_rescue(
        "job-id", drive, settings, tmp_path / "image.iso", {"duration_seconds": 7000, "disc_title_number": 0}
    )

    assert recorded["cluster_sectors"] == 32
    assert recorded["skip_sectors"] == 1024
    assert recorded["dvd_title"] == 0


@pytest.mark.asyncio
async def test_reinserting_the_disc_during_a_damage_review_reads_the_skipped_spots_again(tmp_path: Path) -> None:
    settings = AppSettings(data_root=tmp_path, rescue_extra_minutes=30)
    source = tmp_path / "raw" / "job-id-ai-source-1.partial"
    source.mkdir(parents=True)
    (source / "movie.mkv").write_bytes(b"movie with two long gaps")
    job = make_job(
        settings,
        source,
        state=JobState.AWAITING_REPAIR,
        stage="ai_review",
        recoverable=1,
        metadata={
            "warnings": [{"code": "disc_read_error"}],
            "ai_repair": {"estimate_id": "e", "status": "awaiting_confirmation", "segments": []},
        },
    )
    service, database = make_service(settings, job)
    _saved_image_with_unread_blocks(service, settings)
    drive = DriveInfo(
        id="drive-id", letter="D:", name="Drive", media_loaded=True, volume_label="DAMAGED_DISC", disc_kind=DiscKind.DVD
    )
    service.drives = {drive.id: drive}
    prepared: list[str] = []

    async def prepare(job_id: str) -> dict[str, Any]:
        prepared.append(job_id)
        return database.job

    service.prepare_ai_repair_job = prepare  # type: ignore[method-assign]

    assert service.recoverable_job_for_disc(drive) is database.job
    await service.retry_job("job-id")

    assert prepared == ["job-id"]
    assert database.job["state"] == JobState.CANCELLED

    service.drives = {}
    database.job["state"] = JobState.AWAITING_REPAIR
    with pytest.raises(RuntimeError, match="disc back in the drive"):
        await service.retry_job("job-id")


def _stopped_rescue(service: DiscDockService, settings: AppSettings, *, sweep_done: bool) -> Path:
    from discdock.disc_rescue import FINISHED, NON_SCRAPED, RescueMap
    from discdock.media_tools import DvdSectorRescue

    image = service._rescue_image("job-id", settings)
    paths = DvdSectorRescue.artifact_paths(image)
    image.parent.mkdir(parents=True, exist_ok=True)
    paths["partial"].write_bytes(b"iso")
    RescueMap(64, [(0, 48, FINISHED), (48, 64, NON_SCRAPED)], {"sweep_done": sweep_done}).save(paths["map"])
    return image


@pytest.mark.asyncio
async def test_a_rescue_stopped_by_a_drive_fault_can_finish_without_the_drive(tmp_path: Path) -> None:
    settings = AppSettings(data_root=tmp_path, rescue_extra_minutes=30)
    staging = tmp_path / "raw" / "attempt.partial"
    staging.mkdir(parents=True)
    job = make_job(
        settings,
        staging,
        state=JobState.FAILED,
        stage="failed",
        error_code="damaged_disc_recovery_failed",
        metadata={
            "recovery_route": {"mode": "sector_best_effort", "status": "failed"},
            "recovery": {"phase": "retry"},
        },
    )
    service, database = make_service(settings, job)
    image = _stopped_rescue(service, settings, sweep_done=True)
    routed: list[str] = []

    async def recover(job_id: str) -> dict[str, Any]:
        routed.append(job_id)
        return database.job

    service.recover_damaged_job = recover  # type: ignore[method-assign]

    await service.finish_rescue_now("job-id")

    assert routed == ["job-id"]
    assert database.job["metadata"]["recovery"]["finish_without_reading"] is True

    # The recovery then extracts the movie from the saved image even though the
    # stuck drive still reports the disc, and never starts the disc reader.
    stuck_drive = DriveInfo(
        id="drive-id",
        letter="D:",
        name="Drive",
        media_loaded=True,
        volume_label="DAMAGED_DISC",
        disc_kind=DiscKind.DVD,
    )
    service.drives = {stuck_drive.id: stuck_drive}
    extracted: list[Path] = []

    async def must_not_read(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        pytest.fail("a stuck drive must not be read when finishing with what was rescued")

    async def extract(job_id, letter, active_settings, active_image, destination, main_track) -> None:
        extracted.append(active_image)

    service._run_sector_rescue = must_not_read  # type: ignore[method-assign]
    service._extract_title_from_image = extract  # type: ignore[method-assign]

    await service._recover_disc_to_staging("job-id", stuck_drive, settings, staging, {"source_id": 0})

    assert extracted == [image]
    assert image.is_file(), "the partial image becomes the image MakeMKV reads"
    assert database.job["metadata"]["recovery"]["finish_without_reading"] is False, "a later retry reads again"


@pytest.mark.asyncio
async def test_finishing_a_stopped_rescue_needs_the_whole_disc_read_once(tmp_path: Path) -> None:
    settings = AppSettings(data_root=tmp_path)
    job = make_job(settings, tmp_path / "raw" / "attempt.partial", state=JobState.FAILED, stage="failed")
    service, _ = make_service(settings, job)
    _stopped_rescue(service, settings, sweep_done=False)

    with pytest.raises(RuntimeError, match="not read to the end"):
        await service.finish_rescue_now("job-id")


class FakeMoviePatcher:
    calls: ClassVar[list[list[tuple[float, float]]]] = []

    def __init__(self, *args: Any) -> None:
        del args

    async def add_loading_screens(self, job_id, source, workdir, moments, **kwargs) -> tuple[Path, str]:
        del job_id, source, kwargs
        FakeMoviePatcher.calls.append([(moment["start_seconds"], moment["end_seconds"]) for moment in moments])
        workdir.mkdir(parents=True, exist_ok=True)
        patched = workdir / "patched.mkv"
        patched.write_bytes(b"movie with loading screens")
        return patched, "splice"


async def _verified(path: Path, ffprobe_path: str) -> None:
    del path, ffprobe_path


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("placeholder", "treatments"),
    [
        ("loading_screen", ["loading_screen", "brief_glitch"]),
        ("none", ["skipped", "brief_glitch"]),
        ("ask", ["skipped", "brief_glitch"]),
    ],
)
async def test_a_recovered_movie_records_where_the_disc_was_damaged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, placeholder: str, treatments: list[str]
) -> None:
    tool = tmp_path / "tool.exe"
    tool.write_bytes(b"tool")
    settings = AppSettings(
        data_root=tmp_path, ffmpeg_path=str(tool), ffprobe_path=str(tool), damage_placeholder=placeholder
    )
    staging = tmp_path / "raw" / "recovery.partial"
    staging.mkdir(parents=True)
    movie = staging / "title_t00.mkv"
    movie.write_bytes(b"original movie")
    service, database = make_service(settings, make_job(settings, staging))
    measured = {
        "info": {"width": 720, "height": 576, "fps": 25.0, "duration_seconds": 600.0},
        "gaps": [(100.0, 104.0)],
        "segments": [(99.92, 104.48), (300.0, 300.4)],
    }
    monkeypatch.setattr(workflow_module, "measure_damage", lambda *args, **kwargs: measured)
    monkeypatch.setattr(workflow_module, "MoviePatcher", FakeMoviePatcher)
    monkeypatch.setattr(workflow_module, "verify_media_file", _verified)
    FakeMoviePatcher.calls = []

    await service._mark_damage_in_movie("job-id", settings, staging, DiscKind.DVD)

    moments = database.job["metadata"]["damage"]["moments"]
    assert [moment["treatment"] for moment in moments] == treatments
    assert (moments[0]["start_seconds"], moments[0]["end_seconds"]) == (99.92, 104.48)
    assert moments[0]["missing_seconds"] == 4.0
    kept = staging / "title_t00 - without loading screens.mkv"
    if placeholder == "loading_screen":
        assert FakeMoviePatcher.calls == [[(99.92, 104.48)]], "the 0.4 s glitch gets no loading screen"
        assert movie.read_bytes() == b"movie with loading screens"
        assert kept.read_bytes() == b"original movie", "the movie as read from the disc is kept"
        assert database.job["metadata"]["damage"]["kept_copy"] is True
        assert database.job["metadata"]["damage"]["loading_screen_method"] == "splice"
    else:
        assert FakeMoviePatcher.calls == []
        assert movie.read_bytes() == b"original movie"
        assert not kept.exists()
    # Ask finishes the movie as it was read and leaves the choice to the user, with a notification.
    assert database.job["metadata"]["damage"].get("choice") == ("pending" if placeholder == "ask" else None)
    assert [sent[1] for sent in service.notifications.sent] == (["attention"] if placeholder == "ask" else [])
    assert not (staging / ".discdock-damage").exists(), "work files never reach the library"


@pytest.mark.asyncio
async def test_a_finished_library_movie_gets_loading_screens_over_its_frozen_moments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    tool = tmp_path / "tool.exe"
    tool.write_bytes(b"tool")
    settings = AppSettings(data_root=tmp_path, ffmpeg_path=str(tool), ffprobe_path=str(tool))
    library = tmp_path / "completed" / "movies" / "Damaged movie (2009)"
    library.mkdir(parents=True)
    movie = library / "Damaged movie (2009).mkv"
    movie.write_bytes(b"frozen pictures")
    job = make_job(
        settings,
        tmp_path / "raw" / "gone.partial",
        state=JobState.COMPLETED,
        stage="completed",
        status_detail="Recovered",
        output_path=str(library),
        metadata={
            # A movie kept without AI before damage was recorded separately.
            "ai_repair": {
                "status": "declined",
                "segments": [],
                "skipped": [
                    {
                        "start_seconds": 1751.2,
                        "end_seconds": 1857.8,
                        "duration_seconds": 106.6,
                        "missing_seconds": 100.6,
                        "reason": "longer than 4 seconds",
                    }
                ],
            }
        },
    )
    service, database = make_service(settings, job)
    service._tasks = {}
    service._pending_insertions = {}
    service._shutting_down = False
    monkeypatch.setattr(workflow_module, "MoviePatcher", FakeMoviePatcher)
    monkeypatch.setattr(workflow_module, "verify_media_file", _verified)
    monkeypatch.setattr(workflow_module, "video_info", lambda *args: {"width": 720, "height": 576, "fps": 25.0})
    FakeMoviePatcher.calls = []

    await service.add_loading_screens_to_movie("job-id")
    assert database.job["state"] == JobState.TRANSCODING
    await asyncio.gather(*service._tasks.values())

    assert database.job["state"] == JobState.COMPLETED
    assert FakeMoviePatcher.calls == [[(1751.2, 1857.8)]]
    assert movie.read_bytes() == b"movie with loading screens"
    assert (library / "Damaged movie (2009) - without loading screens.mkv").read_bytes() == b"frozen pictures"
    assert database.job["metadata"]["damage"]["moments"][0]["treatment"] == "loading_screen"
    assert database.job["status_detail"] == "Recovered · loading screens at 1 damaged moment"
    with pytest.raises(RuntimeError, match="no damaged moment"):
        await service.add_loading_screens_to_movie("job-id")


def test_a_stopped_recovery_can_finish_the_movie_it_already_extracted(tmp_path: Path) -> None:
    settings = AppSettings(data_root=tmp_path)
    raw = settings.resolved_directories()["raw"]
    staging = raw / "job-id-recovery-1234abcd.partial"
    staging.mkdir(parents=True)
    job = make_job(settings, staging, state=JobState.CANCELLED, stage="cancelled")

    assert DiscDockService._salvaged_movie_dir(job) is None, "nothing was extracted yet"
    (staging / "title_t00.mkv").write_bytes(b"movie")
    assert DiscDockService._salvaged_movie_dir(job) == staging, "stopped while loading screens were added"
    assert DiscDockService._salvaged_movie_dir({**job, "state": JobState.FAILED}) is None, "it did not pass its checks"
    rip = raw / "job-id-5678.partial"
    rip.mkdir()
    (rip / "title_t00.mkv").write_bytes(b"part of a MakeMKV rip")
    assert DiscDockService._salvaged_movie_dir({**job, "staging_path": str(rip)}) is None, "an interrupted rip is incomplete"


@pytest.mark.asyncio
async def test_a_finished_movie_can_be_kept_as_it_was_read(tmp_path: Path) -> None:
    settings = AppSettings(data_root=tmp_path)
    moment = {"start_seconds": 100.0, "end_seconds": 104.5, "duration_seconds": 4.5, "treatment": "skipped"}
    job = make_job(
        settings,
        tmp_path / "raw" / "gone.partial",
        state=JobState.COMPLETED,
        stage="completed",
        metadata={"damage": {"choice": "pending", "moments": [moment]}},
    )
    service, database = make_service(settings, job)

    await service.keep_damaged_movie_as_is("job-id")

    damage = database.job["metadata"]["damage"]
    assert damage["choice"] == "kept"
    assert damage["moments"] == [moment], "the damaged moments stay listed in the Library"
    with pytest.raises(RuntimeError, match="not waiting"):
        await service.keep_damaged_movie_as_is("job-id")


def _bluray_drive() -> DriveInfo:
    return DriveInfo(
        id="drive-id", letter="D:", name="Drive", media_loaded=True, volume_label="DAMAGED_DISC", disc_kind=DiscKind.BLURAY
    )


class UnopenableDisc:
    async def inspect(self, *args: Any, **kwargs: Any) -> DiscScan:
        del args, kwargs
        raise ProcessFailure("MakeMKV found no video titles", ProcessResult(args=[], return_code=0))


@pytest.mark.asyncio
async def test_a_disc_makemkv_cannot_open_waits_for_recover_when_damaged_discs_ask(tmp_path: Path) -> None:
    settings = AppSettings(data_root=tmp_path, damaged_disc_action="ask")
    job = make_job(
        settings,
        tmp_path / "raw" / "job.partial",
        disc_type=DiscKind.BLURAY.value,
        state=JobState.INSPECTING,
        metadata={"warnings": [{"code": "disc_read_error"}]},
    )
    service, database = make_service(settings, job)
    service._make_mkv = lambda active_settings=None: UnopenableDisc()  # type: ignore[method-assign]

    with pytest.raises(workflow_module.DiscTooDamagedToOpen):
        await service._inspect_disc("job-id", _bluray_drive(), settings)

    database.job["metadata"] = {}
    with pytest.raises(ProcessFailure):
        await service._inspect_disc("job-id", _bluray_drive(), settings)


@pytest.mark.asyncio
async def test_a_disc_makemkv_cannot_open_is_read_from_its_file_system_first(tmp_path: Path) -> None:
    settings = AppSettings(data_root=tmp_path, damaged_disc_action="best_effort")
    job = make_job(
        settings,
        tmp_path / "raw" / "job.partial",
        disc_type=DiscKind.BLURAY.value,
        fingerprint="",
        state=JobState.INSPECTING,
        metadata={"warnings": [{"code": "disc_read_error"}]},
    )
    service, database = make_service(settings, job)
    service._make_mkv = lambda active_settings=None: UnopenableDisc()  # type: ignore[method-assign]
    rescues: list[tuple[Any, dict[str, Any]]] = []
    scans: list[DiscScan | None] = [None, DiscScan(titles=[TitleInfo(id=0, duration_seconds=8976, source_filename="00800.mpls")])]

    async def fake_rescue(job_id, drive, active_settings, image, main_track, **kwargs):
        rescues.append((main_track, kwargs))
        return {}

    async def fake_scan(job_id, active_settings, path):
        return scans.pop(0)

    service._run_sector_rescue = fake_rescue  # type: ignore[method-assign]
    service._scan_image = fake_scan  # type: ignore[method-assign]

    scan = await service._inspect_disc("job-id", _bluray_drive(), settings)

    assert [kwargs for _, kwargs in rescues] == [
        {"structures_only": True, "extra_seconds": 0},
        {"whole_disc": True, "extra_seconds": 0},
    ], "only when MakeMKV cannot list titles from the file system is the whole disc read"
    assert all(track is None for track, _ in rescues), "without titles the rescue picks the longest one itself"
    assert scan.titles[0].source_filename == "00800.mpls"
    assert database.job["metadata"]["titles_from_rescue"] is True
    assert database.job["metadata"]["requested_rip_method"] == "sector_rescue"
    assert database.job["state"] == JobState.INSPECTING


@pytest.mark.asyncio
async def test_recover_starts_a_disc_makemkv_could_not_open_over_from_its_file_system(tmp_path: Path) -> None:
    import asyncio

    settings = AppSettings(data_root=tmp_path)
    job = make_job(
        settings,
        tmp_path / "raw" / "job.partial",
        disc_type=DiscKind.BLURAY.value,
        fingerprint="",
        state=JobState.FAILED,
        stage="failed",
        error_code="disc_unreadable",
        metadata={"warnings": [{"code": "disc_read_error"}]},
    )
    service, database = make_service(settings, job)
    service.drives = {"drive-id": _bluray_drive()}
    service._tasks = {}
    service._pending_insertions = {}
    service._shutting_down = False
    service._external_drive_blockers = lambda letter="": []  # type: ignore[method-assign]
    started: list[str] = []

    async def fake_process(job_id: str, manual: bool = False) -> None:
        started.append(job_id)

    service._process_job = fake_process  # type: ignore[method-assign]

    await service.recover_damaged_job("job-id")
    await asyncio.gather(*service._tasks.values())

    assert started == ["job-id"]
    assert database.job["state"] == JobState.QUEUED
    assert database.job["metadata"]["titles_from_rescue"] is True


@pytest.mark.asyncio
async def test_an_image_with_only_the_movie_read_gets_the_rest_of_the_disc_when_makemkv_needs_it(tmp_path: Path) -> None:
    settings = AppSettings(data_root=tmp_path)
    staging = tmp_path / "raw" / "recovery.partial"
    staging.mkdir(parents=True)
    service, _ = make_service(settings, make_job(settings, staging, disc_type=DiscKind.BLURAY.value))
    service.drives = {"drive-id": _bluray_drive()}
    image = service._rescue_image("job-id", settings)
    image.parent.mkdir(parents=True)
    image.write_bytes(b"iso")
    map_path = DvdSectorRescue.artifact_paths(image)["map"]
    rescue_map = RescueMap(1000)
    rescue_map.set(100, 200, FINISHED)
    rescue_map.meta.update({"sweep_done": True, "swept_ranges": [[100, 200]], "relevant_pending_sectors": 0})
    rescue_map.save(map_path)
    attempts: list[int] = []
    rescues: list[dict[str, Any]] = []

    async def fake_extract(job_id, letter, active_settings, image_path, staging_path, main_track):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("MakeMKV could not find the selected movie in the rescued disc image")

    async def fake_rescue(job_id, drive, active_settings, image_path, main_track, **kwargs):
        rescues.append(kwargs)
        return {}

    service._extract_title_from_image = fake_extract  # type: ignore[method-assign]
    service._run_sector_rescue = fake_rescue  # type: ignore[method-assign]
    track = {"source_id": 1, "source_filename": "00800.mpls", "duration_seconds": 8976}

    await service._recover_disc_to_staging("job-id", _bluray_drive(), settings, staging, track)

    assert rescues == [{"whole_disc": True, "extra_seconds": 0}]
    assert len(attempts) == 2

    rescue_map.meta["swept_ranges"] = [[0, 1000]]
    rescue_map.save(map_path)
    attempts.clear()
    rescues.clear()
    with pytest.raises(RuntimeError, match="could not find"):
        await service._recover_disc_to_staging("job-id", _bluray_drive(), settings, staging, track)
    assert rescues == [], "after the whole disc was read, a failed extraction is a real failure"


@pytest.mark.asyncio
async def test_a_disc_already_in_the_drive_at_startup_does_not_restart_a_stopped_rescue(tmp_path: Path) -> None:
    settings = AppSettings(data_root=tmp_path, auto_rip=True)
    job = make_job(
        settings,
        tmp_path / "raw" / "job.partial",
        disc_type=DiscKind.BLURAY.value,
        state=JobState.FAILED,
        stage="failed",
        error_code="drive_not_responding",
        recoverable=True,
    )
    service, database = make_service(settings, job)
    drive = _bluray_drive()
    service.drives = {drive.id: drive}
    service._pending_insertions = {}
    service._shutting_down = False
    retried: list[str] = []
    created: list[str] = []

    async def fake_retry(job_id: str) -> dict[str, Any]:
        retried.append(job_id)
        return database.job

    async def fake_create(drive_id: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        created.append(drive_id)
        return database.job

    service.retry_job = fake_retry  # type: ignore[method-assign]
    service.create_job = fake_create  # type: ignore[method-assign]

    service._starting = True
    await service._on_inserted(drive)
    assert (retried, created) == ([], []), "a restart does not hammer a drive that just failed"
    assert database.events[-1][1] == "drive.autostart_skipped"

    service._starting = False
    await service._on_inserted(drive)
    assert retried == ["job-id"], "reconnecting the drive or reinserting the disc resumes the job"

    database.job.update(error_code="image_not_decryptable")
    retried.clear()
    await service._on_inserted(drive)
    assert (retried, created) == ([], []), "an image MakeMKV cannot read is never resumed on its own"


def test_a_disc_without_a_label_never_resumes_an_unrelated_job(tmp_path: Path) -> None:
    settings = AppSettings(data_root=tmp_path)
    job = make_job(
        settings, tmp_path / "raw" / "rip.partial", disc_label="", state=JobState.FAILED, stage="failed", recoverable=1
    )
    service, _ = make_service(settings, job)
    audio_cd = DriveInfo(
        id="drive-id", letter="D:", name="Drive", media_loaded=True, volume_label="", disc_kind=DiscKind.AUDIO_CD
    )
    service.drives = {audio_cd.id: audio_cd}

    assert service.recoverable_job_for_disc(audio_cd) is None


def test_an_audio_cd_resumes_only_the_job_of_the_same_cd(tmp_path: Path) -> None:
    settings = AppSettings(data_root=tmp_path)
    job = make_job(
        settings,
        tmp_path / "raw" / "cd.partial",
        disc_label="Audio CD",
        disc_type=DiscKind.AUDIO_CD.value,
        state=JobState.INTERRUPTED,
        stage="media_removed",
        recoverable=1,
        metadata={"cd": {"discid": "bevDpSptpSYY3kX7q5H.3Rw8KIY-", "tracks": 12}},
    )
    service, _ = make_service(settings, job)
    # Windows gives every audio CD the same label.
    audio_cd = DriveInfo(
        id="drive-id", letter="D:", name="Drive", media_loaded=True, volume_label="Audio CD", disc_kind=DiscKind.AUDIO_CD
    )
    service.drives = {audio_cd.id: audio_cd}

    assert service.recoverable_job_for_disc(audio_cd, "NPgsMw_PxxLVnJoP3vhbTeqTIcQ-") is None
    assert service.recoverable_job_for_disc(audio_cd) is None, "the DiscID could not be read"
    assert service.recoverable_job_for_disc(audio_cd, "bevDpSptpSYY3kX7q5H.3Rw8KIY-") is job


@pytest.mark.asyncio
async def test_inserting_another_audio_cd_starts_a_new_job_instead_of_resuming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = AppSettings(data_root=tmp_path, auto_rip=True)
    job = make_job(
        settings,
        tmp_path / "raw" / "cd.partial",
        disc_label="Audio CD",
        disc_type=DiscKind.AUDIO_CD.value,
        state=JobState.INTERRUPTED,
        stage="media_removed",
        recoverable=1,
        metadata={"cd": {"discid": "bevDpSptpSYY3kX7q5H.3Rw8KIY-", "tracks": 12}},
    )
    service, _ = make_service(settings, job)
    drive = DriveInfo(
        id="drive-id", letter="D:", name="Drive", media_loaded=True, volume_label="Audio CD", disc_kind=DiscKind.AUDIO_CD
    )
    service.drives = {drive.id: drive}
    service._pending_insertions = {}
    service._shutting_down = False
    service._starting = False
    in_drive = ["NPgsMw_PxxLVnJoP3vhbTeqTIcQ-"]
    monkeypatch.setattr("discdock.workflow.read_disc_id", lambda _letter: in_drive[0])
    retried: list[str] = []
    created: list[str] = []

    async def fake_retry(job_id: str) -> dict[str, Any]:
        retried.append(job_id)
        return job

    async def fake_create(drive_id: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        created.append(drive_id)
        return job

    service.retry_job = fake_retry  # type: ignore[method-assign]
    service.create_job = fake_create  # type: ignore[method-assign]

    await service._on_inserted(drive)
    assert (retried, created) == ([], ["drive-id"]), "another CD gets a job of its own"

    in_drive[0] = "bevDpSptpSYY3kX7q5H.3Rw8KIY-"
    await service._on_inserted(drive)
    assert retried == ["job-id"], "the same CD continues its job"


def test_adding_loading_screens_to_a_library_movie_leaves_the_drive_free(tmp_path: Path) -> None:
    settings = AppSettings(data_root=tmp_path)
    job = make_job(
        settings,
        tmp_path / "raw" / "gone.partial",
        state=JobState.TRANSCODING,
        stage="damage_screens",
        completed_at="2026-09-12T21:04:33+00:00",
        output_path=str(tmp_path / "library"),
    )
    service, database = make_service(settings, job)

    assert service.active_job_for_drive("drive-id") is None
    database.job["completed_at"] = None
    assert service.active_job_for_drive("drive-id") is database.job, "a rip still being finished keeps its drive"


def test_unread_extras_outside_the_movie_are_not_retry_work(tmp_path: Path) -> None:
    from discdock.disc_rescue import FINISHED, NON_TRIED, RescueMap
    from discdock.media_tools import DvdSectorRescue

    settings = AppSettings(data_root=tmp_path, rescue_extra_minutes=30)
    image = tmp_path / "rescue" / "rescued-disc.iso"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"iso")
    map_path = DvdSectorRescue.artifact_paths(image)["map"]
    rescue_map = RescueMap(64, [(0, 48, FINISHED), (48, 64, NON_TRIED)], {"relevant_pending_sectors": 0})
    rescue_map.save(map_path)

    assert DiscDockService._rescue_has_retry_work(image, settings) is False

    rescue_map.meta["relevant_pending_sectors"] = 16
    rescue_map.save(map_path)
    assert DiscDockService._rescue_has_retry_work(image, settings) is True
