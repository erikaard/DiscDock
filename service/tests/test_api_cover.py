from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from discdock import api as api_module
from discdock.settings import AppSettings


def job_with_photos(tmp_path: Path, front: Path, back: Path | None = None) -> dict:
    metadata = {"album_cover": {"file": str(front), "added_at": "2026-09-15T14:00:00+00:00"}}
    if back:
        metadata["album_back_photo"] = {"file": str(back), "added_at": "2026-09-15T14:01:00+00:00"}
    return {"id": "job-id", "settings": AppSettings(data_root=tmp_path).public_dict(), "metadata": metadata}


@pytest.mark.asyncio
async def test_the_photos_of_the_case_are_served_for_the_dashboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    front = raw / "job-id.cover.jpg"
    front.write_bytes(b"\xff\xd8\xff front")
    back = raw / "job-id.back.png"
    back.write_bytes(b"\x89PNG\r\n\x1a\n back")
    monkeypatch.setattr(api_module, "_job_or_404", lambda job_id: job_with_photos(tmp_path, front, back))

    cover = await api_module.album_photo("job-id", "front")
    case_back = await api_module.album_photo("job-id", "back")

    assert (Path(cover.path), cover.media_type) == (front.resolve(), "image/jpeg")
    assert (Path(case_back.path), case_back.media_type) == (back.resolve(), "image/png")


@pytest.mark.asyncio
async def test_only_photos_in_the_raw_folder_are_served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    elsewhere = tmp_path / "private.jpg"
    elsewhere.write_bytes(b"\xff\xd8\xff")
    monkeypatch.setattr(api_module, "_job_or_404", lambda job_id: job_with_photos(tmp_path, elsewhere))

    for side in ("front", "back"):
        with pytest.raises(HTTPException) as refused:
            await api_module.album_photo("job-id", side)
        assert refused.value.status_code == 404
