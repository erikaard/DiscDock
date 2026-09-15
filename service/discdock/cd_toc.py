"""An audio CD's table of contents, read from the drive, and the MusicBrainz DiscID it gives.

Windows gives every audio CD the volume label "Audio CD", so the label cannot tell two CDs apart.
The DiscID can: it is calculated from where each track starts, the same way cyanrip and MusicBrainz do.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import os
from ctypes import wintypes

IOCTL_CDROM_READ_TOC = 0x00024000
GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
# CDROM_TOC: a 4-byte header, then 8 bytes for each of up to 99 tracks and the lead-out.
TOC_BYTES = 4 + 100 * 8
LEAD_OUT_TRACK = 0xAA
DATA_TRACK = 0x04
# On a CD with a data session after its audio, the audio ends this many frames before the data track.
SESSION_GAP_FRAMES = 11400


def disc_id(first: int, last: int, lead_out: int, offsets: list[int]) -> str:
    """The MusicBrainz DiscID. Offsets are the start frames of tracks first..last, lead-in included."""
    starts = {first + index: offset for index, offset in enumerate(offsets)}
    text = f"{first:02X}{last:02X}{lead_out:08X}"
    text += "".join(f"{starts.get(track, 0):08X}" for track in range(1, 100))
    digest = hashlib.sha1(text.encode("ascii"), usedforsecurity=False).digest()
    return base64.b64encode(digest).decode("ascii").translate(str.maketrans("+/=", "._-"))


def disc_id_from_toc(toc: bytes) -> str:
    """The DiscID for a CDROM_TOC as Windows returns it, or "" when it holds no audio tracks."""
    if len(toc) < 4 or not 1 <= toc[2] <= toc[3] <= 99:
        return ""
    first, last = toc[2], toc[3]
    entries: list[tuple[int, int, int]] = []
    for index in range(last - first + 2):
        entry = toc[4 + index * 8 : 12 + index * 8]
        if len(entry) < 8:
            return ""
        # The control flags are the low half of the second byte; the address is minutes, seconds, frames.
        entries.append((entry[2], entry[1] & 0x0F, (entry[5] * 60 + entry[6]) * 75 + entry[7]))
    *tracks, (lead_out_track, _, lead_out) = entries
    if lead_out_track != LEAD_OUT_TRACK:
        return ""
    audio = [number for number, control, _ in tracks if not control & DATA_TRACK]
    if not audio:
        return ""
    last_audio = max(audio)
    starts = {number: frame for number, _, frame in tracks}
    if last_audio < last:
        # An Enhanced CD: MusicBrainz leaves out the data session and ends the audio before it.
        if last_audio + 1 not in starts:
            return ""
        lead_out = starts[last_audio + 1] - SESSION_GAP_FRAMES
    return disc_id(first, last_audio, lead_out, [starts.get(number, 0) for number in range(first, last_audio + 1)])


def read_disc_id(letter: str) -> str:
    """Read the DiscID of the CD in a drive; "" when there is no audio CD or the drive does not answer."""
    if os.name != "nt" or not letter:
        return ""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.DeviceIoControl.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    kernel32.DeviceIoControl.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.CreateFileW(
        rf"\\.\{letter.rstrip(':').upper()}:",
        GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        0,
        None,
    )
    if handle in (None, INVALID_HANDLE_VALUE):
        return ""
    try:
        toc = ctypes.create_string_buffer(TOC_BYTES)
        returned = wintypes.DWORD()
        if not kernel32.DeviceIoControl(
            handle, IOCTL_CDROM_READ_TOC, None, 0, toc, TOC_BYTES, ctypes.byref(returned), None
        ):
            return ""
    finally:
        kernel32.CloseHandle(handle)
    return disc_id_from_toc(toc.raw[: returned.value])
