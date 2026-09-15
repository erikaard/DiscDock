from __future__ import annotations

from pathlib import Path

import pytest

from discdock.album_tags import flatten_rip_folder, tag_album
from discdock.musicbrainz import AlbumRelease, AlbumTrack
from discdock.processes import ProcessResult

RELEASE = AlbumRelease(
    id="403427d8-6201-4831-a346-f7d910eead70",
    title="Into the Great Wide Open",
    artist="Tom Petty and the Heartbreakers",
    date="1991-07-02",
    country="XE",
    label="MCA",
    track_count=2,
    tracks=[
        AlbumTrack(position=1, title="Learning to Fly", track_id="track-1", recording_id="recording-1"),
        AlbumTrack(position=2, title="Kings Highway", track_id="track-2", recording_id="recording-2"),
    ],
)


class FFmpegRunner:
    """Copies the input track to the output, like FFmpeg with -c copy; can refuse to embed a cover."""

    def __init__(self, *, refuse_cover: bool = False) -> None:
        self.refuse_cover = refuse_cover
        self.calls: list[list[str]] = []

    async def run(self, owner: str, args: list[str], **kwargs) -> ProcessResult:
        del owner, kwargs
        self.calls.append(args)
        if self.refuse_cover and "attached_pic" in args:
            return ProcessResult(args=args, return_code=1)
        Path(args[-1]).write_bytes(Path(args[args.index("-i") + 1]).read_bytes() + b" tagged")
        return ProcessResult(args=args, return_code=0)


def ripped_cd(tmp_path: Path) -> tuple[Path, Path]:
    folder = tmp_path / "album.partial"
    rip = folder / "Unknown disc [FLAC]"
    rip.mkdir(parents=True)
    for number in (1, 2):
        (rip / f"0{number} - Unknown track.flac").write_bytes(b"audio")
    (rip / "Unknown disc.cue").write_text(
        'FILE "01 - Unknown track.flac" WAVE\nFILE "02 - Unknown track.flac" WAVE\n', encoding="utf-8"
    )
    (rip / "Unknown disc.log").write_text("AccurateRip: accurate", encoding="utf-8")
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_bytes(b"")
    return folder, ffmpeg


@pytest.mark.asyncio
async def test_the_tracks_get_the_names_and_tags_of_the_chosen_release(tmp_path: Path) -> None:
    folder, ffmpeg = ripped_cd(tmp_path)
    runner = FFmpegRunner()

    tagged = await tag_album(runner, "job-id", str(ffmpeg), folder, RELEASE, b"jpeg")

    assert tagged == 2
    assert sorted(path.name for path in folder.iterdir()) == [
        "01 - Learning to Fly.flac",
        "02 - Kings Highway.flac",
        "Unknown disc.cue",
        "Unknown disc.log",
        "cover.jpg",
    ]
    assert (folder / "01 - Learning to Fly.flac").read_bytes() == b"audio tagged"
    assert 'FILE "02 - Kings Highway.flac"' in (folder / "Unknown disc.cue").read_text(encoding="utf-8")
    first = runner.calls[0]
    for tag in ("title=Learning to Fly", "album=Into the Great Wide Open", "track=1", "TRACKTOTAL=2", "date=1991-07-02"):
        assert tag in first
    assert f"MUSICBRAINZ_ALBUMID={RELEASE.id}" in first
    assert "attached_pic" in first
    assert first[first.index("-c") + 1] == "copy", "the audio is never encoded again"


@pytest.mark.asyncio
async def test_a_cover_ffmpeg_cannot_embed_does_not_cost_the_names(tmp_path: Path) -> None:
    folder, ffmpeg = ripped_cd(tmp_path)
    runner = FFmpegRunner(refuse_cover=True)

    assert await tag_album(runner, "job-id", str(ffmpeg), folder, RELEASE, b"not really a picture") == 2
    assert len(runner.calls) == 4
    assert (folder / "02 - Kings Highway.flac").is_file()


@pytest.mark.asyncio
async def test_a_png_cover_photo_is_kept_as_png(tmp_path: Path) -> None:
    folder, ffmpeg = ripped_cd(tmp_path)

    await tag_album(FFmpegRunner(), "job-id", str(ffmpeg), folder, RELEASE, b"\x89PNG\r\n\x1a\n picture")

    assert (folder / "cover.png").is_file()
    assert not (folder / "cover.jpg").exists()


@pytest.mark.asyncio
async def test_tags_need_ffmpeg(tmp_path: Path) -> None:
    folder, _ = ripped_cd(tmp_path)

    with pytest.raises(FileNotFoundError):
        await tag_album(FFmpegRunner(), "job-id", str(tmp_path / "missing.exe"), folder, RELEASE, None)


def test_a_folder_with_other_files_is_left_as_it_is(tmp_path: Path) -> None:
    (tmp_path / "Unknown disc [FLAC]").mkdir()
    (tmp_path / "notes.txt").write_text("", encoding="utf-8")

    flatten_rip_folder(tmp_path)

    assert (tmp_path / "Unknown disc [FLAC]").is_dir()
