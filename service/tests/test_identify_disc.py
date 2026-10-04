from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from test_workflow_recovery import make_job, make_service

from discdock.makemkv import DiscScan
from discdock.models import DiscKind, DriveInfo, MediaKind, MetadataCandidate, TitleInfo
from discdock.settings import AppSettings


def _film(provider_id: str, title: str, year: str, runtime: int) -> MetadataCandidate:
    return MetadataCandidate(
        provider="omdb", provider_id=provider_id, title=title, year=year, media_kind=MediaKind.MOVIE,
        runtime_minutes=runtime,
    )


class _Metadata:
    def __init__(self, answer: MetadataCandidate | None = None, error: Exception | None = None) -> None:
        self.answer = answer
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def identify(self, label, media_kind=None, disc_minutes=None, disc_name=""):
        self.calls.append({"label": label, "disc_minutes": disc_minutes, "disc_name": disc_name})
        if self.error:
            raise self.error
        return self.answer


def _setup(tmp_path: Path, metadata: _Metadata):
    settings = AppSettings(data_root=tmp_path)
    service, _ = make_service(settings, make_job(settings, tmp_path / "staging"))
    service.metadata = metadata  # type: ignore[assignment]
    drive = DriveInfo(id="drive-id", letter="D:", name="Drive", volume_label="CATS_AND_DOGS", disc_kind=DiscKind.DVD)
    scan = DiscScan(
        disc_name="Cats & Dogs",
        titles=[TitleInfo(id=0, duration_seconds=5010), TitleInfo(id=1, duration_seconds=834)],
    )
    log = settings.resolved_directories()["logs"] / "job-id.log"
    return service, drive, scan, log


async def _finished(value: MetadataCandidate | None) -> MetadataCandidate | None:
    return value


async def test_the_scan_decides_with_the_discs_name_and_running_times(tmp_path: Path) -> None:
    film = _film("tt0239395", "Cats & Dogs", "2001", 87)
    metadata = _Metadata(film)
    service, drive, scan, log = _setup(tmp_path, metadata)
    early = asyncio.create_task(_finished(_film("tt0142154", "Cats and Dogs", "1932", 7)))

    found = await service._identify_disc("job-id", drive, scan, None, "Cats And Dogs", early)

    assert found is film
    assert metadata.calls == [
        {"label": "CATS_AND_DOGS", "disc_minutes": [83.5, 13.9], "disc_name": "Cats & Dogs"}
    ]
    assert "The label alone suggested Cats and Dogs (1932, 7 min); the disc says Cats & Dogs (2001" in (
        log.read_text(encoding="utf-8")
    )


async def test_a_failed_lookup_leaves_the_disc_unidentified_instead_of_failing_the_job(tmp_path: Path) -> None:
    metadata = _Metadata(error=RuntimeError("OMDb could not be reached"))
    service, drive, scan, log = _setup(tmp_path, metadata)

    async def failing() -> MetadataCandidate | None:
        raise RuntimeError("OMDb could not be reached")

    early = asyncio.create_task(failing())

    assert await service._identify_disc("job-id", drive, scan, None, "Cats And Dogs", early) is None
    assert log.read_text(encoding="utf-8").count("Looking the disc up on OMDb failed") == 2


async def test_without_a_scan_the_label_lookup_stands(tmp_path: Path) -> None:
    film = _film("tt0239395", "Cats & Dogs", "2001", 87)
    metadata = _Metadata()
    service, drive, _, _ = _setup(tmp_path, metadata)

    found = await service._identify_disc(
        "job-id", drive, None, None, "Cats And Dogs", asyncio.create_task(_finished(film))
    )

    assert found is film and metadata.calls == []
