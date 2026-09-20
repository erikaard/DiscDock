from __future__ import annotations

from pathlib import Path

import pytest

from discdock.disc_files import (
    DiscContents,
    DiscEntry,
    check_backup,
    describe_contents,
    image_files,
    list_drive_files,
    list_tree,
)


def _disc(tmp_path: Path, files: dict[str, int]) -> Path:
    root = tmp_path / "disc"
    for name, size in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
    return root


def test_every_file_on_the_disc_is_listed_with_its_size(tmp_path: Path) -> None:
    root = _disc(tmp_path, {"autorun.inf": 64, "setup.exe": 2048, "Data/game.pak": 4096, "Data/Sound/theme.ogg": 512})

    contents = list_tree(root)

    assert [entry.path for entry in contents.entries] == [
        "autorun.inf",
        "Data/game.pak",
        "Data/Sound/theme.ogg",
        "setup.exe",
    ], "one alphabetical list, folders and all"
    assert contents.total_bytes == 64 + 2048 + 4096 + 512
    assert contents.file_count == 4
    assert not contents.truncated and not contents.note
    assert contents.top_level()[0] == ("Data", 2, 4608), "folders are summed up, largest first"


def test_a_disc_with_countless_files_is_listed_as_far_as_it_is_useful(tmp_path: Path) -> None:
    root = _disc(tmp_path, {f"file{index:03d}.dat": 1 for index in range(30)})

    contents = list_tree(root, limit=10)

    assert contents.file_count == 10
    assert contents.truncated and "first part" in contents.note


def test_a_disc_that_answers_too_slowly_stops_being_listed(tmp_path: Path) -> None:
    root = _disc(tmp_path, {"a/one.dat": 1, "b/two.dat": 1})
    ticks = iter([0.0, 0.0, 100.0, 200.0, 300.0])

    contents = list_tree(root, seconds=5.0, clock=lambda: next(ticks, 999.0))

    assert contents.truncated, "a scratched disc must not hold a job for ever"


def test_a_game_disc_is_recognised_by_what_starts_it(tmp_path: Path) -> None:
    contents = list_tree(_disc(tmp_path, {"autorun.inf": 64, "setup.exe": 2048, "Data/game.pak": 4096}))

    described = describe_contents(contents, "SIMS2_EP1")

    assert described["kind"] == "game"
    assert described["markers"] == ["autorun.inf", "setup.exe"]
    assert described["suggested_title"] == "Sims2 Ep1", "the disc label is shouted; a folder name is not"
    assert described["file_count"] == 3


@pytest.mark.parametrize(
    ("files", "kind"),
    [
        ({"install.msi": 4096, "readme.txt": 32}, "software"),
        ({"Songs/one.mp3": 4096, "Songs/two.mp3": 4096}, "media"),
        ({"Holiday/one.jpg": 4096, "Holiday/two.jpg": 4096}, "pictures"),
        ({"Report.pdf": 4096, "Notes.docx": 2048}, "documents"),
        ({"archive.bin": 4096, "table.dat": 2048}, "files"),
    ],
)
def test_the_rest_of_a_disc_is_described_by_what_it_holds(tmp_path: Path, files: dict[str, int], kind: str) -> None:
    contents = list_tree(_disc(tmp_path, files))

    assert describe_contents(contents, "DISC")["kind"] == kind


def test_a_drive_letter_must_be_a_drive_letter() -> None:
    with pytest.raises(ValueError, match="drive letter"):
        list_drive_files("not a drive")


def _iso_image(tmp_path: Path) -> Path:
    from test_disc_rescue import _dvd_reader

    read, total = _dvd_reader()
    image = tmp_path / "disc.iso"
    image.write_bytes(read(0, total))
    return image


def test_a_backup_is_read_back_from_its_own_file_system(tmp_path: Path) -> None:
    contents = image_files(_iso_image(tmp_path))

    names = {Path(entry.path).name for entry in contents.entries}
    assert {"VIDEO_TS.IFO", "VTS_01_1.VOB"} <= names, "the image lists its own files"
    assert all(entry.path.startswith("VIDEO_TS/") for entry in contents.entries), "with the folders they are in"
    assert contents.total_bytes > 0


def test_a_backup_that_holds_every_file_is_complete(tmp_path: Path) -> None:
    image = _iso_image(tmp_path)
    inside = image_files(image)

    check = check_backup(DiscContents(list(inside.entries), inside.total_bytes), image)

    assert check.complete and not check.reason
    assert check.files_in_image == check.files_on_disc == inside.file_count


def test_a_backup_missing_a_file_is_not_accepted(tmp_path: Path) -> None:
    image = _iso_image(tmp_path)
    inside = image_files(image)
    disc = DiscContents([*inside.entries, DiscEntry("EXTRA/README.TXT", 12345)], inside.total_bytes + 12345)

    check = check_backup(disc, image)

    assert not check.complete
    assert "EXTRA/README.TXT" in check.reason


def test_a_backup_that_cannot_be_read_is_not_accepted(tmp_path: Path) -> None:
    broken = tmp_path / "broken.iso"
    broken.write_bytes(b"\0" * (64 * 2048))

    check = check_backup(DiscContents([DiscEntry("game.exe", 2048)], 2048), broken)

    assert not check.complete
    assert "could not be read" in check.reason or "no files" in check.reason


def test_a_disc_nobody_could_list_is_still_worth_backing_up(tmp_path: Path) -> None:
    image = _iso_image(tmp_path)

    # Nothing was listed on the disc, so there is nothing to compare; the image stands on its own.
    check = check_backup(DiscContents([], 0), image)

    assert check.complete and check.files_in_image > 0
