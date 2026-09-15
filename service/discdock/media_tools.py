from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from .disc_rescue import (
    EVENT_PREFIX,
    EXIT_COPY_PROTECTION,
    EXIT_DRIVE_FAULT,
    EXIT_MEDIA_UNAVAILABLE,
    EXIT_WRONG_DISC,
)
from .optical import DVD_ECC_BLOCK_SECTORS
from .processes import ProcessFailure, ProcessRunner, start_external_process

ProgressCallback = Callable[[dict], Awaitable[None] | None]


def _probe_media_duration(path: Path, ffprobe_path: str) -> float:
    if not ffprobe_path or not Path(ffprobe_path).is_file():
        return 0
    process = start_external_process(
        [
            ffprobe_path,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=0x08000000 if os.name == "nt" else 0,
    )
    try:
        stdout, _ = process.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        return 0
    try:
        return float(stdout.strip()) if process.returncode == 0 else 0
    except ValueError:
        return 0


class VlcDvdRecovery:
    """Make a best-effort DVD copy through VLC's playback-oriented reader."""

    def __init__(self, executable: str, ffprobe_path: str, runner: ProcessRunner):
        self.executable = executable
        self.ffprobe_path = ffprobe_path
        self.runner = runner

    async def recover(
        self,
        job_id: str,
        letter: str,
        destination: Path,
        title_number: int,
        chapter_count: int,
        expected_duration_seconds: int,
        estimated_bytes: int,
        timeout: int,
        callback: ProgressCallback | None = None,
        *,
        source_image: Path | None = None,
    ) -> Path:
        if not self.executable or not Path(self.executable).is_file():
            raise FileNotFoundError("VLC is required for damaged-DVD recovery")
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / "recovered.mkv"
        partial = destination / "recovered.part.mkv"
        drive = letter.rstrip(":").upper()
        if len(drive) != 1 or not drive.isalpha():
            raise ValueError("Invalid Windows drive letter")
        # Use the menu-aware reader that VLC uses for normal playback. Some
        # authored DVDs contain deliberately unreadable lead-in cells that
        # dvdsimple touches but dvdnav skips. Pin both ends of the title range
        # so VLC does not continue into fake/duplicate titles afterwards.
        title = max(1, title_number)
        last_chapter = max(1, chapter_count)
        source = source_image.resolve().as_posix() if source_image else f"{drive}:/"
        mrl = f"dvd:///{source}#{title}:1-{title}:{last_chapter}"
        output = partial.resolve().as_posix()
        args = [
            self.executable,
            "--ignore-config",
            "--no-one-instance",
            "--no-media-library",
            "--intf=dummy",
            "--dummy-quiet",
            "--play-and-exit",
            "--sout-all",
            mrl,
            f"--sout=#std{{access=file,mux=mkv,dst={output}}}",
        ]
        run_task = asyncio.create_task(
            self.runner.run(
                job_id,
                args,
                timeout=timeout,
                # The Windows VLC executable does not reliably expose console
                # output, so file growth is used for progress instead.
                no_output_timeout=timeout + 60,
            )
        )
        try:
            while not run_task.done():
                await asyncio.sleep(2)
                if callback and partial.exists() and estimated_bytes > 0:
                    percent = min(98.0, partial.stat().st_size * 100 / estimated_bytes)
                    await _invoke(
                        callback,
                        {
                            "type": "progress",
                            "percent": percent,
                            "message": "Recovering readable DVD sections with VLC",
                        },
                    )
            result = await run_task
        except BaseException:
            if not run_task.done():
                await self.runner.cancel(job_id)
                await asyncio.gather(run_task, return_exceptions=True)
            raise
        if result.return_code != 0 or result.cancelled:
            raise ProcessFailure("VLC damaged-DVD recovery failed", result)
        if not partial.exists() or partial.stat().st_size < 1024 * 1024:
            raise ProcessFailure("VLC did not produce a usable recovery file", result)
        duration = await asyncio.to_thread(_probe_media_duration, partial, self.ffprobe_path)
        minimum_duration = max(60, expected_duration_seconds * 0.8)
        if expected_duration_seconds > 0 and duration < minimum_duration:
            raise ProcessFailure(
                "VLC stopped before enough of the movie was recovered; the disc is too damaged for this pass",
                result,
            )
        os.replace(partial, target)
        return target


class DiscAuthenticationRequired(ProcessFailure):
    """The drive refused encrypted DVD sectors until a CSS handshake is repeated."""


def rescue_helper_command(arguments: list[str]) -> tuple[list[str], Path | None]:
    """Return the command that runs the rescue helper for this installation."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--disc-rescue", *arguments], None
    return [sys.executable, "-m", "discdock.disc_rescue", *arguments], Path(__file__).resolve().parents[1]


def _megabytes(value: int) -> str:
    megabytes = max(0, value) / 1024 / 1024
    return f"{megabytes:.1f} MB" if megabytes < 10 else f"{megabytes:,.0f} MB"


def _gigabytes(value: int) -> str:
    return f"{max(0, value) / 1024 / 1024 / 1024:.2f} GB"


def describe_rescue_progress(event: dict) -> str:
    phase = event.get("phase")
    unreadable = int(event.get("movie_unreadable_bytes") or event.get("unreadable_bytes") or 0)
    pending = int(event.get("movie_pending_bytes") or event.get("pending_bytes") or 0)
    if phase == "sweep":
        if event.get("scope") == "movie":
            text = f"Reading the movie · {min(100, round(float(event.get('percent') or 0) / 0.9))}%"
        else:
            text = (
                f"Reading the disc · {_gigabytes(int(event.get('position_bytes') or 0))} "
                f"of {_gigabytes(int(event.get('total_bytes') or 0))}"
            )
        deferred = int(event.get("deferred_bytes") or 0)
        if event.get("in_damaged_zone"):
            text += " · skipping past damaged spots"
        elif deferred:
            text += f" · {_megabytes(deferred)} set aside to retry"
        return text
    if phase == "structures":
        return "Reading the disc's file system and navigation data"
    if phase in {"retry", "trim", "scrape"}:
        budget_minutes = round(float(event.get("extra_budget_seconds") or 0) / 60)
        used_minutes = min(budget_minutes, round(float(event.get("extra_elapsed_seconds") or 0) / 60))
        round_number = int(event.get("retry_round") or 0)
        return (
            f"Retrying skipped spots{f' (round {round_number})' if round_number else ''} · "
            f"{_megabytes(pending)} left · {_megabytes(unreadable)} unreadable · "
            f"{used_minutes} of {budget_minutes} min · can be skipped"
        )
    return "Rescuing readable disc sectors"


def describe_rescue_event(event: dict) -> str:
    kind = event.get("type")
    if kind == "start":
        origin = {
            "resumed": "resuming the saved rescue image",
            "adopted": "continuing an image saved by an earlier DiscDock version",
        }.get(str(event.get("origin")), "starting a new rescue image")
        method = "direct SCSI reads" if event.get("method") == "scsi_pass_through" else "Windows volume reads"
        units = int(event.get("units") or 0)
        return (
            f"Damaged-disc rescue: {_gigabytes(int(event.get('total_bytes') or 0))} disc, {method}, "
            f"{int(event.get('max_transfer_sectors') or 0) * 2} KB per read, {origin}"
            + (f", skipping damage one of {units:,} video units at a time" if units else "")
        )
    if kind == "layout":
        movie = _gigabytes(int(event.get("movie_bytes") or event.get("priority_bytes") or 0))
        if event.get("verified") and event.get("playlist") is not None:
            clips = event.get("clips") or []
            return (
                f"Movie located in Blu-ray playlist {event.get('playlist')} ({len(clips)} "
                f"{'clip' if len(clips) == 1 else 'clips'}): {movie} of video; only the movie and the disc's "
                "navigation are read"
            )
        if event.get("verified"):
            return (
                f"Movie located in DVD title set {event.get('title_set')}: {movie} of video in "
                f"{int(event.get('units') or 0):,} video units; only the movie and the disc's navigation are read"
            )
        return f"Could not map the movie's sectors ({event.get('note') or 'unknown layout'}); reading the whole disc"
    if kind == "phase":
        scope = "movie" if event.get("scope") == "movie" else "disc"
        return {
            "sweep": f"Pass 1: copying the {scope} and skipping past anything unreadable",
            "retry": "Pass 2: retrying each skipped spot in turn until little more comes back",
        }.get(str(event.get("phase")), f"Rescue phase {event.get('phase')}")
    if kind == "round":
        return f"Retry round {int(event.get('round') or 0)}: {int(event.get('units') or 0):,} damaged spots to revisit"
    if kind == "zone":
        return (
            f"Unreadable spot near {_gigabytes(int(event.get('start_bytes') or 0))}; "
            "skipping to the next moment of video and retrying it later"
        )
    if kind == "waiting":
        return str(event.get("message") or "Waiting for the optical drive")
    if kind == "adopting":
        return f"Checking the saved rescue image ({float(event.get('percent') or 0):.0f}%)"
    return json.dumps(event, separators=(",", ":"))


class DvdSectorRescue:
    """Image a damaged disc, skipping what cannot be read, with a resumable map.

    The work happens in a helper process (``discdock.disc_rescue``) that reads
    the drive directly. Killing that process is always safe: the map records
    only data that was already flushed to the image.
    """

    def __init__(self, runner: ProcessRunner):
        self.runner = runner

    @staticmethod
    def artifact_paths(image: Path) -> dict[str, Path]:
        return {
            "partial": image.with_name(image.name + ".part"),
            "map": image.with_name(image.name + ".map.json"),
            "control": image.with_name(image.name + ".control"),
        }

    @classmethod
    def request_finish(cls, image: Path) -> bool:
        control = cls.artifact_paths(image)["control"]
        if not control.parent.is_dir():
            return False
        control.write_text("finish", encoding="utf-8")
        return True

    async def rip(
        self,
        job_id: str,
        letter: str,
        destination: Path,
        callback: ProgressCallback | None = None,
        *,
        extra_seconds: float = 900,
        dvd_title: int = 0,
        segment_map: str = "",
        cluster_sectors: int = DVD_ECC_BLOCK_SECTORS,
        skip_sectors: int = 256,
        timeout: int = 24 * 3600,
        disc: str = "dvd",
        playlist: str = "",
        auto_title: bool = False,
        whole_disc: bool = False,
        structures_only: bool = False,
    ) -> dict:
        """Rescue the disc into ``destination``.

        ``structures_only`` reads just the file system and navigation data and
        leaves the image as ``.part``: enough for MakeMKV to list the titles of
        a disc it could not open, but not a finished image.
        """
        drive = letter.rstrip(":").upper()
        if len(drive) != 1 or not drive.isalpha():
            raise ValueError("Invalid Windows drive letter")
        destination.parent.mkdir(parents=True, exist_ok=True)
        paths = self.artifact_paths(destination)
        partial = paths["partial"]
        paths["control"].unlink(missing_ok=True)
        if destination.is_file() and not partial.exists():
            # Continuing a finished image re-reads only what its map lists as missing.
            os.replace(destination, partial)
        arguments = [
            "--drive",
            f"{drive}:",
            "--image",
            str(partial),
            "--map",
            str(paths["map"]),
            "--control",
            str(paths["control"]),
            "--cluster",
            str(int(cluster_sectors)),
            "--extra-seconds",
            str(int(max(0, extra_seconds))),
            "--skip-sectors",
            str(int(max(cluster_sectors, skip_sectors))),
        ]
        if disc == "bluray":
            arguments.extend(["--disc", "bluray"])
            if playlist:
                arguments.extend(["--playlist", playlist])
            if segment_map:
                arguments.extend(["--segment-map", segment_map])
        elif dvd_title > 0:
            arguments.extend(["--dvd-title", str(int(dvd_title))])
            if segment_map:
                arguments.extend(["--segment-map", segment_map])
        elif auto_title:
            arguments.append("--auto-title")
        if whole_disc:
            arguments.append("--whole-disc")
        if structures_only:
            arguments.append("--structures-only")
        command, cwd = rescue_helper_command(arguments)
        summary: dict = {}
        failure: dict = {}

        async def on_line(line: str) -> None:
            if not line.startswith(EVENT_PREFIX):
                if callback and line.strip():
                    await _invoke(callback, {"type": "log", "message": line})
                return
            try:
                event = json.loads(line[len(EVENT_PREFIX) :])
            except ValueError:
                return
            kind = event.get("type")
            if kind == "progress":
                if callback:
                    await _invoke(
                        callback,
                        {
                            "type": "progress",
                            "percent": float(event.get("percent") or 0),
                            "message": describe_rescue_progress(event),
                        },
                    )
                    await _invoke(callback, {"type": "rescue_status", "status": event})
            elif kind == "done":
                summary.update(event)
                if callback:
                    await _invoke(callback, {"type": "rescue_status", "status": event, "final": True})
            elif kind == "error":
                failure.update(event)
                if callback:
                    await _invoke(callback, {"type": "log", "message": f"Rescue helper: {event.get('message')}"})
            elif callback:
                await _invoke(callback, {"type": "rescue_event", "event": event, "message": describe_rescue_event(event)})

        result = await self.runner.run(
            job_id,
            command,
            cwd=cwd,
            timeout=timeout,
            # One unreadable block can hold a drive for its whole SCSI timeout;
            # the helper still reports at least every few reads.
            no_output_timeout=900,
            on_line=on_line,
        )
        paths["control"].unlink(missing_ok=True)
        if result.return_code == EXIT_COPY_PROTECTION and not result.cancelled:
            raise DiscAuthenticationRequired(
                failure.get("message") or "The drive needs the DVD to be authenticated again", result
            )
        if result.return_code == EXIT_DRIVE_FAULT and not result.cancelled:
            raise DriveNotResponding(
                failure.get("message")
                or "The optical drive stopped responding. Reconnect the drive or reinsert the disc, then retry.",
                result,
            )
        if result.return_code == EXIT_WRONG_DISC and not result.cancelled:
            raise WrongDiscInserted(
                failure.get("message") or "The disc in the drive is not the one this rescue belongs to", result
            )
        if result.return_code == EXIT_MEDIA_UNAVAILABLE and not result.cancelled:
            raise ProcessFailure(
                failure.get("message") or "The disc was removed or the drive stopped responding", result
            )
        if result.cancelled or result.timed_out or result.return_code != 0 or not summary:
            raise ProcessFailure(
                failure.get("message") or "The damaged-disc rescue stopped before it finished", result
            )
        if not structures_only:
            os.replace(partial, destination)
        return summary


class DriveNotResponding(ProcessFailure):
    """The drive failed during a rescue; the image and its map are kept for a retry."""


class WrongDiscInserted(ProcessFailure):
    """A saved rescue image belongs to a different disc than the one in the drive."""


class HandBrakeTranscoder:
    def __init__(self, executable: str, runner: ProcessRunner):
        self.executable = executable
        self.runner = runner

    async def transcode(
        self,
        job_id: str,
        inputs: list[Path],
        destination: Path,
        preset: str,
        callback: ProgressCallback | None = None,
    ) -> list[Path]:
        if not self.executable or not Path(self.executable).is_file():
            raise FileNotFoundError("HandBrakeCLI is not installed")
        destination.mkdir(parents=True, exist_ok=True)
        outputs: list[Path] = []
        progress_pattern = re.compile(r"Encoding:.*?([0-9]+(?:\.[0-9]+)?)\s*%")
        for index, source in enumerate(inputs):
            target = destination / f"{source.stem}.mp4"

            async def on_line(line: str, current_index: int = index) -> None:
                match = progress_pattern.search(line)
                if callback and match:
                    percent = (current_index + float(match.group(1)) / 100) * 100 / len(inputs)
                    response = callback({"type": "progress", "percent": percent, "message": line})
                    if hasattr(response, "__await__"):
                        await response

            result = await self.runner.run(
                job_id,
                [self.executable, "-i", str(source), "-o", str(target), "--preset", preset, "--json"],
                timeout=24 * 3600,
                no_output_timeout=300,
                on_line=on_line,
            )
            if result.return_code != 0 or not target.exists() or target.stat().st_size < 1024 * 1024:
                raise ProcessFailure(f"HandBrake failed for {source.name}", result)
            outputs.append(target)
        return outputs


# "Ripping and encoding track 3, progress - 45.12%, ETA - 5m": printed for every frame, never logged.
CYANRIP_PROGRESS = re.compile(r"^Ripping.*?track (\d+), progress - ([\d.]+)%")
CYANRIP_DISCID = re.compile(r"^DiscID:\s+(\S+)")
CYANRIP_DISC_TRACKS = re.compile(r"^Disc tracks:\s+(\d+)")
CYANRIP_TRACK_DONE = re.compile(r"^Track \d+ ripped and encoded successfully")


@dataclass
class CyanripOutput:
    """What cyanrip's output says about the CD and how far the rip has come."""

    discid: str = ""
    tracks: int = 0
    finished: int = 0
    current: int = 0
    percent: float = -1.0
    announced: bool = False

    def read(self, line: str) -> list[dict]:
        text = line.strip()
        progress = CYANRIP_PROGRESS.match(text)
        if progress:
            events: list[dict] = []
            track = int(progress.group(1))
            if track != self.current:
                # Shown on the dashboard and written to the log once per track.
                self.current = track
                of = f" of {self.tracks}" if self.tracks else ""
                events.append({"type": "stage", "message": f"Ripping track {track}{of}"})
            if not self.tracks:
                return events
            track_share = min(100.0, float(progress.group(2))) / 100
            percent = min(99.0, (self.finished + track_share) / self.tracks * 100)
            if percent - self.percent >= 0.5:
                self.percent = percent
                events.append({"type": "progress", "percent": round(percent, 1)})
            return events
        if not text:
            return []
        if match := CYANRIP_DISCID.match(text):
            self.discid = match.group(1)
        elif match := CYANRIP_DISC_TRACKS.match(text):
            self.tracks = int(match.group(1))
        elif CYANRIP_TRACK_DONE.match(text):
            self.finished += 1
        events: list[dict] = [{"type": "log", "message": text}]
        if self.discid and self.tracks and not self.announced:
            self.announced = True
            events.append({"type": "cd", "discid": self.discid, "tracks": self.tracks})
        return events


class AudioRipper:
    def __init__(self, executable: str, runner: ProcessRunner):
        self.executable = executable
        self.runner = runner

    async def rip(
        self,
        job_id: str,
        letter: str,
        destination: Path,
        formats: str = "flac",
        callback: ProgressCallback | None = None,
        offset: int = 0,
    ) -> None:
        """Rip the CD, reporting a ``cd`` event with its DiscID and track count, and the progress.

        cyanrip does not ask MusicBrainz or the Cover Art Archive. DiscDock looks the album up itself,
        so a busy MusicBrainz never stops a rip.
        """
        if not self.executable or not Path(self.executable).is_file():
            raise FileNotFoundError("cyanrip is required for audio CDs")
        destination.mkdir(parents=True, exist_ok=True)
        output = CyanripOutput()

        async def emit(event: dict) -> None:
            if callback:
                response = callback(event)
                if hasattr(response, "__await__"):
                    await response

        async def on_line(line: str) -> None:
            for event in output.read(line):
                await emit(event)

        result = await self.runner.run(
            job_id,
            # The offset is always given: without it cyanrip stops at drives that can read ISRC codes.
            [self.executable, "-d", letter, "-s", str(offset), "-N", "-U", "-o", formats, "-T", "unicode"],
            cwd=destination,
            timeout=8 * 3600,
            no_output_timeout=300,
            on_line=on_line,
        )
        of = f" of {output.tracks}" if output.tracks else ""
        if result.return_code != 0 or not any(destination.rglob("*.flac")):
            await emit(
                {
                    "type": "log",
                    "message": f"cyanrip stopped with exit code {result.return_code} after ripping {output.finished}{of} tracks",
                }
            )
            raise ProcessFailure("Audio-CD ripping failed", result)
        await emit({"type": "log", "message": f"cyanrip ripped {output.finished}{of} tracks"})


class DataDiscRipper:
    def __init__(self, runner: ProcessRunner):
        self.runner = runner

    @staticmethod
    def _volume_size(letter: str) -> int:
        drive_letter = letter.rstrip(":").upper()
        if len(drive_letter) != 1 or not drive_letter.isalpha():
            raise ValueError("Invalid Windows drive letter")
        # DriveInfo/disk_usage works for UDF optical media without requiring
        # the Windows management (CIM) permission that Get-Volume needs on
        # some standard user accounts.
        try:
            size = int(shutil.disk_usage(f"{drive_letter}:\\").total)
        except OSError:
            size = 0
        if size > 0:
            return size
        script = f"(Get-Volume -DriveLetter '{drive_letter}' -ErrorAction Stop).Size"
        process = start_external_process(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        try:
            stdout, _ = process.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            return 0
        try:
            return int(stdout.strip()) if process.returncode == 0 else 0
        except ValueError:
            return 0

    async def rip(
        self, job_id: str, letter: str, destination: Path, callback: ProgressCallback | None = None
    ) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_suffix(destination.suffix + ".part")
        powershell = shutil.which("powershell.exe")
        if not powershell:
            raise FileNotFoundError("Windows PowerShell is required for data-disc imaging")
        total = await asyncio.to_thread(self._volume_size, letter)
        drive_value = base64.b64encode(letter.encode("utf-8")).decode("ascii")
        destination_value = base64.b64encode(str(partial).encode("utf-8")).decode("ascii")
        script = r"""
$ProgressPreference = 'SilentlyContinue'
$Drive = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__DRIVE__'))
$Destination = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__DESTINATION__'))
$Total = [long]__TOTAL__
$sourcePath = "\\.\$($Drive.TrimEnd(':')):"
$source = [System.IO.File]::Open($sourcePath, 'Open', 'Read', 'ReadWrite')
$target = [System.IO.File]::Open($Destination, 'Create', 'Write', 'None')
$buffer = New-Object byte[] (4MB)
$readTotal = [long]0
$lastPercent = -1
try {
  while (($count = $source.Read($buffer, 0, $buffer.Length)) -gt 0) {
    $target.Write($buffer, 0, $count)
    $readTotal += $count
    if ($Total -gt 0) {
      $percent = [Math]::Min(100, [Math]::Floor(($readTotal * 100.0) / $Total))
      if ($percent -ne $lastPercent) { Write-Output "DDPROGRESS:$percent"; $lastPercent = $percent }
    }
  }
  $target.Flush($true)
} finally {
  $target.Dispose()
  $source.Dispose()
}
"""
        script = (
            script.replace("__DRIVE__", drive_value)
            .replace("__DESTINATION__", destination_value)
            .replace("__TOTAL__", str(total))
        )
        encoded_script = base64.b64encode(script.encode("utf-16le")).decode("ascii")

        async def on_line(line: str) -> None:
            if callback and line.startswith("DDPROGRESS:"):
                try:
                    await _invoke(
                        callback,
                        {
                            "type": "progress",
                            "percent": float(line.split(":", 1)[1]),
                            "message": "Imaging data disc",
                        },
                    )
                except ValueError:
                    pass
            elif callback:
                await _invoke(callback, {"type": "log", "message": line})

        result = await self.runner.run(
            job_id,
            [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded_script],
            timeout=12 * 3600,
            no_output_timeout=600,
            on_line=on_line,
        )
        if result.return_code != 0 or result.cancelled:
            raise ProcessFailure("Data-disc imaging failed", result)
        os.replace(partial, destination)
        if destination.stat().st_size < 1024 * 1024:
            raise RuntimeError("Data-disc image is unexpectedly small")
        if total and abs(destination.stat().st_size - total) > 2048 * 16:
            raise RuntimeError(
                f"Data-disc image size does not match the source ({destination.stat().st_size} of {total} bytes)"
            )
        return destination


async def _invoke(callback: ProgressCallback, event: dict) -> None:
    response = callback(event)
    if hasattr(response, "__await__"):
        await response
