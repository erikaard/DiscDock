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


def test_a_title_sets_video_is_checked_for_scrambling_in_the_drives_folder(tmp_path: Path) -> None:
    from discdock.optical import dvd_folder_video_scrambled

    (tmp_path / "VTS_02_1.VOB").write_bytes(_pack(0xE0, 0) * 64)
    (tmp_path / "VTS_03_1.VOB").write_bytes(_pack(0xE0, 1) * 64)

    assert dvd_folder_video_scrambled(tmp_path, 2) is False
    assert dvd_folder_video_scrambled(tmp_path, 3) is True
    assert dvd_folder_video_scrambled(tmp_path, 4) is True, "unreadable video counts as scrambled"


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


def _menu_led_vts_ifo() -> bytes:
    """Denver, the Last Dinosaur: chapter 1 is a chain of jump commands with no video, which
    MakeMKV and libdvdnav both reject; the others start the 41:19 play-all chain or one
    that skips the 84-second intro."""
    data = bytearray(4 * SECTOR_SIZE)
    data[:12] = b"DVDVIDEO-VTS"
    data[0xC8:0xCC] = (1).to_bytes(4, "big")
    data[0xCC:0xD0] = (2).to_bytes(4, "big")
    ptt = SECTOR_SIZE
    data[ptt : ptt + 2] = (1).to_bytes(2, "big")
    data[ptt + 8 : ptt + 12] = (12).to_bytes(4, "big")
    for chapter, (chain, program) in enumerate([(1, 0), (2, 1), (3, 1)]):
        at = ptt + 12 + 4 * chapter
        data[at : at + 2] = chain.to_bytes(2, "big")
        data[at + 2 : at + 4] = program.to_bytes(2, "big")
    pgci = 2 * SECTOR_SIZE
    data[pgci : pgci + 2] = (3).to_bytes(2, "big")
    for index, (cells, seconds) in enumerate([([], 0), ([(0, 49), (50, 199)], 2479), ([(50, 199)], 2394)]):
        offset = 0x40 + 0x200 * index
        data[pgci + 12 + 8 * index : pgci + 16 + 8 * index] = offset.to_bytes(4, "big")
        pgc = pgci + offset
        data[pgc + 3] = len(cells)
        hours, rest = divmod(seconds, 3600)
        minutes, remainder = divmod(rest, 60)
        for position, value in enumerate((hours, minutes, remainder)):
            data[pgc + 4 + position] = int(f"{value:02d}", 16)
        data[pgc + 0xE8 : pgc + 0xEA] = (0xEC).to_bytes(2, "big")
        for cell, (first, last) in enumerate(cells):
            entry = pgc + 0xEC + 24 * cell
            data[entry + 8 : entry + 12] = first.to_bytes(4, "big")
            data[entry + 20 : entry + 24] = last.to_bytes(4, "big")
    return bytes(data)


def test_a_title_that_starts_with_jumps_plays_as_long_as_its_longest_chain(tmp_path: Path) -> None:
    from test_disc_rescue import _dvd_reader

    from discdock.optical import DvdTitle, dvd_titles, read_video_ts_files
    from discdock.workflow import DiscDockService

    read, total = _dvd_reader(vts_ifo=_menu_led_vts_ifo(), vts_title=1, chapters=3)

    assert dvd_titles(read, read_video_ts_files(read, total)) == [
        DvdTitle(number=2, title_set=1, duration_seconds=2479, chapters=3)
    ]
    image = tmp_path / "rescued-disc.iso"
    image.write_bytes(read(0, total))
    scan = DiscDockService._titles_from_disc_tables(image)
    assert scan is not None and len(scan.titles) == 1
    assert (scan.titles[0].duration_seconds, scan.titles[0].size_bytes) == (2479, 200 * SECTOR_SIZE)


def test_the_tables_are_read_from_the_drives_video_ts_folder(tmp_path: Path) -> None:
    from test_disc_rescue import _dvd_reader

    from discdock.optical import DvdTitle, dvd_titles_in_folder

    read, _ = _dvd_reader(vts_ifo=_menu_led_vts_ifo(), vts_title=1, chapters=3)
    folder = tmp_path / "VIDEO_TS"
    folder.mkdir()
    (folder / "VIDEO_TS.IFO").write_bytes(read(600, 2))
    (folder / "VTS_01_0.IFO").write_bytes(read(602, 4))

    assert dvd_titles_in_folder(folder) == [DvdTitle(number=2, title_set=1, duration_seconds=2479, chapters=3)]
    assert dvd_titles_in_folder(tmp_path / "missing") == []


def _denver_service(tmp_path: Path):
    from test_workflow_recovery import make_job, make_service

    from discdock.models import DiscKind, DriveInfo
    from discdock.settings import AppSettings

    settings = AppSettings(data_root=tmp_path)
    service, database = make_service(settings, make_job(settings, tmp_path / "staging"))
    database.job["metadata"] = {"titles_from_tables": True}
    drive = DriveInfo(
        id="drive-id", letter="D:", name="Reader", media_loaded=True, volume_label="DENVER", disc_kind=DiscKind.DVD
    )
    return service, database, drive, settings


@pytest.mark.asyncio
async def test_titles_from_the_discs_tables_skip_makemkvs_look_at_the_rescued_copy(tmp_path: Path, monkeypatch) -> None:
    from discdock.makemkv import DiscScan
    from discdock.models import TitleInfo
    from discdock.workflow import DiscDockService

    service, _, drive, settings = _denver_service(tmp_path)
    read_structures: list[bool] = []

    async def rescue(*_args, **kwargs):
        read_structures.append(bool(kwargs.get("structures_only")))
        return {}

    async def makemkv_scan(*_args):
        raise AssertionError("MakeMKV cannot see this title in a copy of the disc either")

    tables = DiscScan(title_count=1, titles=[TitleInfo(id=0, disc_title_number=2, duration_seconds=2479)])
    service._run_sector_rescue = rescue  # type: ignore[method-assign]
    service._scan_image = makemkv_scan  # type: ignore[method-assign]
    monkeypatch.setattr(DiscDockService, "_titles_from_disc_tables", staticmethod(lambda path: tables))

    assert await service._scan_rescued_structures("job-id", drive, settings) is tables
    assert read_structures == [True]


@pytest.mark.asyncio
async def test_such_a_title_is_copied_out_without_makemkv(tmp_path: Path) -> None:
    service, _, _, settings = _denver_service(tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    used: list[str] = []

    async def makemkv(*_args, **_kwargs):
        used.append("makemkv")
        return False

    async def ffmpeg(job_id, _settings, image, extracted, main_track, *_args):
        used.append("ffmpeg")
        extracted.mkdir(parents=True, exist_ok=True)
        (extracted / "recovered.mkv").write_bytes(b"x")
        return True

    service._extract_image_with_makemkv = makemkv  # type: ignore[method-assign]
    service._extract_image_with_mended_copy = makemkv  # type: ignore[method-assign]
    service._extract_image_with_ffmpeg = ffmpeg  # type: ignore[method-assign]

    await service._extract_title_from_image(
        "job-id", "D:", settings, tmp_path / "rescued-disc.iso", staging, {"disc_title_number": 2}
    )

    assert used == ["ffmpeg"] and (staging / "recovered.mkv").is_file()


@pytest.mark.asyncio
async def test_makemkv_is_not_trusted_with_a_title_far_shorter_than_the_one_chosen(tmp_path: Path) -> None:
    from discdock.makemkv import DiscScan
    from discdock.models import TitleInfo

    service, _, _, settings = _denver_service(tmp_path)

    class Client:
        async def inspect_source(self, *_args, **_kwargs):
            return DiscScan(titles=[TitleInfo(id=0, disc_title_number=2, duration_seconds=1035)])

        async def rip_source(self, *_args, **_kwargs):
            raise AssertionError("the 17 minutes MakeMKV can see are not the 41 chosen")

    async def event(_event: dict) -> None:
        return None

    problems: list[str] = []
    finished = await service._extract_image_with_makemkv(
        "job-id", "D:", settings, tmp_path / "rescued-disc.iso", tmp_path / "extracted",
        {"disc_title_number": 2, "duration_seconds": 2479}, Client(), "iso:rescued-disc.iso", 120,
        [], {}, event, event, problems, is_dvd=True, last=False,
    )

    assert finished is False and "17:15" in problems[0] and "41:19" in problems[0]
