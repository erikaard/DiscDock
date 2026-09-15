from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from discdock import bluray_copy
from discdock.bluray_copy import (
    BlurayMovieCopy,
    build_key_folder,
    chapter_metadata,
    concat_listing,
    copy_arguments,
    find_path,
    libmmbd_library,
    movie_decryption_started,
)
from discdock.bluray_decrypt import EVENT_PREFIX as DECRYPT_EVENT_PREFIX
from discdock.optical import PlaylistStream, mpls_chapters, mpls_clips, mpls_play_items, mpls_streams
from discdock.processes import ProcessFailure, ProcessResult

TWO_CLIPS = (("00800", 10, 100), ("00801", 0, 50))


def _stream(pid: int, attributes: bytes, stream_type: int = 1) -> bytes:
    entry = (bytes([stream_type, 0, 0]) if stream_type == 2 else bytes([stream_type])) + pid.to_bytes(2, "big")
    entry = entry.ljust(9, b"\x00")
    return bytes([len(entry)]) + entry + bytes([len(attributes)]) + attributes


def _playlist(clips: tuple[tuple[str, float, float], ...] = TWO_CLIPS) -> bytes:
    """Play items with a stream table, and chapter marks in the first two."""
    streams = (
        _stream(0x1011, bytes([0x1B, 0x61, 0, 0, 0]))
        + _stream(0x1100, bytes([0x86, 0x61]) + b"eng")
        + _stream(0x1A00, bytes([0xA1, 0x31]) + b"eng", stream_type=2)
        + _stream(0x1101, bytes([0x81, 0x31]) + b"ger")
        + _stream(0x1200, bytes([0x90]) + b"fin\x00")
    )
    table = (14 + len(streams)).to_bytes(2, "big") + bytes(2) + bytes([1, 3, 1, 0, 0, 0, 0]) + bytes(5) + streams

    def item(name: str, start: float, end: float) -> bytes:
        body = (
            name.encode()
            + b"M2TS"
            + bytes(3)
            + round(start * 45000).to_bytes(4, "big")
            + round(end * 45000).to_bytes(4, "big")
            + bytes(12)
            + table
        )
        return len(body).to_bytes(2, "big") + body

    items = b"".join(item(*clip) for clip in clips)
    playlist = (len(items) + 6).to_bytes(4, "big") + bytes(2) + len(clips).to_bytes(2, "big") + bytes(2) + items

    def mark(kind: int, play_item: int, seconds: float) -> bytes:
        return bytes([0, kind]) + play_item.to_bytes(2, "big") + round(seconds * 45000).to_bytes(4, "big") + b"\xff\xff" + bytes(4)

    body = mark(1, 0, 10) + mark(1, 0, 40) + mark(2, 0, 70) + mark(1, 1, 20)
    marks = (len(body) + 2).to_bytes(4, "big") + (4).to_bytes(2, "big") + body
    header = b"MPLS0200" + (40).to_bytes(4, "big") + (40 + len(playlist)).to_bytes(4, "big") + bytes(24)
    return header + playlist + marks


def _unit(copy_permission: int, pid: int) -> bytes:
    unit = bytearray()
    for packet in range(32):
        unit += (packet * 100).to_bytes(4, "big") + bytes([0x47]) + pid.to_bytes(2, "big") + bytes([0x10]) + bytes(184)
    unit[0] |= copy_permission << 6
    return bytes(unit)


def _movie_folder(tmp_path: Path, playlist: bytes, clip: bytes) -> Path:
    folder = tmp_path / "makemkv-backup"
    (folder / "BDMV" / "PLAYLIST").mkdir(parents=True)
    (folder / "BDMV" / "PLAYLIST" / "00800.mpls").write_bytes(playlist)
    (folder / "BDMV" / "STREAM").mkdir(parents=True)
    (folder / "BDMV" / "STREAM" / "00800.m2ts").write_bytes(clip)
    return folder


def test_a_playlist_lists_its_main_streams_with_languages_and_its_chapters() -> None:
    data = _playlist()

    assert mpls_play_items(data) == [("00800", 10.0, 100.0), ("00801", 0.0, 50.0)]
    assert mpls_clips(data) == (["00800", "00801"], 140.0)
    assert mpls_streams(data) == [
        PlaylistStream(0x1011, "video"),
        PlaylistStream(0x1100, "audio", "eng"),
        PlaylistStream(0x1101, "audio", "ger"),
        PlaylistStream(0x1200, "subtitle", "fin"),
    ], "a stream from a sub-clip is not part of the movie's clip"
    # Entry marks only, counted from the start of the playlist across play items.
    assert mpls_chapters(data) == [0.0, 30.0, 110.0]


def test_ffmpeg_copies_the_playlists_streams_by_pid_with_languages_and_chapters(tmp_path: Path) -> None:
    streams = [
        PlaylistStream(0x1011, "video"),
        PlaylistStream(0x1100, "audio", "eng"),
        PlaylistStream(0x1200, "subtitle", "fin"),
    ]

    args = copy_arguments("ffmpeg.exe", ["-i", "clip.m2ts"], tmp_path / "movie.mkv", streams, tmp_path / "chapters.txt")

    assert [args[index + 1] for index, value in enumerate(args) if value == "-map"] == ["0:i:4113", "0:i:4352", "0:i:4608"]
    assert args[args.index("-metadata:s:1") + 1] == "language=eng"
    assert args[args.index("-metadata:s:2") + 1] == "language=fin"
    assert "-metadata:s:0" not in args
    assert args[args.index("-map_chapters") + 1] == "1"
    assert args[args.index("-c") + 1] == "copy"
    assert "+discardcorrupt+genpts" in args, "damaged packets are skipped instead of ending the copy"
    assert args[args.index("-bsf") + 1] == r"noise=drop=eq(pts\,nopts)", "packets without a time stamp cannot go into MKV"
    assert args[-1] == str(tmp_path / "movie.mkv")


def test_chapters_end_where_the_next_one_starts() -> None:
    text = chapter_metadata([0.0, 30.0, 110.0], 140.0)

    assert text.startswith(";FFMETADATA1\n")
    assert "START=30000\nEND=110000\ntitle=Chapter 02" in text
    assert text.rstrip().endswith("START=110000\nEND=140000\ntitle=Chapter 03")


def test_several_clips_are_joined_from_their_in_to_their_out_times(tmp_path: Path) -> None:
    text = concat_listing([(tmp_path / "00800.m2ts", 10.0, 100.0), (tmp_path / "it's.m2ts", 0.0, 50.5)])

    assert text.splitlines() == [
        "ffconcat version 1.0",
        f"file '{tmp_path}{os.sep}00800.m2ts'",
        "inpoint 10.000000",
        "outpoint 100.000000",
        f"file '{tmp_path}{os.sep}it'\\''s.m2ts'",
        "inpoint 0.000000",
        "outpoint 50.500000",
    ]


def test_decrypted_files_are_found_whatever_their_spelling(tmp_path: Path) -> None:
    (tmp_path / "BDMV" / "STREAM").mkdir(parents=True)
    (tmp_path / "BDMV" / "STREAM" / "00800.m2ts").write_bytes(b"clip")

    assert find_path(tmp_path, "bdmv", "STREAM", "00800.M2TS") == tmp_path / "BDMV" / "STREAM" / "00800.m2ts"
    assert find_path(tmp_path, "BDMV", "PLAYLIST", "00800.mpls") is None


def test_makemkvs_library_for_players_is_found_next_to_makemkv(tmp_path: Path) -> None:
    makemkv = tmp_path / "makemkvcon64.exe"
    makemkv.write_bytes(b"exe")

    assert libmmbd_library(str(makemkv)) == ""
    (tmp_path / "libmmbd64.dll").write_bytes(b"dll")
    assert libmmbd_library(str(makemkv)) == str(tmp_path / "libmmbd64")
    assert libmmbd_library("") == ""


@pytest.mark.parametrize(
    ("first_unit", "started"),
    [
        (_unit(3, 0x1011), False),
        (_unit(0, 0x1011), True),
        (_unit(0, 0x1FFF), False),
        (b"", False),
    ],
    ids=["encrypted", "decrypted", "empty unit for an unread spot", "no clip data"],
)
def test_a_movie_decrypted_by_an_earlier_attempt_is_recognised(tmp_path: Path, first_unit: bytes, started: bool) -> None:
    folder = _movie_folder(tmp_path, _playlist(), first_unit)

    assert movie_decryption_started(folder, "00800.mpls") is started


def test_the_key_folder_has_the_disc_files_and_only_placeholders_for_the_movies_clips(tmp_path: Path) -> None:
    folder = tmp_path / "makemkv-backup"
    files = {
        "discatt.dat": b"attributes",
        "AACS/Unit_Key_RO.inf": b"keys",
        "AACS/ContentHash000.tbl": b"hashes",
        "AACS/DUPLICATE/ContentHash000.tbl": b"hashes",
        "BDMV/index.bdmv": b"index",
        "BDMV/PLAYLIST/00800.mpls": _playlist(),
        "BDMV/CLIPINF/00002.clpi": b"clip info",
        "BDMV/STREAM/00800.m2ts": b"movie" * 1000,
        "BDMV/STREAM/00801.m2ts": b"movie part two",
        "BDMV/STREAM/00002.m2ts": b"extra",
    }
    for name, content in files.items():
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(content)
    keys = tmp_path / "makemkv-keys"

    linked = build_key_folder(folder, keys, "00800.mpls")

    present = sorted(path.relative_to(keys).as_posix() for path in keys.rglob("*") if path.is_file())
    assert present == [
        "AACS/Unit_Key_RO.inf",
        "BDMV/CLIPINF/00002.clpi",
        "BDMV/PLAYLIST/00800.mpls",
        "BDMV/STREAM/00800.m2ts",
        "BDMV/STREAM/00801.m2ts",
        "BDMV/index.bdmv",
        "discatt.dat",
    ]
    assert linked == 5
    assert os.path.samefile(keys / "discatt.dat", folder / "discatt.dat"), "a link, not a copy"
    placeholder = keys / "BDMV" / "STREAM" / "00800.m2ts"
    # MakeMKV's library locks the clips it opens, so it gets an empty file of the same size instead.
    assert not os.path.samefile(placeholder, folder / "BDMV" / "STREAM" / "00800.m2ts")
    assert placeholder.read_bytes() == bytes(5000)
    assert build_key_folder(folder, keys, "00800.mpls") == 5, "leftovers of an earlier attempt are reused"


@pytest.mark.asyncio
async def test_the_movie_is_decrypted_in_place_by_makemkvs_library_and_copied_from_the_plain_clip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _movie_folder(tmp_path, _playlist((("00800", 10, 100),)), bytes(2000))
    keys = tmp_path / "makemkv-keys"
    clip = folder / "BDMV" / "STREAM" / "00800.m2ts"
    ffmpeg, ffprobe = tmp_path / "ffmpeg.exe", tmp_path / "ffprobe.exe"
    ffmpeg.write_bytes(b"exe")
    ffprobe.write_bytes(b"exe")
    library = tmp_path / "MakeMKV" / "libmmbd64"
    library.parent.mkdir()
    Path(f"{library}.dll").write_bytes(b"dll")

    def fake_capture(args: list[str], *, timeout: float, owner: str = "", env: dict[str, str] | None = None):
        if "stream=id" in args:
            # The clip has no German audio, although the playlist lists it.
            return 0, json.dumps({"streams": [{"id": "0x1011"}, {"id": "0x1100"}, {"id": "0x1200"}]}), ""
        return 0, json.dumps({"format": {"duration": "89.5"}}), ""

    monkeypatch.setattr(bluray_copy, "run_capture", fake_capture)
    events: list[dict] = []

    class Runner:
        def __init__(self) -> None:
            self.commands: list[tuple[list[str], dict[str, str] | None]] = []

        async def run(self, owner_id, args, *, timeout, no_output_timeout, on_line, cwd=None, env=None):
            self.commands.append((args, env))
            if "--clip" in args:
                for event in ({"type": "progress", "done": 1000, "total": 2000}, {"type": "done", "decrypted": 1}):
                    await on_line(DECRYPT_EVENT_PREFIX + json.dumps(event))
                return ProcessResult(args=args, return_code=0)
            Path(args[-1]).write_bytes(b"movie")
            for line in ("out_time_us=N/A", "total_size=1000", "progress=end"):
                await on_line(line)
            return ProcessResult(args=args, return_code=0)

    runner = Runner()

    target = await BlurayMovieCopy(str(ffmpeg), str(ffprobe), runner, str(library)).copy(  # type: ignore[arg-type]
        "job", folder, keys, "00800.mpls", tmp_path / "out", 3600, events.append
    )

    assert target == tmp_path / "out" / "title_t00.mkv" and target.read_bytes() == b"movie"
    (helper, _), (copy, env) = runner.commands
    assert "discdock.bluray_decrypt" in helper or "--bluray-decrypt" in helper
    assert helper[helper.index("--library") + 1] == str(library), "MakeMKV's library decrypts"
    assert helper[helper.index("--disc") + 1] == str(keys), "the library opens the key folder"
    assert helper[helper.index("--clip") + 1] == str(clip), "the rescued clip itself is decrypted"
    assert copy[copy.index("-i") + 1] == str(clip), "FFmpeg reads the plain decrypted clip"
    assert env is None and not any(argument.startswith("bluray:") for argument in copy)
    maps = [copy[index + 1] for index, value in enumerate(copy) if value == "-map"]
    assert maps == ["0:i:4113", "0:i:4352", "0:i:4608"], "a playlist stream missing from the clip is left out"
    # Decrypting counts for the first 60 %; FFmpeg reports no time yet, but the bytes written show progress.
    assert [event["percent"] for event in events] == [30.0, 80.0]
    assert not (tmp_path / "out" / "chapters.ffmetadata").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(("reached", "finishes"), [([1000, 3000, 3000], False), ([1000, 8000], True)])
async def test_decrypting_starts_again_while_the_helper_gets_further(
    tmp_path: Path, reached: list[int], finishes: bool
) -> None:
    runs: list[list[str]] = []

    class Runner:
        async def run(self, owner_id, args, *, cwd, timeout, no_output_timeout, on_line):
            runs.append(args)
            done = reached[len(runs) - 1]
            await on_line(DECRYPT_EVENT_PREFIX + json.dumps({"type": "progress", "done": done, "total": 8000}))
            if done == 8000:
                await on_line(DECRYPT_EVENT_PREFIX + json.dumps({"type": "done", "decrypted": 10}))
                return ProcessResult(args=args, return_code=0)
            # A fault inside MakeMKV's library ends the helper.
            return ProcessResult(args=args, return_code=0xC0000374)

    copier = BlurayMovieCopy("ffmpeg.exe", "ffprobe.exe", Runner(), "libmmbd64")  # type: ignore[arg-type]

    if finishes:
        assert await copier.decrypt("job", tmp_path, [tmp_path / "00800.m2ts"], 3600) == {"type": "done", "decrypted": 10}
    else:
        with pytest.raises(ProcessFailure, match="keeps stopping"):
            await copier.decrypt("job", tmp_path, [tmp_path / "00800.m2ts"], 3600)
    assert len(runs) == len(reached), "a helper that gets no further is not started again"
