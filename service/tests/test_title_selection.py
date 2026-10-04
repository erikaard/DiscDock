"""Choosing which titles to rip, with discs shaped like the ones in the job history."""

from __future__ import annotations

from pathlib import Path

import pytest

import discdock.workflow as workflow_module
from discdock.models import DiscKind, MediaKind, TitleInfo
from discdock.optical import SECTOR_SIZE, dvd_title_contents
from discdock.settings import AppSettings
from discdock.workflow import TitleContent, select_disc_titles, title_contents


def _title(
    id: int, minutes: float, dvd: int = 0, *, streams: int = 2, chapters: int = 10, size: int = 0, segments: str = ""
) -> TitleInfo:
    return TitleInfo(
        id=id,
        disc_title_number=dvd,
        duration_seconds=round(minutes * 60),
        size_bytes=size or round(minutes * 50 * 1024**2),
        chapters=chapters,
        segment_map=segments,
        streams=[{"type": "Audio"}] * streams,
    )


def _settings(main_feature: bool, minimum: int = 600) -> AppSettings:
    return AppSettings(main_feature=main_feature, min_length_seconds=minimum)


def test_one_film_listed_once_per_branch_is_ripped_once_when_ripping_everything() -> None:
    # TANGLED: MakeMKV listed each DVD title twice (two credits branches), and Star Wars five times.
    titles = [_title(0, 95.8, 1), _title(1, 95.8, 1), _title(2, 12.0, 2), _title(3, 12.0, 2)]

    chosen = select_disc_titles(titles, _settings(main_feature=False))

    assert [title.id for title in chosen] == [0, 2], "each video once, the extra too"


def test_titles_that_play_the_same_sectors_are_one_video() -> None:
    # The Matrix: three titles of one film; only the first carries the ten subtitle tracks.
    titles = [_title(0, 130, 6, streams=3), _title(1, 130, 1, streams=12), _title(2, 130, 15, streams=2)]
    film = TitleContent("dvd:1", ((0, 1_000_000),))
    contents = {0: film, 1: film, 2: TitleContent("dvd:1", ((0, 990_000), (995_000, 1_005_000)))}

    chosen = select_disc_titles(titles, _settings(main_feature=False), contents=contents)

    assert [title.id for title in chosen] == [1], "the copy with the most audio and subtitle tracks"


def test_episodes_alike_in_length_are_not_mistaken_for_copies() -> None:
    # Lego Chima: sixteen 21-minute episodes, each its own part of the disc.
    titles = [_title(index, 21.2, index + 2) for index in range(16)]
    contents = {index: TitleContent("dvd:1", ((index * 100_000, (index + 1) * 100_000),)) for index in range(16)}

    chosen = select_disc_titles(titles, _settings(main_feature=False), contents=contents)

    assert len(chosen) == 16


def test_a_series_rips_every_episode_without_its_play_all() -> None:
    episodes = [_title(index, 21.2, index + 2) for index in range(8)]
    play_alls = [_title(8, 84.7, 18), _title(9, 84.7, 19)]
    contents = {index: TitleContent("dvd:1", ((index * 100, (index + 1) * 100),)) for index in range(8)}
    contents[8] = TitleContent("dvd:1", ((0, 400),))
    contents[9] = TitleContent("dvd:1", ((400, 800),))

    for main_feature in (True, False):
        chosen = select_disc_titles(
            episodes + play_alls, _settings(main_feature), contents=contents, media_kind=MediaKind.SERIES
        )
        assert [title.id for title in chosen] == list(range(8)), "the episodes, not the play-alls"


def test_without_the_discs_layout_a_play_all_is_recognised_by_its_length() -> None:
    titles = [_title(index, 21.2, index + 2) for index in range(16)] + [_title(16, 84.7, 18)]

    chosen = select_disc_titles(titles, _settings(main_feature=True), media_kind=MediaKind.SERIES)

    assert len(chosen) == 16 and all(title.duration_seconds < 1300 for title in chosen)


def test_a_series_leaves_out_clips_of_a_few_seconds() -> None:
    # Øisteins blyant: fifteen five-minute episodes and fourteen menu clips, with no minimum length.
    titles = [_title(index, 5.4, index + 1) for index in range(15)] + [
        _title(15 + index, 0.2, 16 + index) for index in range(14)
    ]

    chosen = select_disc_titles(titles, _settings(main_feature=True, minimum=0), media_kind=MediaKind.SERIES)

    assert [title.id for title in chosen] == list(range(15))


def test_a_film_disc_keeps_the_film_even_when_scene_titles_cover_it() -> None:
    film = _title(0, 90, 1)
    scenes = [_title(1 + index, 30, 2 + index) for index in range(3)]
    contents = {0: TitleContent("dvd:1", ((0, 900),))}
    contents.update({1 + index: TitleContent("dvd:1", ((index * 300, (index + 1) * 300),)) for index in range(3)})

    everything = select_disc_titles([film, *scenes], _settings(main_feature=False), contents=contents)
    feature = select_disc_titles([film, *scenes], _settings(main_feature=True), 92, contents=contents)

    assert [title.id for title in everything] == [0, 1, 2, 3], "nothing on a film disc is thrown away"
    assert [title.id for title in feature] == [0]


def test_a_series_disc_with_one_long_title_still_gets_its_feature() -> None:
    # DuckTales the Movie, filed by OMDb under the series' name.
    titles = [_title(0, 74, 1), _title(1, 12, 2)]

    chosen = select_disc_titles(titles, _settings(main_feature=True), 74, media_kind=MediaKind.SERIES)

    assert [title.id for title in chosen] == [0]


def test_blu_ray_playlists_of_the_same_clips_are_one_video() -> None:
    titles = [
        _title(0, 96.2, segments="00801,00802"),
        _title(1, 96.2, segments="801,802"),
        _title(2, 96.2, segments="801/20801,802/20802"),
        _title(3, 14.0, segments="00900"),
    ]
    contents = title_contents(DiscKind.BLURAY, titles)

    chosen = select_disc_titles(titles, _settings(main_feature=False), contents=contents)

    assert contents[0] == contents[1] and contents[2] != contents[0], "the 3D playlist plays more clips"
    assert [title.id for title in chosen] == [0, 2, 3]


def _vmg(titles: list[tuple[int, int, int]]) -> bytes:
    """VIDEO_TS.IFO listing (title set, title within it, chapters) for each disc title."""
    data = bytearray(2 * SECTOR_SIZE)
    data[:12] = b"DVDVIDEO-VMG"
    data[0xC4:0xC8] = (1).to_bytes(4, "big")
    table = SECTOR_SIZE
    data[table : table + 2] = len(titles).to_bytes(2, "big")
    for index, (title_set, vts_title, chapters) in enumerate(titles):
        entry = table + 8 + 12 * index
        data[entry + 2 : entry + 4] = chapters.to_bytes(2, "big")
        data[entry + 6] = title_set
        data[entry + 7] = vts_title
    return bytes(data)


def _vts(programs: list[list[tuple[int, int]]]) -> bytes:
    """A title set IFO with one title per program chain, each playing the given cells."""
    data = bytearray(3 * SECTOR_SIZE)
    data[:12] = b"DVDVIDEO-VTS"
    data[0xC8:0xCC] = (1).to_bytes(4, "big")
    data[0xCC:0xD0] = (2).to_bytes(4, "big")
    ptt = SECTOR_SIZE
    data[ptt : ptt + 2] = len(programs).to_bytes(2, "big")
    for index in range(len(programs)):
        offset = 8 + 4 * len(programs) + 4 * index
        data[ptt + 8 + 4 * index : ptt + 12 + 4 * index] = offset.to_bytes(4, "big")
        data[ptt + offset : ptt + offset + 2] = (index + 1).to_bytes(2, "big")
    pgci = 2 * SECTOR_SIZE
    data[pgci : pgci + 2] = len(programs).to_bytes(2, "big")
    position = 8 + 8 * len(programs)
    for index, cells in enumerate(programs):
        data[pgci + 8 + 8 * index + 4 : pgci + 8 + 8 * index + 8] = position.to_bytes(4, "big")
        pgc = pgci + position
        data[pgc + 3] = len(cells)
        data[pgc + 0xE8 : pgc + 0xEA] = (0xEC).to_bytes(2, "big")
        for cell, (first, last) in enumerate(cells):
            entry = pgc + 0xEC + 24 * cell
            data[entry + 8 : entry + 12] = first.to_bytes(4, "big")
            data[entry + 20 : entry + 24] = last.to_bytes(4, "big")
        position += 0xEC + 24 * len(cells)
    return bytes(data)


def test_the_ifo_files_say_which_titles_play_the_same_film(tmp_path: Path, monkeypatch) -> None:
    # This test reads its own folder only, so the real IFO reader may run.
    monkeypatch.setattr(workflow_module, "dvd_title_contents", dvd_title_contents)
    folder = tmp_path / "VIDEO_TS"
    folder.mkdir()
    # Titles 1 and 2 are the film with Norwegian and Danish credits; title 3 is an extra.
    (folder / "VIDEO_TS.IFO").write_bytes(_vmg([(1, 1, 1), (1, 2, 1), (1, 3, 1)]))
    (folder / "VTS_01_0.IFO").write_bytes(
        _vts([[(0, 99_999), (100_000, 100_499)], [(0, 99_999), (100_500, 100_999)], [(200_000, 209_999)]])
    )

    layout = dvd_title_contents(folder)
    titles = [_title(0, 96.5, 1), _title(1, 96.6, 2), _title(2, 12, 3)]
    contents = title_contents(DiscKind.DVD, titles, folder)
    chosen = select_disc_titles(titles, _settings(main_feature=False), contents=contents)

    assert layout[1] == (1, [(0, 100_500)]) and layout[3] == (1, [(200_000, 210_000)])
    assert [title.id for title in chosen] == [0, 2], "one film, one extra"
    assert dvd_title_contents(tmp_path / "missing") == {}


@pytest.mark.asyncio
async def test_a_damaged_disc_is_not_read_again_for_its_layout(tmp_path: Path, monkeypatch) -> None:
    from test_workflow_recovery import make_job, make_service

    from discdock.makemkv import DiscScan
    from discdock.models import DriveInfo

    settings = AppSettings(data_root=tmp_path)
    service, database = make_service(settings, make_job(settings, tmp_path / "staging"))
    drive = DriveInfo(id="drive-id", letter="D:", name="Drive", disc_kind=DiscKind.DVD)
    scan = DiscScan(titles=[_title(0, 90, 1)])
    reads: list[Path] = []
    monkeypatch.setattr(workflow_module, "dvd_title_contents", lambda folder: reads.append(folder) or {})

    await service._title_contents("job-id", drive, scan)
    database.job["metadata"] = {"warnings": [{"code": "disc_read_error"}]}
    await service._title_contents("job-id", drive, scan)

    assert reads == [Path("D:/VIDEO_TS")], "read once for the healthy disc, not for the damaged one"


def test_a_disc_omdb_does_not_know_is_recognised_as_episodes_by_its_titles() -> None:
    # Barnas Favoritter 2: five cartoons, one of them twice as long as the others.
    titles = [_title(0, 10.0, 4), _title(1, 20.2, 7), _title(2, 10.1, 8), _title(3, 12.3, 9), _title(4, 10.4, 10)]

    chosen = select_disc_titles(titles, _settings(main_feature=True))

    assert [title.id for title in chosen] == [0, 1, 2, 3, 4], "every cartoon, the long one too"


def test_a_film_disc_is_not_mistaken_for_episodes() -> None:
    # Mickey's Twice Upon a Christmas: a 64-minute film and short extras, with no minimum length.
    titles = [_title(0, 64.5, 1), _title(1, 7.1, 11), _title(2, 3.2, 12), _title(3, 3.0, 68), _title(4, 11.8, 72)]
    # And a film with three featurettes of similar length to each other.
    featurettes = [_title(0, 92, 1), _title(1, 14, 2), _title(2, 16, 3), _title(3, 15, 4)]

    assert [title.id for title in select_disc_titles(titles, _settings(main_feature=True, minimum=0))] == [0]
    assert [title.id for title in select_disc_titles(featurettes, _settings(main_feature=True))] == [0]


def test_omdb_naming_a_film_that_fits_the_disc_settles_it() -> None:
    # Three 63-minute titles look like episodes, but OMDb knows a 63-minute film by this name.
    titles = [_title(0, 63.0, 1), _title(1, 63.3, 2), _title(2, 63.3, 3)]

    film = select_disc_titles(titles, _settings(main_feature=True), 66, media_kind=MediaKind.MOVIE)
    unknown = select_disc_titles(titles, _settings(main_feature=True))

    assert len(film) == 1 and len(unknown) == 3
