from __future__ import annotations

from pathlib import Path

import pytest

from discdock.files import verify_outputs


@pytest.mark.asyncio
async def test_a_cd_with_a_track_of_a_few_seconds_passes(tmp_path: Path) -> None:
    album = tmp_path / "album"
    album.mkdir()
    # A four-second FLAC track is only a few hundred kilobytes.
    (album / "01 - Intro.flac").write_bytes(b"f" * 300_000)
    (album / "02 - Song.flac").write_bytes(b"f" * 2_000_000)

    verified = await verify_outputs(album, ffprobe_path="")

    assert sorted(path.name for path in verified) == ["01 - Intro.flac", "02 - Song.flac"]


@pytest.mark.asyncio
async def test_an_empty_track_or_a_tiny_movie_still_fails(tmp_path: Path) -> None:
    album = tmp_path / "album"
    album.mkdir()
    (album / "01 - Nothing.flac").write_bytes(b"")
    with pytest.raises(RuntimeError, match="incomplete"):
        await verify_outputs(album, ffprobe_path="")

    movie = tmp_path / "movie"
    movie.mkdir()
    (movie / "title_t00.mkv").write_bytes(b"m" * 300_000)
    with pytest.raises(RuntimeError, match="incomplete"):
        await verify_outputs(movie, ffprobe_path="")
