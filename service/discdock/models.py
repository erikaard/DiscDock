from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class DiscKind(StrEnum):
    BLURAY = "bluray"
    DVD = "dvd"
    AUDIO_CD = "audio_cd"
    DATA = "data"
    UNKNOWN = "unknown"


class MediaKind(StrEnum):
    MOVIE = "movie"
    SERIES = "series"
    MUSIC = "music"
    # Anything that is not a film or an album: a game, a program, or files to keep.
    OTHER = "other"
    # Discs backed up before DiscDock 1.10 were filed as data and stay there.
    DATA = "data"
    UNKNOWN = "unknown"


class JobState(StrEnum):
    DETECTED = "detected"
    INSPECTING = "inspecting"
    IDENTIFYING = "identifying"
    AWAITING_INPUT = "awaiting_input"
    AWAITING_REPAIR = "awaiting_repair"
    # A ripped CD whose tracks wait in staging for their album; it does not hold the drive.
    AWAITING_ALBUM = "awaiting_album"
    QUEUED = "queued"
    RIPPING = "ripping"
    RIPPED = "ripped"
    VERIFYING = "verifying"
    TRANSCODING = "transcoding"
    FINALIZING = "finalizing"
    EJECTING = "ejecting"
    COMPLETED = "completed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    BLOCKED = "blocked"
    FAILED = "failed"


ACTIVE_JOB_STATES = {
    JobState.DETECTED,
    JobState.INSPECTING,
    JobState.IDENTIFYING,
    JobState.AWAITING_INPUT,
    JobState.QUEUED,
    JobState.RIPPING,
    JobState.RIPPED,
    JobState.VERIFYING,
    JobState.TRANSCODING,
    JobState.FINALIZING,
    JobState.EJECTING,
    JobState.CANCELLING,
}

# DiscDock reads the disc in these states, or is about to. Another program reading it then slows the rip
# and can disturb MakeMKV's decryption, so the dashboard does not open the disc in VLC.
DISC_READING_STATES = {
    JobState.DETECTED,
    JobState.INSPECTING,
    JobState.IDENTIFYING,
    JobState.QUEUED,
    JobState.RIPPING,
    JobState.EJECTING,
    JobState.CANCELLING,
}


class DriveInfo(BaseModel):
    id: str
    letter: str
    name: str
    pnp_device_id: str = ""
    media_loaded: bool = False
    volume_label: str = ""
    disc_kind: DiscKind = DiscKind.UNKNOWN
    state: str = "ready"
    make_mkv_index: int | None = None
    last_seen: str | None = None


class TitleInfo(BaseModel):
    id: int
    disc_title_number: int = 0
    name: str = ""
    duration_seconds: int = 0
    size_bytes: int = 0
    chapters: int = 0
    filename: str = ""
    angle: int = 0
    source_group: str = ""
    segment_map: str = ""
    # The disc file MakeMKV built the title from, for example "00800.mpls" on a Blu-ray.
    source_filename: str = ""
    description: str = ""
    selected: bool = True
    streams: list[dict] = Field(default_factory=list)


class MetadataCandidate(BaseModel):
    provider: str
    provider_id: str
    title: str
    year: str = ""
    media_kind: MediaKind = MediaKind.UNKNOWN
    poster_url: str = ""
    plot: str = ""
    runtime_minutes: int = 0
    user_selected: bool = False


class ScanRequest(BaseModel):
    manual: bool = False
    media_kind: MediaKind | None = None
    rip_method: Literal["normal", "sector_rescue"] = "normal"


class JobPatch(BaseModel):
    title: str | None = None
    year: str | None = None
    media_kind: MediaKind | None = None
    selected_titles: list[int] | None = None
    main_feature: bool | None = None
    metadata: MetadataCandidate | None = None


class AlbumChoice(BaseModel):
    """A release from a MusicBrainz lookup or search, as the dashboard shows it."""

    id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    title: str = Field(min_length=1, max_length=500)
    artist: str = Field("", max_length=500)
    date: str = Field("", max_length=20)
    country: str = Field("", max_length=20)
    label: str = Field("", max_length=500)
    disambiguation: str = Field("", max_length=500)
    format: str = Field("", max_length=100)
    track_count: int = Field(0, ge=0, le=999)
    artist_id: str = Field("", max_length=64)
    disc_number: int = Field(1, ge=1, le=999)
    disc_count: int = Field(1, ge=1, le=999)


class ManualAlbum(BaseModel):
    """An album typed in, with track names read from a photo of the case, for a CD MusicBrainz does not know."""

    artist: str = Field(min_length=1, max_length=500)
    title: str = Field(min_length=1, max_length=500)
    year: str = Field("", pattern=r"^(\d{4})?$")
    tracks: list[str] = Field(default_factory=list, max_length=99)


class CdStaging(BaseModel):
    """Whether a CD waits in staging for its album when the rip is done, instead of finishing without it."""

    keep: bool


class CdFinish(BaseModel):
    """Finish a CD waiting in staging, with the album chosen or entered for it, or without an album."""

    without_album: bool = False


class ContinueRequest(BaseModel):
    selected_titles: list[int] | None = None
    # An audio CD's release from job.metadata.musicbrainz_releases, or "none" to rip it without track names.
    musicbrainz_release: str | None = None


class SettingsPatch(BaseModel):
    values: dict = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)


class AiRepairApplyRequest(BaseModel):
    estimate_id: str = Field(min_length=8, max_length=80)
    accepted_max_cost_usd: float = Field(gt=0, le=100)
