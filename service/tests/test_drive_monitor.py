from __future__ import annotations

import pytest

from discdock import drives as drive_module
from discdock.drives import DriveMonitor
from discdock.models import DriveInfo


@pytest.mark.asyncio
async def test_insertion_callback_can_reconcile_without_recursion(monkeypatch) -> None:
    drive = DriveInfo(id="drive", letter="D:", name="Reader", media_loaded=True)
    snapshots = []
    insertions = []
    monitor = None

    async def enumerate_stub():
        return [drive.model_copy(deep=True)]

    async def on_inserted(item):
        insertions.append(item.id)
        await monitor.reconcile()

    async def on_removed(_item):
        raise AssertionError("unexpected removal")

    async def on_snapshot(items):
        snapshots.append(len(items))

    monkeypatch.setattr(drive_module, "enumerate_drives", enumerate_stub)
    monitor = DriveMonitor(3, on_inserted, on_removed, on_snapshot)
    await monitor.reconcile()
    assert insertions == ["drive"]
    assert snapshots == [1, 1]
