from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from discdock.damage_screens import (
    MoviePatcher,
    SpliceUnavailable,
    build_chapters,
    copy_start_times,
    drop_expression,
    fit_windows,
    format_timestamp,
    is_unrepaired_copy,
    loading_screen_filter,
    plan_splice,
    splice_encoder_args,
    starts_within_half_a_frame,
    unrepaired_copy_path,
    window_pieces,
)
from discdock.files import rename_video_outputs
from discdock.models import MediaKind
from discdock.processes import ProcessResult
from discdock.settings import AppSettings


def test_timestamps_round_up_so_skipping_lands_after_the_damage() -> None:
    assert format_timestamp(1857.2) == "00:30:58"
    assert format_timestamp(3600) == "01:00:00"
    assert format_timestamp(0) == "00:00:00"


def test_the_loading_screen_names_the_resume_time_and_keeps_the_picture_shape() -> None:
    graph = loading_screen_filter(
        width=720,
        height=576,
        sar=64 / 45,
        sar_text="64:45",
        duration=106.6,
        resume_at=1857.8,
        disc="DVD",
        regular_font="C:/Windows/Fonts/segoeui.ttf",
        bold_font="C:/Windows/Fonts/segoeuib.ttf",
    )

    assert "The DVD is scratched here" in graph
    assert r"Skip to 00\:30\:58" in graph, "colons in drawn text must be escaped for FFmpeg"
    assert r"fontfile='C\:/Windows/Fonts/segoeuib.ttf'" in graph
    assert graph.count("alpha=") == 8, "an eight-dot spinner"
    assert "max(0,106.600-t)" in graph, "the countdown runs to the end of the damage"
    assert graph.endswith("scale=720:576,setsar=64/45,format=yuv420p")


def test_chapters_mark_each_loading_screen_and_keep_the_disc_chapters() -> None:
    existing = [
        {"start_time": "0.000000", "tags": {"title": "Chapter 01"}},
        {"start_time": "8.400000", "tags": {"title": "Chapter 02"}},
        {"start_time": "12.000000", "tags": {"title": "Chapter 03"}},
    ]

    text = build_chapters(existing, [{"start_seconds": 4.0, "end_seconds": 8.0}], 20.0)

    lines = text.splitlines()
    titles = [line.removeprefix("title=") for line in lines if line.startswith("title=")]
    starts = [int(line.removeprefix("START=")) for line in lines if line.startswith("START=")]
    ends = [int(line.removeprefix("END=")) for line in lines if line.startswith("END=")]
    assert lines[0] == ";FFMETADATA1"
    assert titles == [
        "Chapter 01",
        "Disc damage - the movie continues at 00:00:08",
        "Movie continues",
        "Chapter 03",
    ], "a disc chapter right at the edge of a loading screen is not repeated"
    assert starts == [0, 4000, 8000, 12000]
    assert ends == [4000, 8000, 12000, 20000]


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def run(self, owner: str, args: list[str], **kwargs: Any) -> ProcessResult:
        del owner, kwargs
        self.calls.append(args)
        Path(args[-1]).write_bytes(b"\0" * (2 * 1024 * 1024))
        return ProcessResult(args=args, return_code=0)


@pytest.mark.asyncio
async def test_only_damage_of_two_seconds_or_more_gets_a_loading_screen(tmp_path: Path) -> None:
    runner = RecordingRunner()
    patcher = MoviePatcher("ffmpeg.exe", "", runner)  # type: ignore[arg-type]
    info = {
        "width": 720,
        "height": 576,
        "fps": 25.0,
        "fps_text": "25/1",
        "sar": 64 / 45,
        "sar_text": "64:45",
        "field_order": "tt",
        "duration_seconds": 20.0,
    }
    moments = [{"start_seconds": 4.0, "end_seconds": 8.0}, {"start_seconds": 15.0, "end_seconds": 15.8}]
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"movie")

    output, method = await patcher.add_loading_screens(
        "job", source, tmp_path / "work", moments, info=info, disc="DVD"
    )

    assert method == "reencode", "without FFprobe the movie cannot be cut at its keyframes"

    renders = [call for call in runner.calls if "lavfi" in call]
    assert len(renders) == 1
    assert renders[0][renders[0].index("-frames:v") + 1] == "100", "exactly the frames of the four-second hole"
    stitch = runner.calls[-1]
    graph = stitch[stitch.index("-filter_complex") + 1]
    assert "trim=start=0.000000:end=4.000000" in graph
    assert "trim=start=8.000000," in graph
    assert stitch[stitch.index("-map_chapters") + 1] == "0", "without FFprobe the disc chapters are copied as they are"
    assert "+ildct+ilme" in stitch, "an interlaced DVD stays flagged as interlaced"
    assert stitch[stitch.index("-c:a") + 1] == "copy"
    assert output.is_file()


def _open_gop_packets(groups: int) -> list[tuple[int, bool]]:
    """A PAL DVD in file order: every keyframe is stored before two pictures shown just before it."""
    packets: list[tuple[int, bool]] = []
    for group in range(groups):
        key = group * 480 + 80
        packets.extend([(key, True), (key - 80, False), (key - 40, False)])
        for step in range(1, 4):
            anchor = key + step * 120
            packets.extend([(anchor, False), (anchor - 80, False), (anchor - 40, False)])
    return packets


def test_the_splice_encodes_from_the_group_before_the_damage_to_the_keyframe_after_it() -> None:
    packets = _open_gop_packets(20)

    windows = plan_splice(packets, Fraction(1, 1000), 25.0, [{"start": 2.0, "end": 4.0}])

    assert len(windows) == 1
    window = windows[0]
    assert window.start_pts == 1440, "the pictures shown before the keyframe at 1.52 s are encoded too"
    assert window.end_pts == 4400, "the copy resumes at the first keyframe after the damage"
    assert window.seek_seconds == pytest.approx(1.04), "decoding starts a group earlier for the references"
    assert copy_start_times(packets, windows) == [80, 4400]
    assert drop_expression(windows) == r"gte(pts\,1440)*lt(pts\,4400)"


def test_close_damaged_moments_are_encoded_together_and_damage_at_the_start_needs_no_copy_before_it() -> None:
    packets = _open_gop_packets(20)

    merged = plan_splice(packets, Fraction(1, 1000), 25.0, [{"start": 2.0, "end": 2.5}, {"start": 3.0, "end": 3.5}])
    at_start = plan_splice(packets, Fraction(1, 1000), 25.0, [{"start": 0.0, "end": 1.0}])

    assert [(window.start_pts, window.end_pts, len(window.patches)) for window in merged] == [(1440, 3920, 2)]
    assert (at_start[0].start_pts, at_start[0].end_pts, at_start[0].seek_seconds) == (0, 1040, 0.0)
    assert copy_start_times(packets, at_start) == [1040]


def test_scattered_damage_is_merged_until_the_copy_command_fits_on_a_windows_command_line() -> None:
    packets = _open_gop_packets(3000)
    # A damaged moment every 4 seconds for 20 minutes.
    patches = [{"start": 10.0 + 4 * index, "end": 10.5 + 4 * index} for index in range(300)]

    planned = plan_splice(packets, Fraction(1, 1000), 25.0, patches)
    fitted = fit_windows(planned, 0.001)

    assert len(drop_expression(planned)) + 12 * len(planned) > 12000, "the plan alone would not fit"
    assert len(drop_expression(fitted)) + 12 * len(fitted) <= 12000
    assert sum(len(window.patches) for window in fitted) == 300, "no damaged moment is lost"
    assert fitted[0].start_pts == planned[0].start_pts and fitted[-1].end_pts == planned[-1].end_pts


def test_encoded_pieces_match_the_movie_format_or_the_whole_movie_is_encoded() -> None:
    blu_ray = splice_encoder_args(
        {"codec": "h264", "pix_fmt": "yuv420p", "fps": 23.976, "field_order": "progressive", "color_primaries": "bt709"}
    )
    dvd = splice_encoder_args(
        {"codec": "mpeg2video", "pix_fmt": "yuv420p", "fps": 25.0, "field_order": "tt", "color_transfer": "bt470bg"}
    )

    assert "sps-id=31:repeat-headers=1" in blu_ray, "the copied movie keeps its own parameter sets"
    assert "-r" not in blu_ray and "passthrough" in blu_ray
    assert dvd[dvd.index("-c:v") + 1] == "mpeg2video"
    assert dvd[dvd.index("-flags") + 1] == "+ildct+ilme" and dvd[dvd.index("-top") + 1] == "1"
    assert dvd[dvd.index("-color_trc") + 1] == "gamma28"
    with pytest.raises(SpliceUnavailable):
        splice_encoder_args({"codec": "vc1", "pix_fmt": "yuv420p", "fps": 23.976})
    with pytest.raises(SpliceUnavailable):
        splice_encoder_args({"codec": "h264", "pix_fmt": "yuv420p10le", "fps": 23.976})


def test_an_encoded_stretch_less_than_half_a_frame_off_is_still_spliced_in() -> None:
    fps = 24000 / 1001
    # A Blu-ray stretch that started 14 ms late used to force encoding the whole movie again.
    assert starts_within_half_a_frame(4365.403, 4365.389, fps)
    assert not starts_within_half_a_frame(4365.431, 4365.389, fps), "a whole frame off is a different picture"
    assert starts_within_half_a_frame(12.0005, 12.0, 0), "without a frame rate, millisecond rounding is allowed"
    assert not starts_within_half_a_frame(12.01, 12.0, 0)


def test_the_copy_kept_without_loading_screens_follows_the_movie_name(tmp_path: Path) -> None:
    folder = tmp_path / "staging"
    folder.mkdir()
    movie = folder / "title_t00.mkv"
    movie.write_bytes(b"m" * 100)
    unrepaired_copy_path(movie).write_bytes(b"k" * 200)
    (folder / "title_t01.mkv").write_bytes(b"e" * 10)

    rename_video_outputs(folder, "Movie (2009)", MediaKind.MOVIE)

    assert sorted(path.name for path in folder.iterdir()) == [
        "Movie (2009) - Extra 01.mkv",
        "Movie (2009) - without loading screens.mkv",
        "Movie (2009).mkv",
    ]
    assert (folder / "Movie (2009).mkv").read_bytes() == b"m" * 100, "the larger kept copy is never taken for the movie"
    assert is_unrepaired_copy(folder / "Movie (2009) - without loading screens.mkv")


def test_the_loading_screen_setting_accepts_only_known_choices() -> None:
    assert AppSettings(damage_placeholder="none").damage_placeholder == "none"
    assert AppSettings().damage_placeholder == "ask", "loading screens are only added when the user chooses them"
    assert AppSettings(damage_placeholder="loading_screen").damage_placeholder == "loading_screen"
    with pytest.raises(ValueError):
        AppSettings(damage_placeholder="gif")


def _patch(start: float, end: float, name: str = "screen.mkv") -> dict[str, Any]:
    return {"start": start, "end": end, "clip": name}


def test_a_stretch_of_movie_shorter_than_one_picture_is_covered_by_the_clip() -> None:
    # The damage at 891.710 s leaves 32 ms of movie after the stretch starts: less than
    # one picture at 25 fps, which FFmpeg cannot encode into anything.
    ranges, order, clips = window_pieces([_patch(891.710, 909.550)], 25.0, 891.678, 910.038)

    assert ranges == [(909.550, 910.038)], "no piece of the movie shorter than a picture"
    assert order == [("clip", 0), ("source", 0)]
    assert clips == [(Path("screen.mkv"), round((909.550 - 891.678) * 25))], "the clip covers the sliver too"


def test_a_stretch_with_room_on_both_sides_keeps_the_movie_around_the_clip() -> None:
    ranges, order, clips = window_pieces([_patch(846.670, 863.110)], 25.0, 846.278, 863.598)

    assert ranges == [(846.278, 846.670), (863.110, 863.598)]
    assert order == [("source", 0), ("clip", 0), ("source", 1)]
    assert clips == [(Path("screen.mkv"), round((863.110 - 846.670) * 25))]


def test_a_tail_shorter_than_one_picture_lets_the_clip_run_to_the_end() -> None:
    ranges, order, clips = window_pieces([_patch(100.0, 119.98)], 25.0, 100.0, 120.0)

    assert ranges == [], "neither side holds a whole picture"
    assert order == [("clip", 0)]
    assert clips[0][1] == round(19.98 * 25) + 1, "the clip covers the last sliver as well"


def test_a_stretch_that_runs_to_the_end_of_the_movie_keeps_its_open_end() -> None:
    ranges, order, clips = window_pieces([_patch(10.0, 14.0)], 25.0, 9.0, None)

    assert ranges == [(9.0, 10.0), (14.0, None)]
    assert order == [("source", 0), ("clip", 0), ("source", 1)]
    assert len(clips) == 1
