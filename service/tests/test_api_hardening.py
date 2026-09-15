from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import HTTPException
from starlette.testclient import TestClient

from discdock import api as api_module


def test_every_response_forbids_framing_and_type_guessing() -> None:
    response = TestClient(api_module.app, base_url="http://127.0.0.1:8199").get("/api/v1/no-such-route")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


class ChunkedUpload:
    """A request body sent without Content-Length, a megabyte at a time."""

    def __init__(self, megabytes: int) -> None:
        self.headers: dict[str, str] = {}
        self.megabytes = megabytes

    async def stream(self) -> AsyncIterator[bytes]:
        for _ in range(self.megabytes):
            yield b"\xff" * (1024 * 1024)


@pytest.mark.asyncio
async def test_a_photo_without_content_length_is_still_limited_to_10_mb(monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[int] = []

    async def set_album_photo(job_id: str, side: str, data: bytes) -> dict:
        del job_id, side
        received.append(len(data))
        return {}

    monkeypatch.setattr(api_module.SERVICE, "set_album_photo", set_album_photo)

    with pytest.raises(HTTPException) as refused:
        await api_module.set_album_photo("job-id", "front", ChunkedUpload(12))
    assert refused.value.status_code == 413
    assert received == [], "the photo is refused before it reaches the service"

    await api_module.set_album_photo("job-id", "front", ChunkedUpload(2))
    assert received == [2 * 1024 * 1024]
