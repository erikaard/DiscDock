from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from discdock import media_tools
from discdock.disc_rescue import EVENT_PREFIX, EXIT_COPY_PROTECTION
from discdock.media_tools import (
    DiscAuthenticationRequired,
    DvdSectorRescue,
    VlcDvdRecovery,
    describe_rescue_progress,
)
from discdock.processes import ProcessFailure, ProcessResult


class RecordingRunner:
    def __init__(self, partial: Path) -> None:
        self.partial = partial
        self.owner = ""
        self.args: list[str] = []
        self.kwargs: dict = {}

    async def run(self, owner: str, args: list[str], **kwargs) -> ProcessResult:
        self.owner = owner
        self.args = args
        self.kwargs = kwargs
        await asyncio.sleep(0)
        self.partial.write_bytes(b"x" * (2 * 1024 * 1024))
        return ProcessResult(args=args, return_code=0)

    async def cancel(self, owner: str) -> bool:
        assert owner == self.owner
        return True


@pytest.mark.asyncio
async def test_vlc_recovery_uses_noninteractive_dvd_title_and_verifies_duration(
    monkeypatch, tmp_path: Path
) -> None:
    executable = tmp_path / "vlc.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    executable.touch()
    ffprobe.touch()
    destination = tmp_path / "recovery"
    runner = RecordingRunner(destination / "recovered.part.mkv")
    monkeypatch.setattr(media_tools, "_probe_media_duration", lambda path, probe: 3585.0)

    output = await VlcDvdRecovery(str(executable), str(ffprobe), runner).recover(
        "job-1",
        "D:",
        destination,
        title_number=3,
        chapter_count=12,
        expected_duration_seconds=3600,
        estimated_bytes=4_000_000_000,
        timeout=7200,
    )

    assert output == destination / "recovered.mkv"
    assert output.exists()
    assert runner.owner == "job-1"
    assert "--intf=dummy" in runner.args
    assert "--play-and-exit" in runner.args
    assert "dvd:///D:/#3:1-3:12" in runner.args
    assert any(value.startswith("--sout=#std{access=file,mux=mkv,dst=") for value in runner.args)
    assert runner.kwargs["no_output_timeout"] == 7260


@pytest.mark.asyncio
async def test_vlc_recovery_rejects_a_copy_that_stops_at_the_damaged_section(
    monkeypatch, tmp_path: Path
) -> None:
    executable = tmp_path / "vlc.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    executable.touch()
    ffprobe.touch()
    destination = tmp_path / "recovery"
    runner = RecordingRunner(destination / "recovered.part.mkv")
    monkeypatch.setattr(media_tools, "_probe_media_duration", lambda path, probe: 600.0)

    with pytest.raises(ProcessFailure, match="stopped before enough"):
        await VlcDvdRecovery(str(executable), str(ffprobe), runner).recover(
            "job-2",
            "D:",
            destination,
            title_number=1,
            chapter_count=18,
            expected_duration_seconds=3600,
            estimated_bytes=4_000_000_000,
            timeout=7200,
        )

    assert not (destination / "recovered.mkv").exists()
    assert (destination / "recovered.part.mkv").exists()


@pytest.mark.asyncio
async def test_vlc_recovery_can_read_a_rescued_iso(monkeypatch, tmp_path: Path) -> None:
    executable = tmp_path / "vlc.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    executable.touch()
    ffprobe.touch()
    image = tmp_path / "rescued-disc.iso"
    image.touch()
    destination = tmp_path / "recovery"
    runner = RecordingRunner(destination / "recovered.part.mkv")
    monkeypatch.setattr(media_tools, "_probe_media_duration", lambda path, probe: 3585.0)

    await VlcDvdRecovery(str(executable), str(ffprobe), runner).recover(
        "job-3",
        "D:",
        destination,
        title_number=3,
        chapter_count=12,
        expected_duration_seconds=3600,
        estimated_bytes=4_000_000_000,
        timeout=7200,
        source_image=image,
    )

    expected = f"dvd:///{image.resolve().as_posix()}#3:1-3:12"
    assert expected in runner.args


class HangingRunner:
    """A VLC that writes a little and then loops without writing another byte, until it is stopped."""

    def __init__(self, partial: Path) -> None:
        self.partial = partial
        self.stopped = asyncio.Event()
        self.cancelled = False

    async def run(self, owner: str, args: list[str], **kwargs) -> ProcessResult:
        del owner, kwargs
        self.partial.write_bytes(b"x" * (2 * 1024 * 1024))
        await self.stopped.wait()
        return ProcessResult(args=args, return_code=1, cancelled=True)

    async def cancel(self, owner: str) -> bool:
        del owner
        self.cancelled = True
        self.stopped.set()
        return True


class SteadyRunner:
    """A VLC that keeps writing, a step at a time with a pause between, and then finishes."""

    def __init__(self, partial: Path, steps: int, pause: float) -> None:
        self.partial = partial
        self.steps = steps
        self.pause = pause
        self.cancelled = False

    async def run(self, owner: str, args: list[str], **kwargs) -> ProcessResult:
        del owner, kwargs
        with self.partial.open("wb") as handle:
            for _ in range(self.steps):
                handle.write(b"x" * (1024 * 1024))
                handle.flush()
                await asyncio.sleep(self.pause)
        return ProcessResult(args=args, return_code=0)

    async def cancel(self, owner: str) -> bool:
        del owner
        self.cancelled = True
        return True


def _vlc_tools(tmp_path: Path) -> tuple[str, str]:
    executable = tmp_path / "vlc.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    executable.touch()
    ffprobe.touch()
    return str(executable), str(ffprobe)


@pytest.mark.asyncio
async def test_vlc_reading_an_image_is_stopped_when_it_stops_writing(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(media_tools, "IMAGE_STALL_SECONDS", 0.3)
    monkeypatch.setattr(media_tools, "VLC_POLL_SECONDS", 0.01)
    image = tmp_path / "rescued-disc.iso"
    image.touch()
    destination = tmp_path / "recovery"
    runner = HangingRunner(destination / "recovered.part.mkv")
    events: list[dict] = []

    with pytest.raises(ProcessFailure, match="stopped making progress") as failure:
        await VlcDvdRecovery(*_vlc_tools(tmp_path), runner).recover(
            "job-6",
            "D:",
            destination,
            title_number=1,
            chapter_count=12,
            expected_duration_seconds=3000,
            estimated_bytes=2_700_000_000,
            timeout=43200,
            callback=events.append,
            source_image=image,
        )

    assert runner.cancelled
    assert failure.value.result.cancelled
    assert any(event["type"] == "log" and "written nothing" in event["message"] for event in events)
    assert not (destination / "recovered.mkv").exists()


@pytest.mark.asyncio
async def test_vlc_reading_an_image_is_left_alone_while_it_keeps_writing(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(media_tools, "IMAGE_STALL_SECONDS", 0.3)
    monkeypatch.setattr(media_tools, "VLC_POLL_SECONDS", 0.01)
    monkeypatch.setattr(media_tools, "_probe_media_duration", lambda path, probe: 2990.0)
    image = tmp_path / "rescued-disc.iso"
    image.touch()
    destination = tmp_path / "recovery"
    runner = SteadyRunner(destination / "recovered.part.mkv", steps=20, pause=0.01)

    output = await VlcDvdRecovery(*_vlc_tools(tmp_path), runner).recover(
        "job-7",
        "D:",
        destination,
        title_number=1,
        chapter_count=12,
        expected_duration_seconds=3000,
        estimated_bytes=2_700_000_000,
        timeout=43200,
        source_image=image,
    )

    assert output.exists()
    assert not runner.cancelled


@pytest.mark.asyncio
async def test_vlc_reading_the_drive_is_never_stopped_for_being_slow(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(media_tools, "IMAGE_STALL_SECONDS", 0.1)
    monkeypatch.setattr(media_tools, "VLC_POLL_SECONDS", 0.01)
    monkeypatch.setattr(media_tools, "_probe_media_duration", lambda path, probe: 2990.0)
    destination = tmp_path / "recovery"
    # A damaged drive can retry a bad block for a long time; the pause is several times the limit.
    runner = SteadyRunner(destination / "recovered.part.mkv", steps=2, pause=0.5)

    output = await VlcDvdRecovery(*_vlc_tools(tmp_path), runner).recover(
        "job-8",
        "D:",
        destination,
        title_number=1,
        chapter_count=12,
        expected_duration_seconds=3000,
        estimated_bytes=2_700_000_000,
        timeout=43200,
    )

    assert output.exists()
    assert not runner.cancelled


def _event(**payload) -> str:
    return EVENT_PREFIX + json.dumps(payload)


class HelperRunner:
    def __init__(self, image: Path, lines: list[str], return_code: int = 0) -> None:
        self.image = image
        self.lines = lines
        self.return_code = return_code
        self.args: list[str] = []
        self.kwargs: dict = {}

    async def run(self, _owner: str, args: list[str], **kwargs) -> ProcessResult:
        self.args = args
        self.kwargs = kwargs
        for line in self.lines:
            await kwargs["on_line"](line)
        if self.return_code == 0:
            DvdSectorRescue.artifact_paths(self.image)["partial"].write_bytes(b"\0" * 4096)
        return ProcessResult(args=args, return_code=self.return_code)


@pytest.mark.asyncio
async def test_sector_rescue_runs_the_helper_and_reports_readable_progress(tmp_path: Path) -> None:
    image = tmp_path / "rescue" / "rescued-disc.iso"
    events: list[dict] = []
    progress = {
        "type": "progress",
        "phase": "sweep",
        "percent": 42.5,
        "position_bytes": 3 * 1024**3,
        "total_bytes": 7 * 1024**3,
        "in_damaged_zone": True,
    }
    runner = HelperRunner(
        image,
        [
            _event(type="start", origin="adopted", total_bytes=7 * 1024**3, max_transfer_sectors=32,
                   method="scsi_pass_through"),
            _event(**progress),
            "unrelated helper output",
            _event(type="done", finished_early=False, rescued_bytes=4096, unreadable_bytes=0),
        ],
    )

    summary = await DvdSectorRescue(runner).rip(
        "job-4", "D:", image, callback=events.append, extra_seconds=900, dvd_title=31
    )

    assert image.is_file()
    assert summary["rescued_bytes"] == 4096
    assert runner.args[runner.args.index("--dvd-title") + 1] == "31"
    assert runner.args[runner.args.index("--extra-seconds") + 1] == "900"
    assert runner.args[runner.args.index("--skip-sectors") + 1] == "256"
    assert runner.args[runner.args.index("--image") + 1].endswith("rescued-disc.iso.part")
    assert "discdock.disc_rescue" in runner.args or "--disc-rescue" in runner.args
    assert any(event["type"] == "progress" and event["percent"] == 42.5 for event in events)
    assert any("skipping past damaged spots" in str(event.get("message")) for event in events)
    assert any(event["type"] == "rescue_status" and event.get("final") for event in events)
    assert any("earlier DiscDock version" in str(event.get("message")) for event in events)


@pytest.mark.asyncio
async def test_sector_rescue_reports_css_authentication_separately(tmp_path: Path) -> None:
    image = tmp_path / "rescued-disc.iso"
    runner = HelperRunner(
        image,
        [_event(type="error", code="copy_protection", message="not authenticated")],
        return_code=EXIT_COPY_PROTECTION,
    )

    with pytest.raises(DiscAuthenticationRequired, match="not authenticated"):
        await DvdSectorRescue(runner).rip("job-5", "D:", image)

    assert not image.exists()


def test_finish_request_writes_the_helper_control_file(tmp_path: Path) -> None:
    image = tmp_path / "rescued-disc.iso"

    assert DvdSectorRescue.request_finish(image) is True
    assert (tmp_path / "rescued-disc.iso.control").read_text(encoding="utf-8") == "finish"
    assert DvdSectorRescue.request_finish(tmp_path / "missing" / "image.iso") is False


def test_rescue_progress_text_explains_later_passes() -> None:
    text = describe_rescue_progress(
        {
            "phase": "scrape",
            "movie_pending_bytes": 50 * 1024**2,
            "movie_unreadable_bytes": 3 * 1024**2,
            "extra_elapsed_seconds": 600,
            "extra_budget_seconds": 1800,
        }
    )

    assert "Retrying skipped spots" in text
    assert "10 of 30 min" in text
    assert "can be skipped" in text
