"""Ghostbusters II: MakeMKV leaves the film out "due to navigation error" on some scans."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import discdock.workflow as workflow_module
from discdock.database import Database
from discdock.makemkv import DiscScan
from discdock.models import DiscKind, DriveInfo, TitleInfo
from discdock.optical import DvdTitle
from discdock.processes import ProcessFailure, ProcessResult
from discdock.secrets import SecretStore
from discdock.settings import AppSettings, SettingsStore
from discdock.workflow import (
    DiscDockService,
    TitleContent,
    WrongTitleRipped,
    _skipped_feature,
    select_disc_titles,
)

FILM_SKIPPED = [
    (
        'MSG:3015,0,2,"Title #1 (1:44:04) was skipped due to navigation error","Title #%1 (%2) was skipped due '
        'to navigation error","1","1:44:04"'
    ),
    'MSG:3028,0,3,"Title #2 was added (2 cell(s), 0:23:46)"',
    'MSG:3028,0,3,"Title #3 was added (4 cell(s), 0:26:17)"',
]


def _cartoons() -> list[TitleInfo]:
    return [
        TitleInfo(id=0, disc_title_number=2, duration_seconds=1426, size_bytes=862_453_760, chapters=2),
        TitleInfo(id=1, disc_title_number=3, duration_seconds=1577, size_bytes=953_849_856, chapters=4),
    ]


def _film() -> TitleInfo:
    return TitleInfo(id=0, disc_title_number=1, duration_seconds=6220, size_bytes=4_200_000_000, chapters=31)


def test_a_skipped_title_longer_than_everything_listed_is_the_film() -> None:
    settings = AppSettings(min_length_seconds=600)

    assert _skipped_feature(FILM_SKIPPED, _cartoons(), settings) == 6244
    assert _skipped_feature([], _cartoons(), settings) == 0
    # A skipped title no longer than what was listed is not the film going missing.
    assert _skipped_feature(FILM_SKIPPED, [_film(), *_cartoons()], settings) == 0
    # Nor is one the length filter would have dropped anyway.
    assert _skipped_feature(FILM_SKIPPED, _cartoons(), AppSettings(min_length_seconds=600, max_length_seconds=3600)) == 0


def _service(tmp_path: Path, scans: list[DiscScan]):
    executable = tmp_path / "makemkvcon64.exe"
    executable.write_bytes(b"stub")
    settings = AppSettings(
        data_root=tmp_path, make_mkv_path=str(executable), omdb_enabled=False, main_feature=True, auto_rip=False
    )
    store = SettingsStore(tmp_path / "config" / "settings.json")
    store.save(settings)
    database = Database(tmp_path / "database" / "discdock.db")
    database.initialize()
    service = DiscDockService(store, SecretStore(tmp_path / "config" / "secrets.bin"), database)
    drive = DriveInfo(
        id="drive-1", letter="D:", name="Reader", media_loaded=True, volume_label="DVD_VIDEO", disc_kind=DiscKind.DVD
    )
    service.drives[drive.id] = drive
    database.upsert_drive(drive.model_dump(mode="json"))
    database.create_job(
        {
            "id": "job-1",
            "drive_id": drive.id,
            "drive_letter": drive.letter,
            "disc_label": drive.volume_label,
            "disc_type": drive.disc_kind.value,
            "state": "detected",
            "stage": "detected",
            "staging_path": str(tmp_path / "raw" / "job-1.partial"),
            "settings": settings.public_dict(),
        }
    )
    calls: list[int] = []

    class Scanner:
        async def inspect(self, *_args, **_kwargs):
            calls.append(1)
            return scans[min(len(calls), len(scans)) - 1]

    service._make_mkv = lambda _settings=None: Scanner()  # type: ignore[method-assign]
    return service, database, drive, settings, calls


@pytest.mark.asyncio
async def test_the_disc_is_scanned_again_when_makemkv_leaves_the_film_out(tmp_path: Path) -> None:
    skipped = DiscScan(title_count=2, titles=_cartoons(), raw_lines=FILM_SKIPPED)
    listed = DiscScan(title_count=3, titles=[_film(), *_cartoons()])
    service, database, drive, settings, calls = _service(tmp_path, [skipped, listed])

    scan = await service._inspect_disc("job-1", drive, settings)

    assert scan is listed and len(calls) == 2
    log = (tmp_path / "logs" / "job-1.log").read_text(encoding="utf-8")
    assert 'MakeMKV left out a 1:44:04 title "due to navigation error"' in log
    assert "feature_skipped_seconds" not in (database.get_job("job-1") or {}).get("metadata", {})


@pytest.mark.asyncio
async def test_a_film_makemkv_never_lists_is_not_replaced_by_a_cartoon(tmp_path: Path, monkeypatch) -> None:
    skipped = DiscScan(title_count=2, titles=_cartoons(), raw_lines=FILM_SKIPPED)
    service, database, _, _, calls = _service(tmp_path, [skipped])

    async def notification_stub(*_args, **_kwargs) -> bool:
        return True

    monkeypatch.setattr(service.notifications, "send", notification_stub)

    await service._process_job("job-1")
    await asyncio.sleep(0)

    job = database.get_job("job-1") or {}
    assert len(calls) == 3, "scanned three times in all"
    assert job["state"] == "awaiting_input"
    assert "left out the 1:44:04 film" in job["status_detail"]
    assert not any(track["selected"] for track in database.list_tracks("job-1")), "no cartoon ticked in advance"


def test_once_the_film_is_listed_only_the_film_is_ripped() -> None:
    # The film, the same film again with the commentary as its first audio track, two cartoon
    # episodes and a making-of: "Select main feature" rips the film once and nothing else.
    film = _film().model_copy(update={"streams": [{"type": "Audio"}] * 3})
    commentary = _film().model_copy(update={"id": 1, "disc_title_number": 4, "streams": [{"type": "Audio"}] * 2})
    cartoons = [title.model_copy(update={"id": title.id + 2}) for title in _cartoons()]
    making_of = TitleInfo(id=4, disc_title_number=5, duration_seconds=1500, size_bytes=900_000_000, chapters=1)
    same = TitleContent("dvd:1", ((0, 2_000_000),))
    contents = {0: same, 1: same}
    titles = [film, commentary, *cartoons, making_of]

    feature = select_disc_titles(titles, AppSettings(main_feature=True), 108, contents=contents)
    everything = select_disc_titles(titles, AppSettings(main_feature=False), contents=contents)

    assert [title.id for title in feature] == [0]
    assert 1 not in {title.id for title in everything}, "the commentary copy of the film is not ripped twice"


@pytest.mark.asyncio
async def test_a_rip_of_another_title_is_thrown_away(tmp_path: Path, monkeypatch) -> None:
    from test_workflow_recovery import make_job, make_service

    settings = AppSettings(data_root=tmp_path)
    staging = tmp_path / "raw" / "attempt.partial"
    staging.mkdir(parents=True)
    service, _ = make_service(settings, make_job(settings, staging))
    ripped = staging / "Ghostbusters 2 - Special Edition-C1_t01.mkv"
    ripped.write_bytes(b"x" * 1024)
    tracks = [{"source_id": 0, "duration_seconds": 6220, "selected": True}]
    lengths = {"seconds": 1577.0}
    monkeypatch.setattr(workflow_module, "_probe_media_duration", lambda path, ffprobe: lengths["seconds"])
    settings.ffprobe_path = str(tmp_path / "ffprobe.exe")

    with pytest.raises(WrongTitleRipped, match="ripped a 0:26:17 title instead of the 1:43:40"):
        await service._check_ripped_title("job-id", staging, tracks, settings)
    assert not ripped.exists(), "the cartoon is not filed under the film's name"

    ripped.write_bytes(b"x" * 1024)
    lengths["seconds"] = 6219.5
    await service._check_ripped_title("job-id", staging, tracks, settings)
    assert ripped.exists()


@pytest.mark.asyncio
async def test_confirming_the_disc_scans_again_when_the_film_goes_missing(tmp_path: Path) -> None:
    listed = DiscScan(title_count=3, titles=[_film(), *_cartoons()])
    skipped = DiscScan(title_count=2, titles=_cartoons(), raw_lines=FILM_SKIPPED)
    service, _, drive, settings, calls = _service(tmp_path, [skipped, listed])
    fingerprint = service._disc_fingerprint(drive, listed, "job-1")

    await service._confirm_disc("job-1", drive, settings, fingerprint)
    assert len(calls) == 2, "the second scan listed the film again, as chosen"

    (tmp_path / "again").mkdir()
    service, _, drive, settings, calls = _service(tmp_path / "again", [skipped])
    with pytest.raises(RuntimeError, match="left out a 1:44:04 title"):
        await service._confirm_disc("job-1", drive, settings, fingerprint)
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_makemkvs_silent_rebuild_is_shown_as_a_step_with_progress(tmp_path: Path, monkeypatch) -> None:
    from test_workflow_recovery import make_job, make_service

    settings = AppSettings(data_root=tmp_path)
    service, database = make_service(settings, make_job(settings, tmp_path / "staging"))
    monkeypatch.setattr(workflow_module, "REBUILD_TICK_SECONDS", 0.01)
    # The scan before the rip took seven and a half minutes for the same rebuild.
    database.job["metadata"] = {"navigation_rebuild_seconds": 450}

    await service._process_event(
        "job-id",
        {"type": "msg", "message": 'MSG:3042,0,1,"IFO file for VTS #1 is corrupt, VOB file must be scanned."'},
    )
    await asyncio.sleep(0.05)

    assert database.job["status_detail"].startswith("Rebuilding the disc's damaged navigation (copy protection) · 0:0")
    assert database.job["status_detail"].endswith("of about 7:30")
    assert 0 <= database.job["progress"] < 5

    # Denver, the Last Dinosaur: a short title is reported 26 seconds in, then MakeMKV
    # works on the next one for three and a half minutes more.
    await service._process_event(
        "job-id", {"type": "msg", "message": 'MSG:3025,0,3,"Title #2/1 has length of 84 seconds"'}
    )
    await service._process_event("job-id", {"type": "msg", "message": 'MSG:3028,0,3,"Title #1 was added (1:43:40)"'})
    await asyncio.sleep(0.05)
    assert database.job["metadata"]["navigation_rebuild_seconds"] == 450, "a title reported on the way is not the end"
    assert "job-id" in service._rebuilds

    await service._process_event(
        "job-id", {"type": "msg", "message": 'MSG:5011,0,0,"Operation successfully completed"'}
    )
    database.job["status_detail"] = "Saving to MKV file"
    await asyncio.sleep(0.05)

    assert database.job["status_detail"] == "Saving to MKV file", "the step ends when MakeMKV has looked at all of it"
    assert database.job["metadata"]["navigation_rebuild_seconds"] == 0, "and its time is kept for the next run"


@pytest.mark.asyncio
async def test_a_first_rebuild_says_how_long_it_has_taken_so_far(tmp_path: Path, monkeypatch) -> None:
    from test_workflow_recovery import make_job, make_service

    settings = AppSettings(data_root=tmp_path)
    service, database = make_service(settings, make_job(settings, tmp_path / "staging"))
    monkeypatch.setattr(workflow_module, "REBUILD_TICK_SECONDS", 0.01)

    await service._process_event("job-id", {"type": "msg", "message": 'MSG:3042,0,1,"VOB file must be scanned."'})
    await asyncio.sleep(0.05)
    assert database.job["status_detail"].endswith("so far, usually a few minutes")

    # The next MakeMKV run begins without the end of this one being seen.
    await service._process_event("job-id", {"type": "msg", "message": 'MSG:1005,0,1,"MakeMKV v1.18.4 started"'})
    database.job["status_detail"] = "Opening DVD disc"
    await asyncio.sleep(0.05)
    assert database.job["status_detail"] == "Opening DVD disc"


@pytest.mark.asyncio
async def test_working_around_the_drives_region_is_shown_while_makemkv_is_silent(tmp_path: Path) -> None:
    from test_workflow_recovery import make_job, make_service

    settings = AppSettings(data_root=tmp_path)
    service, database = make_service(settings, make_job(settings, tmp_path / "staging"))
    database.job["status_detail"] = "Decrypting data"
    region = (
        'MSG:3032,0,2,"Region setting of drive BD-RE HL-DT-ST BD-RE BU40N 1.05:2 does not match the region of '
        'currently inserted disc, trying to work around..."'
    )

    await service._process_event("job-id", {"type": "msg", "message": region})

    assert database.job["status_detail"] == (
        "MakeMKV says the drive's region does not match the disc and is working around it"
    )


REGION_STALL = ProcessResult(
    args=[],
    return_code=-1,
    timed_out=True,
    lines=[
        (
            'MSG:3032,0,2,"Regionsinnstilling for stasjon HL-DT-ST:BD-RE BU40N passer ikke med regionen for den '
            'innsatte platen, forsøker å løse dette..."'
        )
    ]
    * 10,
)


@pytest.mark.asyncio
async def test_a_scan_that_stalls_after_a_region_message_is_made_again(tmp_path: Path) -> None:
    # SPEKTRALSTEINENE: ten region messages just after the disc went in, then silence until
    # the scan was stopped. Scanned again by hand, it listed its titles in under a minute.
    service, _, drive, settings, calls = _service(tmp_path, [])
    outcomes: list[DiscScan | ProcessFailure] = [
        ProcessFailure("MakeMKV inspection timed out", REGION_STALL),
        DiscScan(title_count=1, titles=[_film()]),
    ]

    class Scanner:
        async def inspect(self, *_args, **_kwargs):
            calls.append(1)
            outcome = outcomes[len(calls) - 1]
            if isinstance(outcome, ProcessFailure):
                raise outcome
            return outcome

    service._make_mkv = lambda _settings=None: Scanner()  # type: ignore[method-assign]

    scan = await service._inspect_disc("job-1", drive, settings)

    assert len(calls) == 2 and scan.titles[0].duration_seconds == 6220


@pytest.mark.asyncio
async def test_a_region_stall_is_scanned_again_only_once(tmp_path: Path) -> None:
    service, _, drive, settings, calls = _service(tmp_path, [])

    class Scanner:
        async def inspect(self, *_args, **_kwargs):
            calls.append(1)
            raise ProcessFailure("MakeMKV inspection timed out", REGION_STALL)

    service._make_mkv = lambda _settings=None: Scanner()  # type: ignore[method-assign]

    with pytest.raises(ProcessFailure, match="timed out"):
        await service._inspect_disc("job-1", drive, settings)
    assert len(calls) == 2



DENVER_SKIPPED = [
    (
        'MSG:3015,0,2,"Title #2/2 (0:39:54) was skipped due to navigation error","Title #%1 (%2) was skipped due '
        'to navigation error","2/2","0:39:54"'
    ),
    'MSG:3028,0,3,"Title #2/3 was added (3 cell(s), 0:17:15)"',
]


def _denver_scan() -> DiscScan:
    return DiscScan(
        title_count=1,
        titles=[TitleInfo(id=0, disc_title_number=2, duration_seconds=1035, size_bytes=659_100_000, chapters=1)],
        raw_lines=DENVER_SKIPPED,
    )


@pytest.mark.asyncio
async def test_a_film_makemkv_keeps_leaving_out_is_taken_from_the_discs_own_tables(tmp_path: Path, monkeypatch) -> None:
    # Denver, the Last Dinosaur: three scans leave out the 39:54 title, while the disc's
    # navigation says its title 2 plays for 41:19.
    service, database, drive, settings, calls = _service(tmp_path, [_denver_scan()])
    monkeypatch.setattr(
        workflow_module, "dvd_titles_in_folder", lambda folder: [DvdTitle(1, 1, 0, 3), DvdTitle(2, 2, 2479, 10)]
    )
    monkeypatch.setattr(workflow_module, "dvd_folder_video_scrambled", lambda folder, title_set: False)
    tables = DiscScan(title_count=1, titles=[TitleInfo(id=0, disc_title_number=2, duration_seconds=2479, chapters=10)])
    handed: list[bool] = []

    async def rescued(job_id, _drive, _settings):
        handed.append(bool((database.get_job(job_id) or {})["metadata"].get("titles_from_tables")))
        return tables

    service._scan_rescued_structures = rescued  # type: ignore[method-assign]

    scan = await service._inspect_disc("job-1", drive, settings)

    assert scan is tables and len(calls) == 3
    assert handed == [True], "the rescue lists the titles from the disc's tables, not from MakeMKV"
    assert "feature_skipped_seconds" not in (database.get_job("job-1") or {})["metadata"]


@pytest.mark.asyncio
async def test_when_the_tables_have_nothing_that_long_the_choice_stays_yours(tmp_path: Path, monkeypatch) -> None:
    service, database, drive, settings, _ = _service(tmp_path, [_denver_scan()])
    monkeypatch.setattr(workflow_module, "dvd_titles_in_folder", lambda folder: [DvdTitle(2, 2, 1035, 3)])

    async def rescued(*_args):
        raise AssertionError("nothing in the tables is the film")

    service._scan_rescued_structures = rescued  # type: ignore[method-assign]

    scan = await service._inspect_disc("job-1", drive, settings)

    assert scan.titles[0].duration_seconds == 1035
    assert (database.get_job("job-1") or {})["metadata"]["feature_skipped_seconds"] == 2394



@pytest.mark.asyncio
async def test_a_copy_protected_disc_keeps_the_choice_since_only_makemkv_decrypts(tmp_path: Path, monkeypatch) -> None:
    service, database, drive, settings, _ = _service(tmp_path, [_denver_scan()])
    monkeypatch.setattr(workflow_module, "dvd_titles_in_folder", lambda folder: [DvdTitle(2, 2, 2479, 10)])
    monkeypatch.setattr(workflow_module, "dvd_folder_video_scrambled", lambda folder, title_set: True)

    async def rescued(*_args):
        raise AssertionError("FFmpeg cannot copy scrambled video, so the rescue would end with nothing")

    service._scan_rescued_structures = rescued  # type: ignore[method-assign]

    await service._inspect_disc("job-1", drive, settings)

    assert (database.get_job("job-1") or {})["metadata"]["feature_skipped_seconds"] == 2394


class _Ripper:
    """MakeMKV ripping: each run either loses the film or saves it, in the order given."""

    def __init__(self, outcomes: list[str]) -> None:
        self.outcomes = outcomes
        self.runs = 0

    async def rip(self, job_id, letter, destination, title_ids, timeout, callback=None, backup=False, min_length=None):
        from discdock.processes import ProcessFailure, ProcessResult

        outcome = self.outcomes[min(self.runs, len(self.outcomes) - 1)]
        self.runs += 1
        if outcome == "lost":
            # The Karate Kid Part II: the rip's second look skipped the film, then MakeMKV gave up.
            lines = [
                'MSG:3024,16781312,2,"Complex multiplex encountered - 2 cells and 1170 VOBUs have to be scanned."',
                'MSG:3015,0,2,"Title #1 (1:48:37) was skipped due to navigation error"',
                'MSG:5010,0,0,"Failed to open disc"',
            ]
            raise ProcessFailure("MakeMKV ripping failed", ProcessResult(["makemkvcon64"], 1, lines))
        if outcome == "cancelled":
            raise ProcessFailure("MakeMKV ripping failed", ProcessResult(["makemkvcon64"], 1, [], cancelled=True))
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "title_t00.mkv").write_bytes(b"x" * 1024)
        return [ProcessResult(["makemkvcon64"], 0)]


async def _rip(tmp_path: Path, outcomes: list[str]):
    service, database, drive, settings, _ = _service(tmp_path, [DiscScan()])
    settings.ffprobe_path = ""
    ripper = _Ripper(outcomes)
    service._make_mkv = lambda _settings=None: ripper  # type: ignore[method-assign]
    staging = tmp_path / "raw" / "job-1.partial"
    tracks = [{"source_id": 0, "duration_seconds": 6503, "selected": True}]
    job = database.get_job("job-1") or {}
    await service._rip_with_makemkv("job-1", drive, settings, staging, tracks, [0], job)
    return ripper, staging


@pytest.mark.asyncio
async def test_a_rip_that_lost_the_film_is_run_again(tmp_path: Path) -> None:
    ripper, staging = await _rip(tmp_path, ["lost", "saved"])

    assert ripper.runs == 2 and (staging / "title_t00.mkv").is_file()
    log = (tmp_path / "logs" / "job-1.log").read_text(encoding="utf-8")
    assert 'left out the 1:48:37 title "due to navigation error" when it looked at the disc again' in log


@pytest.mark.asyncio
async def test_a_rip_that_keeps_losing_the_film_fails_after_three_runs(tmp_path: Path) -> None:
    from discdock.processes import ProcessFailure

    with pytest.raises(ProcessFailure, match="ripping failed"):
        await _rip(tmp_path, ["lost"])


@pytest.mark.asyncio
async def test_a_stopped_rip_is_not_run_again(tmp_path: Path) -> None:
    from discdock.processes import ProcessFailure

    # Cancelled, for example to switch to the damaged-disc rescue: that is not MakeMKV losing the film.
    with pytest.raises(ProcessFailure):
        await _rip(tmp_path, ["cancelled", "saved"])
