"""Retrying unreadable spots at the drive's slowest read speed."""

from __future__ import annotations

from pathlib import Path

from test_disc_rescue import FakeClock, FakeDisc, make_engine

from discdock.media_tools import describe_rescue_event
from discdock.optical import SLOWEST_READ_KBPS, UnreadableSectorError, speed_commands


def test_the_speed_commands_ask_for_a_speed_and_give_the_drive_its_own_back() -> None:
    (streaming, descriptor), (cd_speed, nothing) = speed_commands(2_018_656, SLOWEST_READ_KBPS)

    assert streaming[0] == 0xB6 and streaming[10] == 28 and descriptor is not None and len(descriptor) == 28
    assert int.from_bytes(descriptor[8:12], "big") == 2_018_655, "the whole disc"
    assert int.from_bytes(descriptor[12:16], "big") == 176 and int.from_bytes(descriptor[16:20], "big") == 1000
    assert cd_speed[0] == 0xBB and int.from_bytes(cd_speed[2:4], "big") == 176 and nothing is None

    (_, restore), (cd_full, _) = speed_commands(2_018_656, None)
    assert restore is not None and restore[0] == 0x04, "restore the drive's own defaults"
    assert int.from_bytes(cd_full[2:4], "big") == 0xFFFF, "as fast as the drive likes"


class SpeedDisc(FakeDisc):
    """A drive that reads a marginal block only once it has been slowed down."""

    def __init__(self, *args, accepts: bool = True, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.accepts = accepts
        self.speeds: list[int | None] = []
        self.slow = False

    def set_read_speed(self, kilobytes_per_second: int | None) -> bool:
        self.speeds.append(kilobytes_per_second)
        if self.accepts:
            self.slow = kilobytes_per_second is not None
        return self.accepts

    def read(self, lba: int, count: int) -> bytes:
        if not self.slow and any(sector in self.marginal for sector in range(lba, lba + count)):
            self._fail(lba)
        return super().read(lba, count)


def test_skipped_spots_are_retried_slowly_and_the_drive_is_given_its_speed_back(tmp_path: Path) -> None:
    clock = FakeClock()
    disc = SpeedDisc(4096, clock=clock)
    disc.marginal = set(range(1000, 1016))
    engine, events = make_engine(tmp_path, disc, clock, extra_seconds=600, skip_sectors=64)

    summary = engine.run()

    assert summary["unreadable_bytes"] == 0, "the block read at the slow speed"
    assert disc.speeds == [SLOWEST_READ_KBPS, None], "slowed for the retries, then back to full speed"
    speed_events = [event for event in events if event["type"] == "speed"]
    assert [describe_rescue_event(event) for event in speed_events] == [
        "Retrying at the drive's slowest read speed: a block that will not read at full speed often does slowly",
        "The drive is back at its full read speed",
    ]


def test_a_drive_that_will_not_slow_down_is_retried_as_it_is(tmp_path: Path) -> None:
    clock = FakeClock()
    disc = SpeedDisc(4096, clock=clock, accepts=False)
    disc.marginal = set(range(1000, 1016))
    engine, events = make_engine(tmp_path, disc, clock, extra_seconds=600, skip_sectors=64)

    engine.run()

    assert disc.speeds == [SLOWEST_READ_KBPS], "nothing to restore"
    assert describe_rescue_event(next(event for event in events if event["type"] == "speed")).startswith(
        "The drive does not let its read speed be lowered"
    )


def test_full_speed_comes_back_even_when_the_retry_stops_halfway(tmp_path: Path) -> None:
    clock = FakeClock()
    disc = SpeedDisc(4096, clock=clock)
    disc.marginal = set(range(1000, 1016))
    engine, _ = make_engine(tmp_path, disc, clock, extra_seconds=600, skip_sectors=64)

    def broken() -> None:
        raise UnreadableSectorError("the drive gave up")

    try:
        engine._slowly(broken)
    except UnreadableSectorError:
        pass

    assert disc.speeds == [SLOWEST_READ_KBPS, None]


def test_a_disc_read_in_one_pass_never_slows_the_drive(tmp_path: Path) -> None:
    clock = FakeClock()
    disc = SpeedDisc(4096, clock=clock)
    disc.marginal = set()
    engine, _ = make_engine(tmp_path, disc, clock, extra_seconds=600)

    engine.run()

    assert disc.speeds == []
