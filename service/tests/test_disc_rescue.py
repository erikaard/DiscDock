from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from discdock import disc_rescue
from discdock.disc_rescue import (
    BAD,
    FINISHED,
    NON_TRIED,
    PENDING,
    RescueEngine,
    RescueMap,
    RescueOptions,
    WrongDiscError,
    merge_rescue_images,
)
from discdock.optical import (
    SECTOR_SIZE,
    CopyProtectionError,
    OpticalDevice,
    UnreadableSectorError,
    mpls_clips,
    read_bluray_layout,
    read_dvd_layout,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def sector_bytes(lba: int, count: int, salt: int = 0) -> bytes:
    return b"".join(bytes([(sector + salt) % 255 + 1]) * SECTOR_SIZE for sector in range(lba, lba + count))


class FakeDisc(OpticalDevice):
    method = "fake"

    def __init__(
        self,
        total_sectors: int,
        bad: set[int] | None = None,
        *,
        clock: FakeClock | None = None,
        max_transfer: int = 32,
        interrupt_after: int = 0,
        protected: set[int] | None = None,
        flaky: dict[int, int] | None = None,
        salt: int = 0,
    ) -> None:
        self.total_sectors = total_sectors
        self.bad = bad or set()
        self.protected = protected or set()
        # Sectors that fail this many reads before they read (a marginal block).
        self.flaky = flaky or {}
        self.salt = salt
        self.clock = clock
        self.max_transfer_sectors = max_transfer
        self.interrupt_after = interrupt_after
        self.reads: list[tuple[int, int]] = []
        self.failures = 0

    def _fail(self, lba: int) -> None:
        self.failures += 1
        if self.clock:
            self.clock.sleep(8.0)
        raise UnreadableSectorError(f"bad sector near {lba}")

    def read(self, lba: int, count: int) -> bytes:
        assert 0 <= lba and lba + count <= self.total_sectors
        assert count <= self.max_transfer_sectors
        self.reads.append((lba, count))
        if self.interrupt_after and len(self.reads) > self.interrupt_after:
            raise KeyboardInterrupt()
        sectors = range(lba, lba + count)
        if any(sector in self.protected for sector in sectors):
            raise CopyProtectionError("not authenticated")
        if any(sector in self.bad for sector in sectors):
            self._fail(lba)
        marginal = [sector for sector in sectors if self.flaky.get(sector, 0) > 0]
        if marginal:
            for sector in marginal:
                self.flaky[sector] -= 1
            self._fail(lba)
        if self.clock:
            self.clock.sleep(0.05)
        return sector_bytes(lba, count, self.salt)

    def close(self) -> None:
        return None


def make_engine(
    tmp_path: Path, disc: FakeDisc, clock: FakeClock | None = None, **options
) -> tuple[RescueEngine, list[dict]]:
    events: list[dict] = []
    clock = clock or disc.clock or FakeClock()
    engine = RescueEngine(
        disc,
        tmp_path / "disc.iso.part",
        tmp_path / "disc.iso.map.json",
        RescueOptions(**options),
        emit=events.append,
        control_path=tmp_path / "disc.iso.control",
        clock=clock,
        sleep=clock.sleep,
    )
    return engine, events


def load_map(tmp_path: Path) -> RescueMap:
    return RescueMap.load(tmp_path / "disc.iso.map.json")


def assert_image_matches(image: Path, rescue_map: RescueMap) -> None:
    data = image.read_bytes()
    assert len(data) == rescue_map.total_sectors * SECTOR_SIZE
    for start, end, status in rescue_map.ranges():
        chunk = data[start * SECTOR_SIZE : end * SECTOR_SIZE]
        if status == FINISHED:
            assert chunk == sector_bytes(start, end - start), f"finished range {start}-{end} differs"
        else:
            assert chunk == bytes(len(chunk)), f"unread range {start}-{end} is not zero"


def test_map_splits_and_merges_ranges(tmp_path: Path) -> None:
    rescue_map = RescueMap(100)
    rescue_map.set(10, 20, FINISHED)
    rescue_map.set(20, 30, FINISHED)
    rescue_map.set(15, 16, BAD)
    rescue_map.set(0, 10, FINISHED)

    assert list(rescue_map.ranges()) == [(0, 15, FINISHED), (15, 16, BAD), (16, 30, FINISHED), (30, 100, NON_TRIED)]
    assert rescue_map.count(FINISHED) == 29
    assert rescue_map.next_range({NON_TRIED}, 0) == (30, 100, NON_TRIED)
    assert rescue_map.next_range({FINISHED}, 20) == (20, 30, FINISHED)
    assert rescue_map.areas({BAD, NON_TRIED}) == [(15, 16), (30, 100)]
    assert rescue_map.count_within({FINISHED}, [(5, 12), (25, 40)]) == 12

    path = tmp_path / "map.json"
    rescue_map.save(path)
    assert list(RescueMap.load(path).ranges()) == list(rescue_map.ranges())
    with pytest.raises(ValueError):
        RescueMap(10, [(0, 5, FINISHED), (6, 10, BAD)])


def test_clean_disc_is_copied_exactly(tmp_path: Path) -> None:
    disc = FakeDisc(1000, clock=FakeClock())
    engine, events = make_engine(tmp_path, disc)

    summary = engine.run()

    assert summary["unreadable_bytes"] == 0
    assert summary["pending_bytes"] == 0
    assert summary["percent"] == 100
    assert disc.failures == 0
    assert_image_matches(tmp_path / "disc.iso.part", load_map(tmp_path))
    assert events[0]["type"] == "start" and events[-1]["type"] == "done"


def test_an_unreadable_block_skips_to_the_next_video_unit_and_is_retried_later(tmp_path: Path) -> None:
    clock = FakeClock()
    disc = FakeDisc(4096, bad=set(range(320, 336)), clock=clock)
    units = [0, 300, 700, 1000, 1500, 2000, 3000]
    engine, _ = make_engine(tmp_path, disc, clock, unit_starts=units, extra_seconds=0)

    engine.run()

    rescue_map = load_map(tmp_path)
    assert disc.failures == 2, "one large read and one block read locate the damage"
    assert rescue_map.status_at(320) == BAD
    # Like a player, the first pass resumes at the next video unit instead of
    # letting the drive retry every block after the damage.
    assert rescue_map.status_at(336) == NON_TRIED and rescue_map.status_at(699) == NON_TRIED
    assert rescue_map.status_at(700) == FINISHED

    engine, _ = make_engine(tmp_path, disc, clock, unit_starts=units, extra_seconds=10_000)
    summary = engine.run()

    rescue_map = load_map(tmp_path)
    assert list(rescue_map.ranges({BAD})) == [(320, 336, BAD)]
    assert rescue_map.count(PENDING) == 0
    assert disc.failures == 3, "the rest of the unit reads first time; the bad block gets one more try"
    assert summary["stop_reason"] == "finished"
    assert_image_matches(tmp_path / "disc.iso.part", rescue_map)


def test_a_scratch_costs_one_failed_read_per_damaged_unit(tmp_path: Path) -> None:
    # A radial scratch: one unreadable block every 512 sectors across 16 MB.
    clock = FakeClock()
    bad = {sector for sector in range(4096, 12288) if 96 <= sector % 512 < 112}
    units = list(range(0, 16384, 256))
    disc = FakeDisc(16384, bad=bad, clock=clock)
    engine, _ = make_engine(tmp_path, disc, clock, unit_starts=units, extra_seconds=0)

    engine.run()

    assert disc.failures == 16 + 1, "one failed block per damaged unit, plus the large read that found the first"
    assert load_map(tmp_path).count(BAD) == len(bad)

    engine, events = make_engine(tmp_path, disc, clock, unit_starts=units, extra_seconds=10_000_000)
    summary = engine.run()

    rescue_map = load_map(tmp_path)
    assert not any(event.get("phase") == "sweep" for event in events), "a finished first pass is not repeated"
    assert rescue_map.count(PENDING) == 0
    assert rescue_map.count(FINISHED) == 16384 - len(bad)
    assert summary["stop_reason"] == "finished"
    assert_image_matches(tmp_path / "disc.iso.part", rescue_map)


def test_a_long_unreadable_stretch_is_crossed_and_retrying_stops_by_itself(tmp_path: Path) -> None:
    clock = FakeClock()
    bad = set(range(4096, 12288))
    units = list(range(0, 16384, 256))
    disc = FakeDisc(16384, bad=bad, clock=clock)
    engine, _ = make_engine(tmp_path, disc, clock, unit_starts=units, extra_seconds=10_000_000)

    summary = engine.run()

    rescue_map = load_map(tmp_path)
    assert rescue_map.count(FINISHED) == 16384 - len(bad), "readable video on both sides is rescued"
    # Nothing more comes back from the stretch, so retrying ends long before
    # the (huge) time budget instead of trying all 8 MB block by block.
    assert summary["stop_reason"] == "little_left_to_gain"
    assert summary["extra_elapsed_seconds"] < 600
    assert disc.failures <= 60
    assert_image_matches(tmp_path / "disc.iso.part", rescue_map)


def test_the_retry_time_budget_is_kept_across_restarts(tmp_path: Path) -> None:
    bad = {sector for sector in range(2048, 8192) if (sector // 16) % 2 == 0}
    clock = FakeClock()
    disc = FakeDisc(8192, bad=bad, clock=clock)
    engine, _ = make_engine(tmp_path, disc, clock, extra_seconds=120, stall_min_bytes=0)

    summary = engine.run()

    assert summary["stop_reason"] == "budget"
    assert summary["budget_exhausted"] is True
    assert summary["pending_bytes"] > 0
    assert summary["extra_elapsed_seconds"] < 120 + 30

    failures = disc.failures
    engine, _ = make_engine(tmp_path, disc, clock, extra_seconds=120, stall_min_bytes=0)
    summary = engine.run()
    assert disc.failures == failures
    assert summary["budget_exhausted"] is True


def test_finish_request_stops_and_keeps_a_complete_size_image(tmp_path: Path) -> None:
    disc = FakeDisc(4096, clock=FakeClock())
    engine, events = make_engine(tmp_path, disc)
    (tmp_path / "disc.iso.control").write_text("finish", encoding="utf-8")

    summary = engine.run()

    assert summary["finished_early"] is True
    assert summary["stop_reason"] == "skipped"
    assert (tmp_path / "disc.iso.part").stat().st_size == 4096 * SECTOR_SIZE
    assert events[-1]["type"] == "done"


def test_interrupted_rescue_resumes_without_rereading_saved_data(tmp_path: Path) -> None:
    clock = FakeClock()
    first = FakeDisc(2048, clock=clock, interrupt_after=20)
    engine, _ = make_engine(tmp_path, first, clock, save_interval_seconds=0)
    with pytest.raises(KeyboardInterrupt):
        engine.run()
    saved = load_map(tmp_path)
    assert saved.count(FINISHED) >= 19 * 32

    second = FakeDisc(2048, clock=clock)
    engine, _ = make_engine(tmp_path, second, clock)
    engine.run()

    # Single sectors are read first to confirm it is the same disc.
    data_reads = [read for read in second.reads if read[1] > 1]
    assert data_reads[0][0] >= 19 * 32
    rescue_map = load_map(tmp_path)
    assert rescue_map.count(FINISHED) == 2048
    assert_image_matches(tmp_path / "disc.iso.part", rescue_map)


def test_legacy_image_is_adopted_and_only_zero_sectors_are_read_again(tmp_path: Path) -> None:
    image = tmp_path / "disc.iso.part"
    legacy = bytearray(sector_bytes(0, 600))
    legacy[200 * SECTOR_SIZE : 216 * SECTOR_SIZE] = bytes(16 * SECTOR_SIZE)
    image.write_bytes(bytes(legacy))
    disc = FakeDisc(1024, clock=FakeClock())
    engine, events = make_engine(tmp_path, disc)

    engine.run()

    start = next(event for event in events if event["type"] == "start")
    assert start["origin"] == "adopted"
    assert all(not (lba < 200 or 216 <= lba < 600) for lba, count in disc.reads if count > 1)
    rescue_map = load_map(tmp_path)
    assert rescue_map.count(FINISHED) == 1024
    assert_image_matches(image, rescue_map)


def test_only_the_movie_is_read_and_damaged_extras_are_never_touched(tmp_path: Path) -> None:
    disc = FakeDisc(2048, bad=set(range(96, 112)), clock=FakeClock())
    engine, events = make_engine(tmp_path, disc, priority_ranges=[(1024, 2048)])

    summary = engine.run()

    rescue_map = load_map(tmp_path)
    assert disc.failures == 0
    assert all(lba >= 1024 for lba, _ in disc.reads)
    assert rescue_map.status_at(96) == NON_TRIED
    assert rescue_map.count_within({FINISHED}, [(1024, 2048)]) == 1024
    assert summary["movie_pending_bytes"] == 0
    assert summary["not_needed_bytes"] == 1024 * SECTOR_SIZE
    assert rescue_map.meta["relevant_pending_sectors"] == 0, "unread extras are not work for a later retry"
    assert rescue_map.meta["swept_ranges"] == [[1024, 2048]]
    assert {"type": "phase", "phase": "sweep", "scope": "movie"} in events


def test_reading_the_rest_of_the_disc_later_skips_the_movie_already_read(tmp_path: Path) -> None:
    clock = FakeClock()
    disc = FakeDisc(2048, clock=clock)
    engine, _ = make_engine(tmp_path, disc, clock, priority_ranges=[(1024, 2048)])
    engine.run()
    disc.reads.clear()

    engine, _ = make_engine(tmp_path, disc, clock)
    engine.run()

    rescue_map = load_map(tmp_path)
    assert disc.reads and all(lba + count <= 1024 for lba, count in disc.reads)
    assert rescue_map.count(FINISHED) == 2048
    assert rescue_map.meta["swept_ranges"] == [[0, 2048]]
    assert_image_matches(tmp_path / "disc.iso.part", rescue_map)


def test_a_structures_only_run_reads_just_the_navigation_data(tmp_path: Path) -> None:
    clock = FakeClock()
    disc = FakeDisc(2048, clock=clock, flaky={100: 1})
    engine, _ = make_engine(
        tmp_path, disc, clock, priority_ranges=[(0, 2048)], critical_ranges=[(96, 128)], structures_only=True
    )

    summary = engine.run()

    rescue_map = load_map(tmp_path)
    assert rescue_map.count(FINISHED) == 32 and rescue_map.status_at(96) == FINISHED
    assert summary["stop_reason"] == "structures"
    assert not rescue_map.meta.get("sweep_done"), "the movie itself is still to be read"


def test_resuming_reads_rescued_sectors_from_the_saved_image_instead_of_the_drive(tmp_path: Path) -> None:
    image, map_path = _write_rescue(tmp_path / "saved", 64, [(0, 32)])

    read, handle = disc_rescue._saved_image_reader(image, map_path, 64)
    assert read is not None and handle is not None
    try:
        assert read(4, 2) == sector_bytes(4, 2)
        with pytest.raises(UnreadableSectorError):
            read(30, 4)
    finally:
        handle.close()
    assert disc_rescue._saved_image_reader(image, map_path, 128) == (None, None), "another disc never uses the image"


def test_parts_of_ranges_are_subtracted() -> None:
    assert disc_rescue._subtract([(0, 100)], [(10, 20), (50, 60), (90, 120)]) == [(0, 10), (20, 50), (60, 90)]
    assert disc_rescue._subtract([(0, 10), (20, 30)], []) == [(0, 10), (20, 30)]


def _mpls(items: list[tuple[str, float, float, list[str]]]) -> bytes:
    body = b""
    for name, start, end, angles in items:
        item = (
            name.encode()
            + b"M2TS"
            + (0x10 if angles else 0).to_bytes(2, "big")
            + b"\x00"
            + round(start * 45000).to_bytes(4, "big")
            + round(end * 45000).to_bytes(4, "big")
            + bytes(12)
        )
        if angles:
            item += bytes([len(angles) + 1, 0]) + b"".join(angle.encode() + b"M2TS\x00" for angle in angles)
        body += len(item).to_bytes(2, "big") + item
    playlist = (len(body) + 6).to_bytes(4, "big") + bytes(2) + len(items).to_bytes(2, "big") + bytes(2) + body
    return b"MPLS0200" + (40).to_bytes(4, "big") + bytes(28) + playlist


UDF_PARTITION = 1024
UDF_METADATA = 16
UDF_MIRROR = 3040


def _put(image: bytearray, sector: int, offset: int, value: bytes) -> None:
    start = sector * SECTOR_SIZE + offset
    image[start : start + len(value)] = value


def _udf_tag(image: bytearray, sector: int, identifier: int, location: int) -> None:
    base = sector * SECTOR_SIZE
    image[base : base + 4] = identifier.to_bytes(2, "little") + (3).to_bytes(2, "little")
    image[base + 12 : base + 16] = location.to_bytes(4, "little")
    image[base + 4] = (sum(image[base : base + 4]) + sum(image[base + 5 : base + 16])) & 0xFF


def _fid(name: str, block: int, *, directory: bool = False, parent: bool = False) -> bytes:
    identifier = b"" if parent else b"\x08" + name.encode("ascii")
    record = bytearray(38 + len(identifier))
    record[0:2] = (257).to_bytes(2, "little")
    record[16:18] = (1).to_bytes(2, "little")
    record[18] = (0x02 if directory or parent else 0) | (0x08 if parent else 0)
    record[19] = len(identifier)
    record[20:30] = SECTOR_SIZE.to_bytes(4, "little") + block.to_bytes(4, "little") + (1).to_bytes(2, "little")
    record[38:] = identifier
    record[4] = (sum(record[0:4]) + sum(record[5:16])) & 0xFF
    return bytes(record) + bytes(-len(record) % 4)


def _udf_file(image: bytearray, block: int, *, size: int, embedded: bytes = b"", extents: tuple = ()) -> None:
    """An extended file entry in the metadata partition; extents are (partition block, sectors)."""
    sector = UDF_PARTITION + UDF_METADATA + block
    if embedded:
        flags, descriptors = 3, embedded
    else:
        flags = 1
        descriptors = b"".join(
            (count * SECTOR_SIZE).to_bytes(4, "little") + start.to_bytes(4, "little") + bytes(8)
            for start, count in extents
        )
    _put(image, sector, 27, bytes([4 if embedded else 5]))
    _put(image, sector, 34, flags.to_bytes(2, "little"))
    _put(image, sector, 56, size.to_bytes(8, "little"))
    _put(image, sector, 212, len(descriptors).to_bytes(4, "little") + descriptors)
    _udf_tag(image, sector, 266, block)


def _bluray_image(total: int = 4096) -> bytearray:
    """A small UDF 2.50 Blu-ray volume: metadata partition, two playlists, one movie clip in two extents."""
    image = bytearray(total * SECTOR_SIZE)
    _put(image, 256, 16, (16 * SECTOR_SIZE).to_bytes(4, "little") + (32).to_bytes(4, "little"))
    _udf_tag(image, 256, 2, 256)
    _put(image, 32, 22, (0).to_bytes(2, "little"))
    _put(image, 32, 188, UDF_PARTITION.to_bytes(4, "little") + (total - UDF_PARTITION).to_bytes(4, "little"))
    _udf_tag(image, 32, 5, 32)
    _put(image, 33, 212, SECTOR_SIZE.to_bytes(4, "little"))
    _put(image, 33, 248, SECTOR_SIZE.to_bytes(4, "little") + (0).to_bytes(4, "little") + (1).to_bytes(2, "little"))
    _put(image, 33, 268, (2).to_bytes(4, "little"))
    _put(image, 33, 440, bytes([1, 6]) + (1).to_bytes(2, "little") + (0).to_bytes(2, "little"))
    metadata_map = bytearray(64)
    metadata_map[0:2] = bytes([2, 64])
    metadata_map[5:28] = b"*UDF Metadata Partition".ljust(23, b"\x00")
    metadata_map[40:48] = (0).to_bytes(4, "little") + UDF_MIRROR.to_bytes(4, "little")
    _put(image, 33, 446, bytes(metadata_map))
    _udf_tag(image, 33, 6, 33)
    _udf_tag(image, 34, 8, 34)
    for location in (0, UDF_MIRROR):
        sector = UDF_PARTITION + location
        _put(image, sector, 27, bytes([250]))
        _put(image, sector, 56, (32 * SECTOR_SIZE).to_bytes(8, "little"))
        _put(image, sector, 212, (8).to_bytes(4, "little") + (32 * SECTOR_SIZE).to_bytes(4, "little"))
        _put(image, sector, 220, UDF_METADATA.to_bytes(4, "little"))
        _udf_tag(image, sector, 266, location)
    file_set = UDF_PARTITION + UDF_METADATA
    _put(image, file_set, 400, SECTOR_SIZE.to_bytes(4, "little") + (1).to_bytes(4, "little") + (1).to_bytes(2, "little"))
    _udf_tag(image, file_set, 256, 0)
    directories = {
        1: [_fid("", 1, parent=True), _fid("BDMV", 2, directory=True)],
        2: [_fid("", 1, parent=True), _fid("PLAYLIST", 3, directory=True), _fid("STREAM", 4, directory=True), _fid("index.bdmv", 5)],
        3: [_fid("", 2, parent=True), _fid("00800.mpls", 6), _fid("00001.mpls", 7)],
        4: [_fid("", 2, parent=True), _fid("00800.m2ts", 8), _fid("00001.m2ts", 9)],
    }
    for block, identifiers in directories.items():
        content = b"".join(identifiers)
        _udf_file(image, block, size=len(content), embedded=content)
    small_files = {5: (b"INDX0200" + bytes(92), 100), 6: (_mpls([("00800", 0, 7200, [])]), 110), 7: (_mpls([("00001", 0, 60, [])]), 111)}
    for block, (content, start) in small_files.items():
        _put(image, UDF_PARTITION + start, 0, content)
        _udf_file(image, block, size=len(content), extents=((start, 1),))
    _udf_file(image, 8, size=2000 * SECTOR_SIZE, extents=((200, 1500), (1800, 500)))
    _udf_file(image, 9, size=100 * SECTOR_SIZE, extents=((2400, 100),))
    return image


def test_the_blu_ray_layout_finds_the_longest_playlist_through_the_metadata_partition() -> None:
    image = _bluray_image()

    def read(lba: int, count: int) -> bytes:
        return bytes(image[lba * SECTOR_SIZE : (lba + count) * SECTOR_SIZE])

    layout = read_bluray_layout(read, 4096)
    extra = read_bluray_layout(read, 4096, "00001.mpls")

    assert layout.verified, layout.note
    assert (layout.playlist, layout.clips, layout.movie_sectors) == ("00800", ["00800"], 2000)
    assert layout.priority_ranges == [(0, 1125), (1134, 1136), (1224, 2724), (2824, 3324)], "the other clip is left out"
    assert layout.structure_ranges == [(0, 1125), (1134, 1136)]
    assert layout.files["BDMV/STREAM/00800.M2TS"].name == "BDMV/STREAM/00800.m2ts", "names keep the disc's spelling"
    assert (extra.clips, extra.movie_sectors) == (["00001"], 100)


def test_the_blu_ray_layout_uses_the_mirror_when_the_metadata_entry_is_unreadable() -> None:
    image = _bluray_image()

    def read(lba: int, count: int) -> bytes:
        if lba <= UDF_PARTITION < lba + count:
            raise UnreadableSectorError("metadata file entry unreadable")
        return bytes(image[lba * SECTOR_SIZE : (lba + count) * SECTOR_SIZE])

    layout = read_bluray_layout(read, 4096)

    assert layout.verified and layout.clips == ["00800"]


def test_a_blu_ray_playlist_lists_its_clips_and_length() -> None:
    clips, seconds = mpls_clips(_mpls([("00800", 0, 60, []), ("00801", 60, 90, ["00802"]), ("00800", 90, 100, [])]))

    assert clips == ["00800", "00801", "00802"], "every angle is part of the movie, each clip once"
    assert seconds == pytest.approx(100)


def test_navigation_data_is_retried_first_even_without_retry_time(tmp_path: Path) -> None:
    clock = FakeClock()
    disc = FakeDisc(2048, clock=clock, flaky={sector: 2 for sector in range(96, 112)})
    engine, _ = make_engine(tmp_path, disc, clock, extra_seconds=0, critical_ranges=[(96, 112)])

    engine.run()

    rescue_map = load_map(tmp_path)
    assert rescue_map.status_at(96) == FINISHED, "without its IFO data MakeMKV cannot open the image"
    assert rescue_map.status_at(112) == NON_TRIED, "ordinary damage waits for retry time"
    assert_image_matches(tmp_path / "disc.iso.part", rescue_map)


def test_a_different_disc_is_refused_instead_of_mixed_into_the_image(tmp_path: Path) -> None:
    clock = FakeClock()
    engine, _ = make_engine(tmp_path, FakeDisc(1024, clock=clock), clock)
    engine.run()
    before = (tmp_path / "disc.iso.part").read_bytes()

    engine, _ = make_engine(tmp_path, FakeDisc(1024, clock=clock, salt=7), clock)
    with pytest.raises(WrongDiscError):
        engine.run()

    engine, _ = make_engine(tmp_path, FakeDisc(2048, clock=clock), clock)
    with pytest.raises(WrongDiscError):
        engine.run()

    assert (tmp_path / "disc.iso.part").read_bytes() == before


def _write_rescue(folder: Path, total: int, finished: list[tuple[int, int]], salt: int = 0) -> tuple[Path, Path]:
    folder.mkdir(parents=True, exist_ok=True)
    data = bytearray(total * SECTOR_SIZE)
    rescue_map = RescueMap(total)
    for start, end in finished:
        data[start * SECTOR_SIZE : end * SECTOR_SIZE] = sector_bytes(start, end - start, salt)
        rescue_map.set(start, end, FINISHED)
    image = folder / "rescued-disc.iso.part"
    image.write_bytes(bytes(data))
    map_path = folder / "rescued-disc.iso.map.json"
    rescue_map.save(map_path)
    return image, map_path


def test_two_rescues_of_the_same_disc_are_combined(tmp_path: Path) -> None:
    target_image, target_map = _write_rescue(tmp_path / "a", 1024, [(0, 100), (200, 1024)])
    donor_image, donor_map = _write_rescue(tmp_path / "b", 1024, [(0, 50), (100, 150), (300, 1024)])

    copied = merge_rescue_images(target_image, target_map, donor_image, donor_map)

    merged = RescueMap.load(target_map)
    assert copied == 50
    assert list(merged.ranges({FINISHED})) == [(0, 150, FINISHED), (200, 1024, FINISHED)]
    assert_image_matches(target_image, merged)

    stranger_image, stranger_map = _write_rescue(tmp_path / "c", 1024, [(0, 1024)], salt=3)
    with pytest.raises(ValueError, match="not the same disc"):
        merge_rescue_images(target_image, target_map, stranger_image, stranger_map)


@pytest.mark.skipif(os.name != "nt", reason="sparse image allocation is Windows-specific")
def test_a_new_image_is_sized_without_writing_zeros(tmp_path: Path) -> None:
    import ctypes
    from ctypes import wintypes

    total = 256 * 1024  # a 512 MB disc
    engine, _ = make_engine(tmp_path, FakeDisc(total))
    engine.map = RescueMap(total)

    engine._open_image()
    assert engine._image is not None
    engine._image.close()

    image = tmp_path / "disc.iso.part"
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCompressedFileSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetCompressedFileSizeW.restype = wintypes.DWORD
    high = wintypes.DWORD()
    allocated = kernel32.GetCompressedFileSizeW(str(image), ctypes.byref(high)) + (high.value << 32)
    assert image.stat().st_size == total * SECTOR_SIZE
    # Zero-filling took about 100 seconds for a DVD and allocated the whole image.
    assert allocated < 16 * 1024 * 1024, "the image must stay sparse instead of being filled with zeros"


def test_helper_cli_streams_json_events(monkeypatch, tmp_path: Path, capsys) -> None:
    disc = FakeDisc(256)
    monkeypatch.setattr(disc_rescue, "open_optical_device", lambda *_args, **_kwargs: disc)

    code = disc_rescue.main(
        [
            "--drive",
            "D:",
            "--image",
            str(tmp_path / "disc.iso.part"),
            "--map",
            str(tmp_path / "disc.iso.map.json"),
            "--extra-seconds",
            "60",
            "--skip-sectors",
            "512",
        ]
    )

    assert code == disc_rescue.EXIT_OK
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith(disc_rescue.EVENT_PREFIX)]
    events = [json.loads(line[len(disc_rescue.EVENT_PREFIX) :]) for line in lines]
    assert events[0]["type"] == "start"
    assert events[-1]["type"] == "done"
    assert events[-1]["rescued_bytes"] == 256 * SECTOR_SIZE


def test_helper_cli_reports_missing_css_authentication(monkeypatch, tmp_path: Path, capsys) -> None:
    disc = FakeDisc(256, protected={40})
    monkeypatch.setattr(disc_rescue, "open_optical_device", lambda *_args, **_kwargs: disc)

    code = disc_rescue.main(
        ["--drive", "D:", "--image", str(tmp_path / "i.part"), "--map", str(tmp_path / "i.map.json")]
    )

    assert code == disc_rescue.EXIT_COPY_PROTECTION
    assert '"code":"copy_protection"' in capsys.readouterr().out
    assert (tmp_path / "i.map.json").is_file()


def _directory_record(name: bytes, lba: int, size: int, flags: int) -> bytes:
    length = 33 + len(name) + (1 if len(name) % 2 == 0 else 0)
    record = bytearray(length)
    record[0] = length
    record[2:6] = lba.to_bytes(4, "little")
    record[6:10] = lba.to_bytes(4, "big")
    record[10:14] = size.to_bytes(4, "little")
    record[14:18] = size.to_bytes(4, "big")
    record[25] = flags
    record[32] = len(name)
    record[33 : 33 + len(name)] = name
    return bytes(record)


def _dvd_reader(*, vts_ifo: bytes = b"", vts_title: int = 0, chapters: int = 0):
    """A 8 MB DVD-Video disc: title 2 plays title set 1 (video at sectors 700-900)."""
    total = 4096
    disc = bytearray(total * SECTOR_SIZE)

    def put(sector: int, payload: bytes) -> None:
        disc[sector * SECTOR_SIZE : sector * SECTOR_SIZE + len(payload)] = payload

    descriptor = bytearray(SECTOR_SIZE)
    descriptor[0] = 1
    descriptor[1:6] = b"CD001"
    descriptor[156:190] = _directory_record(b"\x00", 20, SECTOR_SIZE, 2)
    put(16, bytes(descriptor))
    put(17, b"\xffCD001")
    put(
        20,
        _directory_record(b"\x00", 20, SECTOR_SIZE, 2)
        + _directory_record(b"\x01", 20, SECTOR_SIZE, 2)
        + _directory_record(b"VIDEO_TS", 21, SECTOR_SIZE, 2),
    )
    ifo_size = max(SECTOR_SIZE, len(vts_ifo))
    put(
        21,
        _directory_record(b"\x00", 21, SECTOR_SIZE, 2)
        + _directory_record(b"\x01", 20, SECTOR_SIZE, 2)
        + _directory_record(b"VIDEO_TS.IFO;1", 600, 2 * SECTOR_SIZE, 0)
        + _directory_record(b"VTS_01_0.IFO;1", 602, ifo_size, 0)
        + _directory_record(b"VTS_01_0.VOB;1", 610, 40 * SECTOR_SIZE, 0)
        + _directory_record(b"VTS_01_1.VOB;1", 700, 200 * SECTOR_SIZE, 0)
        + _directory_record(b"VTS_02_1.VOB;1", 1000, 100 * SECTOR_SIZE, 0),
    )
    header = bytearray(SECTOR_SIZE)
    header[:12] = b"DVDVIDEO-VMG"
    header[0xC4:0xC8] = (1).to_bytes(4, "big")
    put(600, bytes(header))
    table = bytearray(SECTOR_SIZE)
    table[0:2] = (2).to_bytes(2, "big")
    table[8 + 6] = 2
    table[8 + 12 + 2 : 8 + 12 + 4] = chapters.to_bytes(2, "big")
    table[8 + 12 + 6] = 1
    table[8 + 12 + 7] = vts_title
    put(601, bytes(table))
    if vts_ifo:
        put(602, vts_ifo)

    def read(lba: int, count: int) -> bytes:
        return bytes(disc[lba * SECTOR_SIZE : (lba + count) * SECTOR_SIZE])

    return read, total


def _vts_ifo(cells: list[tuple[int, int]], vobus: list[int]) -> bytes:
    """A title set IFO whose one title plays ``cells`` from one program chain."""
    data = bytearray(4 * SECTOR_SIZE)
    data[:12] = b"DVDVIDEO-VTS"
    data[0xC8:0xCC] = (1).to_bytes(4, "big")
    data[0xCC:0xD0] = (2).to_bytes(4, "big")
    data[0xE4:0xE8] = (3).to_bytes(4, "big")
    ptt = SECTOR_SIZE
    data[ptt : ptt + 2] = (1).to_bytes(2, "big")
    data[ptt + 8 : ptt + 12] = (12).to_bytes(4, "big")
    for chapter in range(2):
        data[ptt + 12 + 4 * chapter : ptt + 14 + 4 * chapter] = (1).to_bytes(2, "big")
    pgci = 2 * SECTOR_SIZE
    data[pgci : pgci + 2] = (1).to_bytes(2, "big")
    data[pgci + 12 : pgci + 16] = (16).to_bytes(4, "big")
    pgc = pgci + 16
    data[pgc + 3] = len(cells)
    data[pgc + 0xE8 : pgc + 0xEA] = (0xEC).to_bytes(2, "big")
    for index, (first, last) in enumerate(cells):
        entry = pgc + 0xEC + 24 * index
        data[entry + 8 : entry + 12] = first.to_bytes(4, "big")
        data[entry + 20 : entry + 24] = last.to_bytes(4, "big")
    admap = 3 * SECTOR_SIZE
    data[admap : admap + 4] = (4 + 4 * len(vobus) - 1).to_bytes(4, "big")
    for index, start in enumerate(vobus):
        data[admap + 4 + 4 * index : admap + 8 + 4 * index] = start.to_bytes(4, "big")
    return bytes(data)


def test_dvd_layout_maps_a_title_to_its_video_sectors() -> None:
    read, total = _dvd_reader()

    layout = read_dvd_layout(read, total, 2)

    def covered(start: int, end: int) -> bool:
        return any(low <= start and end <= high for low, high in layout.priority_ranges)

    def touched(start: int, end: int) -> bool:
        return any(low < end and start < high for low, high in layout.priority_ranges)

    assert layout.verified is True
    assert layout.title_set == 1
    assert covered(700, 900), "the title's video must be prioritized"
    assert covered(600, 603), "IFO structures must be prioritized"
    assert covered(0, 600), "filesystem descriptors before the first file must be prioritized"
    assert not touched(610, 650), "menu video is not needed for the movie"
    assert not touched(1000, 1100), "another title set is not needed"
    assert any(low <= 600 and 603 <= high for low, high in layout.structure_ranges)


def test_dvd_layout_reads_the_movie_cells_and_video_units_from_the_ifo() -> None:
    read, total = _dvd_reader(vts_ifo=_vts_ifo([(0, 49), (120, 199)], [0, 50, 120]), vts_title=1, chapters=2)

    layout = read_dvd_layout(read, total, 2)

    assert layout.verified is True and not layout.note
    assert layout.unit_starts == [700, 750, 820]
    assert layout.movie_sectors == 130
    assert any(low <= 700 and 750 <= high for low, high in layout.priority_ranges)
    assert not any(low < 810 and 760 < high for low, high in layout.priority_ranges), (
        "cells the title does not play are not prioritized"
    )
    assert any(low <= 602 and 606 <= high for low, high in layout.structure_ranges)


def test_emitter_ignores_missing_stdout(monkeypatch) -> None:
    monkeypatch.setattr(disc_rescue.sys, "stdout", None)
    disc_rescue._stdout_emitter()({"type": "progress"})
    buffer = io.StringIO()
    monkeypatch.setattr(disc_rescue.sys, "stdout", buffer)
    disc_rescue._stdout_emitter()({"type": "progress"})
    assert buffer.getvalue().startswith(disc_rescue.EVENT_PREFIX)


class FaultyDisc(FakeDisc):
    """A drive that stops working after some reads, as the real TSSTcorp drive did."""

    def __init__(self, *args, fault_after: int, heals_on_reopen: bool, instant_unreadable: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fault_after = fault_after
        self.heals_on_reopen = heals_on_reopen
        self.instant_unreadable = instant_unreadable
        self.faulted = False
        self.reopens = 0

    def reopen(self) -> None:
        self.reopens += 1
        if self.heals_on_reopen:
            self.faulted = False

    def read(self, lba: int, count: int) -> bytes:
        if self.fault_after and len(self.reads) >= self.fault_after:
            self.faulted = True
            self.fault_after = 0
        if self.faulted:
            self.reads.append((lba, count))
            if self.instant_unreadable:
                raise UnreadableSectorError(f"instant failure at {lba}", sense=(4, 0x3E, 1))
            raise disc_rescue.DriveFaultError("hardware fault (sense 4/3E/01)", sense=(4, 0x3E, 1))
        return super().read(lba, count)


def test_a_drive_fault_pauses_reconnects_and_continues_without_recording_damage(tmp_path: Path) -> None:
    clock = FakeClock()
    disc = FaultyDisc(4096, clock=clock, fault_after=20, heals_on_reopen=True)
    engine, events = make_engine(tmp_path, disc, clock)

    summary = engine.run()

    rescue_map = load_map(tmp_path)
    assert rescue_map.count(FINISHED) == 4096
    assert rescue_map.count(BAD) == 0
    assert summary["drive_faults"] == 1
    assert disc.reopens == 1
    assert any(event["type"] == "waiting" for event in events)
    assert_image_matches(tmp_path / "disc.iso.part", rescue_map)


def test_a_drive_that_stays_broken_stops_and_keeps_untested_blocks_pending(tmp_path: Path) -> None:
    clock = FakeClock()
    disc = FaultyDisc(4096, clock=clock, fault_after=20, heals_on_reopen=False)
    engine, _ = make_engine(tmp_path, disc, clock)

    with pytest.raises(disc_rescue.DriveStoppedResponding):
        engine.run()

    rescue_map = load_map(tmp_path)
    assert rescue_map.count(BAD) == 0
    assert rescue_map.count(FINISHED) == 20 * 32
    assert rescue_map.count(PENDING) == 4096 - 20 * 32


def test_a_burst_of_instant_failures_is_undone_instead_of_recorded_as_damage(tmp_path: Path) -> None:
    clock = FakeClock()
    disc = FaultyDisc(16384, clock=clock, fault_after=40, heals_on_reopen=False, instant_unreadable=True)
    engine, _ = make_engine(tmp_path, disc, clock)

    with pytest.raises(disc_rescue.DriveStoppedResponding):
        engine.run()

    rescue_map = load_map(tmp_path)
    # Nothing a broken drive rejected may count as damage; every block that
    # was not rescued stays queued for a later retry.
    assert rescue_map.count(BAD) == 0
    assert rescue_map.count(FINISHED) == 40 * 32
    assert rescue_map.count(PENDING) == 16384 - 40 * 32


def test_helper_cli_reports_a_drive_that_stopped_responding(monkeypatch, tmp_path: Path, capsys) -> None:
    disc = FaultyDisc(1024, clock=FakeClock(), fault_after=5, heals_on_reopen=False)
    monkeypatch.setattr(disc_rescue, "open_optical_device", lambda *_args, **_kwargs: disc)
    monkeypatch.setattr(disc_rescue.time, "sleep", lambda _seconds: None)

    code = disc_rescue.main(
        ["--drive", "D:", "--image", str(tmp_path / "i.part"), "--map", str(tmp_path / "i.map.json")]
    )

    output = capsys.readouterr().out
    assert code == disc_rescue.EXIT_DRIVE_FAULT
    assert '"code":"drive_not_responding"' in output
    assert "reconnect the drive" in output
    assert RescueMap.load(tmp_path / "i.map.json").count(BAD) == 0


@pytest.mark.parametrize(
    ("sense", "expected"),
    [
        ((4, 0x3E, 1), "DriveFaultError"),
        ((0xB, 0x00, 0), "DriveFaultError"),
        ((3, 0x11, 5), "UnreadableSectorError"),
        ((5, 0x6F, 3), "CopyProtectionError"),
    ],
)
def test_sense_codes_separate_drive_faults_from_disc_damage(sense: tuple[int, int, int], expected: str) -> None:
    from discdock.optical import classify_sense

    assert type(classify_sense(123, sense)).__name__ == expected
