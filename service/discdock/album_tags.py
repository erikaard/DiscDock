"""Names and tags for the tracks of a ripped audio CD, from the release chosen on MusicBrainz."""

from __future__ import annotations

import os
import re
from pathlib import Path

from .files import safe_component
from .musicbrainz import AlbumRelease, AlbumTrack
from .processes import ProcessFailure, ProcessRunner

# cyanrip names the tracks "01 - Unknown track", and "1.01 - Unknown track" on a disc of a set.
TRACK_NUMBER = re.compile(r"^(?:\d+\.)?(\d+)")
TAGGING_SUFFIX = ".tagging.flac"


def flatten_rip_folder(folder: Path) -> None:
    """Move the tracks out of the folder cyanrip rips into ("Unknown disc [FLAC]") into the album folder."""
    if not folder.is_dir():
        return
    entries = list(folder.iterdir())
    if len(entries) != 1 or not entries[0].is_dir():
        return
    inner = entries[0]
    for entry in list(inner.iterdir()):
        os.replace(entry, folder / entry.name)
    inner.rmdir()


def ripped_tracks(folder: Path) -> dict[int, Path]:
    """The ripped FLAC files by track number."""
    tracks: dict[int, Path] = {}
    for path in sorted(folder.glob("*.flac")):
        match = TRACK_NUMBER.match(path.stem)
        if match and not path.name.endswith(TAGGING_SUFFIX):
            tracks.setdefault(int(match.group(1)), path)
    return tracks


def tag_arguments(
    ffmpeg: str, source: Path, target: Path, release: AlbumRelease, track: AlbumTrack, cover: Path | None
) -> list[str]:
    arguments = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", str(source)]
    if cover is not None:
        arguments += ["-i", str(cover)]
    arguments += ["-map", "0:a"]
    if cover is not None:
        arguments += ["-map", "1:v:0", "-disposition:v:0", "attached_pic", "-metadata:s:v:0", "comment=Cover (front)"]
    # The audio is copied as it is; cyanrip's own tags stay and these replace its placeholders.
    arguments += ["-c", "copy"]
    tags = {
        "title": track.title,
        "artist": track.artist or release.artist,
        "album": release.title,
        "album_artist": release.artist,
        "date": release.date,
        "track": str(track.position),
        "TRACKTOTAL": str(release.track_count or len(release.tracks)),
        "disc": str(release.disc_number),
        "DISCTOTAL": str(release.disc_count),
        "LABEL": release.label,
        "RELEASECOUNTRY": release.country,
        "MUSICBRAINZ_ALBUMID": release.id,
        "MUSICBRAINZ_ALBUMARTISTID": release.artist_id,
        "MUSICBRAINZ_RELEASETRACKID": track.track_id,
        "MUSICBRAINZ_TRACKID": track.recording_id,
    }
    for key, value in tags.items():
        if value:
            arguments += ["-metadata", f"{key}={value}"]
    return [*arguments, str(target)]


def _rename_in_cue_sheets(folder: Path, renamed: dict[str, str]) -> None:
    if not renamed:
        return
    for sheet in folder.glob("*.cue"):
        try:
            text = sheet.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for old, new in renamed.items():
            text = text.replace(f'FILE "{old}"', f'FILE "{new}"')
        sheet.write_text(text, encoding="utf-8")


async def tag_album(
    runner: ProcessRunner, owner: str, ffmpeg: str, folder: Path, release: AlbumRelease, cover: bytes | None
) -> int:
    """Tag and name the ripped tracks after the release; returns how many tracks were tagged."""
    if not ffmpeg or not Path(ffmpeg).is_file():
        raise FileNotFoundError("FFmpeg is needed to write the album tags")
    flatten_rip_folder(folder)
    picture: Path | None = None
    if cover:
        picture = folder / ("cover.png" if cover.startswith(b"\x89PNG") else "cover.jpg")
        picture.write_bytes(cover)
    positions = {track.position: track for track in release.tracks}
    renamed: dict[str, str] = {}
    tagged = 0
    for number, path in sorted(ripped_tracks(folder).items()):
        track = positions.get(number)
        if track is None:
            continue
        temporary = path.with_name(path.stem + TAGGING_SUFFIX)
        # A cover FFmpeg cannot embed must not cost the track its names.
        for embedded in dict.fromkeys((picture, None)):
            result = await runner.run(
                owner,
                tag_arguments(ffmpeg, path, temporary, release, track, embedded),
                timeout=300,
                no_output_timeout=120,
            )
            if result.return_code == 0 and temporary.is_file() and temporary.stat().st_size > 0:
                break
            temporary.unlink(missing_ok=True)
        else:
            raise ProcessFailure(f"FFmpeg could not write the tags of track {number}", result)
        os.replace(temporary, path)
        name = f"{number:02d} - {safe_component(track.title, f'Track {number:02d}')}.flac"
        if name != path.name and not (folder / name).exists():
            os.replace(path, folder / name)
            renamed[path.name] = name
        tagged += 1
    _rename_in_cue_sheets(folder, renamed)
    return tagged
