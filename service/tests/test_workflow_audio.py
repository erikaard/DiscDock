from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import discdock.workflow as workflow_module
from discdock.models import ACTIVE_JOB_STATES, DiscKind, DriveInfo, JobState, MediaKind
from discdock.musicbrainz import AlbumRelease, AlbumTrack, MusicBrainzBusy
from discdock.settings import AppSettings
from discdock.workflow import DiscDockService

DISCID = "ybzdi.je6cuccUFvEROdOIHCLlQ-"


def release(release_id: str, disambiguation: str) -> AlbumRelease:
    return AlbumRelease(
        id=release_id,
        title="Into the Great Wide Open",
        artist="Tom Petty and the Heartbreakers",
        date="1991",
        country="XE",
        disambiguation=disambiguation,
        track_count=1,
        tracks=[AlbumTrack(position=1, title="Learning to Fly")],
    )


FIRST = release("403427d8-6201-4831-a346-f7d910eead70", "BIEM / MCPS")
SECOND = release("338e72dc-1cf5-4757-b874-a8cab8aa877a", "GEMA / BIEM")


class Database:
    """Keeps one job the way the real database does, with its JSON columns read back."""

    def __init__(self, job: dict[str, Any]) -> None:
        self.job = job

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return self.job if job_id == self.job["id"] else None

    def update_job(self, job_id: str, **changes: Any) -> dict[str, Any]:
        assert job_id == self.job["id"]
        self.job.update(changes)
        for field in ("metadata", "settings"):
            if f"{field}_json" in changes:
                self.job[field] = json.loads(changes[f"{field}_json"])
        return self.job

    def list_tracks(self, job_id: str) -> list[dict[str, Any]]:
        del job_id
        return []

    def query(self, sql: str, parameters: tuple = ()) -> list[dict[str, Any]]:
        del sql, parameters
        return []

    def append_event(self, *args: Any) -> None:
        del args


class Notifications:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, job_id: str | None, event_type: str, title: str, body: str) -> bool:
        del job_id, title
        self.sent.append((event_type, body))
        return True


class FakeMusicBrainz:
    def __init__(self, disc_answers: list[Any], releases: dict[str, Any] | None = None) -> None:
        self.disc_answers = disc_answers
        self.releases = releases or {}
        self.disc_lookups = 0
        self.search_results: list[AlbumRelease] = []

    async def releases_for_disc(self, discid: str, track_count: int = 0) -> list[AlbumRelease]:
        del discid, track_count
        self.disc_lookups += 1
        answer = self.disc_answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def release(self, release_id: str, discid: str = "", track_count: int = 0) -> AlbumRelease:
        del discid, track_count
        answer = self.releases[release_id]
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def front_cover(self, release_id: str) -> bytes | None:
        del release_id
        return None

    async def search(self, text: str) -> list[AlbumRelease]:
        del text
        return list(self.search_results)

    async def search_barcode(self, barcode: str) -> list[AlbumRelease]:
        del barcode
        return list(self.search_results)


def make_service(tmp_path: Path, musicbrainz: FakeMusicBrainz, **job: Any) -> tuple[DiscDockService, Database]:
    settings = AppSettings(data_root=tmp_path, auto_eject=False)
    settings.resolved_directories()["logs"].mkdir(parents=True, exist_ok=True)
    database = Database(
        {
            "id": "job-id",
            "drive_id": "drive-id",
            "drive_letter": "E:",
            "disc_label": "",
            "disc_type": DiscKind.AUDIO_CD.value,
            "fingerprint": "",
            "title": "",
            "year": "",
            "media_kind": MediaKind.MUSIC.value,
            "state": JobState.RIPPING,
            "stage": "ripping",
            "status_detail": "",
            "staging_path": str(tmp_path / "raw" / "cd.partial"),
            "settings": settings.public_dict(),
            "metadata": {"cd": {"discid": DISCID, "tracks": 1}},
            **job,
        }
    )
    service = DiscDockService.__new__(DiscDockService)
    service.database = database
    service.settings = settings
    service.secret_store = SimpleNamespace(all=dict)
    service.notifications = Notifications()
    service.drive_control = SimpleNamespace()
    service.runner = object()
    service.musicbrainz = musicbrainz
    return service, database


@pytest.fixture
def tagged(monkeypatch: pytest.MonkeyPatch) -> list[AlbumRelease]:
    """The releases the tracks were tagged with; the files are renamed instead of running FFmpeg."""
    releases: list[AlbumRelease] = []

    async def tag_album(runner: Any, owner: str, ffmpeg: str, folder: Path, chosen: AlbumRelease, cover: Any) -> int:
        del runner, owner, ffmpeg, cover
        releases.append(chosen)
        for path in folder.glob("*.flac"):
            path.rename(folder / f"01 - {chosen.tracks[0].title}.flac")
        return len(chosen.tracks)

    monkeypatch.setattr(workflow_module, "tag_album", tag_album)
    return releases


@pytest.mark.asyncio
async def test_a_cd_musicbrainz_knows_gets_its_album_while_it_rips(tmp_path: Path) -> None:
    service, database = make_service(tmp_path, FakeMusicBrainz([[FIRST]]))

    await service._look_up_album("job-id")

    assert database.job["metadata"]["album"]["id"] == FIRST.id
    assert database.job["title"] == "Tom Petty and the Heartbreakers - Into the Great Wide Open"
    assert database.job["year"] == "1991"
    assert database.job["state"] == JobState.RIPPING


@pytest.mark.asyncio
async def test_another_cd_than_in_an_earlier_attempt_is_looked_up_again(tmp_path: Path) -> None:
    earlier_cd = {
        "cd": {"discid": "bevDpSptpSYY3kX7q5H.3Rw8KIY-", "tracks": 12},
        "album": SECOND.summary(),
        "album_lookup": {"status": "chosen", "message": ""},
    }
    musicbrainz = FakeMusicBrainz([[FIRST]])
    service, database = make_service(tmp_path, musicbrainz, metadata=earlier_cd, title="Silje Nergaard - Brevet")

    await service._remember_cd("job-id", DISCID, 1)
    assert "album" not in database.job["metadata"]
    assert database.job["title"] == ""

    await service._look_up_album("job-id")

    assert database.job["metadata"]["cd"] == {"discid": DISCID, "tracks": 1}
    assert database.job["metadata"]["album"]["id"] == FIRST.id
    assert musicbrainz.disc_lookups == 1


@pytest.mark.asyncio
async def test_the_same_cd_keeps_the_album_chosen_for_it(tmp_path: Path) -> None:
    musicbrainz = FakeMusicBrainz([])
    service, database = make_service(
        tmp_path, musicbrainz, metadata={"cd": {"discid": DISCID, "tracks": 1}, "album": SECOND.summary()}
    )

    await service._remember_cd("job-id", DISCID, 1)
    await service._look_up_album("job-id")

    assert database.job["metadata"]["album"]["id"] == SECOND.id
    assert musicbrainz.disc_lookups == 0


@pytest.mark.asyncio
async def test_several_releases_can_be_chosen_from_while_the_cd_keeps_ripping(tmp_path: Path) -> None:
    service, database = make_service(tmp_path, FakeMusicBrainz([[FIRST, SECOND]]))

    await service._look_up_album("job-id")

    metadata = database.job["metadata"]
    assert [candidate["id"] for candidate in metadata["album_candidates"]] == [FIRST.id, SECOND.id]
    assert metadata["album_lookup"]["status"] == "several"
    assert "album" not in metadata
    assert database.job["state"] == JobState.RIPPING, "the rip goes on"

    await service.choose_album("job-id", SECOND.summary())

    assert database.job["metadata"]["album"]["id"] == SECOND.id


@pytest.mark.asyncio
async def test_a_busy_musicbrainz_is_explained_and_asked_again_for_the_tags(
    tmp_path: Path, tagged: list[AlbumRelease]
) -> None:
    musicbrainz = FakeMusicBrainz([MusicBrainzBusy(503), [FIRST]], {FIRST.id: FIRST})
    service, database = make_service(tmp_path, musicbrainz)

    await service._look_up_album("job-id")

    lookup = database.job["metadata"]["album_lookup"]
    assert lookup["status"] == "busy"
    assert "503" in lookup["message"]
    assert "Find the album" in lookup["message"]

    staging = Path(database.job["staging_path"])
    (staging / "Unknown disc [FLAC]").mkdir(parents=True)
    (staging / "Unknown disc [FLAC]" / "01 - Unknown track.flac").write_bytes(b"audio")
    await service._tag_audio_cd("job-id", service.settings, staging)

    assert musicbrainz.disc_lookups == 2
    assert tagged == [FIRST]
    assert (staging / "01 - Learning to Fly.flac").read_bytes() == b"audio"
    assert database.job["metadata"]["album_lookup"]["status"] == "tagged"


@pytest.mark.asyncio
async def test_the_first_release_is_used_and_shown_when_none_was_chosen(
    tmp_path: Path, tagged: list[AlbumRelease]
) -> None:
    service, database = make_service(
        tmp_path,
        FakeMusicBrainz([], {FIRST.id: FIRST}),
        metadata={"cd": {"discid": DISCID, "tracks": 1}, "album_candidates": [FIRST.summary(), SECOND.summary()]},
    )
    staging = Path(database.job["staging_path"])
    staging.mkdir(parents=True)

    await service._tag_audio_cd("job-id", service.settings, staging)

    assert tagged == [FIRST]
    assert database.job["metadata"]["album"]["picked_first_of"] == 2


@pytest.mark.asyncio
async def test_a_cd_waits_in_staging_when_musicbrainz_is_busy_as_the_tags_are_written(
    tmp_path: Path, tagged: list[AlbumRelease]
) -> None:
    service, database = make_service(
        tmp_path,
        FakeMusicBrainz([], {FIRST.id: MusicBrainzBusy(503)}),
        metadata={"cd": {"discid": DISCID, "tracks": 1}, "album": FIRST.summary()},
    )
    staging = Path(database.job["staging_path"])
    staging.mkdir(parents=True)

    assert not await service._tag_audio_cd("job-id", service.settings, staging)

    assert tagged == []
    assert database.job["state"] == JobState.AWAITING_ALBUM
    assert "503" in database.job["status_detail"]
    assert service.notifications.sent[-1][0] == "attention"


@pytest.mark.asyncio
async def test_a_cd_waits_in_staging_when_musicbrainz_is_still_busy_at_the_end(
    tmp_path: Path, tagged: list[AlbumRelease]
) -> None:
    service, database = make_service(
        tmp_path,
        FakeMusicBrainz([MusicBrainzBusy(503)]),
        metadata={"cd": {"discid": DISCID, "tracks": 1}, "album_lookup": {"status": "busy", "message": "503"}},
    )
    staging = Path(database.job["staging_path"])
    staging.mkdir(parents=True)

    assert not await service._tag_audio_cd("job-id", service.settings, staging)

    assert tagged == []
    assert database.job["state"] == JobState.AWAITING_ALBUM
    assert JobState.AWAITING_ALBUM not in ACTIVE_JOB_STATES, "the next CD can go into the drive"


@pytest.mark.asyncio
async def test_a_cd_kept_in_staging_waits_for_an_album_musicbrainz_does_not_know(
    tmp_path: Path, tagged: list[AlbumRelease]
) -> None:
    unknown = {"cd": {"discid": DISCID, "tracks": 1}, "album_lookup": {"status": "not_found", "message": ""}}
    service, database = make_service(tmp_path, FakeMusicBrainz([]), metadata=unknown)
    staging = Path(database.job["staging_path"])
    staging.mkdir(parents=True)

    assert await service._tag_audio_cd("job-id", service.settings, staging), "without being asked it finishes"

    await service.keep_cd_in_staging("job-id", True)
    assert not await service._tag_audio_cd("job-id", service.settings, staging)
    assert database.job["state"] == JobState.AWAITING_ALBUM
    assert "not found" in database.job["status_detail"]
    assert tagged == []


@pytest.mark.asyncio
async def test_an_album_entered_by_hand_finishes_a_cd_kept_in_staging(tmp_path: Path, tagged: list[AlbumRelease]) -> None:
    busy = {"cd": {"discid": DISCID, "tracks": 1}, "album_lookup": {"status": "busy", "message": "503"}}
    service, database = make_service(tmp_path, FakeMusicBrainz([]), metadata=busy)
    await service.keep_cd_in_staging("job-id", True)
    await service.set_manual_album("job-id", "Various Artist", "Stemninger", "", ["Første spor"])
    staging = Path(database.job["staging_path"])
    staging.mkdir(parents=True)

    assert await service._tag_audio_cd("job-id", service.settings, staging)

    assert [release.title for release in tagged] == ["Stemninger"]


@pytest.mark.asyncio
async def test_a_waiting_cd_finishes_when_its_album_is_entered_by_hand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, database = make_service(tmp_path, FakeMusicBrainz([]), state=JobState.AWAITING_ALBUM)
    finished: list[str] = []

    async def finish_waiting_cd(job_id: str, *, without_album: bool = False) -> dict[str, Any]:
        assert not without_album
        finished.append(job_id)
        return database.job

    monkeypatch.setattr(service, "finish_waiting_cd", finish_waiting_cd)

    await service.set_manual_album("job-id", "Various Artist", "Stemninger", "", [])

    assert finished == ["job-id"]


@pytest.mark.asyncio
async def test_a_waiting_cd_is_named_tagged_and_moved_into_the_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tagged: list[AlbumRelease]
) -> None:
    waiting = {"cd": {"discid": DISCID, "tracks": 1}, "album": SECOND.summary(), "album_hold": True}
    service, database = make_service(
        tmp_path, FakeMusicBrainz([], {SECOND.id: SECOND}), state=JobState.AWAITING_ALBUM, metadata=waiting
    )
    moved: list[Path] = []

    async def finish_in_library(job_id: str, settings: AppSettings, staging: Path, final_source: Path, **kwargs: Any) -> None:
        del job_id, settings, staging, kwargs
        moved.append(final_source)

    monkeypatch.setattr(service, "_finish_in_library", finish_in_library)
    staging = Path(database.job["staging_path"])
    staging.mkdir(parents=True)

    await service._finish_waiting_cd("job-id", without_album=False)

    assert tagged == [SECOND]
    assert moved == [staging]


@pytest.mark.asyncio
async def test_only_a_waiting_cd_is_finished_and_only_with_an_album_unless_asked(tmp_path: Path) -> None:
    service, database = make_service(tmp_path, FakeMusicBrainz([]))

    with pytest.raises(RuntimeError, match="not waiting"):
        await service.finish_waiting_cd("job-id")

    database.job["state"] = JobState.AWAITING_ALBUM
    with pytest.raises(RuntimeError, match="album first"):
        await service.finish_waiting_cd("job-id")


@pytest.mark.asyncio
async def test_a_search_lists_cds_with_the_discs_number_of_tracks_first(tmp_path: Path) -> None:
    def found(release_id: str, format: str, track_count: int) -> AlbumRelease:
        return AlbumRelease(id=release_id, title="Into the Great Wide Open", format=format, track_count=track_count)

    digital = found("344987c5-02d2-4e53-8bb4-be2245212228", "Digital Media", 12)
    other_cd = found("c45159a1-aae9-4ad6-9dce-8d6bdd65cff8", "CD", 14)
    vinyl = found("d26e6e05-ab2a-4eae-819a-7f3d50f92c29", '12" Vinyl', 12)
    cd = found("71169154-8918-4a24-b0d7-f873a1974dd4", "CD", 12)
    musicbrainz = FakeMusicBrainz([])
    musicbrainz.search_results = [digital, other_cd, vinyl, cd]
    service, _ = make_service(tmp_path, musicbrainz)

    results = await service.search_albums("Tom Petty - Into the Great Wide Open", 12)

    assert [result["id"] for result in results] == [cd.id, other_cd.id, digital.id, vinyl.id]


@pytest.mark.asyncio
async def test_a_barcode_search_lists_the_cd_first(tmp_path: Path) -> None:
    digital = AlbumRelease(id="344987c5-02d2-4e53-8bb4-be2245212228", title="Brevet", format="Digital Media", track_count=12)
    cd = AlbumRelease(id="027fb0b9-db94-4592-bc85-4bf0e3211ad9", title="Brevet", format="CD", track_count=12)
    musicbrainz = FakeMusicBrainz([])
    musicbrainz.search_results = [digital, cd]
    service, _ = make_service(tmp_path, musicbrainz)

    results = await service.search_albums_by_barcode("7029971950223", 12)

    assert [result["id"] for result in results] == [cd.id, digital.id]


@pytest.mark.asyncio
async def test_an_album_typed_in_with_a_cover_photo_names_and_tags_the_tracks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, database = make_service(tmp_path, FakeMusicBrainz([]), metadata={"cd": {"discid": DISCID, "tracks": 3}})
    received: list[tuple[AlbumRelease, bytes | None]] = []

    async def tag_album(runner: Any, owner: str, ffmpeg: str, folder: Path, chosen: AlbumRelease, cover: Any) -> int:
        del runner, owner, ffmpeg, folder
        received.append((chosen, cover))
        return len(chosen.tracks)

    monkeypatch.setattr(workflow_module, "tag_album", tag_album)

    await service.set_album_photo("job-id", "front_original", b"\xff\xd8\xff\xe0 the photo as taken")
    await service.set_album_photo("job-id", "front", b"\xff\xd8\xff\xe0 photo of the front")
    await service.set_album_photo("job-id", "back", b"\xff\xd8\xff\xe0 photo of the back")
    await service.set_manual_album("job-id", "Silje Nergaard", "Brevet", "1995", ["Brevet", "  Når hun   skal hjem "])
    staging = Path(database.job["staging_path"])
    staging.mkdir(parents=True)
    await service._tag_audio_cd("job-id", service.settings, staging)

    chosen, cover = received[0]
    assert (chosen.id, chosen.artist, chosen.title, chosen.date) == ("", "Silje Nergaard", "Brevet", "1995")
    assert [(track.position, track.title) for track in chosen.tracks] == [
        (1, "Brevet"),
        (2, "Når hun skal hjem"),
        (3, "Track 03"),
    ]
    assert cover == b"\xff\xd8\xff\xe0 photo of the front"
    assert database.job["title"] == "Silje Nergaard - Brevet"
    assert database.job["metadata"]["album"]["source"] == "manual"
    assert not (tmp_path / "raw" / "job-id.cover.jpg").exists(), "the photo is in the album folder now"
    assert not (tmp_path / "raw" / "job-id.back.jpg").exists(), "the photo of the back is not needed any more"
    assert not (tmp_path / "raw" / "job-id.cover-original.jpg").exists(), "neither is the unedited photo"


@pytest.mark.asyncio
async def test_a_cover_photo_must_be_a_picture(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path, FakeMusicBrainz([]))

    with pytest.raises(ValueError, match="JPEG or PNG"):
        await service.set_album_photo("job-id", "front", b"<html>not a picture</html>")


@pytest.mark.asyncio
async def test_a_photo_of_the_case_can_be_replaced_and_removed(tmp_path: Path) -> None:
    service, database = make_service(tmp_path, FakeMusicBrainz([]))

    await service.set_album_photo("job-id", "back", b"\xff\xd8\xff first photo")
    await service.set_album_photo("job-id", "back", b"\x89PNG\r\n\x1a\n closer photo")

    path = workflow_module.album_photo_path(database.job, "back")
    assert path is not None and path.name == "job-id.back.png"
    assert path.read_bytes().endswith(b"closer photo")
    assert not path.with_suffix(".jpg").exists(), "the first photo is replaced"

    await service.remove_album_photo("job-id", "back")

    assert "album_back_photo" not in database.job["metadata"]
    assert not path.exists()


@pytest.mark.asyncio
async def test_removing_the_cover_also_forgets_the_photo_it_was_edited_from(tmp_path: Path) -> None:
    service, database = make_service(tmp_path, FakeMusicBrainz([]))
    await service.set_album_photo("job-id", "front_original", b"\xff\xd8\xff the photo as taken")
    await service.set_album_photo("job-id", "front", b"\xff\xd8\xff the edited cover")
    original = workflow_module.album_photo_path(database.job, "front_original")
    assert original is not None and original.name == "job-id.cover-original.jpg"

    await service.remove_album_photo("job-id", "front")

    assert not {"album_cover", "album_cover_original"} & set(database.job["metadata"])
    assert not original.exists()


@pytest.mark.asyncio
async def test_only_an_audio_cd_has_its_table_of_contents_read(monkeypatch: pytest.MonkeyPatch) -> None:
    letters: list[str] = []

    def read_disc_id(letter: str) -> str:
        letters.append(letter)
        return DISCID

    monkeypatch.setattr(workflow_module, "read_disc_id", read_disc_id)
    cd = DriveInfo(
        id="cd", letter="E:", name="Drive", media_loaded=True, volume_label="Audio CD", disc_kind=DiscKind.AUDIO_CD
    )
    dvd = cd.model_copy(update={"id": "dvd", "volume_label": "MOVIE", "disc_kind": DiscKind.DVD})

    assert await DiscDockService._disc_id_for(cd) == DISCID
    assert await DiscDockService._disc_id_for(dvd) == ""
    assert letters == ["E:"]


@pytest.mark.asyncio
async def test_an_album_is_only_chosen_for_a_cd_whose_tracks_are_not_named_yet(tmp_path: Path) -> None:
    service, database = make_service(tmp_path, FakeMusicBrainz([]), state=JobState.COMPLETED)

    with pytest.raises(RuntimeError, match="already named"):
        await service.choose_album("job-id", FIRST.summary())

    database.job.update(state=JobState.RIPPING, disc_type=DiscKind.DVD.value)
    with pytest.raises(RuntimeError, match="audio CD"):
        await service.choose_album("job-id", FIRST.summary())


@pytest.mark.asyncio
async def test_a_cd_is_ripped_and_named_after_its_album(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tagged: list[AlbumRelease]
) -> None:
    service, database = make_service(tmp_path, FakeMusicBrainz([[FIRST]], {FIRST.id: FIRST}), metadata={})

    class FakeAudioRipper:
        def __init__(self, executable: str, runner: object) -> None:
            del executable, runner

        async def rip(self, job_id: str, letter: str, destination: Path, callback: Any = None, **kwargs: Any) -> None:
            del job_id, letter, kwargs
            await callback({"type": "cd", "discid": DISCID, "tracks": 1})
            folder = destination / "Unknown disc [FLAC]"
            folder.mkdir(parents=True)
            (folder / "01 - Unknown track.flac").write_bytes(b"audio")

    async def verified(folder: Path, ffprobe_path: str) -> list[Path]:
        del ffprobe_path
        return list(folder.rglob("*.flac"))

    monkeypatch.setattr(workflow_module, "AudioRipper", FakeAudioRipper)
    monkeypatch.setattr(workflow_module, "verify_outputs", verified)
    monkeypatch.setattr(workflow_module, "disk_space_ok", lambda path, required: True)
    drive = DriveInfo(
        id="drive-id", letter="E:", name="HL-DT-ST BD-RE BU40N", media_loaded=True, volume_label="", disc_kind=DiscKind.AUDIO_CD
    )
    leftover = Path(database.job["staging_path"]) / "Unknown disc (BEVD) [FLAC]"
    leftover.mkdir(parents=True)
    (leftover / "01 - Unknown track.flac").write_bytes(b"a track of another CD")

    await service._rip_and_finish("job-id", drive, service.settings)

    output = Path(database.job["output_path"])
    assert output.name == "Tom Petty and the Heartbreakers - Into the Great Wide Open (1991)"
    assert (output / "01 - Learning to Fly.flac").read_bytes() == b"audio"
    assert [path.name for path in output.rglob("*.flac")] == ["01 - Learning to Fly.flac"], "no tracks of an earlier attempt"
    assert tagged == [FIRST]


@pytest.mark.asyncio
async def test_a_cd_musicbrainz_cannot_name_waits_in_staging_instead_of_the_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tagged: list[AlbumRelease]
) -> None:
    service, database = make_service(tmp_path, FakeMusicBrainz([MusicBrainzBusy(503), MusicBrainzBusy(503)]), metadata={})

    class FakeAudioRipper:
        def __init__(self, executable: str, runner: object) -> None:
            del executable, runner

        async def rip(self, job_id: str, letter: str, destination: Path, callback: Any = None, **kwargs: Any) -> None:
            del job_id, letter, kwargs
            await callback({"type": "cd", "discid": DISCID, "tracks": 1})
            folder = destination / "Unknown disc [FLAC]"
            folder.mkdir(parents=True)
            (folder / "01 - Unknown track.flac").write_bytes(b"audio")

    async def verified(folder: Path, ffprobe_path: str) -> list[Path]:
        del ffprobe_path
        return list(folder.rglob("*.flac"))

    monkeypatch.setattr(workflow_module, "AudioRipper", FakeAudioRipper)
    monkeypatch.setattr(workflow_module, "verify_outputs", verified)
    monkeypatch.setattr(workflow_module, "disk_space_ok", lambda path, required: True)
    drive = DriveInfo(id="drive-id", letter="E:", name="Drive", media_loaded=True, disc_kind=DiscKind.AUDIO_CD)

    await service._rip_and_finish("job-id", drive, service.settings)

    assert database.job["state"] == JobState.AWAITING_ALBUM
    assert not database.job.get("output_path")
    assert [path.name for path in Path(database.job["staging_path"]).rglob("*.flac")] == ["01 - Unknown track.flac"]
    assert tagged == []
