from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import signal
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import Scope

from . import __version__
from .ai_repair import OpenAIFrameRepair
from .database import Database
from .files import ensure_within, open_folder
from .models import (
    DISC_READING_STATES,
    AiRepairApplyRequest,
    AlbumChoice,
    CdFinish,
    CdStaging,
    ContinueRequest,
    JobPatch,
    ManualAlbum,
    ScanRequest,
    SettingsPatch,
)
from .musicbrainz import MAX_COVER_BYTES, MusicBrainzBusy, MusicBrainzUnavailable
from .processes import start_external_process
from .secrets import SecretStore
from .settings import AppSettings, SettingsStore, default_data_root
from .tray import start_tray_icon
from .workflow import DiscDockService, album_photo_path

DATA_ROOT = default_data_root()
SETTINGS_STORE = SettingsStore(DATA_ROOT / "config" / "settings.json")
SECRET_STORE = SecretStore(DATA_ROOT / "config" / "secrets.bin")
DATABASE = Database(DATA_ROOT / "database" / "discdock.db")
SERVICE = DiscDockService(SETTINGS_STORE, SECRET_STORE, DATABASE)


def _make_mkv_gui_path(configured_path: str) -> Path:
    candidates: list[Path] = []
    if configured_path:
        configured = Path(configured_path).expanduser()
        if configured.name.casefold() == "makemkv.exe":
            candidates.append(configured)
        else:
            candidates.append(configured.with_name("makemkv.exe"))
    discovered = shutil.which("makemkv.exe")
    if discovered:
        candidates.append(Path(discovered))
    candidates.extend(
        [
            Path(r"C:\Program Files (x86)\MakeMKV\makemkv.exe"),
            Path(r"C:\Program Files\MakeMKV\makemkv.exe"),
        ]
    )
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved not in seen and resolved.is_file():
            return resolved
        seen.add(resolved)
    raise FileNotFoundError("The MakeMKV desktop app was not found")


@asynccontextmanager
async def lifespan(_: FastAPI):
    await SERVICE.start()
    loop = asyncio.get_running_loop()
    tray = start_tray_icon(
        busy_jobs=SERVICE.busy_jobs,
        # Stop and Restart in the notification area shut DiscDock down the same way Setup's close request does.
        request_exit=lambda: loop.call_soon_threadsafe(_close_after_answering),
        dashboard_url=f"http://{SERVICE.settings.host}:{SERVICE.settings.port}/",
    )
    try:
        yield
    finally:
        if tray:
            tray.stop()
        await SERVICE.stop()


app = FastAPI(
    title="DiscDock",
    version=__version__,
    description="Local Windows-native automatic media ripping service",
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)

ALLOWED_ORIGINS = {
    "http://127.0.0.1:8199",
    "http://localhost:8199",
    "http://127.0.0.1:5173",
    "http://localhost:5173",
}
app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(ALLOWED_ORIGINS),
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "If-Match", "Last-Event-ID", "Idempotency-Key"],
)
# A website can point a host name it controls at 127.0.0.1 (DNS rebinding) and then
# read this API as its own site. Requests must be addressed to a loopback name.
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])


SECURITY_HEADERS = {
    # The browser must not guess another type for a response, for example HTML in a job log.
    "X-Content-Type-Options": "nosniff",
    # Other websites cannot show the dashboard in a frame and trick clicks on its buttons.
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
}


@app.middleware("http")
async def local_origin_guard(request: Request, call_next):
    response = None
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        host = request.client.host if request.client else ""
        if host not in {"127.0.0.1", "::1", "localhost"}:
            response = JSONResponse({"detail": "DiscDock only accepts local requests"}, status_code=403)
        elif origin and origin not in ALLOWED_ORIGINS:
            response = JSONResponse({"detail": "Request origin is not allowed"}, status_code=403)
    if response is None:
        response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    return response


def _job_or_404(job_id: str) -> dict:
    job = DATABASE.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    job["tracks"] = DATABASE.list_tracks(job_id)
    return job


@app.get("/api/v1/health")
async def health():
    return SERVICE.health()


@app.get("/api/v1/bootstrap")
async def bootstrap():
    count_rows = DATABASE.query("SELECT COUNT(*) AS count FROM jobs")
    jobs = DATABASE.list_jobs(int(count_rows[0]["count"]) if count_rows else 0)
    for item in jobs:
        # The dashboard needs tracks only while a manual choice is open. Older
        # job details are loaded on demand, keeping an uncapped history compact.
        if item["state"] == "awaiting_input":
            item["tracks"] = DATABASE.list_tracks(item["id"])
    return {
        "health": SERVICE.health(),
        "drives": [drive.model_dump(mode="json") for drive in SERVICE.drives.values()],
        "jobs": jobs,
        "settings": SERVICE.settings.public_dict(SECRET_STORE.configured_names()),
        "notifications": DATABASE.query(
            "SELECT * FROM notification_outbox WHERE state != 'dismissed' ORDER BY created_at DESC LIMIT 25"
        ),
    }


@app.get("/api/v1/drives")
async def drives(refresh: bool = Query(False)):
    if refresh:
        await SERVICE.refresh_drives()
    return [drive.model_dump(mode="json") for drive in SERVICE.drives.values()]


@app.post("/api/v1/drives/{drive_id}/scan", status_code=202)
async def scan_drive(drive_id: str, body: ScanRequest):
    try:
        return await SERVICE.create_job(
            drive_id,
            manual=body.manual,
            media_kind=body.media_kind,
            rip_method=body.rip_method,
        )
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error


@app.post("/api/v1/drives/{drive_id}/eject")
async def eject_drive(drive_id: str):
    drive = SERVICE.drives.get(drive_id)
    if not drive:
        raise HTTPException(404, "Drive not found")
    if SERVICE.active_job_for_drive(drive_id):
        raise HTTPException(409, "Stop the active job before ejecting")
    try:
        await SERVICE.drive_control.eject(drive.letter)
    except OSError as error:
        raise HTTPException(409, f"Windows could not eject the disc: {error}") from error
    return {"ok": True}


@app.post("/api/v1/drives/{drive_id}/close")
async def close_drive(drive_id: str):
    drive = SERVICE.drives.get(drive_id)
    if not drive:
        raise HTTPException(404, "Drive not found")
    try:
        await SERVICE.drive_control.close_tray(drive.letter)
    except OSError as error:
        raise HTTPException(409, f"Windows could not close the tray: {error}") from error
    return {"ok": True}


@app.post("/api/v1/drives/{drive_id}/preview")
async def preview_drive(drive_id: str):
    drive = SERVICE.drives.get(drive_id)
    if not drive:
        raise HTTPException(404, "Drive not found")
    job = SERVICE.active_job_for_drive(drive_id)
    if job and job["state"] in {state.value for state in DISC_READING_STATES}:
        raise HTTPException(409, "DiscDock is reading this disc. Preview it when the rip is done or stopped.")
    try:
        SERVICE.drive_control.preview(drive.letter, SERVICE.settings.vlc_path, drive.disc_kind)
    except (OSError, FileNotFoundError) as error:
        raise HTTPException(409, str(error)) from error
    return {"ok": True}


@app.get("/api/v1/jobs")
async def jobs(limit: int = Query(100, ge=1, le=500)):
    return DATABASE.list_jobs(limit)


@app.get("/api/v1/jobs/{job_id}")
async def job(job_id: str):
    return _job_or_404(job_id)


@app.patch("/api/v1/jobs/{job_id}")
async def patch_job(job_id: str, patch: JobPatch, if_match: str | None = Header(None)):
    current = _job_or_404(job_id)
    if if_match and if_match.strip('"') != str(current["version"]):
        raise HTTPException(412, "The job changed; refresh and try again")
    values = patch.model_dump(exclude_none=True)
    selected_titles = values.pop("selected_titles", None)
    selected_metadata = values.pop("metadata", None)
    if selected_titles is not None:
        tracks = DATABASE.list_tracks(job_id)
        selected = set(selected_titles)
        DATABASE.replace_tracks(
            job_id,
            [
                {**track, "id": track["source_id"], "selected": track["source_id"] in selected}
                for track in tracks
            ],
        )
    if "media_kind" in values:
        values["media_kind"] = values["media_kind"].value
    if selected_metadata is not None:
        metadata = {**(current.get("metadata") or {}), **selected_metadata}
        values["metadata_json"] = json.dumps(metadata, ensure_ascii=False)
    updated = DATABASE.update_job(job_id, **values)
    return updated


@app.post("/api/v1/jobs/{job_id}/continue", status_code=202)
async def continue_job(job_id: str, request: ContinueRequest):
    try:
        return await SERVICE.continue_job(job_id, request.selected_titles, request.musicbrainz_release)
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error


def _close_after_answering() -> None:
    # uvicorn stops gracefully on SIGINT, like Ctrl+C in a console.
    asyncio.get_running_loop().call_later(0.5, signal.raise_signal, signal.SIGINT)


@app.post("/api/v1/shutdown", status_code=202)
async def shutdown(x_discdock_request: str = Header("")):
    """Lets Setup ask DiscDock to close itself before an update.

    Ending another program's process is what antivirus behaviour monitoring (Bitdefender ATC) flags.
    A web page cannot send this header to DiscDock, because CORS only allows the dashboard's own origins.
    """
    if x_discdock_request != "close":
        raise HTTPException(403, "Send the header X-DiscDock-Request: close to close DiscDock")
    busy = SERVICE.busy_jobs()
    if busy:
        raise HTTPException(409, f"DiscDock is working on {busy[0]}")
    _close_after_answering()
    return {"closing": True}


@app.post("/api/v1/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    try:
        return await SERVICE.cancel_job(job_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error


@app.post("/api/v1/jobs/{job_id}/retry", status_code=202)
async def retry_job(job_id: str):
    try:
        return await SERVICE.retry_job(job_id)
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error


@app.post("/api/v1/jobs/{job_id}/recover-damaged", status_code=202)
async def recover_damaged_job(job_id: str):
    try:
        return await SERVICE.recover_damaged_job(job_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error


@app.post("/api/v1/jobs/{job_id}/rescue/finish", status_code=202)
async def finish_rescue(job_id: str):
    try:
        return await SERVICE.finish_rescue_now(job_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error


@app.post("/api/v1/jobs/{job_id}/ai-repair/prepare", status_code=202)
async def prepare_ai_repair(job_id: str):
    try:
        return await SERVICE.prepare_ai_repair_job(job_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error


@app.post("/api/v1/jobs/{job_id}/ai-repair/apply", status_code=202)
async def apply_ai_repair(job_id: str, body: AiRepairApplyRequest):
    try:
        return await SERVICE.apply_ai_repair_job(
            job_id, body.estimate_id, body.accepted_max_cost_usd
        )
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error


@app.post("/api/v1/jobs/{job_id}/ai-repair/keep", status_code=202)
async def keep_without_ai_repair(job_id: str):
    try:
        return await SERVICE.keep_movie_without_ai(job_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error


@app.post("/api/v1/jobs/{job_id}/loading-screens", status_code=202)
async def add_loading_screens(job_id: str):
    try:
        return await SERVICE.add_loading_screens_to_movie(job_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error


@app.post("/api/v1/jobs/{job_id}/damage/keep")
async def keep_damaged_movie(job_id: str):
    try:
        return await SERVICE.keep_damaged_movie_as_is(job_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error


@app.post("/api/v1/jobs/{job_id}/add-titles")
async def add_titles_to_completed_disc(job_id: str):
    try:
        return await SERVICE.add_titles_to_completed_disc(job_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error


@app.get("/api/v1/jobs/{job_id}/ai-repair/preview/{segment_index}")
async def ai_repair_preview(job_id: str, segment_index: int):
    job = _job_or_404(job_id)
    repair = (job.get("metadata") or {}).get("ai_repair") or {}
    segment = next(
        (
            item
            for item in repair.get("segments") or []
            if int(item.get("index") or 0) == segment_index
        ),
        None,
    )
    if not segment or not segment.get("preview"):
        raise HTTPException(404, "AI repair preview not found")
    root_value = job.get("output_path") or job.get("staging_path")
    if not root_value:
        raise HTTPException(404, "AI repair output is not available")
    root = Path(root_value).resolve()
    try:
        path = ensure_within(root / str(segment["preview"]), root)
    except ValueError as error:
        raise HTTPException(404, "AI repair preview not found") from error
    if not path.is_file():
        raise HTTPException(404, "AI repair preview not found")
    return FileResponse(path, media_type="video/mp4")


@app.get("/api/v1/jobs/{job_id}/log")
async def job_log(job_id: str, download: bool = Query(False)):
    _job_or_404(job_id)
    path = SERVICE.settings.resolved_directories()["logs"] / f"{job_id}.log"
    if not path.exists():
        return Response("", media_type="text/plain")
    if download:
        return FileResponse(path, filename=f"discdock-{job_id}.log", media_type="text/plain")
    lines = await asyncio.to_thread(path.read_text, encoding="utf-8", errors="replace")
    return Response(lines, media_type="text/plain")


@app.post("/api/v1/jobs/{job_id}/open-output")
async def open_job_output(job_id: str):
    job = _job_or_404(job_id)
    path = Path(job["output_path"] or job["staging_path"])
    if not path.exists():
        raise HTTPException(404, "Output folder does not exist")
    open_folder(path)
    return {"ok": True}


@app.get("/api/v1/settings")
async def get_settings():
    return SERVICE.settings.public_dict(SECRET_STORE.configured_names())


@app.put("/api/v1/settings")
async def put_settings(body: SettingsPatch):
    forbidden = {"version", "host", "port"}
    unknown = set(body.values) - set(AppSettings.model_fields)
    if unknown:
        raise HTTPException(422, f"Unknown settings: {', '.join(sorted(unknown))}")
    if forbidden & set(body.values):
        raise HTTPException(422, "Network settings cannot be changed from the dashboard")
    allowed_secrets = {
        "omdb_api_key",
        "tmdb_api_key",
        "apprise_urls",
        "emby_api_key",
        "emby_url",
        "arm_api_key",
        "openai_api_key",
    }
    secret_unknown = set(body.secrets) - allowed_secrets
    if secret_unknown:
        raise HTTPException(422, f"Unknown secrets: {', '.join(sorted(secret_unknown))}")
    try:
        updated = SERVICE.reload_settings(body.values)
        if body.secrets:
            SECRET_STORE.update(body.secrets)
    except (ValueError, OSError) as error:
        raise HTTPException(422, str(error)) from error
    DATABASE.append_event(
        None, "settings.updated", {"fields": sorted(body.values), "secrets": sorted(body.secrets)}
    )
    return updated.public_dict(SECRET_STORE.configured_names())


@app.get("/api/v1/metadata/search")
async def metadata_search(q: str = Query(..., min_length=2), year: str = Query("")):
    try:
        return await SERVICE.metadata.search_omdb(q, year)
    except Exception as error:
        raise HTTPException(502, "OMDb metadata search is temporarily unavailable") from error


def _musicbrainz_problem(error: MusicBrainzBusy | MusicBrainzUnavailable) -> HTTPException:
    if isinstance(error, MusicBrainzBusy):
        return HTTPException(
            503,
            f"MusicBrainz responded with {error.status}, which it does when it gets more than about one "
            "request per second. Wait a few seconds and search again.",
        )
    return HTTPException(502, f"{error}. Check the internet connection and search again.")


@app.get("/api/v1/musicbrainz/search")
async def musicbrainz_search(q: str = Query(..., min_length=2, max_length=200), tracks: int = Query(0, ge=0, le=99)):
    try:
        return await SERVICE.search_albums(q, tracks)
    except (MusicBrainzBusy, MusicBrainzUnavailable) as error:
        raise _musicbrainz_problem(error) from error


@app.get("/api/v1/musicbrainz/barcode")
async def musicbrainz_barcode(code: str = Query(..., min_length=8, max_length=32), tracks: int = Query(0, ge=0, le=99)):
    try:
        return await SERVICE.search_albums_by_barcode(code, tracks)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    except (MusicBrainzBusy, MusicBrainzUnavailable) as error:
        raise _musicbrainz_problem(error) from error


@app.post("/api/v1/jobs/{job_id}/album")
async def choose_album(job_id: str, album: AlbumChoice):
    try:
        return await SERVICE.choose_album(job_id, album.model_dump())
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error


@app.post("/api/v1/jobs/{job_id}/album/manual")
async def set_manual_album(job_id: str, album: ManualAlbum):
    try:
        return await SERVICE.set_manual_album(job_id, album.artist, album.title, album.year, album.tracks)
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error


@app.post("/api/v1/jobs/{job_id}/album/staging")
async def keep_cd_in_staging(job_id: str, request: CdStaging):
    try:
        return await SERVICE.keep_cd_in_staging(job_id, request.keep)
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error


@app.post("/api/v1/jobs/{job_id}/album/finish")
async def finish_waiting_cd(job_id: str, request: CdFinish):
    try:
        return await SERVICE.finish_waiting_cd(job_id, without_album=request.without_album)
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error


@app.get("/api/v1/jobs/{job_id}/album/photos/{side}")
async def album_photo(job_id: str, side: Literal["front", "front_original", "back"]):
    """A photo of a CD's case taken in the dashboard, until the tracks are tagged."""
    path = album_photo_path(_job_or_404(job_id), side)
    if not path:
        raise HTTPException(404, "No such photo is saved for this CD")
    media_type = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-cache"})


@app.put("/api/v1/jobs/{job_id}/album/photos/{side}")
async def set_album_photo(job_id: str, side: Literal["front", "front_original", "back"], request: Request):
    """A photo of the front (the cover) or the back of the case, sent as the request body (JPEG or PNG)."""
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError as error:
        raise HTTPException(400, "Content-Length is not a number") from error
    if declared > MAX_COVER_BYTES:
        raise HTTPException(413, "The photo is larger than 10 MB")
    # A body can be sent without Content-Length, so it is counted while it is read.
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > MAX_COVER_BYTES:
            raise HTTPException(413, "The photo is larger than 10 MB")
    try:
        return await SERVICE.set_album_photo(job_id, side, bytes(data))
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error


@app.delete("/api/v1/jobs/{job_id}/album/photos/{side}")
async def remove_album_photo(job_id: str, side: Literal["front", "front_original", "back"]):
    try:
        return await SERVICE.remove_album_photo(job_id, side)
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error


@app.post("/api/v1/settings/test/omdb")
async def test_omdb(body: SettingsPatch):
    supplied = body.secrets.get("omdb_api_key")
    try:
        return await SERVICE.metadata.test_omdb(supplied)
    except Exception as error:
        raise HTTPException(502, "OMDb could not verify that key") from error


@app.post("/api/v1/settings/test/openai")
async def test_openai(body: SettingsPatch):
    supplied = body.secrets.get("openai_api_key") or SECRET_STORE.get("openai_api_key")
    try:
        ok, message = await OpenAIFrameRepair.test_key(
            supplied, SERVICE.settings.ai_repair_model
        )
    except Exception as error:
        raise HTTPException(502, "OpenAI could not be reached") from error
    if not ok:
        raise HTTPException(409, message)
    return {"ok": True, "message": message}


@app.post("/api/v1/notifications/test")
async def test_notification():
    ok, message = await SERVICE.notifications.test()
    if not ok:
        raise HTTPException(409, message)
    return {"ok": True, "message": message}


@app.post("/api/v1/tools/makemkv/open")
async def open_makemkv():
    try:
        executable = _make_mkv_gui_path(SERVICE.settings.make_mkv_path)
    except FileNotFoundError as error:
        raise HTTPException(
            404, "MakeMKV could not be found. Check its location in DiscDock settings."
        ) from error
    try:
        start_external_process(
            [str(executable)],
            cwd=str(executable.parent),
            close_fds=True,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
    except OSError as error:
        raise HTTPException(409, f"Windows could not open MakeMKV: {error}") from error
    DATABASE.append_event(None, "tool.makemkv_opened", {"path": str(executable)})
    return {
        "ok": True,
        "message": "MakeMKV opened. Activate it, close it, then retry the DiscDock job.",
    }


@app.delete("/api/v1/notifications/{notification_id}", status_code=204)
async def dismiss_notification(notification_id: int):
    existing = DATABASE.query("SELECT id FROM notification_outbox WHERE id=?", (notification_id,))
    if not existing:
        raise HTTPException(404, "Notification not found")
    DATABASE.execute(
        "UPDATE notification_outbox SET state='dismissed',next_attempt_at=NULL WHERE id=?",
        (notification_id,),
    )
    DATABASE.append_event(None, "notification.dismissed", {"id": notification_id})
    return Response(status_code=204)


async def _event_stream(last_event_id: int, request: Request) -> AsyncIterator[str]:
    cursor = last_event_id
    try:
        while not await request.is_disconnected():
            events = DATABASE.events_after(cursor)
            if events:
                for event in events:
                    cursor = event["id"]
                    yield f"id: {cursor}\nevent: {event['event_type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            else:
                yield ": keepalive\n\n"
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        # Browser tabs commonly keep an SSE connection open while Windows is
        # shutting the local service down. Closing it is an expected exit.
        return


@app.get("/api/v1/events")
async def events(request: Request, last_event_id: str | None = Header(None)):
    try:
        cursor = int(last_event_id or 0)
    except ValueError:
        cursor = 0
    return StreamingResponse(
        _event_stream(cursor, request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/v1/diagnostics")
async def diagnostics():
    root = SERVICE.settings.resolved_directories()["root"]
    usage = shutil.disk_usage(root)
    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "data_root": str(root),
        "disk": {"total": usage.total, "used": usage.used, "free": usage.free},
        "health": SERVICE.health(),
        "drives": [drive.model_dump(mode="json") for drive in SERVICE.drives.values()],
    }


def _frontend_directory() -> Path:
    configured = os.environ.get("DISCDOCK_FRONTEND")
    candidates = [Path(configured)] if configured else []
    if getattr(sys, "frozen", False):
        candidates.extend(
            [
                Path(sys.executable).resolve().parent / "web",
                Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "web",
            ]
        )
    candidates.append(Path(__file__).resolve().parents[2] / "out")
    return next(
        (candidate for candidate in candidates if (candidate / "index.html").is_file()), candidates[-1]
    )


class DashboardFiles(StaticFiles):
    """The dashboard's files, with the pages checked again on every visit.

    Without a Cache-Control header a browser can keep showing the previous version's dashboard for
    hours after an update. Next.js assets carry their content hash in the name, so they can be kept.
    """

    def file_response(
        self,
        full_path: str | os.PathLike[str],
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        hashed = str(scope.get("path") or "").startswith("/_next/static/")
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable" if hashed else "no-cache"
        return response


FRONTEND = _frontend_directory()
if FRONTEND.is_dir() and (FRONTEND / "index.html").exists():
    app.mount("/", DashboardFiles(directory=FRONTEND, html=True), name="dashboard")
else:

    @app.get("/")
    async def development_root():
        return {
            "name": "DiscDock",
            "status": "online",
            "dashboard": "Run the frontend development server on http://localhost:5173",
        }
