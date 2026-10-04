from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from discdock import api as api_module


@pytest.mark.asyncio
async def test_setup_can_ask_discdock_to_close_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    closing: list[bool] = []
    monkeypatch.setattr(api_module, "_close_after_answering", lambda: closing.append(True))
    monkeypatch.setattr(api_module.SERVICE, "busy_jobs", list)

    with pytest.raises(HTTPException) as refused:
        await api_module.shutdown(x_discdock_request="")
    assert refused.value.status_code == 403
    assert closing == [], "a request without the header, such as a web page's, is refused"

    assert await api_module.shutdown(x_discdock_request="close") == {"closing": True}
    assert closing == [True]


@pytest.mark.asyncio
async def test_discdock_does_not_close_while_it_works_on_a_disc(monkeypatch: pytest.MonkeyPatch) -> None:
    closing: list[bool] = []
    monkeypatch.setattr(api_module, "_close_after_answering", lambda: closing.append(True))
    monkeypatch.setattr(api_module.SERVICE, "busy_jobs", lambda: ["Silje Nergaard - Brevet"])

    with pytest.raises(HTTPException) as refused:
        await api_module.shutdown(x_discdock_request="close")

    assert refused.value.status_code == 409
    assert "Silje Nergaard - Brevet" in refused.value.detail
    assert closing == []


async def test_an_open_dashboard_stream_ends_when_discdock_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    class StillConnected:
        async def is_disconnected(self) -> bool:
            return False

    monkeypatch.setattr(api_module.DATABASE, "events_after", lambda cursor: [])
    monkeypatch.setattr(api_module, "STREAMS_CLOSING", asyncio.Event())
    stream = api_module._event_stream(0, StillConnected())
    assert await stream.__anext__() == ": keepalive\n\n"

    api_module.STREAMS_CLOSING.set()

    # Within a second the stream ends, so uvicorn can stop without its ten-second wait.
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(stream.__anext__(), timeout=3)
