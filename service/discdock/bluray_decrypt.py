"""Decrypt a rescued Blu-ray movie clip in place with MakeMKV's library for Blu-ray players (libmmbd).

FFmpeg's Blu-ray reader can use the same library, but after a stretch of dense
damage the reader crashes. Asking the library for one aligned unit at a time
works, and FFmpeg copies a plain decrypted clip past the damage. MakeMKV does
the decrypting; this helper hands it the units and writes them back. It runs in
its own process, so a fault in the library cannot take DiscDock down, and it can
simply be started again: units already decrypted are clear and are skipped.
"""

from __future__ import annotations

import argparse
import ctypes
import json
from collections.abc import Callable
from pathlib import Path

from .bluray_folder import ALIGNED_UNIT, NULL_PACKET, SOURCE_PACKET

EVENT_PREFIX = "DECRYPT "
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_CANNOT_OPEN = 3
BATCH_UNITS = 4096
_PACKETS = ALIGNED_UNIT // SOURCE_PACKET
_ARRIVAL_MASK = (1 << 30) - 1
_SYNC_BYTES = b"\x47" * _PACKETS


def unit_is_valid(unit: bytes | bytearray) -> bool:
    """Whether all 32 packets of an aligned unit start with a sync byte."""
    return bytes(unit[4::SOURCE_PACKET]) == _SYNC_BYTES


def decrypt_clip(
    path: Path,
    decrypt: Callable[[bytearray], bool],
    report: Callable[[dict], None] | None = None,
) -> dict[str, int]:
    """Decrypt the encrypted aligned units of ``path`` in place.

    ``decrypt`` changes one unit in place and says whether that worked. A unit
    that does not come out as valid packets becomes an empty unit, so FFmpeg sees
    a short gap instead of garbage. Clear units, including ones decrypted before,
    are left alone. Returns how many units were decrypted, failed or already clear.
    """
    counts = {"decrypted": 0, "failed": 0, "clear": 0}
    total = path.stat().st_size
    whole = total - total % ALIGNED_UNIT
    with path.open("r+b") as clip:
        position = 0
        while position < whole:
            clip.seek(position)
            block = bytearray(clip.read(min(BATCH_UNITS * ALIGNED_UNIT, whole - position)))
            if len(block) < ALIGNED_UNIT:
                break
            changed = False
            for offset in range(0, len(block) - ALIGNED_UNIT + 1, ALIGNED_UNIT):
                # The copy permission bits of the first packet say whether the unit is encrypted.
                if block[offset] >> 6 == 0:
                    counts["clear"] += 1
                    continue
                unit = block[offset : offset + ALIGNED_UNIT]
                if decrypt(unit) and unit_is_valid(unit):
                    unit[0] &= 0x3F
                    counts["decrypted"] += 1
                else:
                    arrival = int.from_bytes(block[offset : offset + 4], "big") & _ARRIVAL_MASK
                    unit = bytearray((arrival.to_bytes(4, "big") + NULL_PACKET) * _PACKETS)
                    counts["failed"] += 1
                block[offset : offset + ALIGNED_UNIT] = unit
                changed = True
            if changed:
                clip.seek(position)
                clip.write(block)
                clip.flush()
            position += len(block) - len(block) % ALIGNED_UNIT
            if report is not None:
                report({"type": "progress", "done": position, "total": whole, **counts})
    return counts


class LibmmbdDecryptor:
    """MakeMKV's library opened on a Blu-ray folder: decrypts aligned units in place."""

    def __init__(self, library: str, disc: Path):
        self._library = ctypes.CDLL(library if library.lower().endswith(".dll") else f"{library}.dll")
        self._library.aacs_open2.restype = ctypes.c_void_p
        self._library.aacs_open2.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_int)]
        self._library.aacs_decrypt_unit.restype = ctypes.c_int
        self._library.aacs_decrypt_unit.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self._library.aacs_close.argtypes = [ctypes.c_void_p]
        error = ctypes.c_int(0)
        self._handle = self._library.aacs_open2(str(disc).encode("utf-8"), None, ctypes.byref(error))
        if not self._handle:
            raise OSError(f"MakeMKV's library could not open the rescued disc files (error {error.value})")

    def __call__(self, unit: bytearray) -> bool:
        view = (ctypes.c_char * ALIGNED_UNIT).from_buffer(unit)
        return self._library.aacs_decrypt_unit(self._handle, view) == 1

    def close(self) -> None:
        if self._handle:
            self._library.aacs_close(self._handle)
            self._handle = None


def _emit(event: dict) -> None:
    print(EVENT_PREFIX + json.dumps(event), flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="discdock-bluray-decrypt")
    parser.add_argument("--library", required=True, help="libmmbd, with or without .dll")
    parser.add_argument("--disc", required=True, help="the folder with AACS, BDMV and discatt.dat")
    parser.add_argument("--clip", action="append", required=True, help="a movie clip to decrypt in place")
    try:
        arguments = parser.parse_args(argv)
    except SystemExit:
        return EXIT_USAGE
    clips = [Path(clip) for clip in arguments.clip]
    grand_total = sum(clip.stat().st_size - clip.stat().st_size % ALIGNED_UNIT for clip in clips)
    totals = {"decrypted": 0, "failed": 0, "clear": 0}
    decryptor: LibmmbdDecryptor | None = None

    def decrypt(unit: bytearray) -> bool:
        nonlocal decryptor
        # A clip without encrypted units never needs MakeMKV's library.
        if decryptor is None:
            decryptor = LibmmbdDecryptor(arguments.library, Path(arguments.disc))
        return decryptor(unit)

    try:
        completed = 0
        for clip in clips:

            def report(event: dict, before: int = completed) -> None:
                _emit({**event, "done": before + event["done"], "total": grand_total})

            counts = decrypt_clip(clip, decrypt, report)
            completed += clip.stat().st_size - clip.stat().st_size % ALIGNED_UNIT
            for key in totals:
                totals[key] += counts[key]
    except OSError as error:
        _emit({"type": "error", "message": str(error)})
        return EXIT_CANNOT_OPEN
    finally:
        if decryptor is not None:
            decryptor.close()
    _emit({"type": "done", **totals})
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
