from __future__ import annotations

import os
from pathlib import Path

import pytest
from test_disc_rescue import UDF_PARTITION, _bluray_image

from discdock.bluray_folder import ALIGNED_UNIT, SOURCE_PACKET, fill_backup_folder, movie_playlist
from discdock.makemkv import MakeMKVClient
from discdock.optical import SECTOR_SIZE
from discdock.processes import ProcessResult


def _sectors(image: bytearray, block: int, count: int) -> slice:
    start = (UDF_PARTITION + block) * SECTOR_SIZE
    return slice(start, start + count * SECTOR_SIZE)


def test_the_rescued_movie_is_written_as_a_makemkv_backup_folder(tmp_path: Path) -> None:
    data = _bluray_image()
    data[_sectors(data, 200, 1500)] = b"\x47" * (1500 * SECTOR_SIZE)
    data[_sectors(data, 1800, 500)] = b"\x48" * (500 * SECTOR_SIZE)
    data[_sectors(data, 2400, 100)] = b"\x33" * (100 * SECTOR_SIZE)
    image = tmp_path / "rescued-disc.iso"
    image.write_bytes(bytes(data))
    folder = tmp_path / "makemkv-backup"
    (folder / "BDMV").mkdir(parents=True)
    (folder / "discatt.dat").write_bytes(b"attributes")
    (folder / "BDMV" / "INDEX.BDMV").write_bytes(b"x" * 100)

    counts = fill_backup_folder(image, folder, "00800.mpls", placeholder_min_bytes=100 * SECTOR_SIZE)

    movie = (folder / "BDMV" / "STREAM" / "00800.M2TS").read_bytes()
    assert movie == b"\x47" * (1500 * SECTOR_SIZE) + b"\x48" * (500 * SECTOR_SIZE), "both extents, in order"
    extra = folder / "BDMV" / "STREAM" / "00001.M2TS"
    assert extra.stat().st_size == 100 * SECTOR_SIZE
    assert extra.read_bytes() == bytes(100 * SECTOR_SIZE), "an extra the movie does not use stays empty"
    assert (folder / "BDMV" / "INDEX.BDMV").read_bytes() == b"x" * 100, "files MakeMKV already saved are kept"
    assert (folder / "discatt.dat").read_bytes() == b"attributes"
    assert counts == {"copied": 3, "placeholders": 1, "kept": 1, "padded_bytes": 0, "refreshed_bytes": 0}
    # MakeMKV looks files up by the disc's own spelling, so a wrongly spelled copy is renamed.
    assert "index.bdmv" in os.listdir(folder / "BDMV") and "INDEX.BDMV" not in os.listdir(folder / "BDMV")
    assert sorted(os.listdir(folder / "BDMV" / "STREAM")) == ["00001.m2ts", "00800.m2ts"]
    assert sorted(os.listdir(folder / "BDMV" / "PLAYLIST")) == ["00001.mpls", "00800.mpls"]


def test_the_movies_playlist_is_found_in_the_image_by_its_length(tmp_path: Path) -> None:
    image = tmp_path / "rescued-disc.iso"
    image.write_bytes(bytes(_bluray_image()))

    assert movie_playlist(image, 8976) == "00800.mpls"
    assert movie_playlist(image, 61) == "00001.mpls", "the playlist closest in length to the title"
    assert movie_playlist(image) == "00800.mpls", "without a length, the longest playlist"


def _clip_units(size: int) -> bytearray:
    """Clip bytes in aligned units of 32 packets; each unit's first header carries its arrival time."""
    clip = bytearray()
    for unit in range(-(-size // ALIGNED_UNIT)):
        block = bytearray(b"\x55" * ALIGNED_UNIT)
        for packet in range(0, ALIGNED_UNIT, SOURCE_PACKET):
            block[packet + 4] = 0x47
        block[0:4] = ((3 << 30) | unit * 3200).to_bytes(4, "big")
        clip += block
    return clip[:size]


def test_unread_spots_of_the_movie_become_empty_whole_units(tmp_path: Path) -> None:
    data = _bluray_image()
    clip = _clip_units(2000 * SECTOR_SIZE)
    data[_sectors(data, 200, 1500)] = clip[: 1500 * SECTOR_SIZE]
    data[_sectors(data, 1800, 500)] = clip[1500 * SECTOR_SIZE :]
    # The rescue left zeros where it could not read: inside units 100 to 102, and the clip's last sectors.
    data[_sectors(data, 501, 7)] = bytes(7 * SECTOR_SIZE)
    data[_sectors(data, 2296, 4)] = bytes(4 * SECTOR_SIZE)
    image = tmp_path / "rescued-disc.iso"
    image.write_bytes(bytes(data))
    folder = tmp_path / "makemkv-backup"
    unread = [(UDF_PARTITION + 501, UDF_PARTITION + 508), (UDF_PARTITION + 2296, UDF_PARTITION + 2300)]

    counts = fill_backup_folder(image, folder, "00800.mpls", unread=unread)

    unit = ALIGNED_UNIT
    movie = (folder / "BDMV" / "STREAM" / "00800.m2ts").read_bytes()
    assert counts["padded_bytes"] == 3 * unit + (2000 * SECTOR_SIZE - 665 * unit)
    assert movie[: 100 * unit] == clip[: 100 * unit]
    assert movie[103 * unit : 665 * unit] == clip[103 * unit : 665 * unit], "only the units with unread sectors change"
    for number in range(100, 103):
        block = movie[number * unit : (number + 1) * unit]
        headers = [block[packet : packet + 8] for packet in range(0, unit, SOURCE_PACKET)]
        assert all(header[4:7] == b"\x47\x1f\xff" and header[0] >> 6 == 0 for header in headers), "clear null packets"
        # Arrival times run on evenly between the units around the spot.
        assert int.from_bytes(block[0:4], "big") == number * 3200
    tail = movie[665 * unit :]
    assert int.from_bytes(tail[0:4], "big") == 664 * 3200, "at the end of the clip the last arrival time repeats"
    assert tail[-(len(tail) % SOURCE_PACKET) :] == bytes(len(tail) % SOURCE_PACKET)
    assert image.read_bytes() == bytes(data), "the rescue image is not changed"

    again = fill_backup_folder(image, folder, "00800.mpls", unread=unread)

    assert again["copied"] == 0 and again["padded_bytes"] == counts["padded_bytes"] and again["refreshed_bytes"] == 0
    assert (folder / "BDMV" / "STREAM" / "00800.m2ts").read_bytes() == movie, "filling a kept clip again changes nothing"


def test_spots_the_rescue_has_read_since_are_filled_from_the_image(tmp_path: Path) -> None:
    data = _bluray_image()
    clip = _clip_units(2000 * SECTOR_SIZE)
    data[_sectors(data, 200, 1500)] = clip[: 1500 * SECTOR_SIZE]
    data[_sectors(data, 1800, 500)] = clip[1500 * SECTOR_SIZE :]
    data[_sectors(data, 501, 7)] = bytes(7 * SECTOR_SIZE)
    image = tmp_path / "rescued-disc.iso"
    image.write_bytes(bytes(data))
    folder = tmp_path / "makemkv-backup"
    fill_backup_folder(image, folder, "00800.mpls", unread=[(UDF_PARTITION + 501, UDF_PARTITION + 508)])
    # A later rescue reads those sectors after all.
    data[_sectors(data, 200, 1500)] = clip[: 1500 * SECTOR_SIZE]
    image.write_bytes(bytes(data))

    counts = fill_backup_folder(image, folder, "00800.mpls", unread=[])

    assert counts["refreshed_bytes"] == 3 * ALIGNED_UNIT and counts["padded_bytes"] == 0
    assert (folder / "BDMV" / "STREAM" / "00800.m2ts").read_bytes() == bytes(clip), "the empty units hold the movie again"
    assert fill_backup_folder(image, folder, "00800.mpls", unread=[])["refreshed_bytes"] == 0, "only once"


@pytest.mark.asyncio
async def test_makemkv_is_stopped_once_it_has_saved_the_discs_attributes(tmp_path: Path) -> None:
    executable = tmp_path / "makemkvcon64.exe"
    executable.write_bytes(b"stub")
    folder = tmp_path / "makemkv-backup"

    class Runner:
        def __init__(self) -> None:
            self.cancelled: list[str] = []
            self.args: list[str] = []

        async def run(self, owner_id: str, args: list[str], **kwargs) -> ProcessResult:
            self.args = args
            for index in range(10):
                if index == 2:
                    (folder / "discatt.dat").write_bytes(b"attributes")
                await kwargs["on_line"](f'PRGC:5046,{index},"Copying file"')
                if self.cancelled:
                    break
            return ProcessResult(args=args, return_code=1, cancelled=bool(self.cancelled))

        async def cancel(self, owner_id: str) -> bool:
            self.cancelled.append(owner_id)
            return True

    runner = Runner()

    saved = await MakeMKVClient(str(executable), runner).capture_disc_attributes("job", 0, folder)  # type: ignore[arg-type]

    assert saved.read_bytes() == b"attributes"
    assert runner.cancelled == ["job"], "the rest of the damaged disc is not copied"
    assert runner.args[-3:] == ["backup", "disc:0", str(folder)]
