from __future__ import annotations

import csv
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from .models import TitleInfo
from .processes import ProcessFailure, ProcessResult, ProcessRunner

ROBOT_LINE = re.compile(r"^(?P<record>[A-Z]+):(?P<body>.*)$")
MAKEMKV_LICENSE_MESSAGE_CODES = {5052, 5053, 5055}
MAKEMKV_LICENSE_ACTION = (
    "MakeMKV needs a license decision before DiscDock can continue. Open MakeMKV on Windows "
    "with this disc. If prompted, choose Yes to start its 30-day evaluation; otherwise use "
    "Help > Register with a valid purchased key. Let MakeMKV open the disc once, close MakeMKV, "
    "then Retry in DiscDock."
)


class MakeMKVLicenseError(ProcessFailure):
    """MakeMKV stopped for its own evaluation or registration requirement."""


# "Title #%1 has length of %2 seconds which is less than minimum title length of %3 seconds ..."
MAKEMKV_TITLE_TOO_SHORT = 3025


class NoVideoTitles(ProcessFailure):
    """MakeMKV listed no titles; ``too_short`` of them were skipped for the minimum title length."""

    def __init__(self, message: str, result: ProcessResult, too_short: int = 0):
        super().__init__(message, result)
        self.too_short = too_short


def has_license_prompt(messages: list[dict]) -> bool:
    return any(message.get("code") in MAKEMKV_LICENSE_MESSAGE_CODES for message in messages)


def parse_robot_line(line: str) -> tuple[str, list[str]] | None:
    match = ROBOT_LINE.match(line.strip())
    if not match:
        return None
    try:
        fields = next(csv.reader([match.group("body")], escapechar="\\", doublequote=True, strict=False))
    except (csv.Error, StopIteration):
        return None
    return match.group("record"), fields


def parse_duration(value: str) -> int:
    try:
        parts = [int(part) for part in value.split(":")]
    except ValueError:
        return 0
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0] if parts else 0


def match_title(titles: list[TitleInfo], track: dict) -> TitleInfo | None:
    """Find a selected disc title again in another scan of the same disc.

    MakeMKV numbers titles by their position in its filtered list, and that
    position can change when damaged sectors alter its structure analysis of a
    rescued image. The DVD title number and runtime identify the movie
    independently of the position.
    """
    if not titles:
        return None
    wanted = int(track.get("disc_title_number") or 0)
    duration = int(track.get("duration_seconds") or 0)
    chapters = int(track.get("chapters") or 0)
    size = int(track.get("size_bytes") or 0)
    # DVD titles carry their disc title number. Blu-ray playlists do not, so
    # they are recognised by runtime, chapter count, and size instead.
    pool = [title for title in titles if wanted and title.disc_title_number == wanted]
    if not pool and duration:
        tolerance = max(10, duration * 0.03)
        pool = [title for title in titles if abs(title.duration_seconds - duration) <= tolerance]
    if not pool:
        return None
    return min(
        pool,
        key=lambda title: (
            abs(title.duration_seconds - duration),
            abs(title.chapters - chapters),
            abs(title.size_bytes - size) // (64 * 1024 * 1024),
            title.angle > 1,
            title.id,
        ),
    )


@dataclass
class DiscScan:
    drive_index: int | None = None
    drive_name: str = ""
    disc_name: str = ""
    device_name: str = ""
    title_count: int = 0
    titles: list[TitleInfo] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)
    raw_lines: list[str] = field(default_factory=list)


class MakeMKVParser:
    def __init__(self, letter: str):
        self.letter = letter.rstrip(":").upper() + ":"
        self.scan = DiscScan()
        self._titles: dict[int, dict] = {}

    def accept(self, line: str) -> dict | None:
        self.scan.raw_lines.append(line)
        parsed = parse_robot_line(line)
        if not parsed:
            return None
        record, fields = parsed
        if record == "DRV" and len(fields) >= 7:
            device = fields[6].upper()
            if self.letter in device or device.rstrip("\\").endswith(self.letter):
                self.scan.drive_index = int(fields[0])
                self.scan.drive_name = fields[4]
                self.scan.disc_name = fields[5]
                self.scan.device_name = fields[6]
        elif record in {"TCOUNT", "TCOUT"} and fields:
            self.scan.title_count = int(fields[0])
        elif record == "MSG" and len(fields) >= 4:
            self.scan.messages.append(
                {
                    "code": int(fields[0]),
                    "flags": int(fields[1]),
                    "count": int(fields[2]),
                    "message": fields[3],
                }
            )
        elif record == "TINFO" and len(fields) >= 4:
            title_id, code, _, value = int(fields[0]), int(fields[1]), fields[2], fields[3]
            title = self._titles.setdefault(title_id, {"id": title_id, "streams": []})
            mapping = {
                2: "name",
                8: "chapters",
                9: "duration",
                10: "size_text",
                11: "size_bytes",
                15: "angle",
                16: "source_filename",
                24: "disc_title_number",
                26: "segment_map",
                27: "filename",
                30: "description",
                49: "source_group",
            }
            key = mapping.get(code)
            if key:
                title[key] = value
        elif record == "SINFO" and len(fields) >= 5:
            title_id, stream_id, code, _, value = (
                int(fields[0]),
                int(fields[1]),
                int(fields[2]),
                fields[3],
                fields[4],
            )
            title = self._titles.setdefault(title_id, {"id": title_id, "streams": []})
            streams = title.setdefault("stream_map", {})
            stream = streams.setdefault(stream_id, {"id": stream_id})
            stream[
                {
                    1: "type",
                    2: "name",
                    3: "language",
                    4: "language_code",
                    6: "codec",
                    7: "codec_short",
                    19: "description",
                }.get(code, f"code_{code}")
            ] = value
        elif record == "PRGV" and len(fields) >= 3:
            current, total, maximum = (int(value) for value in fields[:3])
            return {
                "type": "progress",
                "current": current,
                "total": total,
                "maximum": maximum,
                "percent": round(max(current, total) * 100 / maximum, 2) if maximum else 0,
            }
        elif record in {"PRGC", "PRGT"} and len(fields) >= 3:
            return {"type": "stage", "code": fields[0], "message": fields[2]}
        return {"type": record.lower(), "fields": fields}

    def finish(self) -> DiscScan:
        titles: list[TitleInfo] = []
        for title_id, raw in sorted(self._titles.items()):
            streams = list(raw.get("stream_map", {}).values())
            titles.append(
                TitleInfo(
                    id=title_id,
                    disc_title_number=int(raw.get("disc_title_number", 0) or 0),
                    name=raw.get("name", ""),
                    duration_seconds=parse_duration(raw.get("duration", "")),
                    size_bytes=int(raw.get("size_bytes", 0) or 0),
                    chapters=int(raw.get("chapters", 0) or 0),
                    filename=raw.get("filename", raw.get("source_filename", "")),
                    angle=(
                        int(raw.get("angle", 0))
                        if str(raw.get("angle", 0) or "0").isdigit()
                        else 0
                    ),
                    source_group=raw.get("source_group", ""),
                    segment_map=raw.get("segment_map", ""),
                    source_filename=raw.get("source_filename", ""),
                    description=raw.get("description", ""),
                    streams=streams,
                )
            )
        self.scan.titles = titles
        if not self.scan.title_count:
            self.scan.title_count = len(titles)
        return self.scan


ProgressCallback = Callable[[dict], Awaitable[None] | None]


class MakeMKVClient:
    def __init__(self, executable: str, runner: ProcessRunner, *, no_output_timeout: int = 180):
        self.executable = str(Path(executable))
        self.runner = runner
        self.no_output_timeout = no_output_timeout

    def _base_args(self, source: str = "") -> list[str]:
        args = [self.executable, "-r", "--cache=1", "--messages=-stdout", "--progress=-stdout"]
        if source.startswith(("iso:", "file:")):
            # Reading an image or a backup folder needs no drive. Without --noscan MakeMKV
            # first probes the disc still in the drive, which takes minutes for a damaged disc.
            args.insert(2, "--noscan")
        return args

    async def drive_index(self, owner_id: str, letter: str, timeout: int = 900) -> int | None:
        """MakeMKV's own number for the drive with this letter; backups need a disc: source."""
        parser = MakeMKVParser(letter)

        async def on_line(line: str) -> None:
            parser.accept(line)

        await self.runner.run(
            owner_id,
            [*self._base_args(), "info", "disc:9999"],
            timeout=timeout,
            no_output_timeout=self.no_output_timeout,
            on_line=on_line,
        )
        return parser.scan.drive_index

    async def capture_disc_attributes(self, owner_id: str, drive_index: int, folder: Path, timeout: int = 1800) -> Path:
        """Start a MakeMKV backup of the disc and stop it once MakeMKV has saved discatt.dat.

        MakeMKV writes that file first. It holds what MakeMKV needs to decrypt the
        disc later without the drive, so the rest of the backup is not needed.
        """
        attributes = folder / "discatt.dat"
        folder.mkdir(parents=True, exist_ok=True)
        parser = MakeMKVParser("")
        state = {"size": -1, "stopped": False}

        async def on_line(line: str) -> None:
            parser.accept(line)
            if state["stopped"]:
                return
            try:
                size = attributes.stat().st_size
            except OSError:
                return
            if size > 0 and size == state["size"]:
                state["stopped"] = True
                await self.runner.cancel(owner_id)
            state["size"] = size

        result = await self.runner.run(
            owner_id,
            [*self._base_args(), "backup", f"disc:{int(drive_index)}", str(folder)],
            timeout=timeout,
            no_output_timeout=self.no_output_timeout,
            on_line=on_line,
        )
        if has_license_prompt(parser.scan.messages):
            raise MakeMKVLicenseError(MAKEMKV_LICENSE_ACTION, result)
        if not attributes.is_file() or attributes.stat().st_size == 0:
            raise ProcessFailure("MakeMKV did not save the disc's decryption information", result)
        return attributes

    async def inspect(
        self,
        owner_id: str,
        letter: str,
        min_length: int,
        max_length: int,
        timeout: int,
        callback: ProgressCallback | None = None,
    ) -> DiscScan:
        # MakeMKV's native Windows CLI supports --minlength but not --maxlength.
        # The upper bound is applied to parsed titles by the workflow instead.
        _ = max_length
        return await self.inspect_source(owner_id, f"dev:{letter}", min_length, timeout, callback)

    async def inspect_source(
        self,
        owner_id: str,
        source: str,
        min_length: int,
        timeout: int,
        callback: ProgressCallback | None = None,
    ) -> DiscScan:
        """Scan any MakeMKV source specifier, including a rescued ``iso:`` image."""
        parser = MakeMKVParser(source[4:] if source.startswith("dev:") else "")

        async def on_line(line: str) -> None:
            event = parser.accept(line) or {"type": "log"}
            event.setdefault("message", line)
            if event and callback:
                response = callback(event)
                if hasattr(response, "__await__"):
                    await response

        args = self._base_args(source) + [f"--minlength={min_length}", "info", source]
        result = await self.runner.run(
            owner_id, args, timeout=timeout, no_output_timeout=self.no_output_timeout, on_line=on_line
        )
        scan = parser.finish()
        # A GUI-style license prompt can leave makemkvcon waiting until our
        # no-output timeout. Classify its robot message before generic timeout
        # and exit-code handling so the user gets the actual recovery action.
        if has_license_prompt(scan.messages):
            raise MakeMKVLicenseError(MAKEMKV_LICENSE_ACTION, result)
        if result.timed_out:
            raise ProcessFailure("MakeMKV inspection timed out", result)
        if result.return_code != 0:
            raise ProcessFailure("MakeMKV could not inspect the disc", result)
        if scan.title_count <= 0:
            too_short = sum(message.get("code") == MAKEMKV_TITLE_TOO_SHORT for message in scan.messages)
            raise NoVideoTitles("MakeMKV found no video titles", result, too_short)
        if not scan.titles:
            raise ProcessFailure(
                "MakeMKV found video titles but their details were not captured; retry the scan",
                result,
            )
        return scan

    async def rip(
        self,
        owner_id: str,
        letter: str,
        destination: Path,
        title_ids: list[int] | None,
        timeout: int,
        callback: ProgressCallback | None = None,
        backup: bool = False,
        min_length: int | None = None,
    ) -> list[ProcessResult]:
        return await self.rip_source(
            owner_id,
            f"dev:{letter}",
            destination,
            title_ids,
            timeout,
            callback=callback,
            backup=backup,
            min_length=min_length,
        )

    async def rip_source(
        self,
        owner_id: str,
        source: str,
        destination: Path,
        title_ids: list[int] | None,
        timeout: int,
        callback: ProgressCallback | None = None,
        backup: bool = False,
        min_length: int | None = None,
    ) -> list[ProcessResult]:
        """Rip from any MakeMKV source specifier, including rescued ISO images.

        Title ids are positions in MakeMKV's filtered title list, so ``min_length``
        must match the value used for the scan that produced those ids. Without
        it MakeMKV applies its own default (two minutes) and may rip a
        different title than the one selected.
        """
        destination.mkdir(parents=True, exist_ok=True)
        base = self._base_args(source) + ([f"--minlength={int(min_length)}"] if min_length is not None else [])
        if backup:
            commands = [base + ["backup", "--decrypt", source, str(destination)]]
        else:
            if title_ids is not None and not title_ids:
                raise ValueError("At least one disc title must be selected")
            selected = [str(title) for title in title_ids] if title_ids is not None else ["all"]
            commands = [base + ["mkv", source, title, str(destination)] for title in selected]
        results: list[ProcessResult] = []
        for args in commands:
            parser = MakeMKVParser(source[4:] if source.startswith("dev:") else "")

            async def on_line(line: str, active_parser: MakeMKVParser = parser) -> None:
                event = active_parser.accept(line) or {"type": "log"}
                event.setdefault("message", line)
                if event and callback:
                    response = callback(event)
                    if hasattr(response, "__await__"):
                        await response

            result = await self.runner.run(
                owner_id, args, timeout=timeout, no_output_timeout=self.no_output_timeout, on_line=on_line
            )
            results.append(result)
            if has_license_prompt(parser.scan.messages):
                raise MakeMKVLicenseError(MAKEMKV_LICENSE_ACTION, result)
            if result.timed_out or result.return_code != 0:
                raise ProcessFailure("MakeMKV ripping failed", result)
        if not any(path.is_file() and path.stat().st_size > 1024 * 1024 for path in destination.rglob("*")):
            raise ProcessFailure("MakeMKV reported success but produced no usable files", results[-1])
        return results
