from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS drives (
    id TEXT PRIMARY KEY,
    letter TEXT NOT NULL,
    name TEXT NOT NULL,
    pnp_device_id TEXT NOT NULL DEFAULT '',
    media_loaded INTEGER NOT NULL DEFAULT 0,
    volume_label TEXT NOT NULL DEFAULT '',
    disc_kind TEXT NOT NULL DEFAULT 'unknown',
    state TEXT NOT NULL DEFAULT 'ready',
    make_mkv_index INTEGER,
    last_seen TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    drive_id TEXT NOT NULL,
    drive_letter TEXT NOT NULL,
    disc_label TEXT NOT NULL DEFAULT '',
    disc_type TEXT NOT NULL DEFAULT 'unknown',
    fingerprint TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    year TEXT NOT NULL DEFAULT '',
    media_kind TEXT NOT NULL DEFAULT 'unknown',
    state TEXT NOT NULL,
    stage TEXT NOT NULL,
    progress REAL NOT NULL DEFAULT 0,
    status_detail TEXT NOT NULL DEFAULT '',
    output_path TEXT NOT NULL DEFAULT '',
    staging_path TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    error_code TEXT,
    error_message TEXT,
    settings_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    process_pid INTEGER,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    recoverable INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (drive_id) REFERENCES drives(id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_state_updated ON jobs(state, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_fingerprint ON jobs(fingerprint);
CREATE TABLE IF NOT EXISTS job_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    step TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    progress REAL NOT NULL DEFAULT 0,
    pid INTEGER,
    exit_code INTEGER,
    detail_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    source_id INTEGER NOT NULL,
    disc_title_number INTEGER NOT NULL DEFAULT 0,
    name TEXT NOT NULL DEFAULT '',
    duration_seconds INTEGER NOT NULL DEFAULT 0,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    chapters INTEGER NOT NULL DEFAULT 0,
    filename TEXT NOT NULL DEFAULT '',
    selected INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'pending',
    streams_json TEXT NOT NULL DEFAULT '[]',
    segment_map TEXT NOT NULL DEFAULT '',
    source_filename TEXT NOT NULL DEFAULT '',
    UNIQUE(job_id, source_id),
    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS job_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_job_events_id ON job_events(id);
CREATE TABLE IF NOT EXISTS metadata_cache (
    cache_key TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    response_json TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notification_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT,
    event_type TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(job_id, event_type)
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._write_lock = threading.RLock()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._write_lock, self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(SCHEMA)
            track_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(tracks)").fetchall()
            }
            if "disc_title_number" not in track_columns:
                connection.execute(
                    "ALTER TABLE tracks ADD COLUMN disc_title_number INTEGER NOT NULL DEFAULT 0"
                )
            # The damaged-disc rescue reads the selected title's cells or Blu-ray playlist first.
            for column in ("segment_map", "source_filename"):
                if column not in track_columns:
                    connection.execute(f"ALTER TABLE tracks ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (1, ?)",
                (utc_now(),),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (2, ?)",
                (utc_now(),),
            )
            self.recover_interrupted(connection)

    @staticmethod
    def recover_interrupted(connection: sqlite3.Connection) -> None:
        # A finished movie that was getting loading screens is still finished: its
        # library file is only replaced once the new copy has been verified.
        connection.execute(
            "UPDATE jobs SET state='completed', stage='completed', progress=100, process_pid=NULL, "
            "status_detail=CASE WHEN status_detail LIKE 'Adding loading screens%' THEN 'Completed' "
            "ELSE status_detail END, updated_at=?, version=version+1 "
            "WHERE state IN ('transcoding', 'cancelling') AND stage='damage_screens' "
            "AND completed_at IS NOT NULL AND COALESCE(output_path, '') <> ''",
            (utc_now(),),
        )
        active = (
            "detected",
            "inspecting",
            "identifying",
            "queued",
            "ripping",
            "ripped",
            "verifying",
            "transcoding",
            "finalizing",
            "ejecting",
            "cancelling",
        )
        placeholders = ",".join("?" for _ in active)
        connection.execute(
            f"UPDATE jobs SET state='interrupted', stage='interrupted', recoverable=1, process_pid=NULL, updated_at=?, version=version+1 WHERE state IN ({placeholders})",
            (utc_now(), *active),
        )

    def execute(self, sql: str, parameters: tuple = ()) -> int:
        with self._write_lock, self.connect() as connection:
            cursor = connection.execute(sql, parameters)
            return int(cursor.lastrowid or 0)

    def query(self, sql: str, parameters: tuple = ()) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(sql, parameters).fetchall()]

    def upsert_drive(self, drive: dict[str, Any]) -> None:
        now = utc_now()
        self.execute(
            """INSERT INTO drives(id,letter,name,pnp_device_id,media_loaded,volume_label,disc_kind,state,make_mkv_index,last_seen,details_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET letter=excluded.letter,name=excluded.name,pnp_device_id=excluded.pnp_device_id,
               media_loaded=excluded.media_loaded,volume_label=excluded.volume_label,disc_kind=excluded.disc_kind,
               state=excluded.state,make_mkv_index=excluded.make_mkv_index,last_seen=excluded.last_seen,details_json=excluded.details_json""",
            (
                drive["id"],
                drive["letter"],
                drive["name"],
                drive.get("pnp_device_id", ""),
                int(drive.get("media_loaded", False)),
                drive.get("volume_label", ""),
                drive.get("disc_kind", "unknown"),
                drive.get("state", "ready"),
                drive.get("make_mkv_index"),
                now,
                json.dumps(drive, ensure_ascii=False),
            ),
        )

    def create_job(self, job: dict[str, Any]) -> None:
        now = utc_now()
        self.execute(
            """INSERT INTO jobs(id,drive_id,drive_letter,disc_label,disc_type,fingerprint,title,year,media_kind,state,stage,
               progress,status_detail,staging_path,created_at,updated_at,settings_json,metadata_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                job["id"],
                job["drive_id"],
                job["drive_letter"],
                job.get("disc_label", ""),
                job.get("disc_type", "unknown"),
                job.get("fingerprint", ""),
                job.get("title", ""),
                job.get("year", ""),
                job.get("media_kind", "unknown"),
                job.get("state", "detected"),
                job.get("stage", "detected"),
                float(job.get("progress", 0)),
                job.get("status_detail", ""),
                job.get("staging_path", ""),
                now,
                now,
                json.dumps(job.get("settings", {}), ensure_ascii=False),
                json.dumps(job.get("metadata", {}), ensure_ascii=False),
            ),
        )
        self.append_event(job["id"], "job.created", job)

    def update_job(self, job_id: str, *, emit_event: bool = True, **changes: Any) -> dict[str, Any] | None:
        allowed = {
            # A reconnected USB drive can get a new id; its jobs follow it.
            "drive_id",
            "drive_letter",
            "disc_label",
            "disc_type",
            "fingerprint",
            "title",
            "year",
            "media_kind",
            "state",
            "stage",
            "progress",
            "status_detail",
            "output_path",
            "staging_path",
            "completed_at",
            "error_code",
            "error_message",
            "settings_json",
            "metadata_json",
            "process_pid",
            "cancel_requested",
            "recoverable",
        }
        updates = {key: value for key, value in changes.items() if key in allowed}
        if not updates:
            return self.get_job(job_id)
        updates["updated_at"] = utc_now()
        assignments = ",".join(f"{key}=?" for key in updates)
        values = tuple(updates.values())
        self.execute(f"UPDATE jobs SET {assignments}, version=version+1 WHERE id=?", (*values, job_id))
        job = self.get_job(job_id)
        if job and emit_event:
            self.append_event(
                job_id,
                "job.updated",
                {
                    "id": job_id,
                    "state": job["state"],
                    "stage": job["stage"],
                    "progress": job["progress"],
                    "version": job["version"],
                },
            )
        return job

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        rows = self.query("SELECT * FROM jobs WHERE id=?", (job_id,))
        return self._decode_job(rows[0]) if rows else None

    def list_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        return [
            self._decode_job(row)
            for row in self.query("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))
        ]

    @staticmethod
    def _decode_job(job: dict[str, Any]) -> dict[str, Any]:
        for field in ("settings_json", "metadata_json"):
            try:
                job[field.removesuffix("_json")] = json.loads(job.get(field) or "{}")
            except json.JSONDecodeError:
                job[field.removesuffix("_json")] = {}
            job.pop(field, None)
        for field in ("cancel_requested", "recoverable"):
            job[field] = bool(job.get(field))
        return job

    def replace_tracks(self, job_id: str, tracks: list[dict[str, Any]]) -> None:
        with self._write_lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM tracks WHERE job_id=?", (job_id,))
            for track in tracks:
                connection.execute(
                    """INSERT INTO tracks(job_id,source_id,disc_title_number,name,duration_seconds,size_bytes,chapters,filename,selected,state,streams_json,segment_map,source_filename)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        job_id,
                        track["id"],
                        track.get("disc_title_number", 0),
                        track.get("name", ""),
                        track.get("duration_seconds", 0),
                        track.get("size_bytes", 0),
                        track.get("chapters", 0),
                        track.get("filename", ""),
                        int(track.get("selected", True)),
                        track.get("state", "pending"),
                        json.dumps(track.get("streams", [])),
                        str(track.get("segment_map") or ""),
                        str(track.get("source_filename") or ""),
                    ),
                )
            connection.execute("COMMIT")

    def list_tracks(self, job_id: str) -> list[dict[str, Any]]:
        tracks = self.query("SELECT * FROM tracks WHERE job_id=? ORDER BY source_id", (job_id,))
        for track in tracks:
            track["selected"] = bool(track["selected"])
            track["streams"] = json.loads(track.pop("streams_json") or "[]")
        return tracks

    def append_event(self, job_id: str | None, event_type: str, payload: dict[str, Any]) -> int:
        event_id = self.execute(
            "INSERT INTO job_events(job_id,event_type,payload_json,created_at) VALUES(?,?,?,?)",
            (job_id, event_type, json.dumps(payload, ensure_ascii=False, default=str), utc_now()),
        )
        if event_id and event_id % 1000 == 0:
            self.execute("DELETE FROM job_events WHERE id < ?", (max(0, event_id - 20000),))
        return event_id

    def events_after(self, event_id: int, limit: int = 250) -> list[dict[str, Any]]:
        rows = self.query("SELECT * FROM job_events WHERE id>? ORDER BY id LIMIT ?", (event_id, limit))
        for row in rows:
            row["payload"] = json.loads(row.pop("payload_json"))
        return rows
