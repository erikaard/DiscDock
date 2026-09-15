from __future__ import annotations

import base64
from pathlib import Path

import pytest

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
