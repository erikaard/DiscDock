from __future__ import annotations

import pytest

import discdock.workflow as workflow_module


@pytest.fixture(autouse=True)
def _no_real_disc(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests away from the computer's own drives.

    Workflow tests use drive letters such as D:, which on a ripping station hold a real
    disc, perhaps in the middle of a rescue. A test that needs a disc's IFO files gives
    them itself by patching ``dvd_title_contents`` again.
    """
    monkeypatch.setattr(workflow_module, "dvd_title_contents", lambda folder: {})
