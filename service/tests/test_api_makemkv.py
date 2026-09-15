from __future__ import annotations

from pathlib import Path

import pytest

from discdock import api as api_module


def test_makemkv_gui_is_resolved_next_to_configured_console(tmp_path: Path, monkeypatch) -> None:
    installation = tmp_path / "MakeMKV"
    installation.mkdir()
    console = installation / "makemkvcon64.exe"
    gui = installation / "makemkv.exe"
    console.touch()
    gui.touch()
    monkeypatch.setattr(api_module.shutil, "which", lambda _: None)

    assert api_module._make_mkv_gui_path(str(console)) == gui.resolve()


@pytest.mark.asyncio
async def test_open_makemkv_uses_safe_argument_vector(tmp_path: Path, monkeypatch) -> None:
    installation = tmp_path / "MakeMKV"
    installation.mkdir()
    console = installation / "makemkvcon64.exe"
    gui = installation / "makemkv.exe"
    console.touch()
    gui.touch()
    captured: dict = {}
    events: list[tuple[str | None, str, dict]] = []

    def fake_start(args: list[str], **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(api_module.SERVICE.settings, "make_mkv_path", str(console))
    monkeypatch.setattr(api_module, "start_external_process", fake_start)
    monkeypatch.setattr(
        api_module.DATABASE,
        "append_event",
        lambda job_id, event_type, payload: events.append((job_id, event_type, payload)),
    )

    result = await api_module.open_makemkv()

    assert result["ok"] is True
    assert "close it" in result["message"]
    assert captured["args"] == [str(gui.resolve())]
    assert captured["kwargs"]["cwd"] == str(gui.resolve().parent)
    assert captured["kwargs"]["close_fds"] is True
    assert "shell" not in captured["kwargs"]
    assert events == [(None, "tool.makemkv_opened", {"path": str(gui.resolve())})]
