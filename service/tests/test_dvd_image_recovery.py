from __future__ import annotations

from pathlib import Path

import pytest

from discdock import media_tools
from discdock.media_tools import FfmpegDvdRecovery
from discdock.optical import SECTOR_SIZE, dvd_title_video_ranges, dvd_video_scrambled
from discdock.processes import ProcessFailure, ProcessResult


class RecordingRunner:
    """An FFmpeg that reports progress once and writes a copy."""

    def __init__(self, partial: Path, written: int = 2 * 1024 * 1024, return_code: int = 0):
        self.partial = partial
        self.written = written
        self.return_code = return_code
        self.args: list[str] = []
        self.kwargs: dict = {}

    async def run(self, owner: str, args: list[str], **kwargs) -> ProcessResult:
        self.owner = owner
        self.args = args
        self.kwargs = kwargs
        await kwargs["on_line"]("frame=1000")
        await kwargs["on_line"]("total_size=153600")
        self.partial.parent.mkdir(parents=True, exist_ok=True)
        self.partial.write_bytes(b"x" * self.written)
        return ProcessResult(args=args, return_code=self.return_code)


def _tools(tmp_path: Path) -> tuple[str, str]:
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    ffmpeg.touch()
    ffprobe.touch()
    return str(ffmpeg), str(ffprobe)


@pytest.mark.asyncio
async def test_ffmpeg_reads_the_movies_own_sectors_of_the_image(monkeypatch, tmp_path: Path) -> None:
    image = tmp_path / "rescued-disc.iso"
    image.touch()
    destination = tmp_path / "recovery"
    runner = RecordingRunner(destination / "recovered.part.mkv")
    monkeypatch.setattr(media_tools, "_probe_media_duration", lambda path, probe: 2880.0)
    events: list[dict] = []

    output = await FfmpegDvdRecovery(*_tools(tmp_path), runner).recover(
        "job-1",
        image,
        [(100, 200), (300, 350)],
        destination,
        expected_duration_seconds=3000,
        timeout=43200,
        callback=events.append,
    )

    assert output == destination / "recovered.mkv"
    assert output.exists() and not (destination / "recovered.part.mkv").exists()
    source = runner.args[runner.args.index("-i") + 1]
    assert source == (
        f"concat:subfile,,start,{100 * SECTOR_SIZE},end,{200 * SECTOR_SIZE},,:{image}"
        f"|subfile,,start,{300 * SECTOR_SIZE},end,{350 * SECTOR_SIZE},,:{image}"
    )
    assert runner.args[runner.args.index("-c") + 1] == "copy", "the video and audio are copied, never re-encoded"
    assert "0:v:0" in runner.args and "0:a?" in runner.args
    # Half of the 150 sectors the title holds have been written.
    percent = [event["percent"] for event in events if event["type"] == "progress"]
    assert percent == [50.0], "progress follows the bytes written against the title's own size"


@pytest.mark.asyncio
async def test_ffmpeg_rejects_a_copy_that_stops_in_the_damage(monkeypatch, tmp_path: Path) -> None:
    image = tmp_path / "rescued-disc.iso"
    image.touch()
    destination = tmp_path / "recovery"
    runner = RecordingRunner(destination / "recovered.part.mkv")
    monkeypatch.setattr(media_tools, "_probe_media_duration", lambda path, probe: 600.0)

    with pytest.raises(ProcessFailure, match="copied only 10 of 50 minutes"):
        await FfmpegDvdRecovery(*_tools(tmp_path), runner).recover(
            "job-2", image, [(100, 200)], destination, expected_duration_seconds=3000, timeout=43200
        )

    assert not (destination / "recovered.mkv").exists()


@pytest.mark.asyncio
async def test_ffmpeg_reports_what_it_printed_when_it_fails(tmp_path: Path) -> None:
    image = tmp_path / "rescued-disc.iso"
    image.touch()
    destination = tmp_path / "recovery"

    class FailingRunner(RecordingRunner):
        async def run(self, owner: str, args: list[str], **kwargs) -> ProcessResult:
            return ProcessResult(args=args, return_code=1, lines=["total_size=1", "Invalid data found"])

    with pytest.raises(ProcessFailure, match="Invalid data found"):
        await FfmpegDvdRecovery(*_tools(tmp_path), FailingRunner(destination / "recovered.part.mkv")).recover(
            "job-3", image, [(100, 200)], destination, expected_duration_seconds=3000, timeout=43200
        )


def test_the_movies_own_sectors_are_found_without_the_discs_navigation() -> None:
    from test_disc_rescue import _dvd_reader, _vts_ifo

    read, total = _dvd_reader(vts_ifo=_vts_ifo([(0, 49), (120, 199)], [0, 50, 120]), vts_title=1, chapters=2)

    ranges = dvd_title_video_ranges(read, total, 2)

    assert ranges == [(700, 750), (820, 900)], "only the cells the title plays"
    assert dvd_title_video_ranges(read, total, 7) == [], "a title the disc does not have"


def test_a_title_without_a_readable_cell_table_falls_back_to_its_video_files() -> None:
    from test_disc_rescue import _dvd_reader

    read, total = _dvd_reader()

    assert dvd_title_video_ranges(read, total, 2) == [(700, 900)], "the whole title set, menus excluded"


def _pack(stream_id: int, scrambling: int) -> bytes:
    pack = bytearray(SECTOR_SIZE)
    pack[0:4] = b"\x00\x00\x01\xba"
    pack[14:17] = b"\x00\x00\x01"
    pack[17] = stream_id
    pack[18:20] = (SECTOR_SIZE - 20).to_bytes(2, "big")
    pack[20] = 0x80 | (scrambling << 4)
    return bytes(pack)


def test_scrambled_video_is_recognised_so_it_is_not_copied_as_noise() -> None:
    clear = _pack(0xE0, 0) * 8
    scrambled = _pack(0xE0, 1) * 8
    navigation = _pack(0xBF, 0) * 8

    def reader(data: bytes):
        def read(lba: int, count: int) -> bytes:
            return data[lba * SECTOR_SIZE : (lba + count) * SECTOR_SIZE]

        return read

    assert dvd_video_scrambled(reader(clear), [(0, 8)]) is False
    assert dvd_video_scrambled(reader(scrambled), [(0, 8)]) is True
    # Navigation packs are never scrambled and say nothing about the video.
    assert dvd_video_scrambled(reader(navigation), [(0, 8)]) is False
    assert dvd_video_scrambled(reader(clear), []) is False


def test_the_discs_own_tables_list_the_titles_makemkv_refuses() -> None:
    from test_disc_rescue import _dvd_reader, _vts_ifo

    from discdock.optical import DvdTitle, dvd_longest_title, dvd_titles, read_video_ts_files

    read, total = _dvd_reader(
        vts_ifo=_vts_ifo([(0, 49), (120, 199)], [0, 50, 120], seconds=4431), vts_title=1, chapters=2
    )
    files = read_video_ts_files(read, total)

    assert dvd_titles(read, files) == [DvdTitle(number=2, title_set=1, duration_seconds=4431, chapters=2)]
    assert dvd_longest_title(read, files) == 2, "the longest title is still the one to rescue"


def test_a_rescued_image_can_be_scanned_without_makemkv(tmp_path: Path) -> None:
    from test_disc_rescue import _dvd_reader, _vts_ifo

    from discdock.workflow import DiscDockService

    read, total = _dvd_reader(
        vts_ifo=_vts_ifo([(0, 49), (120, 199)], [0, 50, 120], seconds=4431), vts_title=1, chapters=2
    )
    image = tmp_path / "rescued-disc.iso"
    image.write_bytes(read(0, total))

    scan = DiscDockService._titles_from_disc_tables(image)

    assert scan is not None and len(scan.titles) == 1
    title = scan.titles[0]
    assert (title.id, title.disc_title_number, title.duration_seconds, title.chapters) == (0, 2, 4431, 2)
    assert title.size_bytes == 130 * SECTOR_SIZE, "only the sectors the title plays"
    assert DiscDockService._titles_from_disc_tables(tmp_path / "missing.iso") is None
    (tmp_path / "not-a-disc.iso").write_bytes(b"\0" * (64 * SECTOR_SIZE))
    assert DiscDockService._titles_from_disc_tables(tmp_path / "not-a-disc.iso") is None
