from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _first_existing(*candidates: str | Path | None) -> str:
    for candidate in candidates:
        if not candidate:
            continue
        resolved = shutil.which(str(candidate)) or str(candidate)
        if Path(resolved).is_file():
            return str(Path(resolved).resolve())
    return ""


def _winget_portable(package_prefix: str, executable: str) -> str:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return ""
    packages = Path(local_app_data) / "Microsoft" / "WinGet" / "Packages"
    try:
        matches = sorted(
            packages.glob(f"{package_prefix}_*/{executable}"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return ""
    return str(matches[0].resolve()) if matches else ""


def _ffmpeg_tool(executable: str) -> str:
    """FFmpeg from PATH, or from a winget package that is not on this process's PATH yet."""
    return _first_existing(
        executable,
        *(_winget_portable(package, f"*/bin/{executable}") for package in ("Gyan.FFmpeg", "BtbN.FFmpeg", "yt-dlp.FFmpeg")),
    )


def default_data_root() -> Path:
    configured = os.environ.get("DISCDOCK_DATA_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / "DiscDock").resolve()


class AppSettings(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    version: int = 1
    host: str = "127.0.0.1"
    port: int = 8199
    machine_name: str = "DiscDock"
    data_root: Path = Field(default_factory=default_data_root)
    raw_directory: Path | None = None
    completed_directory: Path | None = None
    failed_directory: Path | None = None
    music_directory: Path | None = None
    auto_rip: bool = True
    auto_eject: bool = True
    prevent_sleep: bool = True
    skip_transcode: bool = True
    keep_raw_after_transcode: bool = True
    main_feature: bool = True
    extras: bool = True
    min_length_seconds: int = 600
    max_length_seconds: int = 99999
    duplicate_policy: str = "ask"
    metadata_provider: str = "omdb"
    omdb_enabled: bool = True
    tmdb_enabled: bool = False
    ai_repair_enabled: bool = True
    ai_repair_model: str = "gpt-image-2.5-sunburst"
    ai_repair_quality: str = "low"
    ai_repair_keyframes_per_second: float = 2.0
    ai_repair_cost_limit_usd: float = 10.0
    rescue_extra_minutes: int = 30
    # "ask" shows the recovery choices; "best_effort" switches a DVD to the
    # damaged-disc rescue as soon as MakeMKV reports an unreadable block.
    damaged_disc_action: str = "ask"
    # Damage of 2 seconds or more after a best-effort recovery: "ask" finishes the movie as it
    # was read and lets the user keep it or add loading screens; "loading_screen" always shows an
    # animated screen with the resume time instead of a frozen picture; "none" never asks.
    damage_placeholder: str = "ask"
    # Audio-CD read offset in samples, the number EAC and the AccurateRip drive list use. cyanrip
    # has no list of drives and will not rip on a drive that reads ISRC codes until it is given one.
    cd_read_offset: int = 0
    notifications_enabled: bool = True
    notification_events: list[str] = Field(default_factory=lambda: ["completed", "failed", "attention"])
    make_mkv_path: str = Field(
        default_factory=lambda: _first_existing(
            r"C:\Program Files (x86)\MakeMKV\makemkvcon64.exe",
            r"C:\Program Files\MakeMKV\makemkvcon64.exe",
            "makemkvcon64.exe",
        )
    )
    handbrake_path: str = Field(
        default_factory=lambda: _first_existing(
            r"C:\Program Files\HandBrake\HandBrakeCLI.exe",
            _winget_portable("HandBrake.HandBrake.CLI", "HandBrakeCLI.exe"),
            "HandBrakeCLI.exe",
        )
    )
    ffmpeg_path: str = Field(default_factory=lambda: _ffmpeg_tool("ffmpeg.exe"))
    ffprobe_path: str = Field(default_factory=lambda: _ffmpeg_tool("ffprobe.exe"))
    vlc_path: str = Field(
        default_factory=lambda: _first_existing(r"C:\Program Files\VideoLAN\VLC\vlc.exe", "vlc.exe")
    )
    cyanrip_path: str = Field(
        default_factory=lambda: _first_existing(
            _winget_portable("cyanreg.cyanrip", "cyanrip.exe"), "cyanrip.exe"
        )
    )
    rip_mode: str = "mkv"
    handbrake_preset_dvd: str = "Fast 1080p30"
    handbrake_preset_bluray: str = "Fast 1080p30"
    max_concurrent_transcodes: int = 1
    poll_interval_seconds: float = 3.0
    process_no_output_timeout_seconds: int = 180
    inspect_timeout_seconds: int = 900
    rip_timeout_seconds: int = 43200
    local_ui_origin: str = "http://localhost:5173"

    @field_validator("host")
    @classmethod
    def localhost_only(cls, value: str) -> str:
        if value not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("DiscDock binds to localhost only")
        return value

    @field_validator("duplicate_policy")
    @classmethod
    def valid_duplicate_policy(cls, value: str) -> str:
        if value not in {"ask", "skip", "replace", "keep_both"}:
            raise ValueError("Duplicate handling must be ask, skip, replace, or keep_both")
        return value

    @field_validator("metadata_provider")
    @classmethod
    def valid_metadata_provider(cls, value: str) -> str:
        if value not in {"omdb", "tmdb", "none"}:
            raise ValueError("Metadata provider must be omdb, tmdb, or none")
        return value

    @field_validator("rip_mode")
    @classmethod
    def valid_rip_mode(cls, value: str) -> str:
        if value not in {"mkv", "backup"}:
            raise ValueError("Rip mode must be mkv or backup")
        return value

    @field_validator("ai_repair_model")
    @classmethod
    def valid_ai_repair_model(cls, value: str) -> str:
        if value not in {"gpt-image-2.5-sunburst", "gpt-image-2.5-flare"}:
            raise ValueError("AI repair model must be GPT Image 2.5 Sunburst or Flare")
        return value

    @field_validator("ai_repair_quality")
    @classmethod
    def valid_ai_repair_quality(cls, value: str) -> str:
        if value not in {"low", "medium"}:
            raise ValueError("AI repair quality must be low or medium")
        return value

    @field_validator("ai_repair_keyframes_per_second")
    @classmethod
    def valid_ai_keyframe_rate(cls, value: float) -> float:
        if not 0.5 <= value <= 4:
            raise ValueError("AI repair keyframes per second must be between 0.5 and 4")
        return value

    @field_validator("ai_repair_cost_limit_usd")
    @classmethod
    def valid_ai_cost_limit(cls, value: float) -> float:
        if not 0.25 <= value <= 100:
            raise ValueError("AI repair cost limit must be between $0.25 and $100")
        return value

    @field_validator("rescue_extra_minutes")
    @classmethod
    def valid_rescue_extra_minutes(cls, value: int) -> int:
        if not 0 <= value <= 1440:
            raise ValueError("Extra time for damaged areas must be between 0 minutes and 24 hours")
        return value

    @field_validator("damaged_disc_action")
    @classmethod
    def valid_damaged_disc_action(cls, value: str) -> str:
        if value not in {"ask", "best_effort"}:
            raise ValueError("Damaged-disc handling must be ask or best_effort")
        return value

    @field_validator("damage_placeholder")
    @classmethod
    def valid_damage_placeholder(cls, value: str) -> str:
        if value not in {"ask", "loading_screen", "none"}:
            raise ValueError("Damaged moments must ask first, show a loading screen or stay as they are")
        return value

    @field_validator("cd_read_offset")
    @classmethod
    def valid_cd_read_offset(cls, value: int) -> int:
        if not -5000 <= value <= 5000:
            raise ValueError("The CD drive read offset must be between -5000 and 5000 samples")
        return value

    @field_validator("poll_interval_seconds")
    @classmethod
    def valid_poll_interval(cls, value: float) -> float:
        if not 1 <= value <= 300:
            raise ValueError("Drive polling interval must be between 1 and 300 seconds")
        return value

    @field_validator("process_no_output_timeout_seconds", "inspect_timeout_seconds", "rip_timeout_seconds")
    @classmethod
    def valid_timeout(cls, value: int) -> int:
        if value < 30:
            raise ValueError("Media-tool timeouts must be at least 30 seconds")
        return value

    @model_validator(mode="after")
    def coherent_options(self) -> AppSettings:
        if self.min_length_seconds < 0 or self.max_length_seconds <= self.min_length_seconds:
            raise ValueError("Maximum title length must be greater than minimum title length")
        if self.rip_mode == "backup" and not self.skip_transcode:
            raise ValueError("Full-disc backup mode cannot be combined with HandBrake conversion")
        if self.max_concurrent_transcodes < 1 or self.max_concurrent_transcodes > 8:
            raise ValueError("Concurrent transcodes must be between 1 and 8")
        return self

    def resolved_directories(self) -> dict[str, Path]:
        root = self.data_root.expanduser().resolve()
        return {
            "root": root,
            "raw": (self.raw_directory or root / "raw").expanduser().resolve(),
            "completed": (self.completed_directory or root / "completed").expanduser().resolve(),
            "failed": (self.failed_directory or root / "failed").expanduser().resolve(),
            "music": (self.music_directory or root / "music").expanduser().resolve(),
            "logs": root / "logs",
            "database": root / "database",
        }

    def ensure_directories(self) -> None:
        for directory in self.resolved_directories().values():
            directory.mkdir(parents=True, exist_ok=True)

    def discover_missing_tools(self) -> bool:
        discovered = {
            "make_mkv_path": _first_existing(
                r"C:\Program Files (x86)\MakeMKV\makemkvcon64.exe",
                r"C:\Program Files\MakeMKV\makemkvcon64.exe",
                "makemkvcon64.exe",
            ),
            "handbrake_path": _first_existing(
                r"C:\Program Files\HandBrake\HandBrakeCLI.exe",
                _winget_portable("HandBrake.HandBrake.CLI", "HandBrakeCLI.exe"),
                "HandBrakeCLI.exe",
            ),
            "ffmpeg_path": _ffmpeg_tool("ffmpeg.exe"),
            "ffprobe_path": _ffmpeg_tool("ffprobe.exe"),
            "vlc_path": _first_existing(r"C:\Program Files\VideoLAN\VLC\vlc.exe", "vlc.exe"),
            "cyanrip_path": _first_existing(
                _winget_portable("cyanreg.cyanrip", "cyanrip.exe"), "cyanrip.exe"
            ),
        }
        changed = False
        for field, value in discovered.items():
            if not getattr(self, field) and value:
                setattr(self, field, value)
                changed = True
        return changed

    def public_dict(self, secret_names: set[str] | None = None) -> dict[str, Any]:
        payload = self.model_dump(mode="json")
        payload["directories"] = {key: str(value) for key, value in self.resolved_directories().items()}
        payload["secrets"] = {name: True for name in sorted(secret_names or set())}
        return payload


class SettingsStore:
    def __init__(self, config_path: Path):
        self.config_path = config_path

    def load(self) -> AppSettings:
        if not self.config_path.exists():
            settings = AppSettings()
            settings.ensure_directories()
            self.save(settings)
            return settings
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            settings = AppSettings.model_validate(data)
        except (OSError, ValueError) as error:
            stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
            preserved = self.config_path.with_name(f"settings-{stamp}.corrupt.json")
            try:
                shutil.copy2(self.config_path, preserved)
            except OSError:
                preserved = self.config_path
            backup = self.config_path.with_suffix(".json.bak")
            try:
                data = json.loads(backup.read_text(encoding="utf-8"))
                settings = AppSettings.model_validate(data)
            except (OSError, ValueError):
                raise RuntimeError(
                    f"DiscDock settings are damaged. The original file is preserved at {preserved}"
                ) from error
        settings.ensure_directories()
        if settings.discover_missing_tools():
            self.save(settings)
        return settings

    def save(self, settings: AppSettings) -> None:
        settings.ensure_directories()
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.config_path.with_suffix(".json.new")
        temporary.write_text(
            json.dumps(settings.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        if self.config_path.exists():
            shutil.copy2(self.config_path, self.config_path.with_suffix(".json.bak"))
        os.replace(temporary, self.config_path)
