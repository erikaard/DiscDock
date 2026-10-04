from __future__ import annotations

import asyncio

import pytest

from discdock.database import Database
from discdock.disc_rescue import DRIVE_RESET_ADVICE
from discdock.makemkv import NoVideoTitles
from discdock.models import DiscKind, DriveInfo
from discdock.processes import ProcessResult
from discdock.secrets import SecretStore
from discdock.settings import AppSettings, SettingsStore
from discdock.workflow import DiscDockService

# What MakeMKV printed when the SE-506CB dropped off USB while scanning LILOSTICH.
LOST_DRIVE = [
    (
        'MSG:2003,0,3,"Error \'OS error - STATUS_DEVICE_NOT_CONNECTED\' occurred while reading '
        "'BD-RE TSSTcorp BDDVDW SE-506CB TS02' at offset '2291126272'\""
    ),
    'MSG:3015,0,2,"Title #1 (1:21:47) was skipped due to navigation error"',
    'MSG:5010,0,0,"Failed to open disc","Failed to open disc"',
    "TCOUNT:0",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True])
async def test_a_drive_that_drops_out_mid_scan_is_reported_as_a_drive_problem(
    tmp_path, monkeypatch, resume: bool
) -> None:
    executable = tmp_path / "makemkvcon64.exe"
    executable.write_bytes(b"stub")
    settings = AppSettings(data_root=tmp_path, make_mkv_path=str(executable), omdb_enabled=False)
    settings_store = SettingsStore(tmp_path / "config" / "settings.json")
    settings_store.save(settings)
    database = Database(tmp_path / "database" / "discdock.db")
    database.initialize()
    service = DiscDockService(settings_store, SecretStore(tmp_path / "config" / "secrets.bin"), database)
    drive = DriveInfo(
        id="drive-1", letter="D:", name="Reader", media_loaded=True, volume_label="LILOSTICH",
        disc_kind=DiscKind.DVD,
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

    class LostDriveClient:
        async def inspect(self, *_args, **_kwargs):
            raise NoVideoTitles("MakeMKV found no video titles", ProcessResult(["makemkvcon64"], 0, LOST_DRIVE))

    async def notification_stub(*_args, **_kwargs) -> bool:
        return True

    monkeypatch.setattr(service, "_make_mkv", lambda _settings=None: LostDriveClient())
    monkeypatch.setattr(service.notifications, "send", notification_stub)

    if resume:
        await service._resume_job("job-1", drive)
    else:
        await service._process_job("job-1")
    await asyncio.sleep(0)

    job = database.get_job("job-1")
    assert job is not None
    assert job["state"] == "failed" and job["recoverable"] == 1, "it resumes when the drive comes back"
    assert job["error_code"] == "drive_not_responding"
    assert job["status_detail"] == "The drive stopped responding"
    assert DRIVE_RESET_ADVICE in job["error_message"]
