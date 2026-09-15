from __future__ import annotations

import sys

import pytest

from discdock import tray


def make_actions(busy: list[str], answer: bool) -> tuple[tray.TrayActions, list[str], list[str]]:
    events: list[str] = []
    questions: list[str] = []

    def ask(question: str) -> bool:
        questions.append(question)
        return answer

    actions = tray.TrayActions(
        busy_jobs=lambda: busy,
        ask=ask,
        request_exit=lambda: events.append("exit"),
        start_successor=lambda: events.append("successor"),
        open_dashboard=lambda: events.append("dashboard"),
    )
    return actions, events, questions


def test_restart_and_stop_go_ahead_when_nothing_is_ripping() -> None:
    actions, events, questions = make_actions([], answer=False)

    assert actions.restart() is True
    assert actions.stop() is True
    actions.open()

    assert events == ["successor", "exit", "exit", "dashboard"]
    assert questions == []


def test_a_rip_in_progress_is_only_interrupted_after_asking() -> None:
    actions, events, questions = make_actions(["Various Artist - Stemninger"], answer=False)

    assert actions.stop() is False
    assert actions.restart() is False
    assert events == []
    assert "Various Artist - Stemninger" in questions[0]
    assert questions[1].endswith("Restart DiscDock anyway?")

    actions, events, _ = make_actions(["Various Artist - Stemninger"], answer=True)
    assert actions.restart() is True
    assert events == ["successor", "exit"]


def test_the_restarted_discdock_waits_for_the_old_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Programs\DiscDock\DiscDock.exe")
    assert tray.successor_command(4321) == [r"C:\Programs\DiscDock\DiscDock.exe", "--background", "--after", "4321"]

    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert tray.successor_command(4321)[1:] == ["-m", "discdock", "--background", "--after", "4321"]


def test_the_icon_is_the_discdock_logo() -> None:
    path = tray.icon_path()

    assert path.name == "discdock.ico"
    assert path.read_bytes()[:4] == b"\0\0\1\0", "an icon file"
