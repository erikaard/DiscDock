from __future__ import annotations

import asyncio

import pytest

from discdock.database import Database
from discdock.makemkv import MAKEMKV_LICENSE_ACTION, MakeMKVLicenseError
from discdock.models import DiscKind, DriveInfo
from discdock.processes import ProcessResult
from discdock.secrets import SecretStore
from discdock.settings import AppSettings, SettingsStore
from discdock.workflow import DiscDockService


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True])
async def test_workflow_surfaces_makemkv_license_action_as_blocked(
    tmp_path, monkeypatch, resume: bool
) -> None:
    executable = tmp_path / "makemkvcon64.exe"
    executable.write_bytes(b"stub")
    settings = AppSettings(data_root=tmp_path, make_mkv_path=str(executable))
    settings_store = SettingsStore(tmp_path / "config" / "settings.json")
    settings_store.save(settings)
    database = Database(tmp_path / "database" / "discdock.db")
    database.initialize()
    service = DiscDockService(
        settings_store,
        SecretStore(tmp_path / "config" / "secrets.bin"),
        database,
    )
    drive = DriveInfo(
        id="drive-1",
        letter="D:",
        name="Reader",
        media_loaded=True,
        volume_label="MOVIE",
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

    class LicenseClient:
        async def inspect(self, *_args, **_kwargs):
            result = ProcessResult(args=[str(executable)], return_code=1, timed_out=True)
            raise MakeMKVLicenseError(MAKEMKV_LICENSE_ACTION, result)

    async def notification_stub(*_args, **_kwargs) -> bool:
        return True

    monkeypatch.setattr(service, "_make_mkv", lambda _settings=None: LicenseClient())
    monkeypatch.setattr(service.notifications, "send", notification_stub)

    if resume:
        await service._resume_job("job-1", drive)
    else:
        await service._process_job("job-1")
    await asyncio.sleep(0)

    job = database.get_job("job-1")
    assert job is not None
    assert job["state"] == "blocked"
    assert job["stage"] == "blocked"
    assert job["status_detail"] == "Action required"
    assert job["error_code"] == "makemkv_license"
    assert job["error_message"] == MAKEMKV_LICENSE_ACTION
    assert job["recoverable"] is True
