from __future__ import annotations

import base64
from pathlib import Path

import pytest

import discdock.workflow as workflow_module
from discdock import media_tools
from discdock.media_tools import DataDiscRipper
from discdock.processes import ProcessFailure, ProcessResult


class RecordingRunner:
    def __init__(self, partial: Path, *, return_code: int = 0, cancelled: bool = False) -> None:
        self.partial = partial
        self.return_code = return_code
        self.cancelled = cancelled
        self.owner: str | None = None
        self.args: list[str] = []
        self.kwargs: dict = {}

    async def run(self, owner: str, args: list[str], **kwargs) -> ProcessResult:
        self.owner = owner
        self.args = args
        self.kwargs = kwargs
        self.partial.write_bytes(b"x" * (2 * 1024 * 1024))
        callback = kwargs.get("on_line")
        if callback:
            await callback("DDPROGRESS:50")
        return ProcessResult(args=args, return_code=self.return_code, cancelled=self.cancelled)


def test_volume_size_uses_a_noninteractive_powershell_argument_vector(monkeypatch) -> None:
    captured: dict = {}

    class FakeProcess:
        returncode = 0

        def communicate(self, timeout: int):
            captured["timeout"] = timeout
            return "734003200\n", ""

    def fake_start(args: list[str], **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(media_tools, "start_external_process", fake_start)
    monkeypatch.setattr(
        media_tools.shutil,
        "disk_usage",
        lambda _path: (_ for _ in ()).throw(OSError("drive size unavailable")),
    )

    assert DataDiscRipper._volume_size("D:") == 734003200
    assert captured["args"] == [
        "powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "(Get-Volume -DriveLetter 'D' -ErrorAction Stop).Size",
    ]
    assert captured["timeout"] == 20
    assert "shell" not in captured["kwargs"]


def test_volume_size_uses_standard_drive_information_without_cim(monkeypatch) -> None:
    monkeypatch.setattr(
        media_tools.shutil,
        "disk_usage",
        lambda path: type("Usage", (), {"total": 734003200})(),
    )
    monkeypatch.setattr(
        media_tools,
        "start_external_process",
        lambda *_args, **_kwargs: pytest.fail("PowerShell fallback should not run"),
    )

    assert DataDiscRipper._volume_size("D:") == 734003200


@pytest.mark.asyncio
async def test_data_disc_command_is_encoded_and_can_be_dry_run(monkeypatch, tmp_path: Path) -> None:
    destination = tmp_path / "disc'; Write-Output PWNED; #.iso"
    partial = destination.with_suffix(destination.suffix + ".part")
    runner = RecordingRunner(partial)
    progress: list[dict] = []

    monkeypatch.setattr(
        media_tools.shutil, "which", lambda _: r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    )
    monkeypatch.setattr(DataDiscRipper, "_volume_size", staticmethod(lambda _letter: 2 * 1024 * 1024))

    result = await DataDiscRipper(runner).rip("job-1", "D:", destination, callback=progress.append)

    assert result == destination
    assert destination.stat().st_size == 2 * 1024 * 1024
    assert not partial.exists()
    assert runner.owner == "job-1"
    assert runner.args[1:5] == ["-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand"]
    assert runner.kwargs["timeout"] == 12 * 3600
    assert runner.kwargs["no_output_timeout"] == 600

    script = base64.b64decode(runner.args[5]).decode("utf-16le")
    encoded_destination = base64.b64encode(str(partial).encode("utf-8")).decode("ascii")
    encoded_drive = base64.b64encode(b"D:").decode("ascii")
    assert encoded_destination in script
    assert encoded_drive in script
    assert str(partial) not in script
    assert "Write-Output PWNED" not in script
    assert "$Total = [long]2097152" in script
    assert progress == [{"type": "progress", "percent": 50.0, "message": "Imaging data disc"}]


@pytest.mark.asyncio
async def test_cancelled_data_disc_dry_run_keeps_only_the_partial(monkeypatch, tmp_path: Path) -> None:
    destination = tmp_path / "disc.iso"
    partial = destination.with_suffix(".iso.part")
    runner = RecordingRunner(partial, return_code=1, cancelled=True)

    monkeypatch.setattr(
        media_tools.shutil, "which", lambda _: r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    )
    monkeypatch.setattr(DataDiscRipper, "_volume_size", staticmethod(lambda _letter: 2 * 1024 * 1024))

    with pytest.raises(ProcessFailure, match="Data-disc imaging failed"):
        await DataDiscRipper(runner).rip("job-2", "D:", destination)

    assert partial.exists()
    assert not destination.exists()


@pytest.mark.asyncio
async def test_a_game_disc_is_backed_up_and_checked_against_its_own_files(tmp_path: Path, monkeypatch) -> None:
    """The backup keeps the disc, proves every file is in it, and says how to play it."""
    from test_disc_rescue import _dvd_reader
    from test_workflow_recovery import make_job, make_service

    from discdock.disc_files import image_files
    from discdock.models import DiscKind, DriveInfo, MediaKind
    from discdock.settings import AppSettings

    settings = AppSettings(data_root=tmp_path)
    settings.resolved_directories()["logs"].mkdir(parents=True, exist_ok=True)
    staging = tmp_path / "raw" / "disc.partial"
    staging.mkdir(parents=True)
    job = make_job(
        settings,
        staging,
        title="Sims2 Ep1",
        disc_label="SIMS2_EP1",
        disc_type=DiscKind.DATA.value,
        media_kind=MediaKind.OTHER.value,
    )
    service, _ = make_service(settings, job)
    drive = DriveInfo(
        id="drive-id", letter="D:", name="Drive", media_loaded=True, volume_label="SIMS2_EP1", disc_kind=DiscKind.DATA
    )
    read, total = _dvd_reader()
    disc_image = bytes(read(0, total))

    class FakeRipper:
        def __init__(self, runner) -> None:
            pass

        async def rip(self, job_id, letter, destination, callback=None):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(disc_image)
            return destination

    monkeypatch.setattr(workflow_module, "DataDiscRipper", FakeRipper)
    # What Windows showed on the disc before the backup started.
    (staging / "probe.iso").write_bytes(disc_image)
    seen = image_files(staging / "probe.iso")
    (staging / "probe.iso").unlink()
    job["metadata"] = {
        "disc_contents": {
            "kind": "game",
            "summary": "Looks like a game or program disc: it starts itself from the disc.",
            "file_count": seen.file_count,
            "total_bytes": seen.total_bytes,
            "entries": [{"path": entry.path, "size": entry.size} for entry in seen.entries],
        }
    }

    await service._back_up_data_disc("job-id", drive, job, staging)

    image = staging / "Sims2 Ep1.iso"
    assert image.is_file() and image.stat().st_size == len(disc_image)
    contents = (staging / "Disc contents.txt").read_text(encoding="utf-8")
    assert "SIMS2_EP1" in contents and "VIDEO_TS/VIDEO_TS.IFO" in contents
    how = (staging / "How to use this backup.txt").read_text(encoding="utf-8")
    assert "Double-click Sims2 Ep1.iso" in how
    assert "setup or autorun" in how, "a game disc says how to install and play it"
    assert "does not remove protection" in how, "and is honest about discs that check for the original"


@pytest.mark.asyncio
async def test_a_backup_missing_files_from_the_disc_is_not_accepted(tmp_path: Path, monkeypatch) -> None:
    from test_workflow_recovery import make_job, make_service

    from discdock.models import DiscKind, DriveInfo, MediaKind
    from discdock.settings import AppSettings

    settings = AppSettings(data_root=tmp_path)
    settings.resolved_directories()["logs"].mkdir(parents=True, exist_ok=True)
    staging = tmp_path / "raw" / "disc.partial"
    staging.mkdir(parents=True)
    job = make_job(
        settings, staging, title="Broken disc", disc_type=DiscKind.DATA.value, media_kind=MediaKind.OTHER.value
    )
    job["metadata"] = {
        "disc_contents": {"kind": "game", "file_count": 1, "total_bytes": 2048,
                          "entries": [{"path": "setup.exe", "size": 2048}]}
    }
    service, _ = make_service(settings, job)
    drive = DriveInfo(
        id="drive-id", letter="D:", name="Drive", media_loaded=True, volume_label="BROKEN", disc_kind=DiscKind.DATA
    )

    class EmptyRipper:
        def __init__(self, runner) -> None:
            pass

        async def rip(self, job_id, letter, destination, callback=None):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"\0" * (64 * 2048))
            return destination

    monkeypatch.setattr(workflow_module, "DataDiscRipper", EmptyRipper)

    with pytest.raises(RuntimeError, match="does not hold everything"):
        await service._back_up_data_disc("job-id", drive, job, staging)
