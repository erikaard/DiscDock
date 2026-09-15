from __future__ import annotations

from discdock.database import Database


def job(job_id: str, state: str = "ripping") -> dict:
    return {
        "id": job_id,
        "drive_id": "drive",
        "drive_letter": "D:",
        "state": state,
        "stage": state,
        "staging_path": rf"C:\DiscDock\raw\{job_id}.partial",
    }


def test_startup_recovers_interrupted_jobs(tmp_path) -> None:
    database = Database(tmp_path / "discdock.db")
    database.initialize()
    database.upsert_drive({"id": "drive", "letter": "D:", "name": "Reader", "last_seen": "now"})
    database.create_job(job("one"))
    database.initialize()
    recovered = database.get_job("one")
    assert recovered["state"] == "interrupted"
    assert recovered["recoverable"] is True


def test_a_finished_movie_interrupted_while_getting_loading_screens_stays_finished(tmp_path) -> None:
    database = Database(tmp_path / "discdock.db")
    database.initialize()
    database.upsert_drive({"id": "drive", "letter": "D:", "name": "Reader", "last_seen": "now"})
    database.create_job(job("movie", "transcoding"))
    database.update_job(
        "movie",
        stage="damage_screens",
        status_detail="Adding loading screens at 2 damaged moments",
        output_path=str(tmp_path / "Movie (2009)"),
        completed_at="2026-09-12T21:04:33+00:00",
    )
    database.create_job(job("rip", "transcoding"))
    database.update_job("rip", stage="damage_screens")

    database.initialize()

    movie = database.get_job("movie")
    assert movie["state"] == "completed"
    assert movie["status_detail"] == "Completed"
    assert database.get_job("rip")["state"] == "interrupted", "a rip that was never finished is still interrupted"


def test_job_events_are_compact(tmp_path) -> None:
    database = Database(tmp_path / "discdock.db")
    database.initialize()
    database.upsert_drive({"id": "drive", "letter": "D:", "name": "Reader", "last_seen": "now"})
    database.create_job(job("one", "detected"))
    database.update_job("one", state="inspecting", progress=12.5)
    event = database.events_after(0)[-1]
    assert set(event["payload"]) == {"id", "state", "stage", "progress", "version"}
    assert "settings" not in event["payload"]
