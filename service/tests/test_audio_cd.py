from __future__ import annotations

from pathlib import Path

import pytest

from discdock.media_tools import AudioRipper
from discdock.processes import ProcessFailure, ProcessResult

# The start of a cyanrip 0.9.3 rip.
START_REPORT = [
    "Checking E: for cdrom...",
    "\t\tCDROM sensed: HL-DT-ST BD-RE BU40N      1.05",
    "Opening drive...",
    "Offset:         +6 samples",
    "DiscID:         ybzdi.je6cuccUFvEROdOIHCLlQ-",
    "Album:          Unknown disc",
    "Disc tracks:    2",
    "Tracks to rip:  all",
]


class CyanripRunner:
    def __init__(self, lines: list[str], *, return_code: int = 0, rips: bool = True) -> None:
        self.lines = lines
        self.return_code = return_code
        self.rips = rips
        self.args: list[str] = []

    async def run(self, owner: str, args: list[str], **kwargs) -> ProcessResult:
        del owner
        self.args = args
        for line in self.lines:
            await kwargs["on_line"](line)
        if self.rips:
            album = Path(kwargs["cwd"]) / "Unknown disc [FLAC]"
            album.mkdir(exist_ok=True)
            (album / "01 - Unknown track.flac").write_bytes(b"flac")
        return ProcessResult(args=args, return_code=self.return_code, lines=self.lines)


def make_ripper(tmp_path: Path, runner: CyanripRunner) -> AudioRipper:
    executable = tmp_path / "cyanrip.exe"
    executable.write_bytes(b"")
    return AudioRipper(str(executable), runner)  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize("offset", [0, 6, -1164])
async def test_cyanrip_always_gets_the_drive_read_offset(tmp_path: Path, offset: int) -> None:
    runner = CyanripRunner(START_REPORT)

    await make_ripper(tmp_path, runner).rip("job-id", "E:", tmp_path / "album", offset=offset)

    # Without -s, cyanrip stops on drives that can read ISRC codes: "Offset is unset!"
    assert runner.args[runner.args.index("-s") + 1] == str(offset)
    assert runner.args[runner.args.index("-d") + 1] == "E:"


@pytest.mark.asyncio
async def test_cyanrip_never_asks_musicbrainz(tmp_path: Path) -> None:
    runner = CyanripRunner(START_REPORT)

    await make_ripper(tmp_path, runner).rip("job-id", "E:", tmp_path / "album")

    # DiscDock looks the album up itself, so "MusicBrainz query failed: 503" can no longer stop a rip.
    assert "-N" in runner.args
    assert "-U" in runner.args


@pytest.mark.asyncio
async def test_the_rip_reports_the_cd_and_its_progress_without_logging_every_tick(tmp_path: Path) -> None:
    def ticks(track: int) -> list[str]:
        return [f"Ripping and encoding track {track}, progress - {percent:.2f}%, ETA - 3m" for percent in (0, 0.1, 25, 50, 50.2, 100)]

    lines = [
        *START_REPORT,
        *ticks(1),
        "Track 1 ripped and encoded successfully!",
        *ticks(2),
        "Track 2 ripped and encoded successfully!",
    ]
    events: list[dict] = []

    await make_ripper(tmp_path, CyanripRunner(lines)).rip("job-id", "E:", tmp_path / "album", callback=events.append)

    assert {"type": "cd", "discid": "ybzdi.je6cuccUFvEROdOIHCLlQ-", "tracks": 2} in events
    assert [event["percent"] for event in events if event["type"] == "progress"] == [0.0, 12.5, 25.0, 50.0, 62.5, 75.0, 99.0]
    logged = [event["message"] for event in events if event["type"] == "log"]
    assert not any("progress -" in message for message in logged)
    assert "Track 1 ripped and encoded successfully!" in logged
    assert logged[-1] == "cyanrip ripped 2 of 2 tracks"
    # The job card and the log say which track is being ripped, once per track.
    assert [event["message"] for event in events if event["type"] == "stage"] == ["Ripping track 1 of 2", "Ripping track 2 of 2"]


@pytest.mark.asyncio
async def test_a_failed_rip_is_reported(tmp_path: Path) -> None:
    runner = CyanripRunner(["Opening drive...", "Error opening drive!"], return_code=1, rips=False)

    with pytest.raises(ProcessFailure, match="Audio-CD ripping failed"):
        await make_ripper(tmp_path, runner).rip("job-id", "E:", tmp_path / "album")
