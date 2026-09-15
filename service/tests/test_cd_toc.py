from __future__ import annotations

from discdock.cd_toc import SESSION_GAP_FRAMES, disc_id, disc_id_from_toc

# Where the tracks of two real CDs start and where their audio ends, from cyanrip's logs,
# with the DiscIDs cyanrip printed for them.
BREVET = [150, 16772, 36820, 53585, 71487, 91582, 116115, 127597, 139117, 156582, 175075, 191587]
BREVET_END = 206985
BREVET_ID = "bevDpSptpSYY3kX7q5H.3Rw8KIY-"
STEMNINGER = [
    150, 19494, 34827, 47155, 70190, 85280, 96967, 107010, 125280, 135687, 150035,
    161352, 180802, 193200, 210447, 228365, 246355, 266480, 280295, 292300, 301892,
]  # fmt: skip
STEMNINGER_END = 342902
STEMNINGER_ID = "NPgsMw_PxxLVnJoP3vhbTeqTIcQ-"


def toc(tracks: list[tuple[int, int]], lead_out: int) -> bytes:
    """A CDROM_TOC as Windows returns it, from (start frame, control flags) per track from track 1."""

    def entry(number: int, control: int, frame: int) -> bytes:
        minutes, rest = divmod(frame, 75 * 60)
        seconds, frames = divmod(rest, 75)
        return bytes([0, 0x10 | control, number, 0, 0, minutes, seconds, frames])

    body = b"".join(entry(number, control, frame) for number, (frame, control) in enumerate(tracks, start=1))
    body += entry(0xAA, 0, lead_out)
    return (len(body) + 2).to_bytes(2, "big") + bytes([1, len(tracks)]) + body


def test_the_disc_id_is_the_one_cyanrip_prints() -> None:
    assert disc_id(1, 12, BREVET_END, BREVET) == BREVET_ID
    assert disc_id(1, 21, STEMNINGER_END, STEMNINGER) == STEMNINGER_ID
    other = [150, 18901, 39738, 59557, 79152, 100126, 124833, 147278, 166336, 182560]
    assert disc_id(1, 10, 206535, other) == "Wn8eRBtfLDfM0qjYPdxrz.Zjs_U-"


def test_two_cds_are_told_apart_by_their_table_of_contents() -> None:
    assert disc_id_from_toc(toc([(frame, 0) for frame in BREVET], BREVET_END)) == BREVET_ID
    assert disc_id_from_toc(toc([(frame, 0) for frame in STEMNINGER], STEMNINGER_END)) == STEMNINGER_ID


def test_the_data_session_of_an_enhanced_cd_is_left_out() -> None:
    data_track = BREVET_END + SESSION_GAP_FRAMES
    enhanced = toc([*((frame, 0) for frame in BREVET), (data_track, 0x04)], data_track + 30000)

    assert disc_id_from_toc(enhanced) == BREVET_ID


def test_a_disc_without_audio_tracks_has_no_disc_id() -> None:
    assert disc_id_from_toc(toc([(150, 0x04)], 30000)) == ""
    assert disc_id_from_toc(b"") == ""
