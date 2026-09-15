from __future__ import annotations

from pathlib import Path

import pytest

from discdock.makemkv import (
    MAKEMKV_LICENSE_ACTION,
    MakeMKVClient,
    MakeMKVLicenseError,
    MakeMKVParser,
    NoVideoTitles,
    has_license_prompt,
    parse_duration,
    parse_robot_line,
)
from discdock.processes import ProcessResult


def test_robot_parser_handles_quoted_fields_and_titles() -> None:
    parser = MakeMKVParser("D:")
    lines = [
        'DRV:0,2,999,1,"TSSTcorp, BDDVDW","THE_SIMPSONS","D:"',
        "TCOUNT:1",
        'TINFO:0,2,0,"Episode, One"',
        'TINFO:0,9,0,"0:22:31"',
        'TINFO:0,11,0,"1450000000"',
        'TINFO:0,8,0,"6"',
        'TINFO:0,15,0,"1"',
        'TINFO:0,24,0,"31"',
        'TINFO:0,26,0,"1-12"',
        'TINFO:0,27,0,"title00.mkv"',
        'TINFO:0,30,0,"Main feature"',
        'TINFO:0,49,0,"B1"',
        'SINFO:0,0,1,0,"Video"',
        "PRGV:50,50,100",
    ]
    events = [parser.accept(line) for line in lines]
    scan = parser.finish()
    assert scan.drive_index == 0
    assert scan.drive_name == "TSSTcorp, BDDVDW"
    assert scan.title_count == 1
    assert scan.titles[0].name == "Episode, One"
    assert scan.titles[0].duration_seconds == 1351
    assert scan.titles[0].angle == 1
    assert scan.titles[0].disc_title_number == 31
    assert scan.titles[0].segment_map == "1-12"
    assert scan.titles[0].description == "Main feature"
    assert scan.titles[0].source_group == "B1"
    assert scan.titles[0].streams[0]["type"] == "Video"
    assert events[-1]["percent"] == 50


def test_robot_helpers_are_defensive() -> None:
    assert parse_robot_line("not robot output") is None
    assert parse_duration("1:02:03") == 3723
    assert parse_duration("bad") == 0


@pytest.mark.parametrize("message_code", [5052, 5053, 5055])
def test_robot_parser_classifies_makemkv_license_prompt_by_code(message_code: int) -> None:
    parser = MakeMKVParser("D:")
    parser.accept(
        f'MSG:{message_code},0,1,"Evaluation period has expired.",'
        '"Evaluation period has expired."'
    )

    assert has_license_prompt(parser.scan.messages) is True


@pytest.mark.asyncio
async def test_native_info_command_never_uses_unsupported_maxlength(tmp_path: Path) -> None:
    executable = tmp_path / "makemkvcon64.exe"
    executable.write_bytes(b"stub")

    class Runner:
        def __init__(self) -> None:
            self.args: list[str] = []

        async def run(self, _owner: str, args: list[str], **kwargs) -> ProcessResult:
            self.args = args
            callback = kwargs["on_line"]
            for line in ('DRV:0,2,999,1,"Drive","DISC","D:"', "TCOUNT:1", 'TINFO:0,9,0,"1:30:00"'):
                await callback(line)
            return ProcessResult(args=args, return_code=0)

    runner = Runner()
    scan = await MakeMKVClient(str(executable), runner).inspect("job", "D:", 600, 99999, 60)
    assert scan.title_count == 1
    assert "--minlength=600" in runner.args
    assert not any(argument.startswith("--maxlength") for argument in runner.args)
    assert "--noscan" not in runner.args, "a disc scan needs MakeMKV to look at the drives"


@pytest.mark.asyncio
async def test_titles_skipped_for_the_minimum_length_are_counted(tmp_path: Path) -> None:
    executable = tmp_path / "makemkvcon64.exe"
    executable.write_bytes(b"stub")

    class Runner:
        async def run(self, _owner: str, args: list[str], **kwargs) -> ProcessResult:
            for number, seconds in (("1/1", 3), ("88", 46)):
                await kwargs["on_line"](
                    f'MSG:3025,0,3,"Title #{number} has length of {seconds} seconds which is less than minimum '
                    f'title length of 600 seconds and was therefore skipped","Title #%1 has length of %2 seconds '
                    f'which is less than minimum title length of %3 seconds and was therefore skipped",'
                    f'"{number}","{seconds}","600"'
                )
            await kwargs["on_line"]("TCOUNT:0")
            return ProcessResult(args=args, return_code=0)

    with pytest.raises(NoVideoTitles, match="found no video titles") as caught:
        await MakeMKVClient(str(executable), Runner()).inspect("job", "D:", 600, 99999, 60)

    assert caught.value.too_short == 2


@pytest.mark.asyncio
async def test_license_prompt_takes_priority_over_generic_inspection_timeout(tmp_path: Path) -> None:
    executable = tmp_path / "makemkvcon64.exe"
    executable.write_bytes(b"stub")

    class Runner:
        async def run(self, _owner: str, args: list[str], **kwargs) -> ProcessResult:
            await kwargs["on_line"](
                'MSG:5053,0,1,"Evaluation period has expired.",'
                '"Evaluation period has expired."'
            )
            return ProcessResult(args=args, return_code=1, timed_out=True)

    with pytest.raises(MakeMKVLicenseError, match="choose Yes to start its 30-day evaluation") as caught:
        await MakeMKVClient(str(executable), Runner()).inspect("job", "D:", 600, 99999, 60)

    assert str(caught.value) == MAKEMKV_LICENSE_ACTION


@pytest.mark.asyncio
async def test_license_prompt_is_classified_during_ripping(tmp_path: Path) -> None:
    executable = tmp_path / "makemkvcon64.exe"
    executable.write_bytes(b"stub")

    class Runner:
        async def run(self, _owner: str, args: list[str], **kwargs) -> ProcessResult:
            await kwargs["on_line"](
                'MSG:5053,0,1,"Registration is required.","Registration is required."'
            )
            return ProcessResult(args=args, return_code=1)

    with pytest.raises(MakeMKVLicenseError, match="Help > Register"):
        await MakeMKVClient(str(executable), Runner()).rip(
            "job", "D:", tmp_path / "out", [0], 60
        )


@pytest.mark.asyncio
async def test_empty_manual_title_selection_is_rejected(tmp_path: Path) -> None:
    executable = tmp_path / "makemkvcon64.exe"
    executable.write_bytes(b"stub")
    with pytest.raises(ValueError, match="At least one"):
        await MakeMKVClient(str(executable), object()).rip("job", "D:", tmp_path / "out", [], 60)


@pytest.mark.asyncio
async def test_rip_source_accepts_a_rescued_iso(tmp_path: Path) -> None:
    executable = tmp_path / "makemkvcon64.exe"
    executable.write_bytes(b"stub")
    image = tmp_path / "rescued disc.iso"
    image.write_bytes(b"image")
    destination = tmp_path / "out"

    class Runner:
        def __init__(self) -> None:
            self.args: list[str] = []

        async def run(self, _owner: str, args: list[str], **_kwargs) -> ProcessResult:
            self.args = args
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "title00.mkv").write_bytes(b"x" * (2 * 1024 * 1024))
            return ProcessResult(args=args, return_code=0)

    runner = Runner()
    await MakeMKVClient(str(executable), runner).rip_source(
        "job", f"iso:{image}", destination, [0], 60
    )

    assert f"iso:{image}" in runner.args
    assert "--noscan" in runner.args, "an image rip must not wait for MakeMKV to probe a damaged disc in the drive"
    assert "0" in runner.args
