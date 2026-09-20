from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import time
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from typing import Any

import psutil

from . import __version__
from .ai_repair import (
    OpenAIFrameRepair,
    analyze_repair,
    damage_moments,
    find_repair_source,
    measure_damage,
    video_info,
)
from .album_tags import flatten_rip_folder, tag_album
from .bluray_copy import BlurayMovieCopy, build_key_folder, libmmbd_library, movie_decryption_started
from .bluray_folder import DISC_ATTRIBUTES, fill_backup_folder, movie_playlist
from .cd_toc import read_disc_id
from .damage_screens import (
    UNREPAIRED_SUFFIX_AI,
    MoviePatcher,
    disc_display_name,
    format_timestamp,
    is_unrepaired_copy,
    loading_screen_worthy,
    unrepaired_copy_path,
)
from .database import Database, utc_now
from .disc_files import DiscContents, DiscEntry, check_backup, describe_contents, list_drive_files
from .disc_rescue import FINISHED, PENDING, RescueMap, merge_rescue_images
from .drives import DriveControl, DriveMonitor
from .files import (
    disk_space_ok,
    ensure_within,
    finalize_directory,
    merge_into_directory,
    move_failed_staging,
    output_folder,
    rename_video_outputs,
    safe_component,
    verify_media_file,
    verify_outputs,
)
from .instance import SingleInstance
from .makemkv import DiscScan, MakeMKVClient, MakeMKVLicenseError, NoVideoTitles, match_title
from .media_tools import (
    AudioRipper,
    DataDiscRipper,
    DiscAuthenticationRequired,
    DriveNotResponding,
    DvdSectorRescue,
    FfmpegDvdRecovery,
    HandBrakeTranscoder,
    VlcDvdRecovery,
    describe_rescue_progress,
)
from .metadata import MetadataService, title_from_label
from .models import (
    ACTIVE_JOB_STATES,
    DiscKind,
    DriveInfo,
    JobState,
    MediaKind,
    MetadataCandidate,
    TitleInfo,
)
from .musicbrainz import (
    MAX_COVER_BYTES,
    WITHOUT_ALBUM,
    AlbumRelease,
    AlbumTrack,
    MusicBrainzBusy,
    MusicBrainzClient,
    MusicBrainzUnavailable,
)
from .notifications import NotificationService
from .optical import (
    SECTOR_SIZE,
    OpticalError,
    dvd_title_video_ranges,
    dvd_titles,
    dvd_video_scrambled,
    read_video_ts_files,
)
from .processes import ProcessFailure, ProcessRunner, begin_captures, cancel_captures
from .secrets import SecretStore
from .settings import AppSettings, SettingsStore


def _equivalent_title(left: Any, right: Any) -> bool:
    """Return whether two MakeMKV titles are alternate angles of the same program."""
    if abs(left.duration_seconds - right.duration_seconds) > 5:
        return False
    larger = max(left.size_bytes, right.size_bytes)
    tolerance = max(64 * 1024 * 1024, int(larger * 0.015))
    return abs(left.size_bytes - right.size_bytes) <= tolerance


def _inside(path: Path, root: Path) -> bool:
    try:
        ensure_within(path, root)
    except ValueError:
        return False
    return True


# Photos of a CD's case taken in the dashboard, by side: the job metadata key, the file name and what
# the log calls it. The front, as edited in the dashboard, becomes the cover; the photo it was edited from
# is kept to edit the cover again; the back is kept to check the track names read from it.
ALBUM_PHOTOS = {
    "front": ("album_cover", "cover", "Cover photo"),
    "front_original": ("album_cover_original", "cover-original", "Unedited cover photo"),
    "back": ("album_back_photo", "back", "Photo of the back of the case"),
}

# An audio CD's album can change until its tracks are named, at the end of the rip.
ALBUM_CHOICE_STATES = frozenset(
    {
        JobState.DETECTED,
        JobState.INSPECTING,
        JobState.IDENTIFYING,
        JobState.AWAITING_INPUT,
        JobState.QUEUED,
        JobState.RIPPING,
        JobState.RIPPED,
        JobState.VERIFYING,
        JobState.EJECTING,
        JobState.AWAITING_ALBUM,
    }
)
MUSICBRAINZ_BUSY_DURING_RIP = (
    "MusicBrainz responded with {status}. It does that when it gets more than about one request per second. "
    "The CD keeps ripping: use Find the album below to search by name or barcode, and if that search gets "
    "the same answer, wait a few seconds and search again."
)


def describe_album(album: dict[str, Any]) -> str:
    """For example: Tom Petty - Into the Great Wide Open (1991, XE, MCA, BIEM / MCPS)."""
    name = " - ".join(part for part in (str(album.get("artist") or ""), str(album.get("title") or "")) if part)
    details = ", ".join(str(album[key]) for key in ("date", "country", "label", "disambiguation") if album.get(key))
    return f"{name} ({details})" if details else name


def album_photo_path(job: dict[str, Any], side: str) -> Path | None:
    """The saved photo of one side of a CD's case, while it is still in the raw folder."""
    photo = (job.get("metadata") or {}).get(ALBUM_PHOTOS[side][0])
    if not isinstance(photo, dict) or not photo.get("file"):
        return None
    raw = AppSettings.model_validate(job.get("settings") or {}).resolved_directories()["raw"]
    try:
        path = ensure_within(Path(str(photo["file"])), raw)
    except ValueError:
        return None
    return path if path.is_file() else None


def manual_release(album: dict[str, Any]) -> AlbumRelease:
    """The release for an album typed in the dashboard; a track without a name is called "Track 03"."""
    tracks = []
    for track in album.get("tracks") or []:
        if isinstance(track, dict):
            position = int(track.get("position") or 0)
            tracks.append(AlbumTrack(position=position, title=str(track.get("title") or "") or f"Track {position:02d}"))
    return AlbumRelease(
        id="",
        title=str(album.get("title") or ""),
        artist=str(album.get("artist") or ""),
        date=str(album.get("date") or ""),
        track_count=int(album.get("track_count") or len(tracks)),
        tracks=tracks,
    )


def select_disc_titles(
    titles: list[Any], settings: AppSettings, runtime_minutes: int = 0
) -> list[Any]:
    """Apply the duration filter and choose one likely feature when requested."""
    eligible = [
        title
        for title in titles
        if settings.min_length_seconds <= title.duration_seconds <= settings.max_length_seconds
    ]
    if not settings.main_feature or not eligible:
        return eligible

    # Multi-angle discs often expose the same program several times. Keep one
    # representative from each near-identical duration/size group before
    # choosing the feature.
    candidates: list[Any] = []
    for title in eligible:
        if title.angle > 1:
            continue
        if not any(_equivalent_title(title, existing) for existing in candidates):
            candidates.append(title)
    if not candidates:
        candidates = eligible

    if runtime_minutes > 0:
        expected_seconds = runtime_minutes * 60
        # PAL DVDs are commonly about four percent shorter than their published
        # cinema runtime, so compare against both clocks.
        targets = (expected_seconds, round(expected_seconds * 24 / 25))
        return [
            min(
                candidates,
                key=lambda title: (
                    min(abs(title.duration_seconds - target) for target in targets),
                    -title.chapters,
                    -title.size_bytes,
                    title.id,
                ),
            )
        ]
    return [
        max(
            candidates,
            key=lambda title: (title.duration_seconds, title.size_bytes, title.chapters, -title.id),
        )
    ]


def _user_metadata_candidate(job: dict[str, Any]) -> MetadataCandidate | None:
    """Return metadata explicitly chosen in the dashboard, if it is complete."""
    metadata = job.get("metadata") or {}
    if not metadata.get("user_selected") or not str(metadata.get("title", "")).strip():
        return None
    try:
        return MetadataCandidate.model_validate(metadata)
    except ValueError:
        return None


def _is_disc_read_warning(message: str) -> bool:
    lowered = message.casefold()
    return any(
        marker in lowered
        for marker in (
            "l-ec uncorrectable error",
            "scsi error - medium error",
            "status_device_data_error",
        )
    )


def _has_disc_read_warning(job: dict[str, Any]) -> bool:
    warnings = (job.get("metadata") or {}).get("warnings") or []
    return any(
        isinstance(warning, dict) and warning.get("code") == "disc_read_error"
        for warning in warnings
    )


class DiscTooDamagedToOpen(RuntimeError):
    """MakeMKV could not open the disc because of read errors; the user decides whether to rescue it."""


class ImageNotDecryptable(RuntimeError):
    """MakeMKV keeps failing on one spot of a rescued image; reading more of the disc cannot help."""


# MakeMKV prints this once for each damaged spot it skips. Hundreds of repeats for
# the same offset mean it is stuck there, for example on a copy-protected Blu-ray.
MAKEMKV_CORRUPT_SPOT = re.compile(r"The source file '([^']+)' is corrupt or invalid at offset (\d+)")
MAKEMKV_STUCK_REPEATS = 200


def _job_reported_damage(job: dict[str, Any]) -> bool:
    """A read warning, a requested rescue rip, or an earlier rescue all mean a damaged disc."""
    metadata = job.get("metadata") or {}
    return (
        _has_disc_read_warning(job)
        or metadata.get("requested_rip_method") == "sector_rescue"
        or isinstance(metadata.get("recovery"), dict)
    )


RESCUE_STATUS_FIELDS = (
    "phase",
    "total_bytes",
    "rescued_bytes",
    "unreadable_bytes",
    "pending_bytes",
    "movie_unreadable_bytes",
    "movie_pending_bytes",
    "deferred_bytes",
    "position_bytes",
    "read_errors",
    "damaged_areas",
    "in_damaged_zone",
    "rate_bytes_per_second",
    "elapsed_seconds",
    "extra_elapsed_seconds",
    "extra_budget_seconds",
    "method",
    "finished_early",
    "budget_exhausted",
    "retry_round",
    "stop_reason",
    "drive_faults",
    "scope",
    "not_needed_bytes",
)

# Error-correction units: a DVD ECC block is 16 sectors, a Blu-ray cluster 32.
RESCUE_CLUSTER_SECTORS = {DiscKind.DVD: 16, DiscKind.BLURAY: 32}
# Where a read fails, the rescue skips ahead one playback unit. DVDs list their
# units (VOBUs, about half a second); on a Blu-ray 2 MB is about half a second.
RESCUE_SKIP_SECTORS = {DiscKind.DVD: 256, DiscKind.BLURAY: 1024}
RESCUE_STOP_REASONS = {
    "skipped": " (retrying skipped on request)",
    "little_left_to_gain": " (stopped retrying: the last few minutes recovered almost nothing)",
    "budget": " (retry time used up)",
}


RESCUE_QUALITY_WARNING = (
    "Unreadable disc blocks were skipped. The movie may briefly freeze, show blocky pictures, "
    "or jump forward where the disc is damaged."
)

# Ways to get the movie out of a rescued disc image, fastest first. MakeMKV keeps
# the most (chapters, every track) and is quickest when the disc's navigation
# still makes sense to it; FFmpeg ignores that navigation and copies the movie's
# own sectors in about a minute; VLC plays the disc and is the slowest by far, so
# it is only asked once nothing else is left. DiscDock walks the list by itself.
EXTRACTION_METHODS = ("makemkv", "ffmpeg", "vlc")

# Choosing "Finish with what's rescued" accepts the movie as it came back, so the check
# that a copy is long enough only has to catch one that stopped almost immediately.
FINISH_NOW_MINIMUM_SHARE = 0.1

# Work on a movie that is already in the library: the disc is long gone, so these
# stages never hold a drive and never stop the next disc from being ripped.
LIBRARY_REPAIR_STAGES = {"damage_screens", "damage_scan"}


def _link_or_copy(source: Path, target: Path) -> None:
    """Put ``source`` at ``target`` without using disk space where the file system allows it."""
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def _format_bytes(value: int) -> str:
    size = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit in {"B", "KB"} else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _ai_review_detail(plan: dict[str, Any]) -> str:
    if plan.get("segments"):
        return (
            f"AI repair ready to review: {int(plan.get('frame_count') or 0)} frames, "
            f"up to ${float(plan.get('estimated_max_cost_usd') or 0):.2f}"
        )
    return "Damage review ready: too long for AI, keep the movie as it is"


def _moment_entry(item: dict[str, Any], treatment: str) -> dict[str, Any]:
    start, end = float(item["start_seconds"]), float(item["end_seconds"])
    return {
        "start_seconds": round(start, 3),
        "end_seconds": round(end, 3),
        "duration_seconds": round(end - start, 3),
        "missing_seconds": round(float(item.get("missing_seconds") or 0), 3),
        "treatment": treatment,
    }


def _moment_key(item: dict[str, Any]) -> tuple[float, float]:
    return round(float(item["start_seconds"]), 3), round(float(item["end_seconds"]), 3)


def _untreated(item: dict[str, Any]) -> str:
    """What a viewer sees at damage nothing was done about."""
    return "skipped" if loading_screen_worthy(item) else "brief_glitch"


def _damage_record_from_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """The damaged moments of a movie that went through the AI analysis."""
    screens = {_moment_key(item) for item in plan.get("loading_screens_added") or []}
    moments = [
        _moment_entry(segment, "ai_frames" if segment.get("applied") else _untreated(segment))
        for segment in plan.get("segments") or []
    ]
    moments += [
        _moment_entry(item, "loading_screen" if _moment_key(item) in screens else _untreated(item))
        for item in plan.get("skipped") or []
    ]
    return {"analyzed_at": utc_now(), "moments": sorted(moments, key=lambda moment: moment["start_seconds"])}


def _damage_moments_for_job(job: dict[str, Any]) -> list[dict[str, Any]]:
    metadata = job.get("metadata") or {}
    damage = metadata.get("damage")
    if isinstance(damage, dict) and isinstance(damage.get("moments"), list):
        return [dict(moment) for moment in damage["moments"] if isinstance(moment, dict)]
    plan = metadata.get("ai_repair")
    return _damage_record_from_plan(plan)["moments"] if isinstance(plan, dict) else []


def _video_info_from_plan(plan: dict[str, Any]) -> dict[str, Any]:
    sar_text = str(plan.get("sample_aspect_ratio") or "1:1")
    numerator, _, denominator = sar_text.replace("/", ":").partition(":")
    try:
        sar = float(numerator) / float(denominator) if denominator and float(denominator) else 1.0
    except ValueError:
        sar = 1.0
    return {
        "width": int(plan["width"]),
        "height": int(plan["height"]),
        "fps": float(plan["fps"]),
        "fps_text": str(plan.get("fps_text") or ""),
        "sar": sar,
        "sar_text": sar_text,
        "field_order": str(plan.get("field_order") or ""),
        "duration_seconds": float(plan.get("source_duration_seconds") or 0),
    }


def _unread_sectors(rescue_map: RescueMap) -> int:
    """Unread sectors that matter: the movie's, when the rescue knew where the movie is."""
    relevant = rescue_map.meta.get("relevant_pending_sectors")
    if isinstance(relevant, int) and relevant >= 0:
        return relevant
    return rescue_map.count(PENDING)


class DiscDockService:
    def __init__(self, settings_store: SettingsStore, secret_store: SecretStore, database: Database):
        self.settings_store = settings_store
        self.secret_store = secret_store
        self.database = database
        self.settings = settings_store.load()
        self.instance = SingleInstance()
        self.runner = ProcessRunner()
        self.metadata = MetadataService(database, secret_store)
        self.musicbrainz = MusicBrainzClient()
        self.notifications = NotificationService(database, secret_store, lambda: self.settings)
        self.drive_control = DriveControl()
        self.drives: dict[str, DriveInfo] = {}
        self._drive_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._tasks: dict[str, asyncio.Task] = {}
        self._last_progress_write: dict[str, float] = {}
        self._last_progress_log: dict[str, float] = {}
        self._interruptions: dict[str, str] = {}
        self._pending_insertions: dict[str, DriveInfo] = {}
        self._shutting_down = False
        self.monitor = DriveMonitor(
            self.settings.poll_interval_seconds,
            self._on_inserted,
            self._on_removed,
            self._on_snapshot,
            self._drive_io_active,
        )

    def _drive_io_active(self) -> bool:
        return any(
            job["state"] in {JobState.INSPECTING, JobState.RIPPING} for job in self.database.list_jobs(50)
        )

    def _make_mkv(self, settings: AppSettings | None = None) -> MakeMKVClient:
        active = settings or self.settings
        return MakeMKVClient(
            active.make_mkv_path,
            self.runner,
            no_output_timeout=active.process_no_output_timeout_seconds,
        )

    async def start(self) -> None:
        self._shutting_down = False
        self.instance.acquire()
        try:
            self.database.initialize()
            await self.notifications.start()
            # Discs already in a drive when DiscDock starts are not new insertions.
            self._starting = True
            try:
                await self.monitor.reconcile()
            finally:
                self._starting = False
            self.monitor.start()
        except Exception:
            self.instance.release()
            raise

    async def stop(self) -> None:
        self._shutting_down = True
        self._pending_insertions.clear()
        await self.monitor.stop()
        for job_id in list(self._tasks):
            await self.runner.cancel(job_id)
        cancel_captures()
        for task in self._tasks.values():
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        await self.notifications.stop()
        await self.metadata.close()
        await self._musicbrainz().close()
        self.instance.release()

    async def _on_snapshot(self, drives: list[DriveInfo]) -> None:
        self.drives = {drive.id: drive for drive in drives}
        for drive in drives:
            self.database.upsert_drive(drive.model_dump(mode="json"))
        for drive in drives:
            if drive.media_loaded and drive.id in self._pending_insertions:
                await self._drain_pending_insertion(drive.id)

    async def _on_inserted(self, drive: DriveInfo) -> None:
        self.database.append_event(None, "drive.media_inserted", drive.model_dump(mode="json"))
        if not self.settings.auto_rip or self._shutting_down:
            return
        self._pending_insertions[drive.id] = drive
        if self.active_job_for_drive(drive.id):
            self.database.append_event(
                None,
                "drive.autostart_deferred",
                {"drive_id": drive.id, "message": "Waiting for the previous drive job to finish"},
            )
            return
        recoverable = self.recoverable_job_for_disc(drive, await self._disc_id_for(drive))
        if recoverable:
            self._pending_insertions.pop(drive.id, None)
            reason = self._autostart_block_reason(recoverable, at_startup=getattr(self, "_starting", False))
            if reason:
                self.database.append_event(
                    recoverable["id"], "drive.autostart_skipped", {"drive_id": drive.id, "message": reason}
                )
                return
            self.database.append_event(
                recoverable["id"],
                "drive.recovery_autostarted",
                {
                    "drive_id": drive.id,
                    "message": "Auto mode resumed the latest recoverable job for this disc",
                },
            )
            try:
                await self.retry_job(recoverable["id"])
            except Exception as error:
                self.database.append_event(
                    recoverable["id"],
                    "drive.autostart_failed",
                    {"drive_id": drive.id, "message": str(error)},
                )
            return
        await self._drain_pending_insertion(drive.id)

    async def _on_removed(self, drive: DriveInfo) -> None:
        self.database.append_event(None, "drive.media_removed", drive.model_dump(mode="json"))
        self._pending_insertions.pop(drive.id, None)
        job = self.active_job_for_drive(drive.id)
        if job and job["state"] == JobState.EJECTING:
            return
        if job and job["state"] not in {JobState.TRANSCODING, JobState.FINALIZING}:
            self._interruptions[job["id"]] = "The disc or drive was removed during processing."
            await self.runner.cancel(job["id"])
            cancel_captures(job["id"])
            task = self._tasks.get(job["id"])
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            else:
                self._mark_cancelled(job["id"])

    async def _drain_pending_insertion(self, drive_id: str) -> None:
        if self._shutting_down or not self.settings.auto_rip:
            return
        pending = self._pending_insertions.get(drive_id)
        current = self.drives.get(drive_id)
        if not pending or not current or not current.media_loaded:
            self._pending_insertions.pop(drive_id, None)
            return
        if self.active_job_for_drive(drive_id):
            return
        recoverable = self.recoverable_job_for_disc(current, await self._disc_id_for(current))
        if recoverable:
            self._pending_insertions.pop(drive_id, None)
            reason = self._autostart_block_reason(recoverable, at_startup=getattr(self, "_starting", False))
            if reason:
                self.database.append_event(
                    recoverable["id"], "drive.autostart_skipped", {"drive_id": drive_id, "message": reason}
                )
                return
            try:
                await self.retry_job(recoverable["id"])
            except Exception as error:
                self.database.append_event(
                    recoverable["id"],
                    "drive.autostart_failed",
                    {"drive_id": drive_id, "message": str(error)},
                )
            return
        self._pending_insertions.pop(drive_id, None)
        try:
            await self.create_job(drive_id, manual=False, refresh=False)
        except Exception as error:
            # Keep the insertion pending so a later monitor snapshot can retry a
            # transient conflict, such as a player still releasing the drive.
            if self.drives.get(drive_id, pending).media_loaded:
                self._pending_insertions[drive_id] = self.drives.get(drive_id, pending)
            self.database.append_event(
                None, "drive.autostart_failed", {"drive_id": drive_id, "message": str(error)}
            )

    def _track_job_task(self, job_id: str, drive_id: str, task: asyncio.Task) -> None:
        # A new task for the job may scan with FFprobe again after an earlier cancel.
        begin_captures(job_id)
        self._tasks[job_id] = task
        task.add_done_callback(lambda completed: self._job_task_finished(job_id, drive_id, completed))

    def _spawn(self, coroutine: Coroutine[Any, Any, Any], name: str) -> asyncio.Task:
        """Start a background task and keep a reference to it.

        The event loop holds only weak references to tasks, so an unreferenced
        task can be garbage collected before it finishes.
        """
        tasks: set[asyncio.Task] = self.__dict__.setdefault("_background_tasks", set())
        task = asyncio.create_task(coroutine, name=name)
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return task

    def _job_task_finished(self, job_id: str, drive_id: str, task: asyncio.Task) -> None:
        if self._tasks.get(job_id) is task:
            self._tasks.pop(job_id, None)
        getattr(self, "_last_progress_write", {}).pop(job_id, None)
        getattr(self, "_last_progress_log", {}).pop(job_id, None)
        getattr(self, "_last_rescue_write", {}).pop(job_id, None)
        if self._shutting_down or drive_id not in self._pending_insertions:
            return
        self._spawn(self._drain_pending_insertion(drive_id), f"pending-disc-{drive_id}")

    async def refresh_drives(self) -> list[DriveInfo]:
        if self._drive_io_active():
            return list(self.drives.values())
        return await self.monitor.reconcile()

    def busy_jobs(self) -> list[str]:
        """Titles of the jobs working on a disc; a job waiting for a choice can be left for later."""
        working = {state.value for state in ACTIVE_JOB_STATES} - {JobState.AWAITING_INPUT.value}
        return [str(job.get("title") or job["id"]) for job in self.database.list_jobs(250) if job["state"] in working]

    def active_job_for_drive(self, drive_id: str) -> dict[str, Any] | None:
        for job in self.database.list_jobs(250):
            # Repairing a finished movie in the library does not use the drive.
            if job.get("stage") in LIBRARY_REPAIR_STAGES and job.get("completed_at"):
                continue
            if job["drive_id"] == drive_id and job["state"] in {state.value for state in ACTIVE_JOB_STATES}:
                return job
        return None

    def _drive_for_job(self, job: dict[str, Any]) -> DriveInfo | None:
        """The drive a job uses, following it to its new id after a USB reconnect."""
        drives: dict[str, DriveInfo] = getattr(self, "drives", {}) or {}
        drive = drives.get(str(job.get("drive_id") or ""))
        if drive is not None:
            return drive
        letter = str(job.get("drive_letter") or "").rstrip(":").upper()
        matches = [item for item in drives.values() if item.letter.rstrip(":").upper() == letter]
        if not matches and len(drives) == 1:
            matches = list(drives.values())
        if not matches:
            return None
        drive = matches[0]
        if job.get("id"):
            self.database.update_job(str(job["id"]), drive_id=drive.id, drive_letter=drive.letter)
            job["drive_id"] = drive.id
            job["drive_letter"] = drive.letter
        return drive

    @staticmethod
    def _placeholder_drive(job: dict[str, Any]) -> DriveInfo:
        """Stand-in for a disconnected drive when a saved rescue makes the disc unnecessary."""
        return DriveInfo(
            id=str(job.get("drive_id") or "disconnected"),
            letter=str(job.get("drive_letter") or "D:"),
            name="Disconnected drive",
            media_loaded=False,
            disc_kind=DiscKind(job.get("disc_type") or DiscKind.UNKNOWN),
        )

    @staticmethod
    def _autostart_block_reason(job: dict[str, Any], *, at_startup: bool) -> str:
        """Why Auto mode must not resume this job for the disc in the drive right now, or ""."""
        if job.get("error_code") == "image_not_decryptable":
            return "MakeMKV cannot read this disc's rescued image, so resuming it again would not help"
        if at_startup and job.get("state") != JobState.INTERRUPTED:
            # A stopped or failed job, for example after a drive fault, waits for the
            # disc to be reinserted or the drive to be reconnected.
            return "The disc was already in the drive when DiscDock started; reinsert it to resume the job"
        return ""

    @staticmethod
    async def _disc_id_for(drive: DriveInfo) -> str:
        """An audio CD's MusicBrainz DiscID, read from its table of contents; "" for other discs."""
        if drive.disc_kind != DiscKind.AUDIO_CD:
            return ""
        return await asyncio.to_thread(read_disc_id, drive.letter)

    def recoverable_job_for_disc(self, drive: DriveInfo, disc_id: str = "") -> dict[str, Any] | None:
        """Avoid duplicate Auto-mode jobs when a saved recovery is available."""
        label = drive.volume_label.casefold()
        audio_cd = drive.disc_kind == DiscKind.AUDIO_CD
        if audio_cd and not disc_id:
            # Windows labels every audio CD "Audio CD", so only the DiscID tells one CD from another.
            # Without it, a new job is safer than resuming the job of another CD.
            return None
        if not audio_cd and not label:
            # Many discs have no label, so it identifies nothing.
            return None
        # A USB drive can come back with a new id after it is reconnected, so a
        # job whose drive is no longer connected still belongs to this disc.
        connected = set(getattr(self, "drives", {}) or {})
        for job in self.database.list_jobs(250):
            if audio_cd:
                cd = (job.get("metadata") or {}).get("cd")
                same_disc = isinstance(cd, dict) and cd.get("discid") == disc_id
            else:
                same_disc = job.get("disc_type") != DiscKind.AUDIO_CD and (
                    str(job.get("disc_label") or "").casefold() == label
                )
            if (
                (job.get("drive_id") == drive.id or job.get("drive_id") not in connected)
                and same_disc
                and bool(job.get("recoverable"))
                and job.get("state")
                in {
                    JobState.FAILED,
                    JobState.CANCELLED,
                    JobState.INTERRUPTED,
                    JobState.BLOCKED,
                    # A damage review whose disc comes back reads its skipped spots again.
                    JobState.AWAITING_REPAIR,
                }
            ):
                return job
        return None

    def health(self) -> dict[str, Any]:
        directories = self.settings.resolved_directories()
        tools = {
            "makemkv": bool(self.settings.make_mkv_path and Path(self.settings.make_mkv_path).is_file()),
            "handbrake": bool(self.settings.handbrake_path and Path(self.settings.handbrake_path).is_file()),
            "ffmpeg": bool(self.settings.ffmpeg_path and Path(self.settings.ffmpeg_path).is_file()),
            "ffprobe": bool(self.settings.ffprobe_path and Path(self.settings.ffprobe_path).is_file()),
            "vlc": bool(self.settings.vlc_path and Path(self.settings.vlc_path).is_file()),
            "cyanrip": bool(self.settings.cyanrip_path and Path(self.settings.cyanrip_path).is_file()),
        }
        required_ok = tools["makemkv"] and all(
            path.exists() and os.access(path, os.W_OK) for key, path in directories.items() if key != "root"
        )
        return {
            "ok": required_ok,
            "service": "online",
            "version": __version__,
            "tools": tools,
            "drive_count": len(self.drives),
            "configured": required_ok,
            "automatic_ripping": self.settings.auto_rip,
            "drive_blockers": self._external_drive_blockers(),
        }

    def _external_drive_blockers(self, letter: str = "") -> list[str]:
        owned = self.runner.active_pids() | self.drive_control.preview_pids()
        blockers: set[str] = set()
        for process in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                if int(process.info["pid"]) in owned:
                    continue
                name = str(process.info.get("name") or "").lower()
                command = " ".join(process.info.get("cmdline") or []).lower()
                if name in {"makemkv.exe", "makemkvcon.exe", "makemkvcon64.exe"}:
                    blockers.add("MakeMKV is open outside DiscDock")
                elif name == "vlc.exe" and (not letter or letter.lower() in command):
                    blockers.add("VLC has the disc open outside DiscDock")
            except (psutil.AccessDenied, psutil.NoSuchProcess, ValueError):
                continue
        return sorted(blockers)

    async def create_job(
        self,
        drive_id: str,
        manual: bool = False,
        media_kind: MediaKind | None = None,
        *,
        refresh: bool = True,
        rip_method: str = "normal",
    ) -> dict[str, Any]:
        if refresh:
            await self.refresh_drives()
        drive = self.drives.get(drive_id)
        if not drive:
            raise LookupError("Optical drive not found")
        if not drive.media_loaded:
            raise RuntimeError("No disc is loaded")
        if rip_method not in {"normal", "sector_rescue"}:
            raise RuntimeError("Unknown rip method")
        if rip_method == "sector_rescue" and drive.disc_kind not in {DiscKind.DVD, DiscKind.BLURAY}:
            raise RuntimeError("Rescue ripping is available for DVDs and Blu-rays")
        blockers = self._external_drive_blockers(drive.letter)
        if blockers:
            raise RuntimeError("; ".join(blockers) + ". Close it before starting a rip.")
        existing = self.active_job_for_drive(drive_id)
        if existing:
            raise RuntimeError(f"Drive already belongs to job {existing['id']}")
        job_id = str(uuid.uuid4())
        staging = self.settings.resolved_directories()["raw"] / f"{job_id}.partial"
        job = {
            "id": job_id,
            "drive_id": drive.id,
            "drive_letter": drive.letter,
            "disc_label": drive.volume_label,
            "disc_type": drive.disc_kind.value,
            "media_kind": (media_kind or MediaKind.UNKNOWN).value,
            "state": JobState.DETECTED.value,
            "stage": "detected",
            "status_detail": "Disc detected",
            "staging_path": str(staging),
            "settings": {**self.settings.public_dict(), "manual": manual},
            "metadata": {"requested_rip_method": rip_method},
        }
        self.database.create_job(job)
        task = asyncio.create_task(self._process_job(job_id, manual), name=f"job-{job_id}")
        self._track_job_task(job_id, drive.id, task)
        return self.database.get_job(job_id) or job

    def _append_log(self, job_id: str, line: str) -> None:
        safe = line
        for secret in self.secret_store.all().values():
            if secret:
                safe = safe.replace(secret, "[redacted]")
        path = self.settings.resolved_directories()["logs"] / f"{job_id}.log"
        with path.open("a", encoding="utf-8", errors="replace") as handle:
            handle.write(f"{utc_now()} {safe}\n")

    @staticmethod
    def _dvd_recovery_title_number(
        job_id: str, track: dict[str, Any], settings: AppSettings
    ) -> int:
        """Return VLC's DVD title number, including for pre-migration jobs."""
        stored = int(track.get("disc_title_number") or 0)
        if stored > 0:
            return stored

        # v1.5.0 stored only MakeMKV's internal zero-based track id. Preserve
        # retry compatibility by recovering TINFO code 24 from the existing
        # per-job log instead of forcing another minute-long disc inspection.
        log_path = settings.resolved_directories()["logs"] / f"{job_id}.log"
        source_id = int(track.get("source_id") or 0)
        pattern = re.compile(rf'TINFO:{source_id},24,\d+,"(\d+)"')
        try:
            matches = pattern.findall(log_path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            matches = []
        if matches:
            return max(1, int(matches[-1]))
        return source_id + 1

    async def _process_event(self, job_id: str, event: dict) -> None:
        event_type = str(event.get("type") or "")
        message = str(event.get("message") or "")
        now = time.monotonic()
        # MakeMKV can emit dozens of progress records per second. Keep periodic
        # samples instead of opening the log file for every single tick.
        should_log_progress = now - self._last_progress_log.get(job_id, 0) >= 5.0
        if message and (event_type != "progress" or should_log_progress):
            await asyncio.to_thread(self._append_log, job_id, message)
            if event_type == "progress":
                self._last_progress_log[job_id] = now
        if message and _is_disc_read_warning(message):
            job = self.database.get_job(job_id)
            if job:
                metadata = dict(job.get("metadata") or {})
                warnings = list(metadata.get("warnings") or [])
                if not any(warning.get("code") == "disc_read_error" for warning in warnings):
                    warnings.append(
                        {
                            "code": "disc_read_error",
                            "message": "The drive is retrying an unreadable part of the disc.",
                            "first_seen": utc_now(),
                        }
                    )
                    metadata["warnings"] = warnings
                    self.database.update_job(
                        job_id,
                        metadata_json=json.dumps(metadata, ensure_ascii=False),
                        status_detail="Disc read problem — MakeMKV is retrying",
                    )
                    # Unattended stations can skip MakeMKV's hours of retries and
                    # go straight to the rescue. Only a normal video rip switches,
                    # so the rescue's own extraction can never trigger it again.
                    if (
                        self._settings_for_job(job).damaged_disc_action == "best_effort"
                        and job.get("disc_type") in {DiscKind.DVD, DiscKind.BLURAY}
                        and job.get("state") == JobState.RIPPING
                        and job.get("stage") == "ripping"
                    ):

                        async def switch_to_best_effort() -> None:
                            try:
                                await asyncio.to_thread(
                                    self._append_log,
                                    job_id,
                                    "Unreadable block reported; switching to best-effort recovery as set in Settings",
                                )
                                await self.recover_damaged_job(job_id)
                            except Exception as error:
                                await asyncio.to_thread(
                                    self._append_log,
                                    job_id,
                                    f"Automatic best-effort recovery could not start: {error}",
                                )

                        self._spawn(switch_to_best_effort(), f"auto-best-effort-{job_id}")
        if event_type == "recovery_gap":
            job = self.database.get_job(job_id)
            if job:
                metadata = dict(job.get("metadata") or {})
                recovery = dict(metadata.get("recovery") or {})
                gaps = list(recovery.get("sector_gaps") or [])
                start = int(event.get("start_byte") or 0)
                end = int(event.get("end_byte") or start)
                gap = {
                    "start_byte": start,
                    "end_byte": end,
                    "bytes": max(0, end - start),
                }
                if gap["bytes"] and gap not in gaps:
                    gaps.append(gap)
                recovery["sector_gaps"] = gaps
                recovery["skipped_bytes"] = sum(int(item.get("bytes") or 0) for item in gaps)
                metadata["recovery"] = recovery
                self.database.update_job(
                    job_id,
                    metadata_json=json.dumps(metadata, ensure_ascii=False),
                    status_detail="Unreadable sectors skipped — recovery continued",
                )
        if event_type == "rescue_status":
            status = event.get("status") if isinstance(event.get("status"), dict) else {}
            final = bool(event.get("final"))
            writes = self.__dict__.setdefault("_last_rescue_write", {})
            if final or now - writes.get(job_id, 0) >= 5.0:
                writes[job_id] = now
                job = self.database.get_job(job_id)
                if job:
                    metadata = dict(job.get("metadata") or {})
                    recovery = dict(metadata.get("recovery") or {})
                    recovery.update({key: status[key] for key in RESCUE_STATUS_FIELDS if key in status})
                    recovery["updated_at"] = utc_now()
                    metadata["recovery"] = recovery
                    changes: dict[str, Any] = {"metadata_json": json.dumps(metadata, ensure_ascii=False)}
                    if not final:
                        changes["status_detail"] = describe_rescue_progress(status)
                    self.database.update_job(job_id, **changes)
            return
        if (
            event_type == "rescue_event"
            and isinstance(event.get("event"), dict)
            and event["event"].get("type") == "waiting"
            and message
        ):
            # For example the drive failing to read while the rescue finds the movie.
            self.database.update_job(job_id, status_detail=message)
        if event_type == "stage":
            self.database.update_job(job_id, status_detail=message or "Working")
        elif (
            event_type == "progress"
            and now - self._last_progress_write.get(job_id, 0) >= 2.0
        ):
            self._last_progress_write[job_id] = now
            self.database.update_job(
                job_id, progress=max(0, min(100, float(event.get("percent", 0))))
            )

    @staticmethod
    def _disc_fingerprint(drive: DriveInfo, scan: DiscScan | None, job_id: str = "") -> str:
        material: dict[str, Any] = {"kind": drive.disc_kind.value, "label": drive.volume_label.casefold()}
        if scan:
            material["titles"] = [
                [title.id, title.duration_seconds, title.size_bytes, title.chapters] for title in scan.titles
            ]
        elif drive.disc_kind in {DiscKind.AUDIO_CD, DiscKind.DATA}:
            # A volume label is not a safe identifier (many audio CDs have none).
            # Until a TOC/filesystem signature is available, prefer no automatic
            # duplicate match over a false positive that could skip another disc.
            material["session"] = job_id
        return hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()

    @staticmethod
    def _settings_for_job(job: dict[str, Any]) -> AppSettings:
        return AppSettings.model_validate(job.get("settings") or {})

    def _mark_cancelled(self, job_id: str) -> None:
        interruption = self._interruptions.pop(job_id, "")
        if interruption:
            self.database.update_job(
                job_id,
                state=JobState.INTERRUPTED,
                stage="media_removed",
                status_detail="Disc removed",
                recoverable=1,
                error_code="media_removed",
                error_message=interruption,
                process_pid=None,
            )
        else:
            self.database.update_job(
                job_id,
                state=JobState.CANCELLED,
                stage="cancelled",
                status_detail="Cancelled safely",
                recoverable=1,
                process_pid=None,
            )

    async def _inspect_disc(self, job_id: str, drive: DriveInfo, settings: AppSettings) -> DiscScan:
        """MakeMKV's titles of the disc, taken from a rescued image when the disc is too damaged to open."""
        job = self.database.get_job(job_id) or {}
        if not (job.get("metadata") or {}).get("titles_from_rescue"):
            make_mkv = self._make_mkv(settings)
            if drive.disc_kind == DiscKind.BLURAY and hasattr(make_mkv, "no_output_timeout"):
                # Decrypting a Blu-ray can keep MakeMKV silent for minutes on a slow drive.
                make_mkv.no_output_timeout = max(make_mkv.no_output_timeout, 600)
            try:
                return await make_mkv.inspect(
                    job_id,
                    drive.letter,
                    settings.min_length_seconds,
                    settings.max_length_seconds,
                    settings.inspect_timeout_seconds,
                    lambda event: self._process_event(job_id, event),
                )
            except MakeMKVLicenseError:
                raise
            except ProcessFailure as error:
                latest = self.database.get_job(job_id) or job
                if (
                    isinstance(error, NoVideoTitles)
                    and error.too_short
                    and settings.min_length_seconds > 0
                    and not _has_disc_read_warning(latest)
                ):
                    return await self._inspect_short_titles(job_id, drive, settings, make_mkv, error.too_short)
                if drive.disc_kind not in {DiscKind.DVD, DiscKind.BLURAY} or not _has_disc_read_warning(latest):
                    raise
                if settings.damaged_disc_action != "best_effort":
                    raise DiscTooDamagedToOpen(
                        "MakeMKV cannot open this disc because parts of it are unreadable. Choose Recover: DiscDock "
                        "reads the disc's file system first, lets MakeMKV find the movie in that copy, and then "
                        "reads the movie while skipping what cannot be read."
                    ) from error
                await asyncio.to_thread(
                    self._append_log,
                    job_id,
                    f"MakeMKV could not open the damaged disc ({error}); reading its file system first, as set in Settings",
                )
        return await self._scan_rescued_structures(job_id, drive, settings)

    async def _inspect_short_titles(
        self, job_id: str, drive: DriveInfo, settings: AppSettings, make_mkv: Any, too_short: int
    ) -> DiscScan:
        """List the titles again without a minimum length when every title on the disc is shorter than it."""
        await asyncio.to_thread(
            self._append_log,
            job_id,
            f"All {too_short} titles on this disc are shorter than the minimum title length in Settings "
            f"({settings.min_length_seconds / 60:g} minutes); listing them all so you can choose what to rip",
        )
        self.database.update_job(job_id, status_detail="Every title is shorter than the minimum length — listing them all")
        scan = await make_mkv.inspect(
            job_id,
            drive.letter,
            0,
            settings.max_length_seconds,
            settings.inspect_timeout_seconds,
            lambda event: self._process_event(job_id, event),
        )
        # MakeMKV numbers the titles within its length filter, so this job keeps ripping without a minimum.
        settings.min_length_seconds = 0
        job = self.database.get_job(job_id) or {}
        self.database.update_job(
            job_id,
            settings_json=json.dumps({**(job.get("settings") or {}), "min_length_seconds": 0}, ensure_ascii=False),
            metadata_json=json.dumps({**(job.get("metadata") or {}), "short_titles_only": True}, ensure_ascii=False),
        )
        return scan

    async def _look_at_data_disc(self, job_id: str, drive: DriveInfo) -> None:
        """List what a data disc holds, so it can be recognised before it is backed up.

        Windows has the disc mounted, so its own file system answers for ISO 9660,
        Joliet and UDF alike. A disc it cannot read is still backed up as an image;
        only the listing is missing.
        """
        self.database.update_job(job_id, status_detail="Looking at what is on the disc")
        try:
            contents = await asyncio.to_thread(list_drive_files, drive.letter)
        except (OSError, ValueError) as error:
            await asyncio.to_thread(
                self._append_log, job_id, f"The files on the disc could not be listed: {error}"
            )
            self._merge_job_metadata(
                job_id,
                {"disc_contents": {"kind": "files", "summary": "Windows could not list the files on this disc.",
                                   "file_count": 0, "total_bytes": 0, "entries": [], "top_level": [],
                                   "unreadable": True}},
            )
            return
        described = describe_contents(contents, drive.volume_label)
        self._merge_job_metadata(job_id, {"disc_contents": described})
        await asyncio.to_thread(
            self._append_log,
            job_id,
            f"The disc holds {contents.file_count} files, {_format_bytes(contents.total_bytes)}"
            f" · {described['summary']}"
            + (f" {contents.note}" if contents.note else ""),
        )

    async def _back_up_data_disc(
        self, job_id: str, drive: DriveInfo, job: dict[str, Any], staging: Path
    ) -> None:
        """Copy the whole disc into an image, then check the image holds every file the disc showed."""
        name = safe_component(str(job.get("title") or job.get("disc_label") or "Disc backup"))
        image = staging / f"{name}.iso"
        described = (job.get("metadata") or {}).get("disc_contents")
        described = described if isinstance(described, dict) else {}
        await DataDiscRipper(self.runner).rip(
            job_id, drive.letter, image, callback=lambda event: self._process_event(job_id, event)
        )
        listed = [
            DiscEntry(str(item.get("path") or ""), int(item.get("size") or 0))
            for item in described.get("entries") or []
            if isinstance(item, dict) and item.get("path")
        ]
        if listed:
            self.database.update_job(job_id, status_detail="Checking the backup against the disc")
            contents = DiscContents(listed, int(described.get("total_bytes") or 0), bool(described.get("truncated")))
            check = await asyncio.to_thread(check_backup, contents, image)
            if not check.complete:
                raise RuntimeError(f"The backup does not hold everything on the disc: {check.reason}")
            await asyncio.to_thread(
                self._append_log,
                job_id,
                f"The backup holds all {check.files_on_disc} files of the disc "
                f"({check.files_in_image} in the image's own file system)",
            )
        else:
            await asyncio.to_thread(
                self._append_log,
                job_id,
                "The disc's files could not be listed, so the backup is checked by its size only",
            )
        await asyncio.to_thread(self._write_backup_notes, staging, image, job, described)

    @staticmethod
    def _write_backup_notes(staging: Path, image: Path, job: dict[str, Any], described: dict[str, Any]) -> None:
        """Write what is in the backup and how to use it, next to the image."""
        title = str(job.get("title") or job.get("disc_label") or "This disc")
        kind = str(described.get("kind") or "files")
        entries = [item for item in described.get("entries") or [] if isinstance(item, dict)]
        lines = [
            f"{title}",
            f"Disc label: {job.get('disc_label') or 'none'}",
            f"Backed up: {utc_now()}",
            f"Files: {described.get('file_count') or len(entries)}",
            f"Size: {_format_bytes(int(described.get('total_bytes') or 0))}",
            "",
        ]
        if described.get("note"):
            lines += [str(described["note"]), ""]
        lines += [f"{item.get('path')}\t{item.get('size')}" for item in entries]
        (staging / "Disc contents.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        playing = (
            [
                "This backup is the disc itself, as an image file.",
                "",
                "To play or install it:",
                f"  1. Double-click {image.name}. Windows attaches it as a drive.",
                "  2. Open that drive and run the game's setup or autorun program.",
                "  3. Leave the drive attached while you play, in case the game reads from it.",
                "  4. When you are done, right-click the drive and choose Eject.",
                "",
                "Some discs check for the original disc in the drive with their own protection.",
                "Those refuse to run from any backup; DiscDock copies the disc, it does not remove protection.",
            ]
            if kind in {"game", "software"}
            else [
                "This backup is the disc itself, as an image file.",
                "",
                "To open it:",
                f"  1. Double-click {image.name}. Windows attaches it as a drive.",
                "  2. Copy what you need out of that drive.",
                "  3. When you are done, right-click the drive and choose Eject.",
            ]
        )
        playing += [
            "",
            "Disc contents.txt lists every file in this backup, so you can search it without opening the image.",
        ]
        (staging / "How to use this backup.txt").write_text("\n".join(playing) + "\n", encoding="utf-8")

    async def _scan_rescued_structures(self, job_id: str, drive: DriveInfo, settings: AppSettings) -> DiscScan:
        """Read a damaged disc's file system and navigation data, then let MakeMKV list the titles from that copy."""
        job = self.database.get_job(job_id) or {}
        metadata = dict(job.get("metadata") or {})
        metadata["titles_from_rescue"] = True
        metadata["requested_rip_method"] = "sector_rescue"
        self.database.update_job(
            job_id,
            state=JobState.RIPPING,
            stage="recovering",
            status_detail="Reading the damaged disc's file system and navigation data",
            progress=0,
            metadata_json=json.dumps(metadata, ensure_ascii=False),
            recoverable=1,
        )
        image = self._rescue_image(job_id, settings)
        self._update_recovery_metadata(
            job_id, image_path=str(image), engine="discdock-rescue-3", quality_warning=RESCUE_QUALITY_WARNING
        )
        await self._run_sector_rescue(job_id, drive, settings, image, None, structures_only=True, extra_seconds=0)
        partial = DvdSectorRescue.artifact_paths(image)["partial"]
        scan = await self._scan_image(job_id, settings, partial if partial.is_file() else image)
        if scan is None:
            await asyncio.to_thread(
                self._append_log,
                job_id,
                "MakeMKV could not list the titles from the file system alone; reading the whole disc once",
            )
            self.database.update_job(job_id, status_detail="Reading the whole damaged disc so MakeMKV can find the titles")
            await self._run_sector_rescue(job_id, drive, settings, image, None, whole_disc=True, extra_seconds=0)
            scan = await self._scan_image(job_id, settings, image)
        if scan is None:
            # MakeMKV calls a title with damaged navigation fake and then lists none at all.
            # The disc's own tables still name the titles, and FFmpeg can copy one out of the image.
            scan = await asyncio.to_thread(self._titles_from_disc_tables, image if image.is_file() else partial)
            if scan is None:
                raise RuntimeError("MakeMKV found no titles in the rescued copy of the disc either")
            await asyncio.to_thread(
                self._append_log,
                job_id,
                f"MakeMKV found no titles; the disc's own tables list {len(scan.titles)}: "
                + ", ".join(
                    f"title {title.disc_title_number} ({title.duration_seconds // 60} min)"
                    for title in scan.titles[:8]
                ),
            )
        else:
            await asyncio.to_thread(
                self._append_log, job_id, f"MakeMKV found {len(scan.titles)} titles in the rescued copy of the disc"
            )
        self.database.update_job(
            job_id,
            state=JobState.INSPECTING,
            stage="inspecting",
            status_detail="Titles found in the rescued copy of the disc",
            progress=0,
        )
        return scan

    @staticmethod
    def _titles_from_disc_tables(path: Path) -> DiscScan | None:
        """The titles a rescued DVD image lists in its own navigation, for a disc MakeMKV gave up on.

        Each title is described well enough to pick one and copy its sectors with
        FFmpeg: how long it plays, how many chapters it has, and how much video it
        holds. MakeMKV never sees these titles, so nothing else can extract them.
        """
        try:
            with path.open("rb") as handle:

                def read(lba: int, count: int) -> bytes:
                    handle.seek(lba * SECTOR_SIZE)
                    return handle.read(count * SECTOR_SIZE).ljust(count * SECTOR_SIZE, b"\0")

                total_sectors = path.stat().st_size // SECTOR_SIZE
                files = read_video_ts_files(read, total_sectors)
                found = dvd_titles(read, files)
                titles: list[TitleInfo] = []
                for index, title in enumerate(sorted(found, key=lambda item: -item.duration_seconds)):
                    ranges = dvd_title_video_ranges(read, total_sectors, title.number)
                    size = sum(end - start for start, end in ranges) * SECTOR_SIZE
                    if title.duration_seconds <= 0 or size <= 0:
                        continue
                    titles.append(
                        TitleInfo(
                            id=index,
                            disc_title_number=title.number,
                            name=f"Title {title.number}",
                            duration_seconds=title.duration_seconds,
                            size_bytes=size,
                            chapters=title.chapters,
                            filename=f"title_t{index:02d}.mkv",
                            description="Listed in the disc's own navigation, not by MakeMKV",
                        )
                    )
        except (OpticalError, OSError, ValueError):
            return None
        if not titles:
            return None
        return DiscScan(title_count=len(titles), titles=titles)

    async def _scan_image(self, job_id: str, settings: AppSettings, path: Path) -> DiscScan | None:
        """MakeMKV's titles in a (partly) rescued disc image, or None when it finds none."""

        async def scan_event(event: dict) -> None:
            if event.get("type") != "progress":
                await self._process_event(job_id, event)

        link = path.with_name("titles-scan.iso")
        source = path
        try:
            if path.suffix.lower() != ".iso":
                # MakeMKV opens a disc image by its .iso name; a hard link costs no space.
                link.unlink(missing_ok=True)
                try:
                    os.link(path, link)
                    source = link
                except OSError:
                    source = path
            make_mkv = self._make_mkv(settings)
            if hasattr(make_mkv, "no_output_timeout"):
                # MakeMKV can work through a large disc image for minutes without printing anything.
                make_mkv.no_output_timeout = max(make_mkv.no_output_timeout, 600)
            return await make_mkv.inspect_source(
                job_id,
                f"iso:{source}",
                settings.min_length_seconds,
                max(settings.inspect_timeout_seconds, 2 * 3600),
                scan_event,
            )
        except MakeMKVLicenseError:
            raise
        except ProcessFailure as error:
            await asyncio.to_thread(self._append_log, job_id, f"MakeMKV could not list titles in the rescued image: {error}")
            return None
        finally:
            if source == link:
                link.unlink(missing_ok=True)

    async def _process_job(self, job_id: str, manual: bool = False) -> None:
        job = self.database.get_job(job_id)
        if not job:
            return
        drive = self._drive_for_job(job)
        if not drive:
            await self._fail(job_id, "drive_missing", "The optical drive is no longer available.")
            return
        settings = self._settings_for_job(job)
        requested_kind = (
            MediaKind(job["media_kind"]) if job["media_kind"] != MediaKind.UNKNOWN else None
        )
        is_video = drive.disc_kind not in {DiscKind.AUDIO_CD, DiscKind.DATA}
        metadata_task: asyncio.Task[MetadataCandidate | None] | None = None
        if settings.omdb_enabled and is_video and drive.volume_label:
            # Metadata lookup does not use the optical drive, so it can run in
            # parallel with MakeMKV's physical disc inspection.
            metadata_task = asyncio.create_task(
                self.metadata.identify(drive.volume_label, requested_kind),
                name=f"metadata-{job_id}",
            )
        async with self._drive_locks[drive.id]:
            try:
                scan: DiscScan | None = None
                tracks: list[dict[str, Any]] = []
                if drive.disc_kind in {DiscKind.BLURAY, DiscKind.DVD, DiscKind.UNKNOWN}:
                    self.database.update_job(
                        job_id,
                        state=JobState.INSPECTING,
                        stage="inspecting",
                        status_detail="Reading disc information — title search is available",
                        progress=0,
                    )
                    scan = await self._inspect_disc(job_id, drive, settings)
                    drive.make_mkv_index = scan.drive_index
                    self.database.upsert_drive(drive.model_dump(mode="json"))
                    selected = select_disc_titles(scan.titles, settings)
                    selected_ids = {title.id for title in selected}
                    tracks = [
                        {**title.model_dump(), "selected": title.id in selected_ids} for title in scan.titles
                    ]
                    self.database.replace_tracks(job_id, tracks)
                elif drive.disc_kind == DiscKind.AUDIO_CD or drive.disc_kind == DiscKind.DATA:
                    tracks = []
                    if drive.disc_kind == DiscKind.DATA:
                        await self._look_at_data_disc(job_id, drive)

                fingerprint = self._disc_fingerprint(drive, scan, job_id)
                duplicate = self.database.query(
                    """SELECT id,title,year,media_kind,output_path FROM jobs
                       WHERE fingerprint=? AND state='completed' AND id<>?
                       ORDER BY completed_at DESC, created_at DESC LIMIT 1""",
                    (fingerprint, job_id),
                )
                refreshed_job = self.database.get_job(job_id) or job
                duplicate_override = bool((refreshed_job.get("metadata") or {}).get("duplicate_override"))
                duplicate_job = duplicate[0] if duplicate else None
                if duplicate_job and settings.duplicate_policy == "skip" and not duplicate_override:
                    if settings.auto_eject:
                        self.database.update_job(
                            job_id,
                            state=JobState.EJECTING,
                            stage="ejecting_duplicate",
                            status_detail="Ejecting an already completed disc",
                            fingerprint=fingerprint,
                        )
                        try:
                            await self.drive_control.eject(drive.letter)
                        except OSError as error:
                            await asyncio.to_thread(self._append_log, job_id, f"Eject warning: {error}")
                    self.database.update_job(
                        job_id,
                        fingerprint=fingerprint,
                        title=duplicate_job["title"],
                        year=duplicate_job["year"],
                        media_kind=duplicate_job["media_kind"],
                        state=JobState.COMPLETED,
                        stage="skipped_duplicate",
                        status_detail="Skipped because this disc is already complete",
                        progress=100,
                        output_path=duplicate_job["output_path"],
                        completed_at=utc_now(),
                        recoverable=0,
                        error_code=None,
                        error_message=None,
                    )
                    await self.notifications.send(
                        job_id,
                        "completed",
                        "Duplicate disc skipped",
                        f"{duplicate_job['title'] or 'This disc'} was already completed.",
                    )
                    return
                if duplicate_job and settings.duplicate_policy == "ask" and not duplicate_override:
                    self.database.update_job(
                        job_id,
                        fingerprint=fingerprint,
                        state=JobState.BLOCKED,
                        stage="duplicate",
                        status_detail="This disc has already been completed",
                        error_code="duplicate",
                        error_message=(
                            f"Previously completed as {duplicate_job['title'] or duplicate_job['id']}"
                        ),
                        recoverable=1,
                    )
                    await self.notifications.send(
                        job_id,
                        "attention",
                        "DiscDock needs attention",
                        "This disc appears to be a duplicate.",
                    )
                    return

                self.database.update_job(
                    job_id,
                    fingerprint=fingerprint,
                    state=JobState.IDENTIFYING,
                    stage="identifying",
                    status_detail="Finding title and artwork",
                    progress=0,
                )
                fallback_title, fallback_year = title_from_label(
                    drive.volume_label or (scan.disc_name if scan else "")
                )
                latest_job = self.database.get_job(job_id) or refreshed_job
                candidate = _user_metadata_candidate(latest_job)
                if candidate is not None and metadata_task and not metadata_task.done():
                    metadata_task.cancel()
                    await asyncio.gather(metadata_task, return_exceptions=True)
                if candidate is None and settings.omdb_enabled and is_video:
                    if metadata_task is None:
                        metadata_task = asyncio.create_task(
                            self.metadata.identify(
                                drive.volume_label or (scan.disc_name if scan else fallback_title),
                                requested_kind,
                            ),
                            name=f"metadata-{job_id}",
                        )
                    automatic_candidate = await metadata_task
                    # A title chosen in the dashboard while this lookup was in
                    # flight must win over automatic identification.
                    latest_job = self.database.get_job(job_id) or latest_job
                    candidate = _user_metadata_candidate(latest_job) or automatic_candidate
                title = (
                    candidate.title
                    if candidate
                    else fallback_title or drive.volume_label or "Unidentified disc"
                )
                year = candidate.year if candidate else fallback_year
                media_kind = (
                    candidate.media_kind
                    if candidate and candidate.media_kind != MediaKind.UNKNOWN
                    else requested_kind
                    or (
                        MediaKind.MUSIC
                        if drive.disc_kind == DiscKind.AUDIO_CD
                        else MediaKind.OTHER
                        if drive.disc_kind == DiscKind.DATA
                        else MediaKind.UNKNOWN
                    )
                )
                identified_metadata = (
                    candidate.model_dump(mode="json")
                    if candidate
                    else {"provider": "disc", "title": title, "year": year}
                )
                metadata = {**(latest_job.get("metadata") or {}), **identified_metadata}
                if duplicate_job:
                    metadata["duplicate"] = {
                        "job_id": duplicate_job["id"],
                        "output_path": duplicate_job["output_path"],
                    }
                self.database.update_job(
                    job_id,
                    title=title,
                    year=year,
                    media_kind=media_kind.value,
                    metadata_json=json.dumps(metadata),
                    status_detail="Disc identified",
                )

                if scan:
                    selected = select_disc_titles(
                        scan.titles,
                        settings,
                        candidate.runtime_minutes if candidate else 0,
                    )
                    selected_ids = {title.id for title in selected}
                    tracks = [
                        {**title.model_dump(), "selected": title.id in selected_ids}
                        for title in scan.titles
                    ]
                    self.database.replace_tracks(job_id, tracks)

                if drive.disc_kind == DiscKind.DATA:
                    # Nobody can name a game or a folder of files from a volume label alone,
                    # so the disc's own files are shown and the backup waits for a name.
                    described = metadata.get("disc_contents") or {}
                    files = int(described.get("file_count") or 0)
                    self.database.update_job(
                        job_id,
                        state=JobState.AWAITING_INPUT,
                        stage="awaiting_input",
                        status_detail=(
                            f"Look at the {files} files on this disc and name the backup"
                            if files
                            else "Name this disc to back it up"
                        ),
                        recoverable=1,
                    )
                    await self.notifications.send(
                        job_id,
                        "attention",
                        "A disc is waiting to be named",
                        f"{title} holds {files} files. Name it in DiscDock and it is backed up.",
                    )
                    return

                needs_identification = bool(
                    is_video and settings.omdb_enabled and candidate is None
                )
                short_titles_only = bool(metadata.get("short_titles_only"))
                # "Always choose titles" in Settings holds every disc here, with the titles
                # between the minimum and maximum length ready to tick, as the Choose titles
                # button does for one disc.
                always_choosing = settings.always_choose_titles
                if (manual or needs_identification or short_titles_only or always_choosing) and is_video:
                    status_detail = (
                        "Every title is shorter than the minimum length in Settings — choose what to rip"
                        if short_titles_only
                        else "No OMDb match found — search or enter the title"
                        if needs_identification
                        else "Review the title and choose what to rip"
                        if manual
                        else "Choose which titles to rip, as set in Settings"
                    )
                    self.database.update_job(
                        job_id,
                        state=JobState.AWAITING_INPUT,
                        stage="awaiting_input",
                        status_detail=status_detail,
                        recoverable=1,
                    )
                    await self.notifications.send(
                        job_id,
                        "attention",
                        "DiscDock is waiting",
                        f"Confirm the title and tracks for {title}.",
                    )
                    return
                latest_job = self.database.get_job(job_id) or job
                await self._rip_and_finish(
                    job_id,
                    drive,
                    settings,
                    best_effort=(
                        (latest_job.get("metadata") or {}).get("requested_rip_method")
                        == "sector_rescue"
                    ),
                )
            except asyncio.CancelledError:
                self._mark_cancelled(job_id)
                raise
            except FileNotFoundError as error:
                await self._fail(job_id, "tool_missing", str(error), blocked=True)
            except MakeMKVLicenseError as error:
                await self._fail(job_id, "makemkv_license", str(error), blocked=True)
            except DiscTooDamagedToOpen as error:
                await self._fail(job_id, "disc_unreadable", str(error), detail="Too damaged for MakeMKV to open")
            except ProcessFailure as error:
                latest = self.database.get_job(job_id) or job
                if (latest.get("metadata") or {}).get("ai_repair_requested"):
                    try:
                        await self._start_ai_repair_preparation(job_id, drive, settings)
                    except asyncio.CancelledError:
                        self._mark_cancelled(job_id)
                        raise
                    except Exception as repair_error:
                        await self._fail(
                            job_id, "ai_repair_preparation_failed", str(repair_error), keep_staging=True
                        )
                    return
                if (latest.get("metadata") or {}).get("recovery_requested"):
                    try:
                        await self._start_best_effort_recovery(job_id, drive, settings)
                    except asyncio.CancelledError:
                        self._mark_cancelled(job_id)
                        raise
                    except Exception as recovery_error:
                        await self._fail(job_id, "damaged_disc_recovery_failed", str(recovery_error))
                    return
                code = "timeout" if error.result.timed_out else "media_tool_failed"
                tail = "\n".join(error.result.lines[-8:])
                await self._fail(job_id, code, f"{error}\n{tail}".strip())
            except Exception as error:
                await self._fail(job_id, "unexpected", str(error))
            finally:
                if metadata_task and not metadata_task.done():
                    metadata_task.cancel()
                    await asyncio.gather(metadata_task, return_exceptions=True)

    async def _recover_disc_to_staging(
        self,
        job_id: str,
        drive: DriveInfo,
        settings: AppSettings,
        staging: Path,
        main_track: dict[str, Any],
    ) -> None:
        """Rescue the disc into its shared image, then extract the selected title locally."""
        image = self._rescue_image(job_id, settings)
        job = self.database.get_job(job_id) or {}
        previous = [Path(job["staging_path"])] if job.get("staging_path") else []
        adopted = await asyncio.to_thread(
            self._adopt_previous_rescue_image, job_id, settings, image, previous
        )
        if adopted:
            await asyncio.to_thread(
                self._append_log, job_id, f"Continuing the rescue image saved earlier at {adopted}"
            )
        owners = self._rescue_image_owners(job_id)
        for note in await asyncio.to_thread(self._collect_rescue_images, settings, image, owners):
            await asyncio.to_thread(self._append_log, job_id, note)
        self._update_recovery_metadata(
            job_id,
            image_path=str(image),
            engine="discdock-rescue-3",
            quality_warning=RESCUE_QUALITY_WARNING,
        )
        current = getattr(self, "drives", {}).get(drive.id, drive)
        disc_present = current.media_loaded and (
            not job.get("disc_label")
            or current.volume_label.casefold() == str(job["disc_label"]).casefold()
        )
        partial = DvdSectorRescue.artifact_paths(image)["partial"]
        minimum_share = 0.8
        recovery = ((self.database.get_job(job_id) or job).get("metadata") or {}).get("recovery")
        if isinstance(recovery, dict) and recovery.get("finish_without_reading") and (
            image.is_file() or partial.is_file()
        ):
            # The user chose to finish with what was rescued, for example while
            # the drive is stuck after a hardware fault. A later retry reads again.
            needs_reading = False
            # Asking to finish now means accepting whatever the disc gave, holes and all.
            minimum_share = FINISH_NOW_MINIMUM_SHARE
            if not image.is_file():
                await asyncio.to_thread(os.replace, partial, image)
            self._update_recovery_metadata(job_id, finish_without_reading=False)
            await asyncio.to_thread(
                self._append_log,
                job_id,
                "Finishing with the sectors rescued so far; the disc is not read again, and the movie is kept "
                "however much of it came back",
            )
        elif not image.is_file():
            needs_reading = True
        elif disc_present and await asyncio.to_thread(self._rescue_has_retry_work, image, settings):
            needs_reading = True
            await asyncio.to_thread(
                self._append_log,
                job_id,
                "The saved rescue image still has unread blocks and retry time left; reading them first",
            )
        else:
            needs_reading = False
            await asyncio.to_thread(
                self._append_log, job_id, "Using the completed rescue image from the previous attempt"
            )
        if needs_reading:
            summary = await self._run_sector_rescue(job_id, drive, settings, image, main_track)
            await asyncio.to_thread(
                self._append_log,
                job_id,
                (
                    f"Rescue image ready: {_format_bytes(int(summary.get('rescued_bytes') or 0))} read, "
                    f"{_format_bytes(int(summary.get('movie_unreadable_bytes', summary.get('unreadable_bytes')) or 0))} "
                    "of the movie unreadable, "
                    f"{_format_bytes(int(summary.get('movie_pending_bytes', summary.get('pending_bytes')) or 0))} "
                    "of it not retried"
                    + (
                        f", {_format_bytes(int(summary['not_needed_bytes']))} of extras and menus not read"
                        if summary.get("not_needed_bytes")
                        else ""
                    )
                    + RESCUE_STOP_REASONS.get(str(summary.get("stop_reason") or ""), "")
                ),
            )
        # The ways to finish the movie, in the order they take: reading the image
        # first, then the rest of the disc, and playing it out with VLC last.
        # DiscDock moves on to the next by itself, so nothing waits for a click.
        problems: list[str] = []
        try:
            await self._extract_title_from_image(
                job_id, drive.letter, settings, image, staging, main_track,
                methods=("makemkv", "ffmpeg"), problems=problems, minimum_share=minimum_share,
            )
            return
        except (MakeMKVLicenseError, ImageNotDecryptable):
            raise
        except (ProcessFailure, RuntimeError) as error:
            problem = error
        # The image holds the movie and the disc's navigation. Should MakeMKV
        # need more of the disc, read the rest once and extract again.
        if disc_present and not await asyncio.to_thread(self._rescue_swept_whole_disc, image):
            await asyncio.to_thread(
                self._append_log,
                job_id,
                f"The movie could not be taken out of the image with only the movie read ({problem}); "
                "reading the rest of the disc once",
            )
            self.database.update_job(job_id, status_detail="Reading the rest of the disc for MakeMKV")
            await self._run_sector_rescue(job_id, drive, settings, image, main_track, whole_disc=True, extra_seconds=0)
            try:
                await self._extract_title_from_image(
                    job_id, drive.letter, settings, image, staging, main_track,
                    methods=("makemkv", "ffmpeg"), problems=problems, minimum_share=minimum_share,
                )
                return
            except (MakeMKVLicenseError, ImageNotDecryptable):
                raise
            except (ProcessFailure, RuntimeError) as error:
                problem = error
        # An image finished early, for example with "Finish with what's rescued", can be missing
        # most of the movie. Reading the spots that were skipped beats guessing at the rest.
        if disc_present and await asyncio.to_thread(self._rescue_has_retry_work, image, settings):
            await asyncio.to_thread(
                self._append_log,
                job_id,
                f"The movie could not be taken out of the image ({problem}); the disc still has unread spots, "
                "so they are read before trying again",
            )
            self.database.update_job(job_id, status_detail="Reading the spots that were skipped")
            await self._run_sector_rescue(job_id, drive, settings, image, main_track)
            try:
                await self._extract_title_from_image(
                    job_id, drive.letter, settings, image, staging, main_track,
                    methods=("makemkv", "ffmpeg"), problems=problems, minimum_share=minimum_share,
                )
                return
            except (MakeMKVLicenseError, ImageNotDecryptable):
                raise
            except (ProcessFailure, RuntimeError) as error:
                problem = error
        if (self.database.get_job(job_id) or {}).get("disc_type") != DiscKind.DVD:
            raise problem
        await asyncio.to_thread(
            self._append_log,
            job_id,
            "Nothing else is left to read; letting VLC play the movie out of the image as a last resort",
        )
        await self._extract_title_from_image(
            job_id, drive.letter, settings, image, staging, main_track,
            methods=("vlc",), problems=problems, minimum_share=minimum_share,
        )

    @staticmethod
    def _rescue_swept_whole_disc(image: Path) -> bool:
        """Whether a saved rescue has read every sector of the disc at least once."""
        try:
            rescue_map = RescueMap.load(DvdSectorRescue.artifact_paths(image)["map"])
        except (OSError, ValueError, KeyError, TypeError):
            return False
        swept = rescue_map.meta.get("swept_ranges")
        if swept is None:
            return bool(rescue_map.meta.get("sweep_done"))
        return sum(max(0, int(end) - int(start)) for start, end in swept) >= rescue_map.total_sectors

    @staticmethod
    def _rescue_unread_bytes(image: Path) -> int:
        try:
            return _unread_sectors(RescueMap.load(DvdSectorRescue.artifact_paths(image)["map"])) * SECTOR_SIZE
        except (OSError, ValueError, KeyError, TypeError):
            return 0

    @staticmethod
    def _rescue_sweep_done(image: Path) -> bool:
        """Whether a saved rescue has read the whole disc once."""
        try:
            return bool(RescueMap.load(DvdSectorRescue.artifact_paths(image)["map"]).meta.get("sweep_done"))
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def _saved_rescue_usable(self, job: dict[str, Any], settings: AppSettings) -> bool:
        """Whether the job can continue from what it already rescued, without the disc."""
        image = self._rescue_image(str(job["id"]), settings)
        recovery = (job.get("metadata") or {}).get("recovery")
        finishing = isinstance(recovery, dict) and bool(recovery.get("finish_without_reading"))
        return (
            image.is_file()
            or self._salvaged_movie_dir(job) is not None
            or (finishing and DvdSectorRescue.artifact_paths(image)["partial"].is_file())
        )

    def _disc_can_improve(self, job: dict[str, Any], settings: AppSettings) -> bool:
        """Whether the disc is in the drive and its saved rescue still has unread blocks and retry time."""
        drive = self._drive_for_job(job)
        if not drive or not drive.media_loaded:
            return False
        if job.get("disc_label") and drive.volume_label.casefold() != str(job["disc_label"]).casefold():
            return False
        return self._rescue_has_retry_work(self._rescue_image(str(job["id"]), settings), settings)

    @staticmethod
    def _rescue_has_retry_work(image: Path, settings: AppSettings) -> bool:
        """Whether a saved rescue image still lists unread blocks and retry time remains."""
        try:
            rescue_map = RescueMap.load(DvdSectorRescue.artifact_paths(image)["map"])
        except (OSError, ValueError, KeyError, TypeError):
            return False
        used = float(rescue_map.meta.get("extra_used_seconds") or 0)
        return _unread_sectors(rescue_map) > 0 and used < settings.rescue_extra_minutes * 60

    def _rescue_image(self, job_id: str, settings: AppSettings) -> Path:
        """One rescue image per disc, shared by every job and attempt that reads it."""
        job = self.database.get_job(job_id) or {}
        fingerprint = str(job.get("fingerprint") or "")
        folder = f"disc-{fingerprint[:16]}.rescue" if fingerprint else f"{job_id}.rescue"
        return settings.resolved_directories()["raw"] / folder / "rescued-disc.iso"

    def _rescue_image_owners(self, job_id: str) -> list[str]:
        """This job and every other job for the same disc, whose rescues can be combined."""
        job = self.database.get_job(job_id) or {}
        fingerprint = str(job.get("fingerprint") or "")
        others = [
            str(other["id"])
            for other in self.database.list_jobs(250)
            if fingerprint and other.get("id") != job_id and other.get("fingerprint") == fingerprint
        ]
        return [job_id, *others]

    @staticmethod
    def _collect_rescue_images(settings: AppSettings, image: Path, owners: list[str]) -> list[str]:
        """Combine every earlier rescue of this disc into its shared image.

        Earlier releases gave each job its own image, so a reconnected drive or a
        second job read the disc from the start again. Reads from a damaged disc
        are not repeatable either: blocks one attempt could not read, another
        often did, so together those images hold more than any one of them.
        """
        raw = settings.resolved_directories()["raw"]
        donors: list[tuple[int, Path, Path]] = []
        for owner in owners:
            folder = raw / f"{owner}.rescue"
            if folder == image.parent:
                continue
            for name in ("rescued-disc.iso.part", "rescued-disc.iso"):
                candidate = folder / name
                if not candidate.is_file():
                    continue
                donor_map = folder / "rescued-disc.iso.map.json"
                try:
                    finished = RescueMap.load(donor_map).count(FINISHED)
                except (OSError, ValueError, KeyError, TypeError):
                    finished = -1
                donors.append((finished, candidate, donor_map))
                break
        paths = DvdSectorRescue.artifact_paths(image)
        notes: list[str] = []
        # The most complete rescue becomes the base; the others fill its holes.
        for finished, donor, donor_map in sorted(donors, key=lambda item: -item[0]):
            current = paths["partial"] if paths["partial"].is_file() else image if image.is_file() else None
            if current is None:
                image.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(donor), str(paths["partial"] if donor.name.endswith(".part") else image))
                if donor_map.is_file():
                    shutil.move(str(donor_map), str(paths["map"]))
                notes.append(f"Continuing the rescue image saved at {donor}")
            elif finished < 0 or not paths["map"].is_file():
                notes.append(f"Left {donor} untouched: without its rescue map it cannot be combined safely")
                continue
            else:
                try:
                    copied = merge_rescue_images(current, paths["map"], donor, donor_map)
                except (OSError, ValueError) as error:
                    notes.append(f"Could not combine the earlier rescue at {donor}: {error}")
                    continue
                donor.unlink(missing_ok=True)
                donor_map.unlink(missing_ok=True)
                notes.append(
                    f"Combined the earlier rescue at {donor}: it added "
                    f"{_format_bytes(copied * SECTOR_SIZE)} this image was missing"
                )
            (donor.parent / "rescued-disc.iso.control").unlink(missing_ok=True)
            try:
                donor.parent.rmdir()
            except OSError:
                pass
        return notes

    @staticmethod
    def _adopt_previous_rescue_image(
        job_id: str, settings: AppSettings, image: Path, extra_folders: list[Path]
    ) -> Path | None:
        """Move a rescue image left by an earlier attempt into the job's rescue folder.

        Earlier releases kept the image inside each attempt's staging folder, so
        switching between best effort and AI repair started the disc read again.
        """
        paths = DvdSectorRescue.artifact_paths(image)
        if image.is_file() or paths["partial"].is_file():
            return None
        directories = settings.resolved_directories()
        folders = [folder for folder in extra_folders if folder.is_dir() and folder != image.parent]
        for root in (directories["raw"], directories["failed"]):
            if root.is_dir():
                # Whole-disc rescue folders are combined by _collect_rescue_images,
                # which keeps the most complete image as the base.
                folders.extend(
                    folder
                    for folder in root.glob(f"{job_id}*")
                    if folder.is_dir() and folder != image.parent and folder.suffix != ".rescue"
                )
        candidates: dict[Path, Path] = {}
        for folder in folders:
            for name in ("rescued-disc.iso.part", "rescued-disc.iso"):
                for path in folder.rglob(name):
                    if path.is_file() and path.stat().st_size > 0:
                        candidates.setdefault(path.resolve(), path)
        if not candidates:
            return None
        best = max(candidates.values(), key=lambda path: (path.stat().st_size, path.stat().st_mtime))
        image.parent.mkdir(parents=True, exist_ok=True)
        old_map = best.with_name("rescued-disc.iso.map.json")
        shutil.move(str(best), str(paths["partial"] if best.name.endswith(".part") else image))
        if old_map.is_file():
            shutil.move(str(old_map), str(paths["map"]))
        best.with_name("rescued-disc.iso.control").unlink(missing_ok=True)
        try:
            best.parent.rmdir()
        except OSError:
            pass
        return best

    def _update_recovery_metadata(self, job_id: str, **fields: Any) -> None:
        job = self.database.get_job(job_id)
        if not job:
            return
        metadata = dict(job.get("metadata") or {})
        recovery = dict(metadata.get("recovery") or {})
        recovery.update(fields)
        metadata["recovery"] = recovery
        self.database.update_job(job_id, metadata_json=json.dumps(metadata, ensure_ascii=False))

    async def _discard_rescue_image(self, job_id: str, settings: AppSettings) -> None:
        folder = self._rescue_image(job_id, settings).parent
        if not folder.exists():
            return
        try:
            await asyncio.to_thread(shutil.rmtree, folder)
        except OSError as error:
            await asyncio.to_thread(self._append_log, job_id, f"Could not remove the rescue image: {error}")

    async def _run_sector_rescue(
        self,
        job_id: str,
        drive: DriveInfo,
        settings: AppSettings,
        image: Path,
        main_track: dict[str, Any] | None,
        *,
        whole_disc: bool = False,
        structures_only: bool = False,
        extra_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Run the rescue helper. Without ``main_track`` it picks the longest title itself."""

        async def rescue_event(event: dict) -> None:
            adjusted = dict(event)
            if adjusted.get("type") == "progress":
                adjusted["percent"] = min(100.0, float(adjusted.get("percent") or 0)) * 0.85
            await self._process_event(job_id, adjusted)

        async def quiet_event(event: dict) -> None:
            if event.get("type") != "progress":
                await self._process_event(job_id, event)

        job = self.database.get_job(job_id) or {}
        disc_kind = DiscKind(job.get("disc_type") or drive.disc_kind)
        is_dvd = disc_kind == DiscKind.DVD
        rescue = DvdSectorRescue(self.runner)
        for attempt in range(2):
            try:
                return await rescue.rip(
                    job_id,
                    drive.letter,
                    image,
                    callback=rescue_event,
                    extra_seconds=settings.rescue_extra_minutes * 60 if extra_seconds is None else extra_seconds,
                    dvd_title=(
                        self._dvd_recovery_title_number(job_id, main_track, settings) if is_dvd and main_track else 0
                    ),
                    segment_map=str((main_track or {}).get("segment_map") or ""),
                    cluster_sectors=RESCUE_CLUSTER_SECTORS.get(disc_kind, 16),
                    skip_sectors=RESCUE_SKIP_SECTORS.get(disc_kind, 256),
                    timeout=max(settings.rip_timeout_seconds, 24 * 3600),
                    disc="dvd" if is_dvd else "bluray",
                    playlist="" if is_dvd else str((main_track or {}).get("source_filename") or ""),
                    auto_title=main_track is None,
                    whole_disc=whole_disc,
                    structures_only=structures_only,
                )
            except DiscAuthenticationRequired as error:
                if attempt:
                    raise RuntimeError(
                        "The drive keeps refusing this DVD's encrypted sectors. Eject and reinsert the disc, "
                        "then choose Retry."
                    ) from error
                await asyncio.to_thread(
                    self._append_log,
                    job_id,
                    "The drive needs the DVD to be authenticated again; letting MakeMKV open the disc first",
                )
                self.database.update_job(
                    job_id, status_detail="Re-authenticating the DVD before continuing the rescue"
                )
                await self._make_mkv(settings).inspect(
                    job_id,
                    drive.letter,
                    settings.min_length_seconds,
                    settings.max_length_seconds,
                    settings.inspect_timeout_seconds,
                    quiet_event,
                )
        raise RuntimeError("The damaged-disc rescue could not start")

    async def _extract_title_from_image(
        self,
        job_id: str,
        letter: str,
        settings: AppSettings,
        image: Path,
        staging: Path,
        main_track: dict[str, Any],
        *,
        methods: tuple[str, ...] = EXTRACTION_METHODS,
        problems: list[str] | None = None,
        minimum_share: float = 0.8,
    ) -> None:
        """Get the movie out of a rescued disc image, trying ``methods`` in order.

        Each method that cannot finish hands over to the next one by itself, so a
        damaged disc is not left waiting for someone to pick the next step. A
        shared ``problems`` list collects what every method ran into, so the job
        can say what was tried if none of them works.
        """
        client = self._make_mkv(settings)
        if hasattr(client, "no_output_timeout"):
            # MakeMKV can work through a large disc image for minutes without printing anything.
            client.no_output_timeout = max(client.no_output_timeout, 600)
        source = f"iso:{image}"
        duration = int(main_track.get("duration_seconds") or 0)
        if (self.database.get_job(job_id) or {}).get("disc_type") == DiscKind.BLURAY and duration > 0:
            # Blu-ray playlist lengths do not change with damage. Skipping titles
            # shorter than the movie keeps MakeMKV away from extras never read.
            min_length = max(120, int(duration * 0.9))
        else:
            # Damaged cells can shorten what MakeMKV measures on a DVD, so the normal
            # length filter must not hide the movie inside a rescued image.
            min_length = min(settings.min_length_seconds, 120)

        async def extraction_event(event: dict) -> None:
            adjusted = dict(event)
            if adjusted.get("type") == "progress":
                adjusted["percent"] = 85 + min(100.0, float(adjusted.get("percent") or 0)) * 0.13
                adjusted["message"] = "Extracting the movie from the rescued disc image"
            await self._process_event(job_id, adjusted)

        repeats: dict[tuple[str, ...], int] = {}
        stuck: list[str] = []

        async def scan_event(event: dict) -> None:
            spot = MAKEMKV_CORRUPT_SPOT.search(str(event.get("message") or ""))
            if spot:
                count = repeats[spot.groups()] = repeats.get(spot.groups(), 0) + 1
                if count == MAKEMKV_STUCK_REPEATS:
                    stuck.append(f"{spot.group(1)} at offset {spot.group(2)}")
                    await self.runner.cancel(job_id)
                if count >= MAKEMKV_STUCK_REPEATS:
                    return
            if event.get("type") != "progress":
                await self._process_event(job_id, event)

        extracted = staging / ".rescued-title.partial"
        if extracted.exists():
            await asyncio.to_thread(shutil.rmtree, extracted)
        is_dvd = (self.database.get_job(job_id) or {}).get("disc_type") == DiscKind.DVD
        problems = problems if problems is not None else []
        extraction_done = False
        if "makemkv" in methods:
            extraction_done = await self._extract_image_with_makemkv(
                job_id,
                letter,
                settings,
                image,
                extracted,
                main_track,
                client,
                source,
                min_length,
                stuck,
                repeats,
                scan_event,
                extraction_event,
                problems,
                is_dvd=is_dvd,
                last=not [method for method in methods if method != "makemkv"] or not is_dvd,
            )
        if not extraction_done and is_dvd and "ffmpeg" in methods:
            extraction_done = await self._extract_image_with_ffmpeg(
                job_id, settings, image, extracted, main_track, extraction_event, problems, minimum_share
            )
        if not extraction_done and is_dvd and "vlc" in methods:
            extraction_done = await self._extract_image_with_vlc(
                job_id, letter, settings, image, extracted, main_track, extraction_event, problems, minimum_share
            )
        if not extraction_done:
            raise RuntimeError(
                "The movie could not be taken out of the rescued disc image. "
                + " ".join(dict.fromkeys(problems))
            )
        for path in extracted.iterdir():
            os.replace(path, staging / path.name)
        extracted.rmdir()

    async def _extract_image_with_makemkv(
        self,
        job_id: str,
        letter: str,
        settings: AppSettings,
        image: Path,
        extracted: Path,
        main_track: dict[str, Any],
        client: MakeMKVClient,
        source: str,
        min_length: int,
        stuck: list[str],
        repeats: dict[tuple[str, ...], int],
        scan_event: Callable[[dict], Awaitable[None]],
        extraction_event: Callable[[dict], Awaitable[None]],
        problems: list[str],
        *,
        is_dvd: bool,
        last: bool,
    ) -> bool:
        """Let MakeMKV extract the movie: the fastest method, and the one that keeps the most.

        Returns whether it finished. ``last`` raises instead of handing over when
        no other method can read this disc's image.
        """
        self.database.update_job(
            job_id, status_detail="Finding the movie in the rescued disc image", progress=85
        )
        attributes_saved = (image.parent / "makemkv-backup" / DISC_ATTRIBUTES).is_file()
        try:
            if attributes_saved and (self.database.get_job(job_id) or {}).get("disc_type") == DiscKind.BLURAY:
                # An earlier attempt found that MakeMKV needs the disc's decryption information.
                await self._extract_through_backup_folder(
                    job_id,
                    letter,
                    settings,
                    image,
                    extracted,
                    main_track,
                    min_length,
                    "the start of the movie in an earlier attempt",
                    stuck,
                    scan_event,
                    extraction_event,
                )
            else:
                # Analysing a large damaged image can take MakeMKV far longer than a disc scan.
                scan = await client.inspect_source(
                    job_id, source, min_length, max(settings.inspect_timeout_seconds, 2 * 3600), scan_event
                )
                match = match_title(scan.titles, main_track)
                if match is None:
                    raise RuntimeError("MakeMKV could not find the selected movie in the rescued disc image")
                await asyncio.to_thread(
                    self._append_log,
                    job_id,
                    (
                        f"Rescued image title {match.id} is the selected movie ({match.duration_seconds} s, "
                        f"DVD title {match.disc_title_number or 'unknown'})"
                    ),
                )
                self.database.update_job(job_id, status_detail="Extracting the movie from the rescued disc image")
                await client.rip_source(
                    job_id,
                    source,
                    extracted,
                    [match.id],
                    settings.rip_timeout_seconds,
                    callback=extraction_event,
                    min_length=min_length,
                )
        except MakeMKVLicenseError:
            raise
        except (ProcessFailure, RuntimeError) as error:
            if stuck and not is_dvd:
                # MakeMKV cannot decrypt this Blu-ray from a plain image. With the
                # discatt.dat its own backup saves from the drive, it can.
                repeats.clear()
                if extracted.exists():
                    await asyncio.to_thread(shutil.rmtree, extracted)
                await self._extract_through_backup_folder(
                    job_id,
                    letter,
                    settings,
                    image,
                    extracted,
                    main_track,
                    min_length,
                    stuck[0],
                    stuck,
                    scan_event,
                    extraction_event,
                )
                return True
            if last:
                # A Blu-ray image has no second reader; only MakeMKV decrypts one.
                raise RuntimeError(
                    f"MakeMKV could not extract the movie from the rescued disc image: {error}"
                ) from error
            problems.append(f"MakeMKV could not use the rescued image ({error}).")
            await asyncio.to_thread(
                self._append_log, job_id, f"MakeMKV could not extract the rescued image: {error}"
            )
            return False
        return True

    async def _extract_image_with_ffmpeg(
        self,
        job_id: str,
        settings: AppSettings,
        image: Path,
        extracted: Path,
        main_track: dict[str, Any],
        extraction_event: Callable[[dict], Awaitable[None]],
        problems: list[str],
        minimum_share: float = 0.8,
    ) -> bool:
        """Copy the movie's own sectors out of a rescued DVD image with FFmpeg.

        This needs none of the disc's navigation, which is what MakeMKV and VLC
        stumble over, and it copies at disk speed.
        """
        if not settings.ffmpeg_path or not Path(settings.ffmpeg_path).is_file():
            problems.append("FFmpeg is not set up, so the movie's sectors could not be copied.")
            return False
        title_number = self._dvd_recovery_title_number(job_id, main_track, settings)
        ranges, scrambled = await asyncio.to_thread(
            self._dvd_movie_sectors, image, title_number, str(main_track.get("segment_map") or "")
        )
        if not ranges:
            problems.append("The movie's own sectors could not be found in the rescued image.")
            return False
        if scrambled:
            # Without the disc's keys these sectors are noise; MakeMKV and VLC decrypt, DiscDock does not.
            problems.append("The rescued image is still scrambled, so only a player that decrypts DVDs can read it.")
            await asyncio.to_thread(
                self._append_log,
                job_id,
                "The rescued image is still scrambled, so its sectors cannot be copied directly",
            )
            return False
        megabytes = sum(end - start for start, end in ranges) * SECTOR_SIZE / 1024**2
        await asyncio.to_thread(
            self._append_log,
            job_id,
            f"Copying the movie's own sectors from the rescued image with FFmpeg ({megabytes:,.0f} MB, "
            f"DVD title {title_number})",
        )
        self.database.update_job(job_id, status_detail="Copying the movie out of the rescued disc image")
        if extracted.exists():
            await asyncio.to_thread(shutil.rmtree, extracted)
        try:
            await FfmpegDvdRecovery(settings.ffmpeg_path, settings.ffprobe_path, self.runner).recover(
                job_id,
                image,
                ranges,
                extracted,
                int(main_track.get("duration_seconds") or 0),
                settings.rip_timeout_seconds,
                callback=extraction_event,
                minimum_share=minimum_share,
            )
        except (ProcessFailure, FileNotFoundError, ValueError, OSError) as error:
            problems.append(f"FFmpeg could not copy the movie ({error}).")
            await asyncio.to_thread(
                self._append_log, job_id, f"FFmpeg could not copy the movie from the rescued image: {error}"
            )
            return False
        await asyncio.to_thread(
            self._append_log, job_id, "FFmpeg copied the movie out of the rescued disc image"
        )
        return True

    async def _extract_image_with_vlc(
        self,
        job_id: str,
        letter: str,
        settings: AppSettings,
        image: Path,
        extracted: Path,
        main_track: dict[str, Any],
        extraction_event: Callable[[dict], Awaitable[None]],
        problems: list[str],
        minimum_share: float = 0.8,
    ) -> bool:
        """Let VLC play the movie out of a rescued DVD image: the slowest method, so it comes last."""
        if not settings.vlc_path or not Path(settings.vlc_path).is_file():
            problems.append("VLC is not installed, so the movie could not be played out of the image.")
            return False
        await asyncio.to_thread(
            self._append_log, job_id, "Playing the movie out of the rescued disc image with VLC"
        )
        if extracted.exists():
            await asyncio.to_thread(shutil.rmtree, extracted)
        try:
            await VlcDvdRecovery(settings.vlc_path, settings.ffprobe_path, self.runner).recover(
                job_id,
                letter,
                extracted,
                self._dvd_recovery_title_number(job_id, main_track, settings),
                int(main_track.get("chapters") or 1),
                int(main_track.get("duration_seconds") or 0),
                int(main_track.get("size_bytes") or 0),
                settings.rip_timeout_seconds,
                callback=extraction_event,
                source_image=image,
                minimum_share=minimum_share,
            )
        except (ProcessFailure, FileNotFoundError, ValueError, OSError) as error:
            problems.append(f"VLC could not read the movie ({error}).")
            await asyncio.to_thread(
                self._append_log, job_id, f"VLC could not read the movie from the rescued image: {error}"
            )
            return False
        return True

    @staticmethod
    def _dvd_movie_sectors(image: Path, title_number: int, segment_map: str) -> tuple[list[tuple[int, int]], bool]:
        """The sectors of the selected DVD title in a rescued image, and whether they are still scrambled."""
        try:
            with image.open("rb") as handle:

                def read(lba: int, count: int) -> bytes:
                    handle.seek(lba * SECTOR_SIZE)
                    return handle.read(count * SECTOR_SIZE).ljust(count * SECTOR_SIZE, b"\0")

                total_sectors = image.stat().st_size // SECTOR_SIZE
                ranges = dvd_title_video_ranges(read, total_sectors, title_number, segment_map)
                return ranges, bool(ranges) and dvd_video_scrambled(read, ranges)
        except (OpticalError, OSError, ValueError):
            return [], False

    def _drive_with_disc(self, job_id: str, letter: str) -> DriveInfo | None:
        """The connected drive with this letter, if the job's disc is in it."""
        job = self.database.get_job(job_id) or {}
        wanted = letter.rstrip(":\\").upper()
        for drive in (getattr(self, "drives", {}) or {}).values():
            if (
                drive.letter.rstrip(":\\").upper() == wanted
                and drive.media_loaded
                and (not job.get("disc_label") or drive.volume_label.casefold() == str(job["disc_label"]).casefold())
            ):
                return drive
        return None

    async def _extract_through_backup_folder(
        self,
        job_id: str,
        letter: str,
        settings: AppSettings,
        image: Path,
        extracted: Path,
        main_track: dict[str, Any],
        min_length: int,
        spot: str,
        stuck: list[str],
        scan_event: Callable[[dict], Awaitable[None]],
        extraction_event: Callable[[dict], Awaitable[None]],
    ) -> None:
        """Extract a Blu-ray MakeMKV cannot decrypt from a plain image.

        A backup MakeMKV starts from the drive saves the disc's decryption
        information in discatt.dat before anything else. Next to the rescued
        files, that lets MakeMKV read the movie as from its own backup.
        ``spot`` says where MakeMKV got stuck; ``stuck`` collects new stuck spots.
        """
        folder = image.parent / "makemkv-backup"
        attributes = folder / DISC_ATTRIBUTES
        if not (attributes.is_file() and attributes.stat().st_size > 0):
            if self._drive_with_disc(job_id, letter) is None:
                raise ImageNotDecryptable(
                    f"MakeMKV cannot decrypt the movie from the rescued disc image alone: it keeps failing at {spot}, "
                    "although that part of the disc was read. Put the disc back in the drive and choose Retry: "
                    "MakeMKV then saves the disc's decryption information and extracts the movie from the rescued "
                    "data. The rescued image is kept."
                )
            await asyncio.to_thread(
                self._append_log,
                job_id,
                f"MakeMKV cannot decrypt the image alone (it keeps failing at {spot}); "
                "letting MakeMKV save the disc's decryption information from the drive",
            )
            self.database.update_job(job_id, status_detail="Letting MakeMKV read the disc's decryption information")
            client = self._make_mkv(settings)
            index = await client.drive_index(job_id, letter)
            if index is None:
                raise ImageNotDecryptable(
                    "MakeMKV did not list the drive with the disc, so it could not save the disc's decryption "
                    "information. Reconnect the drive and choose Retry. The rescued image is kept."
                )
            await client.capture_disc_attributes(job_id, index, folder)
        self.database.update_job(job_id, status_detail="Preparing the rescued movie for MakeMKV")
        playlist = str(main_track.get("source_filename") or "")
        if not playlist:
            # Titles scanned before DiscDock 1.7.0 do not record their playlist; the rescued image shows it.
            playlist = await asyncio.to_thread(movie_playlist, image, int(main_track.get("duration_seconds") or 0))
            if playlist:
                await asyncio.to_thread(
                    self._append_log, job_id, f"The movie is playlist {playlist} of the rescued disc"
                )
        unread = await asyncio.to_thread(self._unread_sectors, image)
        written = await asyncio.to_thread(fill_backup_folder, image, folder, playlist, unread=unread)
        await asyncio.to_thread(
            self._append_log,
            job_id,
            f"Rebuilt the disc's files from the rescued image for MakeMKV: {written['copied']} copied, "
            f"{written['placeholders']} unused extras left empty, {written['kept']} already saved by MakeMKV"
            + (
                f"; {written['padded_bytes'] / 1024**2:.0f} MB of the movie that could not be read are empty packets"
                if written["padded_bytes"]
                else ""
            )
            + (
                f"; {written['refreshed_bytes'] / 1024**2:.0f} MB read since the last attempt put back into the movie"
                if written.get("refreshed_bytes")
                else ""
            ),
        )
        library = libmmbd_library(settings.make_mkv_path)
        if (
            written["padded_bytes"]
            and library
            and playlist
            and await asyncio.to_thread(movie_decryption_started, folder, playlist)
        ):
            # An earlier attempt already went past MakeMKV's stop this way; carry on where it ended.
            await self._copy_damaged_bluray(
                job_id, settings, folder, playlist, extracted, library, "in an earlier attempt", extraction_event
            )
            return
        source = f"file:{folder}"
        client = self._make_mkv(settings)
        if hasattr(client, "no_output_timeout"):
            client.no_output_timeout = max(client.no_output_timeout, 600)
        before = len(stuck)
        try:
            scan = await client.inspect_source(
                job_id, source, min_length, max(settings.inspect_timeout_seconds, 2 * 3600), scan_event
            )
        except ProcessFailure as error:
            if len(stuck) > before:
                raise ImageNotDecryptable(
                    f"MakeMKV cannot decrypt the movie even with the disc's decryption information "
                    f"(it keeps failing at {stuck[-1]}). The rescued image is kept."
                ) from error
            raise
        match = match_title(scan.titles, main_track)
        if match is None:
            raise RuntimeError("MakeMKV could not find the selected movie in the rescued disc files")
        playlist = playlist or str(getattr(match, "source_filename", "") or "")
        await asyncio.to_thread(
            self._append_log, job_id, f"MakeMKV reads the rescued files with the disc's decryption information: title {match.id}"
        )
        self.database.update_job(job_id, status_detail="Extracting the movie from the rescued disc image")
        try:
            await client.rip_source(
                job_id,
                source,
                extracted,
                [match.id],
                settings.rip_timeout_seconds,
                callback=extraction_event,
                min_length=min_length,
            )
        except MakeMKVLicenseError:
            raise
        except ProcessFailure as error:
            library = libmmbd_library(settings.make_mkv_path)
            if error.result.cancelled or not written["padded_bytes"] or not library or not playlist:
                raise
            await self._copy_damaged_bluray(
                job_id, settings, folder, playlist, extracted, library, str(error), extraction_event
            )

    @staticmethod
    def _unread_sectors(image: Path) -> list[tuple[int, int]]:
        """Sector ranges a saved rescue has not read or could not read."""
        try:
            rescue_map = RescueMap.load(DvdSectorRescue.artifact_paths(image)["map"])
        except (OSError, ValueError, KeyError, TypeError):
            return []
        return [(low, high) for low, high, status in rescue_map.ranges() if status != FINISHED]

    async def _copy_damaged_bluray(
        self,
        job_id: str,
        settings: AppSettings,
        folder: Path,
        playlist: str,
        extracted: Path,
        library: str,
        reason: str,
        callback: Callable[[dict], Awaitable[None]],
    ) -> None:
        """Copy a Blu-ray movie MakeMKV gives up on with FFmpeg, decrypted by MakeMKV's library.

        MakeMKV stops in a stretch of dense damage. FFmpeg's Blu-ray reader skips
        what cannot be used, so the movie only has gaps there.
        """
        await asyncio.to_thread(
            self._append_log,
            job_id,
            f"MakeMKV stopped in the damaged part of the movie ({reason}); copying the movie with FFmpeg, "
            "decrypted by MakeMKV's library",
        )
        self.database.update_job(job_id, status_detail="Copying the movie past the damaged spots")
        keys = folder.parent / "makemkv-keys"
        if extracted.exists():
            await asyncio.to_thread(shutil.rmtree, extracted)
        try:
            await asyncio.to_thread(build_key_folder, folder, keys, playlist)
            copied = await BlurayMovieCopy(settings.ffmpeg_path, settings.ffprobe_path, self.runner, library).copy(
                job_id, folder, keys, playlist, extracted, settings.rip_timeout_seconds, callback
            )
        finally:
            # Hard links and empty placeholders only; MakeMKV may still hold a placeholder for a moment.
            await asyncio.to_thread(shutil.rmtree, keys, True)
        await asyncio.to_thread(
            self._append_log, job_id, f"FFmpeg copied the movie past the damaged spots into {copied.name}"
        )

    async def _rip_and_finish(
        self,
        job_id: str,
        drive: DriveInfo,
        settings: AppSettings | None = None,
        *,
        best_effort: bool = False,
        salvaged: bool = False,
    ) -> None:
        job = self.database.get_job(job_id)
        if not job:
            return
        settings = settings or self._settings_for_job(job)
        # The job remembers what was inspected. A recovery that works from a
        # saved image must not depend on what is in the drive right now.
        disc_kind = DiscKind(job.get("disc_type") or drive.disc_kind)
        staging = Path(job["staging_path"])
        staging.mkdir(parents=True, exist_ok=True)
        tracks = self.database.list_tracks(job_id)
        selected_ids = [track["source_id"] for track in tracks if track["selected"]]
        if (
            disc_kind not in {DiscKind.AUDIO_CD, DiscKind.DATA}
            and settings.rip_mode != "backup"
            and not selected_ids
        ):
            raise RuntimeError("Select at least one disc title before ripping")
        estimated = sum(track["size_bytes"] for track in tracks if track["selected"])
        if best_effort and not self._rescue_image_saved(job_id, settings):
            # The rescue keeps a whole-disc image next to the extracted movie.
            estimated = estimated * 2 + 1024**3
        if not disk_space_ok(staging.parent, estimated):
            raise RuntimeError("Not enough free disk space for this rip")
        self.database.update_job(
            job_id,
            state=JobState.RIPPING,
            stage="recovering" if best_effort else "ripping",
            status_detail=(
                "Best-effort recovery — unreadable blocks will be skipped"
                if best_effort
                # cyanrip's own "Ripping track N of M" replaces this once it has read the CD.
                else "Reading the CD" if disc_kind == DiscKind.AUDIO_CD else "Ripping disc"
            ),
            progress=0,
            recoverable=1,
        )
        if best_effort:
            if disc_kind not in {DiscKind.DVD, DiscKind.BLURAY}:
                raise RuntimeError("Best-effort recovery is available for DVDs and Blu-rays")
            selected_tracks = [track for track in tracks if track["selected"]]
            main_track = max(
                selected_tracks,
                key=lambda track: (
                    int(track.get("duration_seconds") or 0),
                    int(track.get("size_bytes") or 0),
                ),
            )
            if not salvaged:
                await self._recover_disc_to_staging(job_id, drive, settings, staging, main_track)
        elif disc_kind == DiscKind.AUDIO_CD:
            if any(staging.iterdir()):
                # cyanrip rips every track again, and what an earlier attempt left may even be of another CD.
                await asyncio.to_thread(shutil.rmtree, staging)
                staging.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(
                    self._append_log, job_id, "Removed the tracks of an earlier attempt; the CD is ripped from the start"
                )
            await asyncio.to_thread(
                self._append_log,
                job_id,
                f"Ripping the audio CD in {drive.letter} with cyanrip, read offset {settings.cd_read_offset:+d} samples",
            )
            lookups: list[asyncio.Task] = []

            async def audio_event(event: dict) -> None:
                if event.get("type") != "cd":
                    await self._process_event(job_id, event)
                    return
                await self._remember_cd(job_id, str(event["discid"]), int(event["tracks"]))
                # MusicBrainz is asked once, while the CD rips; the album is only needed for the tags at the end.
                lookups.append(self._spawn(self._look_up_album(job_id), f"album-lookup-{job_id}"))

            await AudioRipper(settings.cyanrip_path, self.runner).rip(
                job_id, drive.letter, staging, callback=audio_event, offset=settings.cd_read_offset
            )
            await asyncio.gather(*lookups, return_exceptions=True)
        elif disc_kind == DiscKind.DATA:
            await self._back_up_data_disc(job_id, drive, job, staging)
        else:
            results = await self._make_mkv(settings).rip(
                job_id,
                drive.letter,
                staging,
                selected_ids,
                settings.rip_timeout_seconds,
                callback=lambda event: self._process_event(job_id, event),
                backup=settings.rip_mode == "backup",
                # Title ids come from a scan filtered by this length.
                min_length=settings.min_length_seconds,
            )
            latest = self.database.get_job(job_id) or job
            if (latest.get("metadata") or {}).get("ai_repair_requested"):
                raise ProcessFailure("Switching to AI repair preparation", results[-1])
            if (latest.get("metadata") or {}).get("recovery_requested"):
                raise ProcessFailure("Switching to damaged-disc recovery", results[-1])

        self.database.update_job(
            job_id,
            state=JobState.VERIFYING,
            stage="verifying",
            status_detail="Checking the ripped files",
            progress=0,
        )
        files = await verify_outputs(staging, settings.ffprobe_path)

        # A recovery that reused a saved image may run with the drive empty.
        current_drive = getattr(self, "drives", {}).get(drive.id, drive)
        if settings.auto_eject and current_drive.media_loaded:
            self.database.update_job(
                job_id,
                state=JobState.EJECTING,
                stage="ejecting",
                status_detail="Releasing and ejecting the disc",
            )
            try:
                await self.drive_control.eject(drive.letter)
            except OSError as error:
                await asyncio.to_thread(self._append_log, job_id, f"Eject warning: {error}")

        if best_effort and disc_kind in {DiscKind.DVD, DiscKind.BLURAY}:
            await self._mark_damage_in_movie(job_id, settings, staging, disc_kind)
        if disc_kind == DiscKind.AUDIO_CD and not await self._tag_audio_cd(job_id, settings, staging):
            # The tracks wait in staging for their album; the CD is finished from the dashboard.
            return

        final_source = staging
        keep_source = False
        remove_raw_after_finalize = False
        if not settings.skip_transcode and disc_kind in {DiscKind.BLURAY, DiscKind.DVD}:
            self.database.update_job(
                job_id,
                state=JobState.TRANSCODING,
                stage="transcoding",
                status_detail="Encoding video",
                progress=0,
            )
            transcode_dir = staging.with_name(staging.name.removesuffix(".partial") + ".transcode.partial")
            preset = (
                settings.handbrake_preset_bluray
                if disc_kind == DiscKind.BLURAY
                else settings.handbrake_preset_dvd
            )
            await HandBrakeTranscoder(settings.handbrake_path, self.runner).transcode(
                job_id,
                [path for path in files if path.suffix.lower() == ".mkv"],
                transcode_dir,
                preset,
                callback=lambda event: self._process_event(job_id, event),
            )
            await verify_outputs(transcode_dir, settings.ffprobe_path)
            final_source = transcode_dir
            # The encoded staging tree always moves into the final library.
            # The setting controls only whether the original MakeMKV tree stays.
            keep_source = False
            remove_raw_after_finalize = not settings.keep_raw_after_transcode

        await self._finish_in_library(
            job_id,
            settings,
            staging,
            final_source,
            keep_source=keep_source,
            remove_raw_after_finalize=remove_raw_after_finalize,
            best_effort=best_effort,
        )

    async def _finish_in_library(
        self,
        job_id: str,
        settings: AppSettings,
        staging: Path,
        final_source: Path,
        *,
        keep_source: bool = False,
        remove_raw_after_finalize: bool = False,
        best_effort: bool = False,
    ) -> None:
        """Move the ripped files into the library and complete the job."""
        # Title search stays available during the rip. Re-read the job before
        # finalization so a newly selected title controls the output folder.
        job = self.database.get_job(job_id)
        if not job:
            return
        self.database.update_job(
            job_id,
            state=JobState.FINALIZING,
            stage="finalizing",
            status_detail="Moving files into the completed library",
            progress=99,
        )
        media_kind = MediaKind(job["media_kind"])
        directories = settings.resolved_directories()
        library_root = directories["music"] if media_kind == MediaKind.MUSIC else directories["completed"]
        existing_folder: Path | None = None
        joining = (job.get("metadata") or {}).get("add_to_existing")
        if isinstance(joining, dict) and joining.get("output_path"):
            try:
                candidate = ensure_within(Path(str(joining["output_path"])), library_root)
                if candidate != library_root and candidate.is_dir():
                    existing_folder = candidate
            except (OSError, ValueError):
                existing_folder = None
            if existing_folder is None:
                await asyncio.to_thread(
                    self._append_log,
                    job_id,
                    "The folder these titles were to join is no longer in the library; they get a folder of their own",
                )
        if existing_folder is not None:
            # Titles added to a disc completed earlier join its folder, next to the files already there.
            await asyncio.to_thread(
                rename_video_outputs, final_source, existing_folder.name, media_kind, existing=existing_folder
            )
            finalized = await merge_into_directory(final_source, existing_folder, keep_source)
        else:
            duplicate_policy = settings.duplicate_policy
            if duplicate_policy == "ask" and (job.get("metadata") or {}).get("duplicate_override"):
                duplicate_policy = "keep_both"
            if duplicate_policy in {"ask", "skip"}:
                # Those policies return before ripping when a duplicate exists. For a
                # first-time disc they both use the canonical unused destination.
                duplicate_policy = "keep_both"
            destination = output_folder(
                library_root,
                media_kind,
                job["title"],
                job["year"],
                job["fingerprint"],
                duplicate_policy=duplicate_policy,
                include_category=media_kind != MediaKind.MUSIC,
            )
            if duplicate_policy == "replace":
                duplicate_output = ((job.get("metadata") or {}).get("duplicate") or {}).get("output_path")
                if duplicate_output:
                    try:
                        previous_destination = ensure_within(Path(duplicate_output), library_root)
                        if previous_destination != library_root and previous_destination.exists():
                            destination = previous_destination
                    except (OSError, ValueError):
                        # Ignore stale or unsafe history and use the new canonical path.
                        pass
            await asyncio.to_thread(rename_video_outputs, final_source, destination.name, media_kind)
            finalized = await finalize_directory(
                final_source,
                destination,
                keep_source,
                replace_existing=duplicate_policy == "replace",
            )
        if remove_raw_after_finalize and staging.exists():
            try:
                await asyncio.to_thread(shutil.rmtree, staging)
            except OSError as error:
                await asyncio.to_thread(
                    self._append_log, job_id, f"Raw cleanup warning after successful finalization: {error}"
                )
        recovery = (job.get("metadata") or {}).get("recovery") or {}
        skipped = int(recovery.get("movie_unreadable_bytes") or recovery.get("unreadable_bytes") or 0) + int(
            recovery.get("movie_pending_bytes") or 0
        )
        if not best_effort:
            detail = "Completed"
        elif skipped:
            detail = f"Recovered, {_format_bytes(skipped)} of unreadable disc data skipped"
        else:
            detail = "Recovered, every damaged block was read in the end"
        self.database.update_job(
            job_id,
            state=JobState.COMPLETED,
            stage="completed",
            status_detail=detail,
            progress=100,
            output_path=str(finalized),
            completed_at=utc_now(),
            process_pid=None,
            recoverable=0,
            error_code=None,
            error_message=None,
        )
        if best_effort:
            await self._discard_rescue_image(job_id, settings)
        await self.notifications.send(
            job_id,
            "completed",
            "Damaged disc recovery completed" if best_effort else "Disc rip completed",
            f"{job['title']} is ready in {finalized}."
            + (
                f" {_format_bytes(skipped)} of unreadable disc data was skipped, so the movie may glitch "
                "or jump at the damaged spots."
                if best_effort and skipped
                else ""
            ),
        )

    @staticmethod
    def _movie_files(folder: Path) -> list[Path]:
        """MKV files of a movie folder, largest first, without DiscDock's work files or the kept unrepaired copy."""
        if not folder.is_dir():
            return []
        movies = [
            path
            for path in folder.rglob("*.mkv")
            if path.is_file()
            and not is_unrepaired_copy(path)
            and not any(part.startswith(".discdock-") for part in path.relative_to(folder).parts)
        ]
        return sorted(movies, key=lambda path: path.stat().st_size, reverse=True)

    def _record_damage(
        self,
        job_id: str,
        moments: list[dict[str, Any]],
        screens: list[dict[str, Any]],
        details: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        added = {_moment_key(item) for item in screens}
        recorded = [
            _moment_entry(
                moment,
                "loading_screen" if _moment_key(moment) in added else str(moment.get("treatment") or _untreated(moment)),
            )
            for moment in moments
        ]
        job = self.database.get_job(job_id)
        if job:
            metadata = dict(job.get("metadata") or {})
            previous = metadata.get("damage") if isinstance(metadata.get("damage"), dict) else {}
            kept = {
                key: previous[key]
                for key in ("kept_copy", "loading_screen_method", "reviewed_at")
                if key in previous
            }
            metadata["damage"] = {**kept, **(details or {}), "analyzed_at": utc_now(), "moments": recorded}
            self.database.update_job(job_id, metadata_json=json.dumps(metadata, ensure_ascii=False))
        return recorded

    async def _apply_loading_screens(
        self,
        job_id: str,
        settings: AppSettings,
        movie: Path,
        moments: list[dict[str, Any]],
        info: dict[str, Any],
        disc_type: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Put loading screens into the movie and keep the movie as it was read next to it.

        Returns the moments that got a loading screen and details for the damage
        record. If anything fails, the movie stays exactly as it was.
        """
        screens = [moment for moment in moments if loading_screen_worthy(moment)]
        if not screens:
            return [], {}
        workdir = movie.parent / ".discdock-damage"
        # An attempt interrupted by a restart may have left its work files behind.
        await asyncio.to_thread(shutil.rmtree, workdir, ignore_errors=True)
        count = len(screens)
        self.database.update_job(
            job_id,
            state=JobState.TRANSCODING,
            stage="damage_screens",
            status_detail=f"Adding loading screens at {count} damaged {'moment' if count == 1 else 'moments'}",
            progress=0,
        )
        kept = unrepaired_copy_path(movie)
        if (
            self._rescue_image(job_id, settings).parent.exists()
            and shutil.disk_usage(movie.parent).free < movie.stat().st_size * 2 + 1024**3
        ):
            # The movie is extracted and checked; its rescue image only serves another attempt.
            await asyncio.to_thread(
                self._append_log, job_id, "Removing the rescue image now to make room for the loading screens"
            )
            await self._discard_rescue_image(job_id, settings)
        try:
            patched, method = await MoviePatcher(
                settings.ffmpeg_path, settings.ffprobe_path, self.runner
            ).add_loading_screens(
                job_id,
                movie,
                workdir,
                screens,
                info=info,
                disc=disc_display_name(disc_type),
                progress_callback=lambda event: self._process_event(job_id, event),
            )
            await verify_media_file(patched, settings.ffprobe_path)
            if kept.exists():
                # An earlier pass already kept the movie as it was read from the disc.
                await asyncio.to_thread(os.replace, patched, movie)
            else:
                await asyncio.to_thread(os.replace, movie, kept)
                try:
                    await asyncio.to_thread(os.replace, patched, movie)
                except OSError:
                    await asyncio.to_thread(os.replace, kept, movie)
                    raise
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await asyncio.to_thread(
                self._append_log, job_id, f"Could not add loading screens; the movie is kept as it was: {error}"
            )
            return [], {}
        finally:
            await asyncio.to_thread(shutil.rmtree, workdir, ignore_errors=True)
        how = (
            "only those moments were encoded again, the rest of the movie is copied unchanged"
            if method == "splice"
            else "the whole video was encoded again"
        )
        await asyncio.to_thread(
            self._append_log,
            job_id,
            "Added loading screens at "
            + ", ".join(
                f"{format_timestamp(moment['start_seconds'], round_up=False)}-{format_timestamp(moment['end_seconds'])}"
                for moment in screens
            )
            + f"; {how}. The movie without loading screens is kept as {kept.name}",
        )
        return screens, {"loading_screen_method": method, "kept_copy": True}

    async def _mark_damage_in_movie(
        self, job_id: str, settings: AppSettings, folder: Path, disc_kind: DiscKind
    ) -> None:
        """Record where the disc was damaged and put a loading screen over each longer hole.

        The movie is already complete here, so nothing in this step fails the job.
        """
        movies = await asyncio.to_thread(self._movie_files, folder)
        if not movies or not settings.ffprobe_path or not Path(settings.ffprobe_path).is_file():
            return
        movie = movies[0]
        job = self.database.get_job(job_id) or {}
        plan = (job.get("metadata") or {}).get("ai_repair")
        try:
            if (
                isinstance(plan, dict)
                and plan.get("status") != "applied"
                and Path(str(plan.get("source_path") or "")).name == movie.name
            ):
                # The AI analysis already measured this very file.
                moments = _damage_record_from_plan(plan)["moments"]
                info = _video_info_from_plan(plan)
            else:
                self.database.update_job(job_id, status_detail="Finding where the disc was damaged in the movie")
                measured = await asyncio.to_thread(
                    measure_damage, movie, settings.ffprobe_path, scan_without_gaps=False, owner=job_id
                )
                moments = damage_moments(measured)
                info = measured["info"]
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await asyncio.to_thread(self._append_log, job_id, f"Could not map the damaged moments of the movie: {error}")
            return
        screens: list[dict[str, Any]] = []
        details: dict[str, Any] = {}
        # The current setting, not the copy saved with the job: it is a choice about how the movie looks.
        placeholder = self.settings.damage_placeholder
        if placeholder == "loading_screen" and settings.ffmpeg_path and Path(settings.ffmpeg_path).is_file():
            screens, details = await self._apply_loading_screens(
                job_id, settings, movie, moments, info, disc_kind.value
            )
        elif placeholder == "ask" and any(loading_screen_worthy(moment) for moment in moments):
            # The movie is finished as it was read; the dashboard offers to keep it or add loading screens.
            details = {"choice": "pending"}
            await self.notifications.send(
                job_id,
                "attention",
                "The movie freezes where the disc was damaged",
                "It was kept as it was read. In DiscDock you can keep it like that or add loading screens.",
            )
        self._record_damage(job_id, moments, screens, details)

    async def _fail(
        self,
        job_id: str,
        code: str,
        message: str,
        blocked: bool = False,
        *,
        keep_staging: bool = False,
        detail: str | None = None,
    ) -> None:
        state = JobState.BLOCKED if blocked else JobState.FAILED
        changes: dict[str, Any] = {
            "state": state,
            "stage": state.value,
            "status_detail": detail or ("Action required" if blocked else "Rip failed"),
            "error_code": code,
            "error_message": message[:4000],
            "recoverable": 1,
            "process_pid": None,
        }
        job = self.database.get_job(job_id)
        if job:
            metadata = dict(job.get("metadata") or {})
            route = metadata.get("recovery_route")
            if isinstance(route, dict):
                route = dict(route)
                route.update(
                    {
                        "status": "failed",
                        "failed_at": utc_now(),
                        "error_code": code,
                    }
                )
                metadata["recovery_route"] = route
                changes["metadata_json"] = json.dumps(metadata, ensure_ascii=False)
            try:
                await asyncio.to_thread(
                    self._append_log,
                    job_id,
                    f"FAILED [{code}]: {message}",
                )
            except Exception:
                # Database state remains authoritative if a log directory is
                # temporarily unavailable.
                pass
        if job and job.get("staging_path") and not keep_staging:
            try:
                directories = self._settings_for_job(job).resolved_directories()
                staging = Path(job["staging_path"])
                if not _inside(staging, directories["raw"]):
                    current = self.settings.resolved_directories()
                    if _inside(staging, current["raw"]):
                        # The data folder was changed in Settings after the job was created.
                        directories = current
                moved = await move_failed_staging(
                    staging,
                    directories["failed"],
                    job_id,
                    raw_root=directories["raw"],
                )
                if moved:
                    changes["staging_path"] = str(moved)
            except Exception as move_error:
                await asyncio.to_thread(
                    self._append_log,
                    job_id,
                    f"Could not move failed staging files: {move_error}",
                )
        self.database.update_job(job_id, **changes)
        self._spawn(
            self.notifications.send(job_id, "failed", "DiscDock could not finish", message[:500]),
            f"notify-failed-{job_id}",
        )

    async def _start_best_effort_recovery(
        self, job_id: str, drive: DriveInfo, settings: AppSettings, *, keep_salvaged: bool = False
    ) -> None:
        job = self.database.get_job(job_id)
        if not job:
            raise LookupError("Job not found")
        directories = settings.resolved_directories()
        salvaged = await asyncio.to_thread(self._salvaged_movie_dir, job)
        current = getattr(self, "drives", {}).get(drive.id, drive)
        disc_present = current.media_loaded and (
            not job.get("disc_label")
            or current.volume_label.casefold() == str(job["disc_label"]).casefold()
        )
        if (
            salvaged
            and not keep_salvaged
            and disc_present
            and await asyncio.to_thread(
                self._rescue_has_retry_work, self._rescue_image(job_id, settings), settings
            )
        ):
            # That movie was cut from an image that can still be improved, so
            # re-reading first gives a better result than keeping its holes.
            salvaged = None
        if salvaged:
            # A refused AI analysis already extracted the movie from the rescue
            # image; finishing it needs neither the disc nor another extraction.
            staging = salvaged
        else:
            await self._archive_previous_attempt(
                job_id, settings, Path(job["staging_path"]), "makemkv-partial"
            )
            staging = directories["raw"] / f"{job_id}-recovery-{uuid.uuid4().hex[:8]}.partial"
        job = self.database.get_job(job_id) or job
        metadata = dict(job.get("metadata") or {})
        metadata.pop("recovery_requested", None)
        route = dict(metadata.get("recovery_route") or {})
        route.update(
            {
                "mode": "sector_best_effort",
                "status": "running",
                "started_at": utc_now(),
            }
        )
        metadata["recovery_route"] = route
        saved = self._rescue_image_saved(job_id, settings)
        previous = metadata.get("recovery") if isinstance(metadata.get("recovery"), dict) else {}
        kept = (*RESCUE_STATUS_FIELDS, "image_path", "engine", "finish_without_reading")
        metadata["recovery"] = {
            **({key: previous[key] for key in kept if key in previous} if saved or salvaged else {}),
            "mode": "sector_best_effort",
            "started_at": utc_now(),
            "quality_warning": RESCUE_QUALITY_WARNING,
        }
        self.database.update_job(
            job_id,
            state=JobState.RIPPING,
            stage="recovering",
            status_detail="Starting best-effort recovery",
            progress=0,
            staging_path=str(staging),
            metadata_json=json.dumps(metadata, ensure_ascii=False),
            cancel_requested=0,
            error_code=None,
            error_message=None,
        )
        if salvaged:
            message = "Finishing the movie already extracted from the rescued disc, with the damaged moments skipped."
        elif saved:
            message = "Continuing the saved damaged-disc rescue image."
        else:
            message = "MakeMKV could not read the damaged section; starting the damaged-disc rescue."
        await asyncio.to_thread(self._append_log, job_id, message)
        await self._rip_and_finish(job_id, drive, settings, best_effort=True, salvaged=salvaged is not None)

    @staticmethod
    def _salvaged_movie_dir(job: dict[str, Any]) -> Path | None:
        """A folder with a movie already extracted from the rescue, which can be finished without the disc.

        That is the movie extracted for an AI analysis, or the movie of a recovery that was
        stopped after extraction, for example while loading screens were added; the rescue
        image may be gone by then. Files only reach a recovery folder once extracted in full.
        """
        recovery = (job.get("metadata") or {}).get("recovery")
        candidates = [recovery.get("salvaged_movie_dir")] if isinstance(recovery, dict) else []
        staging = str(job.get("staging_path") or "")
        # A failed job's movie did not pass the checks after extraction.
        if staging and "-recovery-" in Path(staging).name and job.get("state") != JobState.FAILED:
            candidates.append(staging)
        for value in candidates:
            if not value:
                continue
            folder = Path(str(value))
            try:
                if any(path.is_file() for path in folder.glob("*.mkv")):
                    return folder
            except OSError:
                continue
        return None

    def _rescue_image_saved(self, job_id: str, settings: AppSettings) -> bool:
        image = self._rescue_image(job_id, settings)
        return image.is_file() or DvdSectorRescue.artifact_paths(image)["partial"].is_file()

    async def _archive_previous_attempt(
        self, job_id: str, settings: AppSettings, previous_staging: Path, label: str
    ) -> None:
        """Keep partial movie output from an earlier attempt and reuse its rescue image."""
        directories = settings.resolved_directories()
        image = self._rescue_image(job_id, settings)
        await asyncio.to_thread(
            self._adopt_previous_rescue_image, job_id, settings, image, [previous_staging]
        )
        if not previous_staging.exists() or previous_staging == image.parent:
            return

        def has_media() -> bool:
            return any(
                path.is_file() and path.suffix.lower() in {".mkv", ".mp4"} for path in previous_staging.rglob("*")
            )

        if not await asyncio.to_thread(has_media):
            if previous_staging.parent == directories["raw"]:
                await asyncio.to_thread(shutil.rmtree, previous_staging, ignore_errors=True)
            return
        archived = await move_failed_staging(
            previous_staging,
            directories["failed"],
            f"{job_id}-{label}",
            raw_root=directories["raw"],
        )
        if archived and archived != previous_staging.resolve():
            await asyncio.to_thread(
                self._append_log, job_id, f"Preserved the incomplete earlier attempt at {archived}"
            )

    async def _resume_damaged_recovery(
        self, job_id: str, drive: DriveInfo, *, keep_salvaged: bool = False
    ) -> None:
        async with self._drive_locks[drive.id]:
            try:
                job = self.database.get_job(job_id)
                if not job:
                    return
                await self._start_best_effort_recovery(
                    job_id, drive, self._settings_for_job(job), keep_salvaged=keep_salvaged
                )
            except asyncio.CancelledError:
                self._mark_cancelled(job_id)
                raise
            except FileNotFoundError as error:
                await self._fail(job_id, "tool_missing", str(error), blocked=True)
            except DriveNotResponding as error:
                # Resumes only after the drive is reconnected or the disc reinserted.
                await self._fail(job_id, "drive_not_responding", str(error))
            except ImageNotDecryptable as error:
                await self._fail(job_id, "image_not_decryptable", str(error))
            except Exception as error:
                await self._fail(job_id, "damaged_disc_recovery_failed", str(error))

    async def recover_damaged_job(self, job_id: str) -> dict[str, Any]:
        job = self.database.get_job(job_id)
        if not job:
            raise LookupError("Job not found")
        if job["disc_type"] not in {DiscKind.DVD, DiscKind.BLURAY}:
            raise RuntimeError("Best-effort recovery is available for DVDs and Blu-rays")
        if not _job_reported_damage(job):
            raise RuntimeError("This job has not reported an unreadable disc section")
        metadata = dict(job.get("metadata") or {})
        if metadata.get("recovery_requested") or (
            job["state"] in {JobState.QUEUED, JobState.RIPPING}
            and job.get("stage") == "recovering"
        ):
            raise RuntimeError("Damaged-disc recovery is already running")
        settings = self._settings_for_job(job)
        # A finished rescue image, or a movie already extracted for a refused AI
        # analysis, holds everything needed; the disc does not have to be inserted.
        image_ready = self._saved_rescue_usable(job, settings)
        drive = self._drive_for_job(job)
        if not image_ready:
            if not drive or not drive.media_loaded:
                raise RuntimeError("Reinsert the damaged disc before starting recovery")
            if job["disc_label"] and drive.volume_label.casefold() != job["disc_label"].casefold():
                raise RuntimeError("Reinsert the same damaged disc before starting recovery")
        drive = drive or self._placeholder_drive(job)
        tracks = self.database.list_tracks(job_id)
        if not any(track["selected"] for track in tracks):
            if tracks and job.get("error_code") != "disc_unreadable":
                raise RuntimeError("This job has no selected movie title to recover")
            # Nothing was ever listed for this disc: start again from its file system,
            # which also finds the titles MakeMKV refuses to report.
            return await self._recover_before_titles(job_id, job, drive)

        requested_at = utc_now()
        previous_recovery = metadata.pop("recovery", None)
        if isinstance(previous_recovery, dict) and previous_recovery.get("finish_without_reading"):
            metadata["recovery"] = {"finish_without_reading": True}
        metadata["recovery_requested"] = {"requested_at": requested_at}
        metadata["recovery_route"] = {
            "mode": "sector_best_effort",
            "status": "requested",
            "requested_at": requested_at,
        }
        if job["state"] == JobState.RIPPING:
            self.database.update_job(
                job_id,
                status_detail="Stopping MakeMKV and preparing best-effort recovery",
                metadata_json=json.dumps(metadata, ensure_ascii=False),
            )
            await self.runner.cancel(job_id)
            return self.database.get_job(job_id) or job

        if job["state"] not in {
            JobState.FAILED,
            JobState.CANCELLED,
            JobState.INTERRUPTED,
            JobState.BLOCKED,
        }:
            raise RuntimeError("This job cannot start damaged-disc recovery right now")
        blockers = self._external_drive_blockers(drive.letter)
        if blockers:
            raise RuntimeError("; ".join(blockers) + ". Close it before starting recovery.")
        if self.active_job_for_drive(drive.id):
            raise RuntimeError("The drive is already processing another disc")
        self._pending_insertions.pop(drive.id, None)
        self.database.update_job(
            job_id,
            state=JobState.QUEUED,
            stage="recovering",
            status_detail="Queued for best-effort damaged-disc recovery",
            progress=0,
            metadata_json=json.dumps(metadata, ensure_ascii=False),
            cancel_requested=0,
            error_code=None,
            error_message=None,
        )
        task = asyncio.create_task(
            self._resume_damaged_recovery(job_id, drive), name=f"damaged-recovery-{job_id}"
        )
        self._track_job_task(job_id, drive.id, task)
        return self.database.get_job(job_id) or job

    async def _recover_before_titles(self, job_id: str, job: dict[str, Any], drive: DriveInfo) -> dict[str, Any]:
        """Start a disc MakeMKV could not open over from its file system and navigation data."""
        if not drive.media_loaded:
            raise RuntimeError("Reinsert the damaged disc before starting recovery")
        blockers = self._external_drive_blockers(drive.letter)
        if blockers:
            raise RuntimeError("; ".join(blockers) + ". Close it before starting recovery.")
        if self.active_job_for_drive(drive.id):
            raise RuntimeError("The drive is already processing another disc")
        metadata = dict(job.get("metadata") or {})
        metadata["titles_from_rescue"] = True
        metadata["requested_rip_method"] = "sector_rescue"
        self._pending_insertions.pop(drive.id, None)
        self.database.update_job(
            job_id,
            state=JobState.QUEUED,
            stage="recovering",
            status_detail="Queued to read the damaged disc's file system",
            progress=0,
            metadata_json=json.dumps(metadata, ensure_ascii=False),
            cancel_requested=0,
            error_code=None,
            error_message=None,
        )
        task = asyncio.create_task(self._process_job(job_id), name=f"damaged-titles-{job_id}")
        self._track_job_task(job_id, drive.id, task)
        return self.database.get_job(job_id) or job

    async def finish_rescue_now(self, job_id: str) -> dict[str, Any]:
        """Stop re-reading damaged areas and continue with what was rescued."""
        job = self.database.get_job(job_id)
        if not job:
            raise LookupError("Job not found")
        image = self._rescue_image(job_id, self._settings_for_job(job))
        reading = (
            job["state"] == JobState.RIPPING
            and job.get("stage") in {"recovering", "ai_salvage"}
            and DvdSectorRescue.artifact_paths(image)["partial"].is_file()
        )
        if not reading and job["state"] in {JobState.FAILED, JobState.CANCELLED, JobState.INTERRUPTED}:
            return await self._finish_stopped_rescue(job, image)
        if not reading or not DvdSectorRescue.request_finish(image):
            raise RuntimeError("No damaged-disc rescue is reading this disc right now")
        await asyncio.to_thread(
            self._append_log, job_id, "Finish requested: continuing with the sectors rescued so far"
        )
        return (
            self.database.update_job(
                job_id, status_detail="Finishing the rescue with the data read so far"
            )
            or job
        )

    async def _finish_stopped_rescue(self, job: dict[str, Any], image: Path) -> dict[str, Any]:
        """Finish from a saved rescue after it stopped, for example when the drive hit a hardware fault."""
        job_id = str(job["id"])
        partial = DvdSectorRescue.artifact_paths(image)["partial"]
        if not (image.is_file() or partial.is_file()):
            raise RuntimeError(
                "This job's rescued disc image is no longer available; start a rescue rip with the disc instead"
            )
        if not await asyncio.to_thread(self._rescue_sweep_done, image):
            raise RuntimeError("The disc was not read to the end yet, so there is no complete movie to finish")
        self._update_recovery_metadata(job_id, finish_without_reading=True)
        await asyncio.to_thread(
            self._append_log, job_id, "Finish requested: using the sectors rescued so far without reading the disc"
        )
        metadata = (self.database.get_job(job_id) or job).get("metadata") or {}
        route = metadata.get("recovery_route")
        mode = str(route.get("mode") or "") if isinstance(route, dict) else ""
        if mode == "ai_repair" or job.get("error_code") in {"ai_repair_not_possible", "ai_repair_preparation_failed"}:
            return await self.retry_job(job_id)
        return await self.recover_damaged_job(job_id)

    async def _start_ai_repair_preparation(
        self, job_id: str, drive: DriveInfo, settings: AppSettings
    ) -> None:
        job = self.database.get_job(job_id)
        if not job:
            raise LookupError("Job not found")
        metadata = dict(job.get("metadata") or {})
        metadata.pop("ai_repair_requested", None)
        route = dict(metadata.get("recovery_route") or {})
        route.update(
            {
                "mode": "ai_repair",
                "status": "analyzing",
                "started_at": utc_now(),
            }
        )
        metadata["recovery_route"] = route
        directories = settings.resolved_directories()
        disc_kind = DiscKind(job["disc_type"])
        tracks = self.database.list_tracks(job_id)
        selected = [track for track in tracks if track["selected"]]
        if not selected:
            raise RuntimeError("This job has no selected movie title to recover")
        main_track = max(
            selected,
            key=lambda track: (
                int(track.get("duration_seconds") or 0),
                int(track.get("size_bytes") or 0),
            ),
        )
        salvaged = await asyncio.to_thread(self._salvaged_movie_dir, job)
        current = getattr(self, "drives", {}).get(drive.id, drive)
        disc_present = current.media_loaded and (
            not job.get("disc_label")
            or current.volume_label.casefold() == str(job["disc_label"]).casefold()
        )
        if (
            salvaged
            and disc_present
            and await asyncio.to_thread(
                self._rescue_has_retry_work, self._rescue_image(job_id, settings), settings
            )
        ):
            salvaged = None
        if salvaged:
            # The movie was already extracted from the rescue image; analyzing
            # it again needs neither the disc nor another extraction.
            source_root = salvaged
            self.database.update_job(
                job_id,
                state=JobState.RIPPING,
                stage="ai_analyzing",
                progress=0,
                staging_path=str(source_root),
                metadata_json=json.dumps(metadata, ensure_ascii=False),
                cancel_requested=0,
                error_code=None,
                error_message=None,
            )
        else:
            # DVDs and Blu-rays both go through the shared rescue image: switching
            # between best effort and AI never reads the disc twice, and the
            # movie continues past the damage instead of ending at it.
            await self._archive_previous_attempt(
                job_id, settings, Path(job["staging_path"]), "makemkv-ai-partial"
            )
            source_root = directories["raw"] / f"{job_id}-ai-source-{uuid.uuid4().hex[:8]}.partial"
            self.database.update_job(
                job_id,
                state=JobState.RIPPING,
                stage="ai_salvage",
                status_detail="Recovering readable video before AI analysis",
                progress=0,
                staging_path=str(source_root),
                metadata_json=json.dumps(metadata, ensure_ascii=False),
                cancel_requested=0,
                error_code=None,
                error_message=None,
            )
            await self._recover_disc_to_staging(job_id, drive, settings, source_root, main_track)
            self._update_recovery_metadata(job_id, salvaged_movie_dir=str(source_root))

        self.database.update_job(
            job_id,
            stage="ai_analyzing",
            status_detail="Finding missing and broken frames — free, no API request",
        )
        source = await asyncio.to_thread(find_repair_source, source_root)
        # Damage too long for AI never ends the job: the plan lists it as
        # skipped, and the review offers to keep the movie as it is.
        plan = await asyncio.to_thread(
            analyze_repair,
            source,
            settings.ffprobe_path,
            model=settings.ai_repair_model,
            quality=settings.ai_repair_quality,
            keyframes_per_second=settings.ai_repair_keyframes_per_second,
            expected_duration_seconds=int(main_track.get("duration_seconds") or 0),
            owner=job_id,
        )
        plan["source_root"] = str(source_root)
        image = self._rescue_image(job_id, settings)
        unread = await asyncio.to_thread(self._rescue_unread_bytes, image)
        plan["can_reread"] = bool(unread) and await asyncio.to_thread(self._rescue_has_retry_work, image, settings)
        if plan["can_reread"]:
            how = "automatically" if settings.auto_rip else "when you choose Read skipped spots again"
            plan.setdefault("notes", []).append(
                f"{_format_bytes(unread)} of the disc has not been read yet. Put the disc back in (if the drive "
                f"reported a fault, unplug and reconnect it first) and DiscDock reads those spots again {how}."
            )
        plan["disc_type"] = disc_kind.value
        plan["configured_cost_limit_usd"] = settings.ai_repair_cost_limit_usd
        metadata = dict((self.database.get_job(job_id) or job).get("metadata") or metadata)
        metadata.pop("ai_repair_requested", None)
        metadata["ai_repair"] = plan
        route = dict(metadata.get("recovery_route") or {})
        route.update({"status": "awaiting_confirmation", "analyzed_at": utc_now()})
        metadata["recovery_route"] = route
        self.database.update_job(
            job_id,
            state=JobState.AWAITING_REPAIR,
            stage="ai_review",
            status_detail=_ai_review_detail(plan),
            progress=0,
            staging_path=str(source_root),
            metadata_json=json.dumps(metadata, ensure_ascii=False),
            recoverable=1,
            error_code=None,
            error_message=None,
        )
        await asyncio.to_thread(
            self._append_log,
            job_id,
            (
                f"AI repair estimate {plan['estimate_id']}: {plan['frame_count']} replacement frames, "
                f"{plan['ai_keyframe_count']} OpenAI keyframes, estimated ceiling "
                f"${plan['estimated_max_cost_usd']:.2f}. No API request has been made."
            ),
        )
        if settings.auto_eject and getattr(self, "drives", {}).get(drive.id, drive).media_loaded:
            try:
                await self.drive_control.eject(drive.letter)
            except OSError as error:
                await asyncio.to_thread(self._append_log, job_id, f"Eject warning: {error}")
        await self.notifications.send(
            job_id,
            "attention",
            "AI repair estimate is ready" if plan["segments"] else "Damaged movie is ready to keep",
            str(plan.get("summary") or "Review the damaged moments before finishing the movie."),
        )

    async def _resume_ai_repair_preparation(self, job_id: str, drive: DriveInfo) -> None:
        async with self._drive_locks[drive.id]:
            try:
                job = self.database.get_job(job_id)
                if not job:
                    return
                await self._start_ai_repair_preparation(
                    job_id, drive, self._settings_for_job(job)
                )
            except asyncio.CancelledError:
                self._mark_cancelled(job_id)
                raise
            except Exception as error:
                await self._fail(job_id, "ai_repair_preparation_failed", str(error), keep_staging=True)

    async def prepare_ai_repair_job(self, job_id: str) -> dict[str, Any]:
        job = self.database.get_job(job_id)
        if not job:
            raise LookupError("Job not found")
        if job["disc_type"] not in {DiscKind.DVD, DiscKind.BLURAY}:
            raise RuntimeError("AI frame repair is available for DVDs and Blu-rays only")
        if not _job_reported_damage(job) and not _damage_moments_for_job(job):
            raise RuntimeError("This job has not reported an unreadable video section")
        settings = self._settings_for_job(job)
        if not settings.ai_repair_enabled:
            raise RuntimeError("Enable AI frame repair in Settings first")
        if not self.secret_store.get("openai_api_key"):
            raise RuntimeError("Add your OpenAI API key in Settings first")
        if not settings.ffmpeg_path or not Path(settings.ffmpeg_path).is_file():
            raise RuntimeError("FFmpeg is required for AI frame repair")
        if not settings.ffprobe_path or not Path(settings.ffprobe_path).is_file():
            raise RuntimeError("FFprobe is required for AI frame repair")
        metadata = dict(job.get("metadata") or {})
        plan = metadata.get("ai_repair") if isinstance(metadata.get("ai_repair"), dict) else None
        if metadata.get("ai_repair_requested") or job["state"] == JobState.AWAITING_REPAIR:
            raise RuntimeError("AI repair is already prepared or running for this job")
        if plan and plan.get("status") == "applied":
            raise RuntimeError("AI repair was already applied to this job")
        if job["state"] == JobState.COMPLETED and job.get("output_path"):
            # A finished movie is repaired where it lies, from the estimate the damage review worked out.
            return await self._review_library_ai_repair(job_id, job, settings, metadata, plan)
        if (
            plan
            and job["state"] in {JobState.FAILED, JobState.CANCELLED, JobState.INTERRUPTED}
            and Path(str(plan.get("source_path") or "")).is_file()
            # With the disc back in the drive, unread blocks can still be read:
            # analyze a better copy instead of restoring the old review.
            and not self._disc_can_improve(job, settings)
        ):
            # A cancelled estimate or a failed approved repair goes straight back
            # to review, without reading the disc or analyzing the movie again.
            plan = {**plan, "status": "awaiting_confirmation", "estimate_id": uuid.uuid4().hex}
            metadata["ai_repair"] = plan
            metadata["recovery_route"] = {
                "mode": "ai_repair",
                "status": "awaiting_confirmation",
                "requested_at": utc_now(),
            }
            self.database.update_job(
                job_id,
                state=JobState.AWAITING_REPAIR,
                stage="ai_review",
                status_detail=_ai_review_detail(plan),
                progress=0,
                staging_path=str(plan.get("source_root") or Path(str(plan["source_path"])).parent),
                metadata_json=json.dumps(metadata, ensure_ascii=False),
                recoverable=1,
                cancel_requested=0,
                error_code=None,
                error_message=None,
            )
            return self.database.get_job(job_id) or job
        metadata.pop("ai_repair", None)
        drive = self._drive_for_job(job)
        image_ready = self._saved_rescue_usable(job, settings)
        if not image_ready:
            if not drive or not drive.media_loaded:
                raise RuntimeError("Reinsert the damaged disc before preparing AI repair")
            if job["disc_label"] and drive.volume_label.casefold() != job["disc_label"].casefold():
                raise RuntimeError("Reinsert the same damaged disc before preparing AI repair")
        drive = drive or self._placeholder_drive(job)

        requested_at = utc_now()
        metadata["ai_repair_requested"] = {"requested_at": requested_at}
        metadata["recovery_route"] = {
            "mode": "ai_repair",
            "status": "requested",
            "requested_at": requested_at,
        }
        if job["state"] == JobState.RIPPING:
            self.database.update_job(
                job_id,
                status_detail="Stopping MakeMKV and preparing a no-cost AI repair estimate",
                metadata_json=json.dumps(metadata, ensure_ascii=False),
            )
            await self.runner.cancel(job_id)
            return self.database.get_job(job_id) or job

        if job["state"] not in {JobState.FAILED, JobState.CANCELLED, JobState.INTERRUPTED}:
            raise RuntimeError("This job cannot prepare AI repair right now")
        if drive.media_loaded:
            blockers = self._external_drive_blockers(drive.letter)
            if blockers:
                raise RuntimeError("; ".join(blockers) + ". Close it before preparing repair.")
        if self.active_job_for_drive(drive.id):
            raise RuntimeError("The drive is already processing another disc")
        self._pending_insertions.pop(drive.id, None)
        self.database.update_job(
            job_id,
            state=JobState.QUEUED,
            stage="ai_analyzing",
            status_detail="Preparing a no-cost AI repair estimate",
            progress=0,
            metadata_json=json.dumps(metadata, ensure_ascii=False),
            cancel_requested=0,
            error_code=None,
            error_message=None,
        )
        task = asyncio.create_task(
            self._resume_ai_repair_preparation(job_id, drive),
            name=f"ai-repair-prepare-{job_id}",
        )
        self._track_job_task(job_id, drive.id, task)
        return self.database.get_job(job_id) or job

    @staticmethod
    def _replace_movie_in_library(staging: Path, destination: Path, previous: Path) -> Path:
        """Move the repaired movie into the library folder it came from, over the file it replaces."""
        moved: list[str] = []
        for path in sorted(staging.iterdir()):
            if path.is_dir():
                continue
            os.replace(path, destination / path.name)
            moved.append(path.name)
        if previous.is_file() and previous.name not in moved:
            # The repaired movie was named differently; the file it replaces goes.
            previous.unlink()
        shutil.rmtree(staging, ignore_errors=True)
        return destination

    async def _finalize_ai_repair(
        self, job_id: str, settings: AppSettings, repaired: Path, plan: dict[str, Any]
    ) -> None:
        await verify_media_file(repaired, settings.ffprobe_path)
        job = self.database.get_job(job_id)
        if not job:
            return
        metadata = dict(job.get("metadata") or {})
        metadata["ai_repair"] = plan
        metadata["damage"] = {
            **_damage_record_from_plan(plan),
            "kept_copy": any(is_unrepaired_copy(path) for path in Path(job["staging_path"]).glob("*.mkv")),
        }
        self.database.update_job(
            job_id,
            state=JobState.FINALIZING,
            stage="finalizing",
            status_detail="Moving the AI-repaired movie into the library",
            progress=99,
            metadata_json=json.dumps(metadata, ensure_ascii=False),
        )
        media_kind = MediaKind(job["media_kind"])
        directories = settings.resolved_directories()
        library_root = directories["completed"]
        staging = Path(job["staging_path"])
        library_movie = Path(str(plan.get("library_path") or ""))
        if plan.get("library_path") and library_movie.parent.is_dir():
            # The movie was repaired where it already lived: the repaired file takes its
            # place in that folder and everything else in there is left alone.
            destination = library_movie.parent
            await asyncio.to_thread(rename_video_outputs, staging, destination.name, media_kind)
            finalized = await asyncio.to_thread(self._replace_movie_in_library, staging, destination, library_movie)
        else:
            duplicate_policy = settings.duplicate_policy
            if duplicate_policy in {"ask", "skip"}:
                duplicate_policy = "keep_both"
            destination = output_folder(
                library_root,
                media_kind,
                job["title"],
                job["year"],
                job["fingerprint"],
                duplicate_policy=duplicate_policy,
                include_category=True,
            )
            await asyncio.to_thread(rename_video_outputs, staging, destination.name, media_kind)
            finalized = await finalize_directory(
                staging,
                destination,
                keep_source=False,
                replace_existing=duplicate_policy == "replace",
            )
        self.database.update_job(
            job_id,
            state=JobState.COMPLETED,
            stage="completed",
            status_detail=f"Completed with {plan['frame_count']} AI-reconstructed frames",
            progress=100,
            output_path=str(finalized),
            completed_at=utc_now(),
            process_pid=None,
            recoverable=0,
            error_code=None,
            error_message=None,
            metadata_json=json.dumps(metadata, ensure_ascii=False),
        )
        await self._discard_rescue_image(job_id, settings)
        await self.notifications.send(
            job_id,
            "completed",
            "AI-assisted disc repair completed",
            (
                f"{job['title']} is ready with {plan['frame_count']} generated frames. "
                f"OpenAI cost: ${plan.get('actual_cost_usd', 0):.4f}."
            ),
        )

    async def _run_ai_repair(
        self, job_id: str, accepted_max_cost_usd: float
    ) -> None:
        job = self.database.get_job(job_id)
        if not job:
            return
        settings = self._settings_for_job(job)
        metadata = dict(job.get("metadata") or {})
        plan = dict(metadata.get("ai_repair") or {})
        source_root = Path(str(plan.get("source_root") or Path(str(plan["source_path"])).parent))
        destination = settings.resolved_directories()["raw"] / (
            f"{job_id}-ai-repaired-{uuid.uuid4().hex[:8]}.partial"
        )
        self.database.update_job(
            job_id,
            state=JobState.RIPPING,
            stage="ai_repair",
            status_detail="Generating replacement frames with OpenAI",
            progress=0,
            staging_path=str(destination),
            cancel_requested=0,
            error_code=None,
            error_message=None,
        )
        try:
            repair = OpenAIFrameRepair(
                self.secret_store.get("openai_api_key"),
                settings.ai_repair_model,
                settings.ai_repair_quality,
                settings.ffmpeg_path,
                self.runner,
                settings.ffprobe_path,
            )
            # Damage too long for AI gets a loading screen in the same encode.
            plan["loading_screens"] = settings.damage_placeholder == "loading_screen"
            plan["disc_name"] = disc_display_name(str(job.get("disc_type") or ""))
            repaired, finished_plan = await repair.generate_and_apply(
                job_id,
                plan,
                destination,
                accepted_max_cost_usd,
                progress_callback=lambda event: self._process_event(job_id, event),
            )
            source_movie = Path(str(plan.get("source_path") or ""))
            if source_movie.is_file():
                # The movie as it was read from the disc stays next to the repaired one.
                await asyncio.to_thread(
                    os.replace, source_movie, destination / f"{repaired.stem}{UNREPAIRED_SUFFIX_AI}{repaired.suffix}"
                )
            try:
                await move_failed_staging(
                    source_root,
                    settings.resolved_directories()["failed"],
                    f"{job_id}-pre-ai-source",
                    raw_root=settings.resolved_directories()["raw"],
                )
            except (OSError, ValueError) as error:
                await asyncio.to_thread(
                    self._append_log, job_id, f"Could not archive pre-AI source: {error}"
                )
            metadata["ai_repair"] = finished_plan
            self.database.update_job(
                job_id,
                metadata_json=json.dumps(metadata, ensure_ascii=False),
                staging_path=str(destination),
            )
            await self._finalize_ai_repair(job_id, settings, repaired, finished_plan)
        except asyncio.CancelledError:
            self._mark_cancelled(job_id)
            raise
        except Exception as error:
            await self._fail(job_id, "ai_repair_failed", str(error))

    async def apply_ai_repair_job(
        self, job_id: str, estimate_id: str, accepted_max_cost_usd: float
    ) -> dict[str, Any]:
        job = self.database.get_job(job_id)
        if not job:
            raise LookupError("Job not found")
        if job["state"] != JobState.AWAITING_REPAIR:
            raise RuntimeError("This job is not waiting for AI repair approval")
        settings = self._settings_for_job(job)
        plan = (job.get("metadata") or {}).get("ai_repair") or {}
        if plan.get("estimate_id") != estimate_id or plan.get("status") != "awaiting_confirmation":
            raise RuntimeError("The AI repair estimate changed; refresh before approving it")
        if not plan.get("segments"):
            raise RuntimeError("No damaged moment is short enough for AI; keep the movie instead")
        estimate = float(plan.get("estimated_max_cost_usd") or 0)
        if accepted_max_cost_usd + 0.000001 < estimate:
            raise RuntimeError("The approved cost must cover the displayed estimate ceiling")
        if accepted_max_cost_usd > settings.ai_repair_cost_limit_usd:
            raise RuntimeError(
                f"The approved cost exceeds the ${settings.ai_repair_cost_limit_usd:.2f} Settings limit"
            )
        if not self.secret_store.get("openai_api_key"):
            raise RuntimeError("The OpenAI API key is no longer configured")
        task = asyncio.create_task(
            self._run_ai_repair(job_id, accepted_max_cost_usd),
            name=f"ai-repair-apply-{job_id}",
        )
        self._track_job_task(job_id, job["drive_id"], task)
        return (
            self.database.update_job(
                job_id,
                state=JobState.QUEUED,
                stage="ai_repair",
                status_detail="AI repair approved and queued",
                recoverable=1,
            )
            or job
        )

    async def _review_library_ai_repair(
        self,
        job_id: str,
        job: dict[str, Any],
        settings: AppSettings,
        metadata: dict[str, Any],
        plan: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Offer AI repair for a movie already in the library, from the estimate the damage review made."""
        movies = await asyncio.to_thread(self._movie_files, Path(str(job["output_path"])))
        if not movies:
            raise RuntimeError("The movie file is no longer in its library folder")
        movie = movies[0]
        if not plan or not plan.get("segments") or Path(str(plan.get("source_path") or "")) != movie:
            # Nothing has priced this movie yet; look through it first and come back with the estimate.
            return await self.review_damaged_movie(job_id)
        # The repair reads and replaces its own copy of the movie. A hard link costs no disk space,
        # and the movie in the library stays untouched until the repaired one is ready.
        source_root = settings.resolved_directories()["raw"] / f"{job_id}-ai-source-{uuid.uuid4().hex[:8]}.partial"
        await asyncio.to_thread(source_root.mkdir, parents=True, exist_ok=True)
        source = source_root / movie.name
        await asyncio.to_thread(_link_or_copy, movie, source)
        plan = {
            **plan,
            "status": "awaiting_confirmation",
            "estimate_id": uuid.uuid4().hex,
            "source_path": str(source),
            "source_root": str(source_root),
            "library_path": str(movie),
            "configured_cost_limit_usd": settings.ai_repair_cost_limit_usd,
        }
        metadata["ai_repair"] = plan
        metadata["recovery_route"] = {
            "mode": "ai_repair",
            "status": "awaiting_confirmation",
            "requested_at": utc_now(),
        }
        await asyncio.to_thread(
            self._append_log,
            job_id,
            f"AI repair estimate {plan['estimate_id']} for the finished movie: {plan['frame_count']} frames, "
            f"up to ${float(plan.get('estimated_max_cost_usd') or 0):.2f}. No API request has been made.",
        )
        self.database.update_job(
            job_id,
            state=JobState.AWAITING_REPAIR,
            stage="ai_review",
            status_detail=_ai_review_detail(plan),
            progress=0,
            staging_path=str(source_root),
            metadata_json=json.dumps(metadata, ensure_ascii=False),
            recoverable=1,
            cancel_requested=0,
            error_code=None,
            error_message=None,
        )
        return self.database.get_job(job_id) or job

    async def keep_movie_without_ai(self, job_id: str) -> dict[str, Any]:
        """Finish the movie extracted for the AI analysis as it is, with the damaged moments skipped."""
        job = self.database.get_job(job_id)
        if not job:
            raise LookupError("Job not found")
        metadata = dict(job.get("metadata") or {})
        plan = metadata.get("ai_repair") if isinstance(metadata.get("ai_repair"), dict) else None
        if job["state"] != JobState.AWAITING_REPAIR or not plan:
            raise RuntimeError("This job is not waiting for an AI repair decision")
        if plan.get("library_path"):
            # The movie is already in the library; declining leaves it exactly as it is.
            return await self._keep_library_movie_without_ai(job_id, job, metadata, plan)
        source_root = Path(str(plan.get("source_root") or Path(str(plan.get("source_path") or "")).parent))
        if not await asyncio.to_thread(lambda: any(path.is_file() for path in source_root.glob("*.mkv"))):
            raise RuntimeError("The extracted movie is no longer available; choose Retry to extract it again")
        drive = self._drive_for_job(job) or self._placeholder_drive(job)
        if self.active_job_for_drive(drive.id):
            raise RuntimeError("The drive is already processing another disc")
        now = utc_now()
        metadata["ai_repair"] = {**plan, "status": "declined", "declined_at": now}
        recovery = dict(metadata.get("recovery") or {}) if isinstance(metadata.get("recovery"), dict) else {}
        recovery["salvaged_movie_dir"] = str(source_root)
        metadata["recovery"] = recovery
        metadata["recovery_route"] = {"mode": "sector_best_effort", "status": "requested", "requested_at": now}
        self.database.update_job(
            job_id,
            state=JobState.QUEUED,
            stage="recovering",
            status_detail="Keeping the movie without AI frames",
            progress=0,
            staging_path=str(source_root),
            metadata_json=json.dumps(metadata, ensure_ascii=False),
            cancel_requested=0,
            error_code=None,
            error_message=None,
        )
        task = asyncio.create_task(
            self._resume_damaged_recovery(job_id, drive, keep_salvaged=True), name=f"keep-without-ai-{job_id}"
        )
        self._track_job_task(job_id, drive.id, task)
        return self.database.get_job(job_id) or job

    async def _keep_library_movie_without_ai(
        self, job_id: str, job: dict[str, Any], metadata: dict[str, Any], plan: dict[str, Any]
    ) -> dict[str, Any]:
        """Decline AI repair for a movie in the library: it stays as it is, and the copy is thrown away."""
        source_root = Path(str(plan.get("source_root") or ""))
        if source_root.name.endswith(".partial"):
            await asyncio.to_thread(shutil.rmtree, source_root, True)
        # The estimate keeps pointing at the movie in the library, so changing your mind needs no new scan.
        metadata["ai_repair"] = {
            **plan,
            "status": "declined",
            "declined_at": utc_now(),
            "source_path": str(plan.get("library_path") or plan.get("source_path") or ""),
            "source_root": "",
        }
        metadata.pop("recovery_route", None)
        damage = dict(metadata.get("damage") or {}) if isinstance(metadata.get("damage"), dict) else {}
        # The damaged moments stay listed; only the question is answered.
        damage.pop("choice", None)
        if damage:
            metadata["damage"] = damage
        self.database.update_job(
            job_id,
            state=JobState.COMPLETED,
            stage="completed",
            status_detail=str(job.get("status_detail") or "Completed"),
            progress=100,
            staging_path=None,
            metadata_json=json.dumps(metadata, ensure_ascii=False),
            recoverable=0,
            error_code=None,
            error_message=None,
        )
        await asyncio.to_thread(self._append_log, job_id, "AI repair declined; the movie stays as it is")
        return self.database.get_job(job_id) or job

    async def add_titles_to_completed_disc(self, job_id: str) -> dict[str, Any]:
        """Let a disc completed before get more of its titles, ripped into the same library folder.

        A series first ripped as a single movie, for example, gets its other episodes next to it.
        The titles ripped last time are left unticked.
        """
        job = self.database.get_job(job_id)
        if not job:
            raise LookupError("Job not found")
        if job["state"] != JobState.BLOCKED or job.get("error_code") != "duplicate":
            raise RuntimeError("This disc is not waiting as an already completed disc")
        rows = self.database.query(
            """SELECT id,title,year,media_kind,output_path FROM jobs
               WHERE fingerprint=? AND state='completed' AND id<>?
               ORDER BY completed_at DESC, created_at DESC LIMIT 1""",
            (job.get("fingerprint") or "", job_id),
        )
        previous = rows[0] if rows else None
        folder = Path(str(previous.get("output_path") or "")) if previous else None
        if previous is None or folder is None or not str(previous.get("output_path") or "") or not folder.is_dir():
            raise RuntimeError("The library folder of this disc is gone; choose Retry to rip it again")
        tracks = self.database.list_tracks(job_id)
        if not tracks:
            raise RuntimeError("DiscDock has no titles of this disc; choose Retry to scan it again")
        ripped = sorted(track["source_id"] for track in self.database.list_tracks(str(previous["id"])) if track["selected"])
        self.database.replace_tracks(
            job_id,
            [{**track, "id": track["source_id"], "selected": track["source_id"] not in ripped} for track in tracks],
        )
        metadata = dict(job.get("metadata") or {})
        metadata["duplicate_override"] = True
        metadata["duplicate"] = {"job_id": previous["id"], "output_path": previous["output_path"]}
        metadata["add_to_existing"] = {
            "job_id": previous["id"],
            "output_path": previous["output_path"],
            "ripped_titles": ripped,
        }
        return self.database.update_job(
            job_id,
            title=previous.get("title") or job.get("title") or "",
            year=previous.get("year") or job.get("year") or "",
            media_kind=previous.get("media_kind") or job.get("media_kind"),
            state=JobState.AWAITING_INPUT,
            stage="awaiting_input",
            status_detail=f"Choose the titles to add to {folder.name}",
            metadata_json=json.dumps(metadata, ensure_ascii=False),
            error_code=None,
            error_message=None,
            recoverable=1,
        )

    async def keep_damaged_movie_as_is(self, job_id: str) -> dict[str, Any]:
        """Keep a finished movie as it was read, frozen where the disc was damaged, and stop asking."""
        job = self.database.get_job(job_id)
        if not job:
            raise LookupError("Job not found")
        metadata = dict(job.get("metadata") or {})
        damage = metadata.get("damage")
        if job["state"] != JobState.COMPLETED or not isinstance(damage, dict) or damage.get("choice") != "pending":
            raise RuntimeError("This movie is not waiting for a decision about its damaged moments")
        metadata["damage"] = {**damage, "choice": "kept", "chosen_at": utc_now()}
        await asyncio.to_thread(self._append_log, job_id, "Keeping the movie as it was read, without loading screens")
        return self.database.update_job(job_id, metadata_json=json.dumps(metadata, ensure_ascii=False))

    async def review_damaged_movie(self, job_id: str) -> dict[str, Any]:
        """Look through a finished movie for broken parts and work out how they can be repaired.

        The quick check after a rescue only sees moments the disc never delivered
        at all. A part the disc delivered broken looks like ordinary video until
        it is decoded, so this scan decodes the whole movie. It takes a few
        minutes, uses no OpenAI credit, and ends with the choice between keeping
        the movie, loading screens and AI repair.
        """
        job = self.database.get_job(job_id)
        if not job:
            raise LookupError("Job not found")
        if job["state"] != JobState.COMPLETED or not job.get("output_path"):
            raise RuntimeError("Broken parts can be looked for in finished movies in the library")
        if MediaKind(job["media_kind"]) == MediaKind.MUSIC:
            raise RuntimeError("Only movies and series can be looked through for broken parts")
        settings = self._settings_for_job(job)
        if not settings.ffprobe_path or not Path(settings.ffprobe_path).is_file():
            raise RuntimeError("FFprobe is required to look for broken parts")
        movies = await asyncio.to_thread(self._movie_files, Path(str(job["output_path"])))
        if not movies:
            raise RuntimeError("The movie file is no longer in its library folder")
        previous_detail = str(job.get("status_detail") or "Completed")
        self.database.update_job(
            job_id,
            state=JobState.TRANSCODING,
            stage="damage_scan",
            status_detail="Looking through the movie for broken parts",
            progress=0,
        )
        task = asyncio.create_task(
            self._run_damage_review(job_id, settings, movies[0], previous_detail),
            name=f"damage-review-{job_id}",
        )
        self._track_job_task(job_id, str(job.get("drive_id") or ""), task)
        return self.database.get_job(job_id) or job

    def _ai_repair_possible(self, settings: AppSettings) -> bool:
        """Whether an AI repair estimate can be worked out. The estimate itself never calls OpenAI."""
        return bool(
            settings.ai_repair_enabled
            and self.secret_store.get("openai_api_key")
            and settings.ffmpeg_path
            and Path(settings.ffmpeg_path).is_file()
        )

    async def _run_damage_review(
        self, job_id: str, settings: AppSettings, movie: Path, previous_detail: str
    ) -> None:
        job = self.database.get_job(job_id) or {}
        plan: dict[str, Any] | None = None
        try:
            if self._ai_repair_possible(settings):
                # One pass measures the damage and prices replacing it; nothing is sent to OpenAI.
                plan = await asyncio.to_thread(
                    analyze_repair,
                    movie,
                    settings.ffprobe_path,
                    model=settings.ai_repair_model,
                    quality=settings.ai_repair_quality,
                    keyframes_per_second=settings.ai_repair_keyframes_per_second,
                    owner=job_id,
                )
                moments = _damage_record_from_plan(plan)["moments"]
            else:
                measured = await asyncio.to_thread(measure_damage, movie, settings.ffprobe_path, owner=job_id)
                moments = damage_moments(measured)
        except asyncio.CancelledError:
            self.database.update_job(
                job_id, state=JobState.COMPLETED, stage="completed", progress=100, status_detail=previous_detail
            )
            raise
        except Exception as error:
            await asyncio.to_thread(
                self._append_log, job_id, f"Could not look through the movie for broken parts: {error}"
            )
            self.database.update_job(
                job_id, state=JobState.COMPLETED, stage="completed", progress=100, status_detail=previous_detail
            )
            await self.notifications.send(
                job_id, "failed", "The movie could not be looked through", str(error)[:500]
            )
            return
        untreated = [moment for moment in moments if moment.get("treatment") in {"skipped", "brief_glitch", None}]
        details: dict[str, Any] = {"reviewed_at": utc_now()}
        if untreated and (
            (plan or {}).get("segments") or any(loading_screen_worthy(moment) for moment in untreated)
        ):
            # There is something to decide: AI can replace frames, a loading screen can cover the hole, or both.
            details["choice"] = "pending"
        if plan is not None:
            plan.update({"status": "estimated", "source_path": str(movie), "source_root": str(movie.parent)})
            plan["disc_type"] = str(job.get("disc_type") or "")
            plan["configured_cost_limit_usd"] = settings.ai_repair_cost_limit_usd
            metadata = dict((self.database.get_job(job_id) or job).get("metadata") or {})
            metadata["ai_repair"] = plan
            self.database.update_job(job_id, metadata_json=json.dumps(metadata, ensure_ascii=False))
        self._record_damage(job_id, moments, [], details)
        await asyncio.to_thread(
            self._append_log,
            job_id,
            f"Looked through {movie.name} for broken parts: {len(moments)} found"
            + (
                f", {plan['frame_count']} frames could be replaced with AI for at most "
                f"${plan['estimated_max_cost_usd']:.2f}"
                if plan and plan.get("segments")
                else ""
            ),
        )
        self.database.update_job(
            job_id,
            state=JobState.COMPLETED,
            stage="completed",
            progress=100,
            process_pid=None,
            status_detail=(
                f"{previous_detail} · {len(moments)} broken {'moment' if len(moments) == 1 else 'moments'}"
                if moments
                else f"{previous_detail} · no broken parts found"
            ),
        )
        await self.notifications.send(
            job_id,
            "attention" if moments else "completed",
            "Broken parts found in the movie" if moments else "No broken parts in the movie",
            (
                f"{len(moments)} damaged {'moment' if len(moments) == 1 else 'moments'} in "
                f"{job.get('title') or 'the movie'}. Choose in DiscDock whether to keep the movie, add loading "
                "screens or replace the frames with AI."
                if moments
                else f"{job.get('title') or 'The movie'} plays through without broken video."
            ),
        )

    async def add_loading_screens_to_movie(self, job_id: str) -> dict[str, Any]:
        """Put loading screens into a finished movie where its damaged moments still freeze."""
        job = self.database.get_job(job_id)
        if not job:
            raise LookupError("Job not found")
        if job["state"] != JobState.COMPLETED or not job.get("output_path"):
            raise RuntimeError("Loading screens can be added to finished movies in the library")
        settings = self._settings_for_job(job)
        for tool in (settings.ffmpeg_path, settings.ffprobe_path):
            if not tool or not Path(tool).is_file():
                raise RuntimeError("FFmpeg and FFprobe are required to add loading screens")
        moments = _damage_moments_for_job(job)
        if not any(loading_screen_worthy(moment) and moment.get("treatment") in {"skipped", None} for moment in moments):
            raise RuntimeError("This movie has no damaged moment of 2 seconds or more without a loading screen")
        movies = await asyncio.to_thread(self._movie_files, Path(str(job["output_path"])))
        if not movies:
            raise RuntimeError("The movie file is no longer in its library folder")
        previous_detail = str(job.get("status_detail") or "Completed")
        self.database.update_job(
            job_id,
            state=JobState.TRANSCODING,
            stage="damage_screens",
            status_detail="Adding loading screens where the disc was damaged",
            progress=0,
        )
        task = asyncio.create_task(
            self._run_library_loading_screens(job_id, settings, movies[0], moments, previous_detail),
            name=f"loading-screens-{job_id}",
        )
        self._track_job_task(job_id, str(job.get("drive_id") or ""), task)
        return self.database.get_job(job_id) or job

    async def _run_library_loading_screens(
        self,
        job_id: str,
        settings: AppSettings,
        movie: Path,
        moments: list[dict[str, Any]],
        previous_detail: str,
    ) -> None:
        pending = [
            moment for moment in moments if loading_screen_worthy(moment) and moment.get("treatment") in {"skipped", None}
        ]
        screens: list[dict[str, Any]] = []
        details: dict[str, Any] = {}
        try:
            info = await asyncio.to_thread(video_info, movie, settings.ffprobe_path, job_id)
            job = self.database.get_job(job_id) or {}
            screens, details = await self._apply_loading_screens(
                job_id, settings, movie, pending, info, str(job.get("disc_type") or "")
            )
        except asyncio.CancelledError:
            # The library movie is untouched; it is still a finished movie.
            self.database.update_job(
                job_id, state=JobState.COMPLETED, stage="completed", progress=100, status_detail=previous_detail
            )
            raise
        except Exception as error:
            await asyncio.to_thread(self._append_log, job_id, f"Could not add loading screens: {error}")
        if not screens:
            # The movie was left exactly as it was, so the choice about it is still open.
            previous = ((self.database.get_job(job_id) or {}).get("metadata") or {}).get("damage")
            if isinstance(previous, dict) and previous.get("choice"):
                details = {**details, "choice": previous["choice"]}
        self._record_damage(job_id, moments, screens, details)
        count = len(screens)
        self.database.update_job(
            job_id,
            state=JobState.COMPLETED,
            stage="completed",
            progress=100,
            process_pid=None,
            status_detail=(
                f"{previous_detail} · loading screens at {count} damaged {'moment' if count == 1 else 'moments'}"
                if count
                else previous_detail
            ),
        )
        job = self.database.get_job(job_id) or {}
        await self.notifications.send(
            job_id,
            "completed" if count else "failed",
            "Loading screens added" if count else "Loading screens could not be added",
            (
                f"{job.get('title') or 'The movie'} now shows a loading screen with the resume time at {count} "
                f"damaged {'moment' if count == 1 else 'moments'}. The movie without loading screens is kept "
                "next to it."
                if count
                else "The movie was left unchanged. See the job log for details."
            ),
        )

    def _musicbrainz(self) -> MusicBrainzClient:
        client = self.__dict__.get("musicbrainz")
        if client is None:
            client = self.__dict__["musicbrainz"] = MusicBrainzClient()
        return client

    def _merge_job_metadata(self, job_id: str, values: dict[str, Any], **changes: Any) -> None:
        """Merge values into the job's current metadata, so work running beside it keeps its own."""
        job = self.database.get_job(job_id) or {}
        metadata = {**(job.get("metadata") or {}), **values}
        self.database.update_job(job_id, metadata_json=json.dumps(metadata, ensure_ascii=False), **changes)

    def _set_album(self, job_id: str, album: dict[str, Any], status: str, message: str = "") -> None:
        """Remember a CD's album for its tags, and show it as the job's title while the CD rips."""
        name = " - ".join(part for part in (str(album.get("artist") or ""), str(album.get("title") or "")) if part)
        year = str(album.get("date") or "")[:4]
        changes: dict[str, Any] = {"year": year if year.isdigit() else ""}
        if name:
            changes["title"] = name
        self._merge_job_metadata(
            job_id, {"album": album, "album_lookup": {"status": status, "message": message}}, **changes
        )

    async def _remember_cd(self, job_id: str, discid: str, tracks: int) -> None:
        """Record the CD cyanrip read. Another CD than in an earlier attempt of the job starts over with its album."""
        job = self.database.get_job(job_id) or {}
        metadata = dict(job.get("metadata") or {})
        previous = metadata.get("cd") if isinstance(metadata.get("cd"), dict) else {}
        notes = [f"cyanrip read the CD: {tracks} tracks, DiscID {discid}"]
        changes: dict[str, Any] = {}
        if previous.get("discid") and previous["discid"] != discid:
            for side, (key, _, _) in ALBUM_PHOTOS.items():
                photo = album_photo_path(job, side)
                if photo:
                    photo.unlink(missing_ok=True)
                metadata.pop(key, None)
            for key in ("album", "album_candidates", "album_lookup", "album_hold", "musicbrainz_releases"):
                metadata.pop(key, None)
            changes = {"title": "", "year": ""}
            notes.append(
                f"This is another CD than in the earlier attempt (DiscID {previous['discid']}), so the album found "
                "or entered for that CD is dropped and this CD is looked up instead"
            )
        metadata["cd"] = {"discid": discid, "tracks": tracks}
        self.database.update_job(job_id, metadata_json=json.dumps(metadata, ensure_ascii=False), **changes)
        for note in notes:
            await asyncio.to_thread(self._append_log, job_id, note)

    async def _look_up_album(self, job_id: str) -> None:
        """Ask MusicBrainz which album the CD is while it rips; a busy MusicBrainz only leaves a note."""
        metadata = (self.database.get_job(job_id) or {}).get("metadata") or {}
        cd = metadata.get("cd") if isinstance(metadata.get("cd"), dict) else {}
        if not cd.get("discid"):
            return
        if isinstance(metadata.get("album"), dict):
            await asyncio.to_thread(
                self._append_log,
                job_id,
                f"Not asking MusicBrainz: the album is already {describe_album(metadata['album'])}",
            )
            return
        await asyncio.to_thread(self._append_log, job_id, f"Looking up DiscID {cd['discid']} on MusicBrainz")
        try:
            releases = await self._musicbrainz().releases_for_disc(str(cd["discid"]), int(cd.get("tracks") or 0))
        except MusicBrainzBusy as error:
            status, message = "busy", MUSICBRAINZ_BUSY_DURING_RIP.format(status=error.status)
        except MusicBrainzUnavailable as error:
            status = "error"
            message = f"{error}. Use Find the album below, or rip the CD again later."
        else:
            latest = (self.database.get_job(job_id) or {}).get("metadata") or {}
            if isinstance(latest.get("album"), dict):
                # Chosen in the dashboard while MusicBrainz answered.
                return
            if len(releases) == 1:
                album = releases[0].summary()
                self._set_album(job_id, album, "found")
                await asyncio.to_thread(self._append_log, job_id, f"MusicBrainz: this CD is {describe_album(album)}")
                return
            if releases:
                status = "several"
                message = (
                    f"MusicBrainz knows {len(releases)} releases of this CD. Choose the one you have; "
                    "the CD keeps ripping."
                )
                self._merge_job_metadata(
                    job_id,
                    {
                        "album_candidates": [release.summary() for release in releases],
                        "album_lookup": {"status": status, "message": message},
                    },
                )
                await asyncio.to_thread(self._append_log, job_id, message)
                return
            status = "not_found"
            message = (
                "MusicBrainz does not know this CD. Use Find the album below to search by barcode or to enter "
                'the album with OCR; otherwise the tracks keep names like "01 - Unknown track".'
            )
        self._merge_job_metadata(job_id, {"album_lookup": {"status": status, "message": message}})
        await asyncio.to_thread(self._append_log, job_id, message)

    async def _wait_for_album(self, job_id: str, reason: str) -> None:
        """Keep a ripped CD in staging instead of finishing it without its album; the dashboard finishes it."""
        detail = f"{reason}. The tracks wait in staging: find or enter the album, then finish the CD."
        self.database.update_job(
            job_id,
            state=JobState.AWAITING_ALBUM,
            stage="awaiting_album",
            status_detail=detail,
            progress=100,
            process_pid=None,
            recoverable=0,
        )
        await asyncio.to_thread(self._append_log, job_id, detail)
        title = (self.database.get_job(job_id) or {}).get("title") or "The CD"
        await self.notifications.send(
            job_id,
            "attention",
            "A CD is waiting for its album",
            f"{title} is ripped. Find or enter its album in DiscDock to finish it.",
        )

    async def _tag_audio_cd(self, job_id: str, settings: AppSettings, staging: Path) -> bool:
        """Name and tag the ripped tracks after the album found or chosen on MusicBrainz, or entered by hand.

        The rip never waits for MusicBrainz. Returns False when the tracks wait in staging for their album
        instead: when that was asked for, or when MusicBrainz is busy or unreachable at the end. An album
        entered by hand needs nothing from MusicBrainz, so it always finishes. Otherwise a CD whose album
        stays unknown keeps cyanrip's track names, and the job says why.
        """
        await asyncio.to_thread(flatten_rip_folder, staging)
        metadata = (self.database.get_job(job_id) or {}).get("metadata") or {}
        cd = metadata.get("cd") if isinstance(metadata.get("cd"), dict) else {}
        lookup = metadata.get("album_lookup") if isinstance(metadata.get("album_lookup"), dict) else {}
        if (
            cd.get("discid")
            and not isinstance(metadata.get("album"), dict)
            and not metadata.get("album_candidates")
            and lookup.get("status") != "not_found"
        ):
            # For example MusicBrainz was busy while the CD ripped; it has had minutes to calm down since.
            await self._look_up_album(job_id)
            metadata = (self.database.get_job(job_id) or {}).get("metadata") or {}
        album = metadata.get("album")
        candidates = [candidate for candidate in metadata.get("album_candidates") or [] if isinstance(candidate, dict)]
        if not isinstance(album, dict):
            lookup = metadata.get("album_lookup") if isinstance(metadata.get("album_lookup"), dict) else {}
            if lookup.get("status") in {"busy", "error"}:
                await self._wait_for_album(job_id, "MusicBrainz could not be asked which album this CD is")
                return False
            if metadata.get("album_hold"):
                await self._wait_for_album(
                    job_id, "No release of this CD is chosen yet" if candidates else "The album of this CD is not found yet"
                )
                return False
        if not isinstance(album, dict) and candidates:
            album = {**candidates[0], "picked_first_of": len(candidates)}
            self._set_album(job_id, album, "picked_first")
            await asyncio.to_thread(
                self._append_log,
                job_id,
                f"No release was chosen, so the first of {len(candidates)} is used: {describe_album(album)}",
            )
        manual = isinstance(album, dict) and album.get("source") == "manual"
        if not isinstance(album, dict) or not (album.get("id") or manual):
            return True
        self.database.update_job(
            job_id, state=JobState.FINALIZING, stage="finalizing", status_detail="Naming and tagging the tracks"
        )
        await asyncio.to_thread(self._append_log, job_id, f"Naming and tagging the tracks as {describe_album(album)}")
        client = self._musicbrainz()
        cover_photo = metadata.get("album_cover") if isinstance(metadata.get("album_cover"), dict) else {}
        photo_file = Path(str(cover_photo["file"])) if cover_photo.get("file") else None
        case_photos = [
            Path(str(photo["file"]))
            for key, _, _ in ALBUM_PHOTOS.values()
            if isinstance(photo := metadata.get(key), dict) and photo.get("file")
        ]
        try:
            if manual:
                release = manual_release(album)
            else:
                release = await client.release(
                    str(album["id"]), str(cd.get("discid") or ""), int(cd.get("tracks") or 0)
                )
            # A photo taken in the dashboard is the cover the user chose; otherwise the Cover Art Archive's.
            if photo_file is not None and photo_file.is_file():
                cover = await asyncio.to_thread(photo_file.read_bytes)
            else:
                cover = None if manual else await client.front_cover(release.id)
            tagged = await tag_album(self.runner, job_id, settings.ffmpeg_path, staging, release, cover)
        except MusicBrainzBusy as error:
            await self._wait_for_album(
                job_id, f"MusicBrainz responded with {error.status} when the album tags were to be written"
            )
            return False
        except MusicBrainzUnavailable as error:
            await self._wait_for_album(job_id, f"The album tags could not be fetched from MusicBrainz ({error})")
            return False
        except (ProcessFailure, OSError, ValueError) as error:
            note = f'The album tags could not be written ({error}); the tracks keep names like "01 - Unknown track".'
        else:
            named = {**release.summary(), "tagged_tracks": tagged}
            if manual:
                named["source"] = "manual"
            if album.get("picked_first_of"):
                named["picked_first_of"] = album["picked_first_of"]
            self._set_album(job_id, named, "tagged")
            # The cover is in the tracks and the album folder now, so the photos of the case are not needed.
            for photo in case_photos:
                photo.unlink(missing_ok=True)
            await asyncio.to_thread(
                self._append_log, job_id, f"Named and tagged {tagged} tracks as {describe_album(named)}"
            )
            return True
        self._merge_job_metadata(job_id, {"album_lookup": {"status": "untagged", "message": note}})
        await asyncio.to_thread(self._append_log, job_id, note)
        return True

    def _album_job(self, job_id: str) -> dict[str, Any]:
        """The job of a CD whose album can still be chosen, because its tracks are not named yet."""
        job = self.database.get_job(job_id)
        if not job or job.get("disc_type") != DiscKind.AUDIO_CD:
            raise RuntimeError("An album can only be chosen for an audio CD")
        if job["state"] not in ALBUM_CHOICE_STATES:
            raise RuntimeError("The tracks of this CD are already named. Rip it again to use another album.")
        return job

    async def choose_album(self, job_id: str, album: dict[str, Any]) -> dict[str, Any]:
        """Use a release found on MusicBrainz for a CD being ripped; its tags are written when the rip is done."""
        job = self._album_job(job_id)
        self._set_album(job_id, album, "chosen")
        await asyncio.to_thread(self._append_log, job_id, f"Album chosen in the dashboard: {describe_album(album)}")
        return self.database.get_job(job_id) or job

    async def set_manual_album(
        self, job_id: str, artist: str, title: str, year: str, tracks: list[str]
    ) -> dict[str, Any]:
        """Use an album typed in, with track names read from a photo of the case, for a CD MusicBrainz does not know."""
        job = self._album_job(job_id)
        cd = (job.get("metadata") or {}).get("cd") or {}
        track_count = int(cd.get("tracks") or 0) or len(tracks)
        names = [" ".join(str(name).split())[:500] for name in tracks[:track_count]]
        names += [""] * (track_count - len(names))
        album = {
            "id": "",
            "source": "manual",
            "title": " ".join(title.split()),
            "artist": " ".join(artist.split()),
            "date": year,
            "track_count": track_count,
            "tracks": [{"position": index + 1, "title": name} for index, name in enumerate(names)],
        }
        self._set_album(job_id, album, "manual")
        named = sum(1 for name in names if name)
        await asyncio.to_thread(
            self._append_log, job_id, f"Album entered in the dashboard: {describe_album(album)}, {named} track names"
        )
        if job["state"] == JobState.AWAITING_ALBUM:
            # An album entered by hand needs nothing from MusicBrainz, so the waiting CD finishes now.
            return await self.finish_waiting_cd(job_id)
        return self.database.get_job(job_id) or job

    async def keep_cd_in_staging(self, job_id: str, keep: bool) -> dict[str, Any]:
        """Let a CD wait in staging for its album when the rip is done, instead of finishing without it."""
        job = self._album_job(job_id)
        if job["state"] == JobState.AWAITING_ALBUM:
            raise RuntimeError("This CD is already waiting in staging for its album")
        self._merge_job_metadata(job_id, {"album_hold": keep})
        note = (
            "When the rip is done, the CD waits in staging until its album is found or entered"
            if keep
            else "When the rip is done, the CD finishes with or without its album"
        )
        await asyncio.to_thread(self._append_log, job_id, note)
        return self.database.get_job(job_id) or job

    async def finish_waiting_cd(self, job_id: str, *, without_album: bool = False) -> dict[str, Any]:
        """Finish a CD that waits in staging: name and tag its tracks, then move them into the library."""
        job = self.database.get_job(job_id)
        if not job or job.get("disc_type") != DiscKind.AUDIO_CD or job["state"] != JobState.AWAITING_ALBUM:
            raise RuntimeError("This CD is not waiting for its album")
        if not without_album and not isinstance((job.get("metadata") or {}).get("album"), dict):
            raise RuntimeError("Choose or enter the album first, or finish the CD without it")
        self._merge_job_metadata(job_id, {"album_hold": False})
        self.database.update_job(
            job_id, state=JobState.FINALIZING, stage="finalizing", status_detail="Finishing the CD", progress=99
        )
        task = asyncio.create_task(self._finish_waiting_cd(job_id, without_album), name=f"finish-cd-{job_id}")
        self._track_job_task(job_id, str(job.get("drive_id") or ""), task)
        return self.database.get_job(job_id) or job

    async def _finish_waiting_cd(self, job_id: str, without_album: bool) -> None:
        job = self.database.get_job(job_id)
        if not job:
            return
        settings = self._settings_for_job(job)
        staging = Path(job["staging_path"])
        try:
            if without_album:
                await asyncio.to_thread(
                    self._append_log,
                    job_id,
                    'Finishing the CD without its album; the tracks keep names like "01 - Unknown track"',
                )
            elif not await self._tag_audio_cd(job_id, settings, staging):
                return
            await self._finish_in_library(job_id, settings, staging, staging)
        except asyncio.CancelledError:
            self._mark_cancelled(job_id)
            raise
        except Exception as error:
            await self._fail(job_id, "cd_finish_failed", str(error), keep_staging=True)

    async def set_album_photo(self, job_id: str, side: str, data: bytes) -> dict[str, Any]:
        """Keep a photo of the case. The front is embedded as the cover when the tracks are tagged."""
        job = self._album_job(job_id)
        key, name, what = ALBUM_PHOTOS[side]
        if data.startswith(b"\xff\xd8\xff"):
            extension = ".jpg"
        elif data.startswith(b"\x89PNG\r\n\x1a\n"):
            extension = ".png"
        else:
            raise ValueError("The photo must be a JPEG or PNG picture")
        if len(data) > MAX_COVER_BYTES:
            raise ValueError("The photo is larger than 10 MB")
        raw = self._settings_for_job(job).resolved_directories()["raw"]
        path = raw / f"{job_id}.{name}{extension}"
        await asyncio.to_thread(raw.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, data)
        for other in {".jpg", ".png"} - {extension}:
            (raw / f"{job_id}.{name}{other}").unlink(missing_ok=True)
        self._merge_job_metadata(job_id, {key: {"file": str(path), "added_at": utc_now()}})
        await asyncio.to_thread(self._append_log, job_id, f"{what} added in the dashboard")
        return self.database.get_job(job_id) or job

    async def remove_album_photo(self, job_id: str, side: str) -> dict[str, Any]:
        """Forget a photo of the case, and with the cover the photo it was edited from.

        Without a front photo, a MusicBrainz album gets its usual cover again."""
        job = self._album_job(job_id)
        metadata = dict(job.get("metadata") or {})
        for name in (side, "front_original") if side == "front" else (side,):
            path = album_photo_path(job, name)
            if path:
                await asyncio.to_thread(path.unlink, missing_ok=True)
            metadata.pop(ALBUM_PHOTOS[name][0], None)
        self.database.update_job(job_id, metadata_json=json.dumps(metadata, ensure_ascii=False))
        await asyncio.to_thread(self._append_log, job_id, f"{ALBUM_PHOTOS[side][2]} removed in the dashboard")
        return self.database.get_job(job_id) or job

    @staticmethod
    def _cds_first(releases: list[AlbumRelease], track_count: int) -> list[dict[str, Any]]:
        # The sort keeps MusicBrainz's own order within each group.
        releases.sort(
            key=lambda release: ("CD" not in release.format, bool(track_count) and release.track_count != track_count)
        )
        return [release.summary() for release in releases]

    async def search_albums(self, text: str, track_count: int = 0) -> list[dict[str, Any]]:
        """MusicBrainz releases for a search; CDs with the disc's number of tracks come first."""
        return self._cds_first(await self._musicbrainz().search(text), track_count)

    async def search_albums_by_barcode(self, barcode: str, track_count: int = 0) -> list[dict[str, Any]]:
        """MusicBrainz releases with the barcode from the back of the case; CDs with the disc's tracks first."""
        return self._cds_first(await self._musicbrainz().search_barcode(barcode), track_count)

    async def continue_job(
        self, job_id: str, selected_titles: list[int] | None = None, musicbrainz_release: str | None = None
    ) -> dict[str, Any]:
        job = self.database.get_job(job_id)
        if not job or job["state"] != JobState.AWAITING_INPUT:
            raise RuntimeError("This job is not waiting for title selection")
        metadata = dict(job.get("metadata") or {})
        releases = metadata.get("musicbrainz_releases")
        choosing_release = isinstance(releases, list) and bool(releases)
        if choosing_release:
            # A CD that waited for this choice in DiscDock 1.7.9 continues with the release as its album.
            offered = {str(release.get("id")): release for release in releases if isinstance(release, dict)}
            if musicbrainz_release != WITHOUT_ALBUM and musicbrainz_release not in offered:
                raise RuntimeError("Choose one of the releases MusicBrainz lists for this CD")
            metadata.pop("musicbrainz_releases", None)
            if musicbrainz_release in offered:
                chosen = offered[str(musicbrainz_release)]
                metadata["album"] = {"id": musicbrainz_release, "title": str(chosen.get("title") or "")}
        if selected_titles is not None:
            if not selected_titles:
                raise RuntimeError("Select at least one title to continue")
            tracks = self.database.list_tracks(job_id)
            selected = set(selected_titles)
            available = {track["source_id"] for track in tracks}
            if not selected <= available:
                raise RuntimeError("One or more selected titles are not on this disc")
            self.database.replace_tracks(
                job_id,
                [
                    {**track, "id": track["source_id"], "selected": track["source_id"] in selected}
                    for track in tracks
                ],
            )
        drive = self._drive_for_job(job)
        if not drive or not drive.media_loaded:
            raise RuntimeError("Reinsert the original disc before continuing")
        task = asyncio.create_task(self._resume_job(job_id, drive), name=f"resume-{job_id}")
        self._track_job_task(job_id, drive.id, task)
        changes = {"metadata_json": json.dumps(metadata, ensure_ascii=False)} if choosing_release else {}
        return (
            self.database.update_job(
                job_id, state=JobState.QUEUED, stage="queued", status_detail="Queued to rip", **changes
            )
            or job
        )

    async def _resume_job(self, job_id: str, drive: DriveInfo) -> None:
        async with self._drive_locks[drive.id]:
            try:
                job = self.database.get_job(job_id)
                if not job:
                    return
                settings = self._settings_for_job(job)
                # A disc MakeMKV cannot open was identified from its rescued image instead.
                if drive.disc_kind in {DiscKind.BLURAY, DiscKind.DVD, DiscKind.UNKNOWN} and not (
                    job.get("metadata") or {}
                ).get("titles_from_rescue"):
                    self.database.update_job(
                        job_id,
                        state=JobState.INSPECTING,
                        stage="confirming_disc",
                        status_detail="Confirming the original disc",
                    )
                    scan = await self._make_mkv(settings).inspect(
                        job_id,
                        drive.letter,
                        settings.min_length_seconds,
                        settings.max_length_seconds,
                        settings.inspect_timeout_seconds,
                        lambda event: self._process_event(job_id, event),
                    )
                    if self._disc_fingerprint(drive, scan, job_id) != job["fingerprint"]:
                        raise RuntimeError(
                            "This is not the same disc that was inspected. Reinsert the original disc."
                        )
                latest = self.database.get_job(job_id) or job
                await self._rip_and_finish(
                    job_id,
                    drive,
                    settings,
                    best_effort=(
                        (latest.get("metadata") or {}).get("requested_rip_method")
                        == "sector_rescue"
                    ),
                )
            except asyncio.CancelledError:
                self._mark_cancelled(job_id)
                raise
            except MakeMKVLicenseError as error:
                await self._fail(job_id, "makemkv_license", str(error), blocked=True)
            except ProcessFailure as error:
                latest = self.database.get_job(job_id) or job
                if (latest.get("metadata") or {}).get("ai_repair_requested"):
                    try:
                        await self._start_ai_repair_preparation(job_id, drive, settings)
                    except asyncio.CancelledError:
                        self._mark_cancelled(job_id)
                        raise
                    except Exception as repair_error:
                        await self._fail(
                            job_id, "ai_repair_preparation_failed", str(repair_error), keep_staging=True
                        )
                    return
                if (latest.get("metadata") or {}).get("recovery_requested"):
                    try:
                        await self._start_best_effort_recovery(job_id, drive, settings)
                    except asyncio.CancelledError:
                        self._mark_cancelled(job_id)
                        raise
                    except Exception as recovery_error:
                        await self._fail(
                            job_id, "damaged_disc_recovery_failed", str(recovery_error)
                        )
                    return
                await self._fail(job_id, "resume_failed", str(error))
            except Exception as error:
                await self._fail(job_id, "resume_failed", str(error))

    async def cancel_job(self, job_id: str) -> dict[str, Any]:
        job = self.database.get_job(job_id)
        if not job:
            raise LookupError("Job not found")
        if job["state"] in {JobState.COMPLETED, JobState.CANCELLED, JobState.FAILED, JobState.INTERRUPTED}:
            raise RuntimeError("This job is no longer running")
        self.database.update_job(
            job_id,
            state=JobState.CANCELLING,
            stage="cancelling",
            cancel_requested=1,
            status_detail="Stopping safely",
        )
        await self.runner.cancel(job_id)
        # FFprobe scans of the damage analysis run outside the process runner.
        cancel_captures(job_id)
        task = self._tasks.get(job_id)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        else:
            self._mark_cancelled(job_id)
        return self.database.get_job(job_id) or job

    async def retry_job(self, job_id: str) -> dict[str, Any]:
        job = self.database.get_job(job_id)
        if job and job["state"] == JobState.AWAITING_REPAIR:
            # The disc is back: read the spots the reviewed copy is missing, then
            # analyze the better copy instead of deciding on the damaged one.
            settings = self._settings_for_job(job)
            if not self._rescue_has_retry_work(self._rescue_image(job_id, settings), settings):
                raise RuntimeError("Everything readable was already tried; keep the movie or approve AI frames")
            if not self._disc_can_improve(job, settings):
                raise RuntimeError("Put the damaged disc back in the drive first")
            self.database.update_job(
                job_id,
                state=JobState.CANCELLED,
                status_detail="Reading the skipped spots again now that the disc is back",
            )
            return await self.prepare_ai_repair_job(job_id)
        if not job or job["state"] not in {
            JobState.FAILED,
            JobState.INTERRUPTED,
            JobState.BLOCKED,
            JobState.CANCELLED,
        }:
            raise RuntimeError("This job cannot be retried")
        metadata = dict(job.get("metadata") or {})
        route = metadata.get("recovery_route")
        route_mode = str(route.get("mode") or "") if isinstance(route, dict) else ""
        if job.get("error_code") in {"ai_repair_not_possible", "ai_repair_preparation_failed"}:
            # Older releases ended the job when the damage was too long for AI;
            # the analysis now always ends in a review that can keep the movie.
            route_mode = "ai_repair"
        elif not route_mode:
            previous_recovery = metadata.get("recovery")
            if isinstance(previous_recovery, dict) and previous_recovery.get("mode") in {
                "vlc_best_effort",
                "sector_best_effort",
            }:
                route_mode = str(previous_recovery["mode"])

        if (
            route_mode == "ai_repair"
            and metadata.get("titles_from_rescue")
            and not self.database.list_tracks(job_id)
        ):
            # AI frames need a movie to put them in, and MakeMKV never listed a title for
            # this disc. Read it again from its file system instead of failing every retry.
            route_mode = "sector_best_effort"
        # A failed recovery attempt must retry the chosen recovery engine. A
        # generic MakeMKV retry would hit the same unreadable sector and stall
        # again, which is both surprising and wasteful. Those routes check for
        # the disc themselves, because a saved rescue image can replace it.
        if route_mode in {"vlc_best_effort", "sector_best_effort", "ai_repair"}:
            metadata.pop("recovery_requested", None)
            metadata.pop("ai_repair_requested", None)
            if route_mode == "vlc_best_effort":
                metadata.pop("recovery", None)
                route_mode = "sector_best_effort"
            metadata["recovery_route"] = {
                "mode": route_mode,
                "status": "retrying",
                "requested_at": utc_now(),
            }
            self.database.update_job(
                job_id,
                metadata_json=json.dumps(metadata, ensure_ascii=False),
            )
            if route_mode == "sector_best_effort":
                return await self.recover_damaged_job(job_id)
            return await self.prepare_ai_repair_job(job_id)

        drive = self._drive_for_job(job)
        if not drive or not drive.media_loaded:
            raise RuntimeError("Reinsert the disc before retrying")
        old_staging = Path(job["staging_path"])
        if old_staging.exists():
            abandoned = old_staging.with_name(f"{old_staging.name}.abandoned-{int(time.time())}")
            os.replace(old_staging, abandoned)
        fresh_staging = (
            self.settings.resolved_directories()["raw"] / f"{job_id}-{uuid.uuid4().hex[:8]}.partial"
        )
        if job.get("error_code") == "duplicate":
            metadata["duplicate_override"] = True
        metadata.pop("short_titles_only", None)
        # The disc is scanned again, so the retry uses the settings as they are now, for example a
        # minimum title length lowered after the first attempt found no titles.
        job_settings = {**self.settings.public_dict(), "manual": bool((job.get("settings") or {}).get("manual"))}
        task = asyncio.create_task(self._process_job(job_id, manual=False), name=f"retry-{job_id}")
        self._track_job_task(job_id, drive.id, task)
        return (
            self.database.update_job(
                job_id,
                state=JobState.DETECTED,
                stage="retrying",
                progress=0,
                status_detail="Retrying",
                staging_path=str(fresh_staging),
                metadata_json=json.dumps(metadata),
                settings_json=json.dumps(job_settings, ensure_ascii=False),
                cancel_requested=0,
                error_code=None,
                error_message=None,
            )
            or job
        )

    def reload_settings(self, values: dict[str, Any]) -> AppSettings:
        current = self.settings.model_dump(mode="python")
        current.update(values)
        updated = AppSettings.model_validate(current)
        self.settings_store.save(updated)
        self.settings = updated
        self.monitor.poll_interval = updated.poll_interval_seconds
        return updated
