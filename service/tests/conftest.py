from __future__ import annotations

import pytest

import discdock.workflow as workflow_module


@pytest.fixture(autouse=True)
def _no_real_disc(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests away from the computer's own drives.

    Workflow tests use drive letters such as D:, which on a ripping station hold a real
    disc, perhaps in the middle of a rescue. A test that needs a disc's IFO files gives
    them itself by patching ``dvd_title_contents``, ``dvd_titles_in_folder`` or
    ``dvd_folder_video_scrambled`` again.
    """
    monkeypatch.setattr(workflow_module, "dvd_title_contents", lambda folder: {})
    monkeypatch.setattr(workflow_module, "dvd_titles_in_folder", lambda folder: [], raising=False)
    monkeypatch.setattr(workflow_module, "dvd_folder_video_scrambled", lambda folder, title_set: True, raising=False)
