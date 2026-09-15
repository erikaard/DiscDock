from __future__ import annotations

from pathlib import Path

import pytest

import discdock.files as files_module
from discdock.files import (
    ensure_within,
    finalize_directory,
    merge_into_directory,
    move_failed_staging,
    output_folder,
    rename_video_outputs,
    safe_component,
)
from discdock.models import MediaKind
from discdock.settings import AppSettings, SettingsStore


def test_safe_windows_path_components() -> None:
    assert safe_component("Quantum: Of/Solace?") == "Quantum_ Of_Solace_"
    assert safe_component("CON") == "_CON"
    assert safe_component("  ") == "Unidentified"


def test_path_escape_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="escapes"):
        ensure_within(tmp_path.parent / "outside", tmp_path)


@pytest.mark.asyncio
async def test_finalization_preserves_complete_tree(tmp_path: Path) -> None:
    source = tmp_path / "raw" / "job.partial"
    source.mkdir(parents=True)
    (source / "movie.mkv").write_bytes(b"media")
    destination = tmp_path / "completed" / "movies" / "Movie"
    result = await finalize_directory(source, destination, keep_source=False)
    assert result == destination
    assert (destination / "movie.mkv").read_bytes() == b"media"
    assert not source.exists()


@pytest.mark.asyncio
async def test_finalization_replaces_an_existing_tree_safely(tmp_path: Path) -> None:
    source = tmp_path / "raw" / "replacement.partial"
    source.mkdir(parents=True)
    (source / "new.mkv").write_bytes(b"new media")
    destination = tmp_path / "completed" / "movies" / "Movie"
    destination.mkdir(parents=True)
    (destination / "old.mkv").write_bytes(b"old media")

    result = await finalize_directory(source, destination, keep_source=False, replace_existing=True)

    assert result == destination
    assert (destination / "new.mkv").read_bytes() == b"new media"
    assert not (destination / "old.mkv").exists()
    assert not source.exists()
    assert not list(destination.parent.glob(f".{destination.name}.replaced-*"))


@pytest.mark.asyncio
async def test_failed_replacement_restores_both_old_output_and_new_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "raw" / "replacement.partial"
    source.mkdir(parents=True)
    (source / "new.mkv").write_bytes(b"new media")
    destination = tmp_path / "completed" / "movies" / "Movie"
    destination.mkdir(parents=True)
    (destination / "old.mkv").write_bytes(b"old media")
    real_replace = files_module.os.replace

    def fail_promotion(source_path: Path | str, destination_path: Path | str) -> None:
        source_candidate = Path(source_path)
        if source_candidate.name.startswith(".Movie.incoming-") and Path(destination_path) == destination:
            raise OSError("simulated promotion failure")
        real_replace(source_path, destination_path)

    monkeypatch.setattr(files_module.os, "replace", fail_promotion)

    with pytest.raises(OSError, match="promotion failure"):
        await finalize_directory(source, destination, keep_source=False, replace_existing=True)

    assert (destination / "old.mkv").read_bytes() == b"old media"
    assert (source / "new.mkv").read_bytes() == b"new media"


def test_episodes_added_to_a_completed_folder_continue_its_numbering(tmp_path: Path) -> None:
    library = tmp_path / "completed" / "tv" / "Noddy (1992)"
    library.mkdir(parents=True)
    (library / "Noddy (1992) - Episode 01.mkv").write_bytes(b"first")
    staging = tmp_path / "raw" / "more.partial"
    staging.mkdir(parents=True)
    (staging / "title_t02.mkv").write_bytes(b"third")
    (staging / "title_t01.mkv").write_bytes(b"second")

    renamed = rename_video_outputs(staging, library.name, MediaKind.SERIES, existing=library)

    assert [path.name for path in renamed] == ["Noddy (1992) - Episode 02.mkv", "Noddy (1992) - Episode 03.mkv"]
    assert (staging / "Noddy (1992) - Episode 02.mkv").read_bytes() == b"second"


def test_titles_added_to_a_completed_movie_folder_become_extras(tmp_path: Path) -> None:
    library = tmp_path / "completed" / "movies" / "Noddy (1992)"
    library.mkdir(parents=True)
    (library / "Noddy (1992).mkv").write_bytes(b"movie")
    (library / "Noddy (1992) - Extra 01.mkv").write_bytes(b"extra")
    staging = tmp_path / "raw" / "more.partial"
    staging.mkdir(parents=True)
    (staging / "title_t05.mkv").write_bytes(b"x" * 10)
    (staging / "title_t04.mkv").write_bytes(b"x" * 20)

    renamed = rename_video_outputs(staging, library.name, MediaKind.MOVIE, existing=library)

    assert [path.name for path in renamed] == ["Noddy (1992) - Extra 02.mkv", "Noddy (1992) - Extra 03.mkv"], "the movie keeps its name"
    assert (staging / "Noddy (1992) - Extra 02.mkv").read_bytes() == b"x" * 20, "larger titles come first"


@pytest.mark.asyncio
async def test_added_titles_join_the_completed_folder_without_replacing_its_files(tmp_path: Path) -> None:
    library = tmp_path / "completed" / "tv" / "Noddy (1992)"
    library.mkdir(parents=True)
    (library / "Noddy (1992) - Episode 01.mkv").write_bytes(b"first")
    staging = tmp_path / "raw" / "more.partial"
    staging.mkdir(parents=True)
    (staging / "Noddy (1992) - Episode 02.mkv").write_bytes(b"second")

    result = await merge_into_directory(staging, library, keep_source=False)

    assert result == library.resolve()
    assert (library / "Noddy (1992) - Episode 01.mkv").read_bytes() == b"first"
    assert (library / "Noddy (1992) - Episode 02.mkv").read_bytes() == b"second"
    assert not staging.exists()
    clash = tmp_path / "raw" / "clash.partial"
    clash.mkdir()
    (clash / "Noddy (1992) - Episode 01.mkv").write_bytes(b"other")
    (clash / "Noddy (1992) - Episode 09.mkv").write_bytes(b"ninth")

    with pytest.raises(FileExistsError):
        await merge_into_directory(clash, library, keep_source=False)

    assert (library / "Noddy (1992) - Episode 01.mkv").read_bytes() == b"first", "nothing in the library is replaced"
    assert not (library / "Noddy (1992) - Episode 09.mkv").exists(), "when a name is taken, nothing moves"
    assert (clash / "Noddy (1992) - Episode 01.mkv").read_bytes() == b"other"


def test_output_folder_distinguishes_replace_keep_both_and_music(tmp_path: Path) -> None:
    completed = tmp_path / "completed"
    canonical = completed / "movies" / "Movie (2024)"
    canonical.mkdir(parents=True)

    replace = output_folder(
        completed,
        MediaKind.MOVIE,
        "Movie",
        "2024",
        "abcdef123456",
        duplicate_policy="replace",
    )
    first_copy = output_folder(
        completed,
        MediaKind.MOVIE,
        "Movie",
        "2024",
        "abcdef123456",
        duplicate_policy="keep_both",
    )
    first_copy.mkdir()
    second_copy = output_folder(
        completed,
        MediaKind.MOVIE,
        "Movie",
        "2024",
        "abcdef123456",
        duplicate_policy="keep_both",
    )
    music = output_folder(
        tmp_path / "my-music",
        MediaKind.MUSIC,
        "Album",
        "",
        "12345678",
        duplicate_policy="keep_both",
        include_category=False,
    )

    assert replace == canonical.resolve()
    assert first_copy.name == "Movie (2024) [abcdef12]"
    assert second_copy.name == "Movie (2024) [abcdef12-2]"
    assert music == (tmp_path / "my-music" / "Album").resolve()


def test_movie_mkvs_are_named_after_folder_with_numbered_extras(tmp_path: Path) -> None:
    folder = tmp_path / "Movie (2024)"
    folder.mkdir()
    (folder / "title_t00.mkv").write_bytes(b"x" * 20)
    (folder / "title_t01.mkv").write_bytes(b"x" * 5)
    (folder / "title_t02.mkv").write_bytes(b"x" * 10)

    renamed = rename_video_outputs(folder, folder.name, MediaKind.MOVIE)

    assert {path.name for path in renamed} == {
        "Movie (2024).mkv",
        "Movie (2024) - Extra 01.mkv",
        "Movie (2024) - Extra 02.mkv",
    }
    assert (folder / "Movie (2024).mkv").stat().st_size == 20
    assert (folder / "Movie (2024) - Extra 01.mkv").stat().st_size == 10


@pytest.mark.asyncio
async def test_failed_raw_and_transcode_staging_are_moved_together(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    staging = raw / "job.partial"
    transcode = raw / "job.transcode.partial"
    staging.mkdir(parents=True)
    transcode.mkdir(parents=True)
    (staging / "raw.mkv").write_bytes(b"raw")
    (transcode / "encoded.mkv").write_bytes(b"encoded")

    moved = await move_failed_staging(staging, tmp_path / "failed", "job-id", raw_root=raw)

    assert moved is not None
    assert moved.parent.parent == (tmp_path / "failed").resolve()
    assert (moved / "raw.mkv").read_bytes() == b"raw"
    assert (moved.parent / "job.transcode.partial" / "encoded.mkv").read_bytes() == b"encoded"
    assert not staging.exists()
    assert not transcode.exists()


def test_settings_validation_and_backup(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "config" / "settings.json")
    settings = AppSettings(data_root=tmp_path / "data", duplicate_policy="ask")
    store.save(settings)
    settings.auto_rip = True
    store.save(settings)
    assert store.config_path.with_suffix(".json.bak").exists()
    assert store.load().auto_rip is True
    with pytest.raises(ValueError):
        AppSettings(data_root=tmp_path / "data2", poll_interval_seconds=0)


def test_cd_read_offset_defaults_to_zero_and_stays_in_range(tmp_path: Path) -> None:
    assert AppSettings(data_root=tmp_path).cd_read_offset == 0
    assert AppSettings(data_root=tmp_path, cd_read_offset=6).cd_read_offset == 6
    with pytest.raises(ValueError):
        AppSettings(data_root=tmp_path, cd_read_offset=6000)
