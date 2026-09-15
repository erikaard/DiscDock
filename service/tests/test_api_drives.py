from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from discdock import api as api_module
from discdock.models import DiscKind, DriveInfo


@pytest.mark.asyncio
async def test_a_disc_discdock_is_reading_is_not_opened_in_vlc(monkeypatch: pytest.MonkeyPatch) -> None:
    drive = DriveInfo(id="drive-id", letter="D:", name="Drive", media_loaded=True, disc_kind=DiscKind.DVD)
    opened: list[str] = []
    job: dict[str, Any] = {"id": "job-id", "state": "ripping"}
    monkeypatch.setattr(api_module.SERVICE, "drives", {drive.id: drive}, raising=False)
    monkeypatch.setattr(api_module.SERVICE, "active_job_for_drive", lambda drive_id: job)
    control = SimpleNamespace(preview=lambda letter, *_: opened.append(letter))
    monkeypatch.setattr(api_module.SERVICE, "drive_control", control, raising=False)

    for state in ("inspecting", "ripping", "cancelling"):
        job["state"] = state
        with pytest.raises(HTTPException) as refused:
            await api_module.preview_drive(drive.id)
        assert refused.value.status_code == 409, state
    assert opened == []

    job["state"] = "awaiting_input"
    assert await api_module.preview_drive(drive.id) == {"ok": True}, "the disc is not read while titles are chosen"
    assert opened == ["D:"]
