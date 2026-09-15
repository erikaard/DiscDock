from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from .database import Database, utc_now
from .secrets import SecretStore


class NotificationService:
    def __init__(self, database: Database, secrets: SecretStore, settings: Callable[[], Any]):
        self.database = database
        self.secrets = secrets
        self._settings = settings
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self._retry_loop(), name="notification-outbox")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            await self._task

    def _enqueue(self, job_id: str | None, event_type: str, title: str, body: str) -> int:
        existing = self.database.query(
            "SELECT id FROM notification_outbox WHERE job_id IS ? AND event_type=? LIMIT 1",
            (job_id, event_type),
        )
        if existing:
            outbox_id = int(existing[0]["id"])
            self.database.execute(
                "UPDATE notification_outbox SET title=?,body=?,state='pending',next_attempt_at=NULL,last_error=NULL WHERE id=?",
                (title, body, outbox_id),
            )
            return outbox_id
        return self.database.execute(
            """INSERT INTO notification_outbox(job_id,event_type,title,body,state,created_at)
               VALUES(?,?,?,?,?,?)""",
            (job_id, event_type, title, body, "pending", utc_now()),
        )

    async def send(
        self, job_id: str | None, event_type: str, title: str, body: str, *, force: bool = False
    ) -> bool:
        outbox_id = self._enqueue(job_id, event_type, title, body)
        settings = self._settings()
        enabled = force or (
            bool(settings.notifications_enabled) and event_type in set(settings.notification_events)
        )
        if not enabled:
            self.database.execute("UPDATE notification_outbox SET state='in_app' WHERE id=?", (outbox_id,))
            return True
        return await self._deliver(outbox_id)

    async def test(self) -> tuple[bool, str]:
        if not self.secrets.get("apprise_urls").strip():
            return False, "Add at least one notification destination first"
        success = await self.send(
            None,
            f"test-{int(datetime.now(UTC).timestamp())}",
            "DiscDock test",
            "Notifications are configured correctly.",
            force=True,
        )
        return (
            success,
            "Test notification sent" if success else "The notification provider did not accept the test",
        )

    async def _deliver(self, outbox_id: int) -> bool:
        rows = self.database.query("SELECT * FROM notification_outbox WHERE id=?", (outbox_id,))
        if not rows:
            return False
        row = rows[0]
        urls = [url.strip() for url in self.secrets.get("apprise_urls").splitlines() if url.strip()]
        if not urls:
            self.database.execute("UPDATE notification_outbox SET state='in_app' WHERE id=?", (outbox_id,))
            return True

        def deliver() -> bool:
            import apprise

            client = apprise.Apprise()
            for url in urls:
                client.add(url)
            return bool(client.notify(title=row["title"], body=row["body"]))

        try:
            success = await asyncio.wait_for(asyncio.to_thread(deliver), timeout=30)
            error = None if success else "Notification provider rejected the message"
        except (TimeoutError, Exception) as caught:
            success = False
            error = (
                "Notification delivery timed out" if isinstance(caught, TimeoutError) else str(caught)[:500]
            )
        attempts = int(row["attempts"]) + 1
        retry_at = None
        if not success and attempts < 5:
            retry_at = (datetime.now(UTC) + timedelta(minutes=min(60, 2**attempts))).isoformat(
                timespec="seconds"
            )
        self.database.execute(
            "UPDATE notification_outbox SET state=?,attempts=?,next_attempt_at=?,last_error=? WHERE id=?",
            ("sent" if success else "failed", attempts, retry_at, error, outbox_id),
        )
        return success

    async def _retry_loop(self) -> None:
        while not self._stopping.is_set():
            now = utc_now()
            rows = self.database.query(
                """SELECT id FROM notification_outbox
                   WHERE state IN ('pending','failed') AND attempts < 5
                   AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                   ORDER BY created_at LIMIT 10""",
                (now,),
            )
            for row in rows:
                if self._stopping.is_set():
                    break
                await self._deliver(int(row["id"]))
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=30)
            except TimeoutError:
                continue
