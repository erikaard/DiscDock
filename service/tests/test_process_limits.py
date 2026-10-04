from __future__ import annotations

import sys
from pathlib import Path

import pytest

from discdock.makemkv import (
    LICENSE_PROMPT_SILENCE_SECONDS,
    SLOW_SCAN_SILENCE_SECONDS,
    SLOW_SCAN_TIMEOUT_SECONDS,
    MakeMKVClient,
)
from discdock.processes import ProcessLimits, ProcessResult, ProcessRunner


@pytest.mark.asyncio
async def test_a_tool_can_be_given_more_time_while_it_runs() -> None:
    runner = ProcessRunner()
    limits = ProcessLimits(timeout=20, no_output_timeout=1)
    # Silent for two seconds after its first line: longer than the one second it starts with.
    program = "import time; print('SLOW', flush=True); time.sleep(2); print('DONE', flush=True)"

    def on_line(line: str) -> None:
        if line == "SLOW":
            limits.no_output_timeout = 10

    result = await runner.run(
        "limits-test",
        [sys.executable, "-c", program],
        timeout=20,
        no_output_timeout=1,
        on_line=on_line,
        keep_awake=False,
        limits=limits,
    )

    assert not result.timed_out
    assert result.lines == ["SLOW", "DONE"]


class _Runner:
    """Plays MakeMKV's lines and records the time limits the scan ended up with."""

    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.limits: ProcessLimits | None = None

    async def run(self, _owner: str, args: list[str], **kwargs) -> ProcessResult:
        self.limits = kwargs["limits"]
        for line in self.lines:
            await kwargs["on_line"](line)
        return ProcessResult(args=args, return_code=0)


@pytest.mark.asyncio
async def test_a_dvd_whose_ifo_must_be_rebuilt_gets_the_time_makemkv_asks_for(tmp_path: Path) -> None:
    executable = tmp_path / "makemkvcon64.exe"
    executable.write_bytes(b"stub")
    runner = _Runner(
        [
            (
                'MSG:3042,0,1,"IFO file for VTS #1 is corrupt, VOB file must be scanned. This may take very long '
                'time, please be patient."'
            ),
            "TCOUNT:1",
            'TINFO:0,9,0,"1:59:00"',
        ]
    )

    await MakeMKVClient(str(executable), runner, no_output_timeout=180).inspect("job", "D:", 600, 99999, 900)

    assert runner.limits is not None
    assert runner.limits.no_output_timeout == SLOW_SCAN_SILENCE_SECONDS
    assert runner.limits.timeout == SLOW_SCAN_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_a_licence_question_is_not_waited_on_for_long(tmp_path: Path) -> None:
    executable = tmp_path / "makemkvcon64.exe"
    executable.write_bytes(b"stub")
    runner = _Runner(['MSG:5021,0,1,"This application version is too old."', "TCOUNT:1", 'TINFO:0,9,0,"1:30:00"'])

    with pytest.raises(Exception, match="too old"):
        await MakeMKVClient(str(executable), runner, no_output_timeout=600).inspect("job", "D:", 600, 99999, 900)

    assert runner.limits is not None and runner.limits.no_output_timeout == LICENSE_PROMPT_SILENCE_SECONDS


@pytest.mark.asyncio
async def test_ripping_such_a_dvd_gets_the_same_time_as_scanning_it(tmp_path: Path) -> None:
    # Ghostbusters II: the scan was given its seven silent minutes, then the rip, which
    # repeats MakeMKV's analysis of the disc, was stopped three minutes into it.
    executable = tmp_path / "makemkvcon64.exe"
    executable.write_bytes(b"stub")
    destination = tmp_path / "rip"

    class RipRunner(_Runner):
        async def run(self, _owner: str, args: list[str], **kwargs) -> ProcessResult:
            result = await super().run(_owner, args, **kwargs)
            (destination / "title_t00.mkv").write_bytes(b"x" * (2 * 1024 * 1024))
            return result

    runner = RipRunner(['MSG:3042,0,1,"IFO file for VTS #1 is corrupt, VOB file must be scanned."'])

    await MakeMKVClient(str(executable), runner, no_output_timeout=180).rip("job", "D:", destination, [0], 43200)

    assert runner.limits is not None
    assert runner.limits.no_output_timeout == SLOW_SCAN_SILENCE_SECONDS
    assert runner.limits.timeout == 43200, "a long rip keeps its own, longer limit"
