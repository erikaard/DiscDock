from __future__ import annotations

import json
from pathlib import Path

import pytest

from discdock.bluray_decrypt import EVENT_PREFIX, EXIT_CANNOT_OPEN, EXIT_OK, decrypt_clip, main
from discdock.bluray_folder import ALIGNED_UNIT, SOURCE_PACKET


def _plain_unit(arrival: int, pid: int = 0x1011) -> bytearray:
    unit = bytearray()
    for packet in range(ALIGNED_UNIT // SOURCE_PACKET):
        unit += (arrival + packet).to_bytes(4, "big") + bytes([0x47]) + pid.to_bytes(2, "big") + bytes([0x10])
        unit += bytes([(packet * 7) % 256]) * 184
    return unit


def _encrypted(unit: bytearray) -> bytearray:
    """A stand-in for encryption: copy permission bits set, everything after the first 16 bytes changed."""
    scrambled = bytearray(unit)
    scrambled[0] |= 0xC0
    for index in range(16, len(scrambled)):
        scrambled[index] ^= 0xA5
    return scrambled


def _fake_decrypt(calls: list[int], broken: set[int]):
    def decrypt(unit: bytearray) -> bool:
        arrival = int.from_bytes(unit[0:4], "big") & ((1 << 30) - 1)
        calls.append(arrival)
        if arrival in broken:
            return False
        for index in range(16, len(unit)):
            unit[index] ^= 0xA5
        unit[0] &= 0x3F
        return True

    return decrypt


def test_encrypted_units_are_decrypted_in_place_and_clear_units_are_left(tmp_path: Path) -> None:
    plain = [_plain_unit(1000 * number) for number in range(4)]
    empty = bytearray((5000).to_bytes(4, "big") + bytes([0x47, 0x1F, 0xFF, 0x10]) + b"\xff" * 184) * 32
    clip = tmp_path / "00800.m2ts"
    clip.write_bytes(bytes(_encrypted(plain[0]) + empty + _encrypted(plain[2]) + _encrypted(plain[3]) + b"tail"))
    calls: list[int] = []
    events: list[dict] = []

    counts = decrypt_clip(clip, _fake_decrypt(calls, broken={2000}), events.append)

    data = clip.read_bytes()
    units = [data[index * ALIGNED_UNIT : (index + 1) * ALIGNED_UNIT] for index in range(4)]
    assert counts == {"decrypted": 2, "failed": 1, "clear": 1}
    assert units[0] == plain[0] and units[3] == plain[3]
    assert units[1] == empty, "an empty unit for an unread spot is not handed to the library"
    # A unit the library cannot decrypt becomes an empty unit that keeps its arrival time.
    assert int.from_bytes(units[2][0:4], "big") == 2000
    assert all(units[2][offset + 4 : offset + 7] == b"\x47\x1f\xff" for offset in range(0, ALIGNED_UNIT, SOURCE_PACKET))
    assert data.endswith(b"tail")
    assert calls == [0, 2000, 3000]
    assert events[-1] == {"type": "progress", "done": 4 * ALIGNED_UNIT, "total": 4 * ALIGNED_UNIT, **counts}

    calls.clear()
    again = decrypt_clip(clip, _fake_decrypt(calls, broken=set()))

    assert again == {"decrypted": 0, "failed": 0, "clear": 4}
    assert calls == [], "a clip decrypted before is not decrypted twice"
    assert clip.read_bytes() == data


def test_a_clip_without_encrypted_units_does_not_need_makemkvs_library(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clip = tmp_path / "00800.m2ts"
    clip.write_bytes(bytes(_plain_unit(0) + _plain_unit(1000)))
    arguments = ["--library", str(tmp_path / "missing"), "--disc", str(tmp_path), "--clip", str(clip)]

    assert main(arguments) == EXIT_OK
    events = [json.loads(line[len(EVENT_PREFIX) :]) for line in capsys.readouterr().out.splitlines()]
    assert events[-1] == {"type": "done", "decrypted": 0, "failed": 0, "clear": 2}

    clip.write_bytes(bytes(_encrypted(_plain_unit(0))))

    assert main(arguments) == EXIT_CANNOT_OPEN, "without MakeMKV's library an encrypted clip cannot be decrypted"
    events = [json.loads(line[len(EVENT_PREFIX) :]) for line in capsys.readouterr().out.splitlines()]
    assert events[-1]["type"] == "error"
