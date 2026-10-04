from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_disc_rescue import _dvd_reader, _vts_ifo
from test_workflow_recovery import make_job, make_service

from discdock.disc_rescue import BAD, FINISHED, RescueMap
from discdock.dvd_mend import (
    DSI_CELL_ID,
    DSI_END_ADDRESS,
    DSI_LBN,
    DSI_REFERENCES,
    DSI_VOB_ID,
    PADDING_PACK,
    PCI_END_PTM,
    PCI_LBN,
    PCI_START_PTM,
    SRI_END_OF_CELL,
    SRI_HAS_VIDEO,
    SRI_NEXT,
    SRI_PREVIOUS,
    _Holes,
    _restore_from_backups,
    is_navigation_pack,
    plan_dvd_mend,
    plan_image_mend,
    write_mended_copy,
)
from discdock.makemkv import DiscScan, NoVideoTitles
from discdock.optical import SECTOR_SIZE, DiscFile, OpticalError
from discdock.processes import ProcessResult
from discdock.settings import AppSettings

# Title 2 of the test disc plays title set 1, whose video sits at sectors 700-899:
# ten units of 20 sectors, each a navigation pack, 15 video packs and 4 audio packs.
VIDEO_START = 700
UNIT = 20
UNITS = 10


def _be32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "big")


def _navigation(unit: int, vob_id: int, cell_id: int, start_ptm: int) -> bytes:
    pack = bytearray(SECTOR_SIZE)
    pack[0:4] = b"\x00\x00\x01\xba"
    pack[4:14] = bytes.fromhex("44000400040101 89c3f8".replace(" ", ""))
    pack[14:18] = b"\x00\x00\x01\xbb"
    pack[18:20] = (18).to_bytes(2, "big")
    pack[0x26:0x2A] = b"\x00\x00\x01\xbf"
    pack[0x2A:0x2C] = (0x3D4).to_bytes(2, "big")
    pack[0x2C] = 0x00
    pack[0x400:0x404] = b"\x00\x00\x01\xbf"
    pack[0x404:0x406] = (0x3FA).to_bytes(2, "big")
    pack[0x406] = 0x01
    relative = (unit * UNIT).to_bytes(4, "big")
    pack[0x2D:0x31] = relative
    pack[0x40B:0x40F] = relative
    pack[0x40F:0x413] = (UNIT - 1).to_bytes(4, "big")
    pack[0x413:0x417] = (6).to_bytes(4, "big")
    pack[0x39:0x3D] = start_ptm.to_bytes(4, "big")
    pack[0x3D:0x41] = (start_ptm + 45_000).to_bytes(4, "big")
    pack[0x41F:0x421] = vob_id.to_bytes(2, "big")
    pack[0x422] = cell_id
    return bytes(pack)


def _elementary(stream_id: int) -> bytes:
    pack = bytearray(SECTOR_SIZE)
    pack[0:4] = b"\x00\x00\x01\xba"
    pack[4:14] = bytes.fromhex("44000400040101 89c3f8".replace(" ", ""))
    pack[14:18] = b"\x00\x00\x01" + bytes([stream_id])
    pack[18:20] = (SECTOR_SIZE - 20).to_bytes(2, "big")
    pack[20] = 0x90  # scrambled, as on a CSS disc
    pack[21:] = bytes([stream_id]) * (SECTOR_SIZE - 21)
    return bytes(pack)


def _cell_table(cells: list[tuple[int, int, int, int]]) -> bytes:
    table = bytearray(SECTOR_SIZE)
    table[0:2] = (1).to_bytes(2, "big")
    table[4:8] = (8 + 12 * len(cells) - 1).to_bytes(4, "big")
    for index, (vob_id, cell_id, first, last) in enumerate(cells):
        entry = 8 + 12 * index
        table[entry : entry + 2] = vob_id.to_bytes(2, "big")
        table[entry + 2] = cell_id
        table[entry + 4 : entry + 8] = first.to_bytes(4, "big")
        table[entry + 8 : entry + 12] = last.to_bytes(4, "big")
    return bytes(table)


def _disc(*, second_vob: bool = False) -> tuple[bytearray, int]:
    """The test disc with two cells; the second can have its own VOB and so its own clock."""
    cells = [(1, 1, 0, 99), (2 if second_vob else 1, 2 if not second_vob else 1, 100, 199)]
    ifo = bytearray(_vts_ifo([(0, 99), (100, 199)], [unit * UNIT for unit in range(UNITS)]))
    ifo[0xE0:0xE4] = (4).to_bytes(4, "big")
    ifo += _cell_table(cells)
    read, total = _dvd_reader(vts_ifo=bytes(ifo), vts_title=1, chapters=2)
    disc = bytearray(read(0, total))
    for unit in range(UNITS):
        vob_id, cell_id = cells[0][:2] if unit < 5 else cells[1][:2]
        start_ptm = 500 + (unit - 5) * 45_000 if second_vob and unit >= 5 else 1_000 + unit * 45_000
        sector = VIDEO_START + unit * UNIT
        packs = [_navigation(unit, vob_id, cell_id, start_ptm)]
        packs += [_elementary(0xE0)] * 15 + [_elementary(0xBD)] * 4
        disc[sector * SECTOR_SIZE : (sector + UNIT) * SECTOR_SIZE] = b"".join(packs)
    return disc, total


def _reader(disc: bytearray, holes: list[tuple[int, int]]):
    """Reads ``disc`` as rescued: zeros wherever the disc was not read."""
    image = bytearray(disc)
    for low, high in holes:
        image[low * SECTOR_SIZE : high * SECTOR_SIZE] = bytes((high - low) * SECTOR_SIZE)

    def read(lba: int, count: int) -> bytes:
        return bytes(image[lba * SECTOR_SIZE : (lba + count) * SECTOR_SIZE])

    return read, image


def test_padding_is_a_whole_mpeg_pack() -> None:
    assert len(PADDING_PACK) == SECTOR_SIZE
    assert PADDING_PACK[:4] == b"\x00\x00\x01\xba" and PADDING_PACK[13] & 0x07 == 0
    assert PADDING_PACK[14:18] == b"\x00\x00\x01\xbe"
    assert int.from_bytes(PADDING_PACK[18:20], "big") == SECTOR_SIZE - 20, "the packet ends with the sector"


def test_a_lost_navigation_pack_is_rebuilt_and_the_broken_pictures_cleared() -> None:
    disc, total = _disc()
    # Unit 3 lost its navigation pack and the start of its pictures; unit 6 lost two sectors mid-way.
    holes = [(760, 764), (830, 832)]
    read, _ = _reader(disc, holes)

    mend = plan_dvd_mend(read, total, holes, 2)

    assert mend.title_set == 1 and mend.unread_sectors == 6
    assert mend.rebuilt_navigation == 1
    # Unit 3's video (sectors 761-775) is all gone or depends on what is gone; unit 6 keeps
    # its pictures before the hole (821-829) and loses the three after it (832-835).
    assert mend.cleared_video_sectors == 12 + 4
    assert mend.padding == [(760, 776), (830, 836)], "the audio after each hole is kept"
    rebuilt = mend.sectors[760]
    assert is_navigation_pack(rebuilt)
    assert _be32(rebuilt, PCI_LBN) == _be32(rebuilt, DSI_LBN) == 60
    assert _be32(rebuilt, DSI_END_ADDRESS) == UNIT - 1
    # Time runs on evenly between the units either side: unit 3 covers what unit 3 did.
    assert (_be32(rebuilt, PCI_START_PTM), _be32(rebuilt, PCI_END_PTM)) == (136_000, 181_000)
    assert (int.from_bytes(rebuilt[DSI_VOB_ID : DSI_VOB_ID + 2], "big"), rebuilt[DSI_CELL_ID]) == (1, 1)
    assert all(_be32(rebuilt, offset) == 0 for offset in DSI_REFERENCES), "its pictures are cleared"
    assert _be32(rebuilt, SRI_NEXT) == SRI_HAS_VIDEO | UNIT
    assert _be32(rebuilt, SRI_PREVIOUS) == SRI_HAS_VIDEO | UNIT
    assert set(mend.sectors) == {760}, "surviving navigation packs are left alone"


def test_a_rebuilt_pack_at_a_cell_boundary_does_not_point_into_the_cell_before() -> None:
    disc, total = _disc()
    holes = [(800, 801)]
    read, _ = _reader(disc, holes)

    rebuilt = plan_dvd_mend(read, total, holes, 2).sectors[800]

    assert rebuilt[DSI_CELL_ID] == 2
    assert _be32(rebuilt, SRI_PREVIOUS) == SRI_END_OF_CELL
    assert _be32(rebuilt, SRI_NEXT) == SRI_HAS_VIDEO | UNIT


def test_a_unit_of_another_vob_takes_its_time_from_its_own_vob() -> None:
    # The second cell is another VOB whose clock starts again at 500.
    disc, total = _disc(second_vob=True)
    holes = [(800, 803)]
    read, _ = _reader(disc, holes)

    rebuilt = plan_dvd_mend(read, total, holes, 2).sectors[800]

    assert int.from_bytes(rebuilt[DSI_VOB_ID : DSI_VOB_ID + 2], "big") == 2
    assert (_be32(rebuilt, PCI_START_PTM), _be32(rebuilt, PCI_END_PTM)) == (500, 45_500)


def test_damage_outside_the_movie_needs_no_mending() -> None:
    disc, total = _disc()
    holes = [(1000, 1010)]  # title set 2, not the movie's
    read, _ = _reader(disc, holes)

    mend = plan_dvd_mend(read, total, holes, 2)

    assert not mend.changes and mend.unread_sectors == 0


def test_a_unit_map_that_does_not_match_the_image_is_not_used() -> None:
    disc, total = _disc()
    for unit in range(UNITS):
        sector = (VIDEO_START + unit * UNIT) * SECTOR_SIZE
        disc[sector : sector + SECTOR_SIZE] = _elementary(0xE0)
    holes = [(760, 764)]
    read, _ = _reader(disc, holes)

    with pytest.raises(OpticalError, match="does not match"):
        plan_dvd_mend(read, total, holes, 2)


def test_a_title_the_disc_does_not_have_cannot_be_mended() -> None:
    disc, total = _disc()
    read, _ = _reader(disc, [(760, 764)])

    with pytest.raises(OpticalError, match="title 9"):
        plan_dvd_mend(read, total, [(760, 764)], 9)


def test_an_ifo_sector_the_disc_did_not_give_comes_from_its_backup() -> None:
    disc = bytearray(40 * SECTOR_SIZE)
    for sector in range(10, 14):
        disc[sector * SECTOR_SIZE : (sector + 1) * SECTOR_SIZE] = bytes([sector]) * SECTOR_SIZE
    for sector in range(20, 24):
        disc[sector * SECTOR_SIZE : (sector + 1) * SECTOR_SIZE] = bytes([sector - 10]) * SECTOR_SIZE
    files = {
        "VTS_01_0.IFO": DiscFile("VTS_01_0.IFO", [(10, 4 * SECTOR_SIZE)]),
        "VTS_01_0.BUP": DiscFile("VTS_01_0.BUP", [(20, 4 * SECTOR_SIZE)]),
    }

    def read(lba: int, count: int) -> bytes:
        return bytes(disc[lba * SECTOR_SIZE : (lba + count) * SECTOR_SIZE])

    restored = _restore_from_backups(read, files, _Holes([(11, 12), (23, 24)]))

    assert restored == {11: bytes([11]) * SECTOR_SIZE, 23: bytes([13]) * SECTOR_SIZE}


def test_the_mended_copy_leaves_the_rescued_image_untouched(tmp_path: Path) -> None:
    disc, total = _disc()
    holes = [(760, 764), (830, 832)]
    _, image_bytes = _reader(disc, holes)
    image = tmp_path / "rescued-disc.iso"
    image.write_bytes(image_bytes)
    copy = tmp_path / "mended-disc.iso"

    mend = plan_image_mend(image, holes, 2)
    write_mended_copy(image, copy, mend)

    assert image.read_bytes() == bytes(image_bytes)
    mended = copy.read_bytes()
    assert len(mended) == total * SECTOR_SIZE

    def sector(number: int) -> bytes:
        return mended[number * SECTOR_SIZE : (number + 1) * SECTOR_SIZE]

    assert sector(760) == mend.sectors[760]
    assert all(sector(number) == PADDING_PACK for number in range(761, 776))
    assert sector(776) == disc[776 * SECTOR_SIZE : 777 * SECTOR_SIZE], "audio after the hole"
    assert sector(829) == disc[829 * SECTOR_SIZE : 830 * SECTOR_SIZE], "pictures before the hole"
    assert sector(900) == disc[900 * SECTOR_SIZE : 901 * SECTOR_SIZE]


def _rescued_image(tmp_path: Path, holes: list[tuple[int, int]]) -> tuple[Path, bytes]:
    disc, total = _disc()
    _, image_bytes = _reader(disc, holes)
    image = tmp_path / "rescue" / "rescued-disc.iso"
    image.parent.mkdir(parents=True)
    image.write_bytes(image_bytes)
    rescue_map = RescueMap(total, [(0, total, FINISHED)])
    for low, high in holes:
        rescue_map.set(low, high, BAD)
    rescue_map.save(image.with_name("rescued-disc.iso.map.json"))
    return image, bytes(image_bytes)


@pytest.mark.asyncio
async def test_makemkv_gets_a_mended_copy_when_it_cannot_use_the_rescued_image(tmp_path: Path) -> None:
    settings = AppSettings(data_root=tmp_path, ffmpeg_path="", vlc_path="")
    staging = tmp_path / "raw" / "attempt.partial"
    staging.mkdir(parents=True)
    image, original = _rescued_image(tmp_path, [(760, 764)])
    service, database = make_service(settings, make_job(settings, staging))
    sources: list[str] = []
    seen: dict[str, Any] = {}

    class FakeMakeMKV:
        async def inspect_source(self, job_id, source, min_length, timeout, callback=None):
            sources.append(source)
            if source == f"iso:{image}":
                # "Title #1 (1:30:13) was skipped due to navigation error"
                raise NoVideoTitles("MakeMKV found no video titles", ProcessResult([], 0))
            mended = Path(source.removeprefix("iso:"))
            seen["navigation"] = mended.read_bytes()[760 * SECTOR_SIZE : 761 * SECTOR_SIZE]
            return DiscScan(titles=[_title()])

        async def rip_source(self, job_id, source, destination, title_ids, timeout, **kwargs):
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "title_t00.mkv").write_bytes(b"movie")
            return []

    service._make_mkv = lambda active_settings=None: FakeMakeMKV()  # type: ignore[method-assign]

    await service._extract_title_from_image(
        "job-id", "D:", settings, image, staging, {"disc_title_number": 2, "duration_seconds": 5413}
    )

    assert sources == [f"iso:{image}", f"iso:{image.with_name('mended-disc.iso')}"]
    assert is_navigation_pack(seen["navigation"]), "MakeMKV read the copy with the navigation rebuilt"
    assert (staging / "title_t00.mkv").read_bytes() == b"movie"
    assert not image.with_name("mended-disc.iso").exists(), "the copy is removed afterwards"
    assert image.read_bytes() == original, "the rescued image keeps its holes for later reads"
    log = (settings.resolved_directories()["logs"] / "job-id.log").read_text(encoding="utf-8")
    assert "of the movie unread, 1 navigation packs rebuilt, " in log
    assert "MakeMKV extracted the movie from the mended copy" in log
    assert database.job["status_detail"] == "Extracting the movie from the rescued disc image"


def _title():
    from discdock.models import TitleInfo

    return TitleInfo(id=0, disc_title_number=2, duration_seconds=5399, chapters=2)


@pytest.mark.asyncio
async def test_the_mended_copy_hands_over_to_vlc_when_makemkv_still_fails(tmp_path: Path) -> None:
    settings = AppSettings(data_root=tmp_path, ffmpeg_path="", vlc_path="")
    staging = tmp_path / "raw" / "attempt.partial"
    staging.mkdir(parents=True)
    image, _ = _rescued_image(tmp_path, [(760, 764)])
    service, _ = make_service(settings, make_job(settings, staging))
    sources: list[str] = []

    class FakeMakeMKV:
        async def inspect_source(self, job_id, source, min_length, timeout, callback=None):
            sources.append(source)
            raise NoVideoTitles("MakeMKV found no video titles", ProcessResult([], 0))

    service._make_mkv = lambda active_settings=None: FakeMakeMKV()  # type: ignore[method-assign]

    with pytest.raises(RuntimeError) as failure:
        await service._extract_title_from_image(
            "job-id", "D:", settings, image, staging, {"disc_title_number": 2, "duration_seconds": 5413}
        )

    assert len(sources) == 2
    message = str(failure.value)
    assert "MakeMKV could not use the rescued image" in message
    assert "MakeMKV could not use the mended copy of the image either" in message
    assert "VLC is not installed" in message, "VLC was next in line"
    assert not image.with_name("mended-disc.iso").exists()


@pytest.mark.asyncio
async def test_without_room_for_a_copy_the_image_is_not_mended(monkeypatch, tmp_path: Path) -> None:
    import discdock.workflow as workflow_module

    settings = AppSettings(data_root=tmp_path, ffmpeg_path="", vlc_path="")
    staging = tmp_path / "raw" / "attempt.partial"
    staging.mkdir(parents=True)
    image, _ = _rescued_image(tmp_path, [(760, 764)])
    service, _ = make_service(settings, make_job(settings, staging))
    sources: list[str] = []

    class FakeMakeMKV:
        async def inspect_source(self, job_id, source, min_length, timeout, callback=None):
            sources.append(source)
            raise NoVideoTitles("MakeMKV found no video titles", ProcessResult([], 0))

    service._make_mkv = lambda active_settings=None: FakeMakeMKV()  # type: ignore[method-assign]
    monkeypatch.setattr(
        workflow_module.shutil, "disk_usage", lambda path: type("Usage", (), {"free": 1024})()
    )

    with pytest.raises(RuntimeError, match="not enough free space for a mended copy"):
        await service._extract_title_from_image(
            "job-id", "D:", settings, image, staging, {"disc_title_number": 2, "duration_seconds": 5413},
            methods=("makemkv", "mended"),
        )

    assert sources == [f"iso:{image}"]
