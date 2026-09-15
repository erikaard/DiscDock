"""MusicBrainz lookups for audio CDs.

DiscDock looks the album up itself instead of letting cyanrip do it, so a CD is always ripped, even
while MusicBrainz is busy. MusicBrainz allows about one request per second from an address and
answers 503 above that. DiscDock sends one request at a time with a few seconds between them, waits
as long as MusicBrainz asks after a 503, and remembers every answer, so a CD normally costs one request.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from . import __version__

API = "https://musicbrainz.org/ws/2"
COVER_ART = "https://coverartarchive.org"
RELEASE_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# MusicBrainz asks every application to name itself, and turns anonymous clients away first.
HEADERS = {"User-Agent": f"DiscDock/{__version__} ( Windows disc ripping station )", "Accept": "application/json"}
BUSY_STATUSES = {429, 502, 503, 504}
LUCENE_SPECIAL = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/])')
MAX_COVER_BYTES = 10 * 1024 * 1024
# The choice that rips a CD without an album from MusicBrainz.
WITHOUT_ALBUM = "none"
# While a CD rips there is time to wait for a busy MusicBrainz; a search in the dashboard should answer soon.
BACKGROUND_RETRY_SECONDS = (10.0, 30.0, 60.0)
SEARCH_RETRY_SECONDS = (4.0,)
MAX_RETRY_AFTER_SECONDS = 120.0


class MusicBrainzBusy(RuntimeError):
    """MusicBrainz declined the request, usually with 503 when it gets more than about one request per second."""

    def __init__(self, status: int):
        super().__init__(f"MusicBrainz responded with {status}")
        self.status = status


class MusicBrainzUnavailable(RuntimeError):
    """MusicBrainz could not be reached, or its answer could not be read."""


@dataclass
class AlbumTrack:
    position: int
    title: str
    artist: str = ""
    length_seconds: int = 0
    track_id: str = ""
    recording_id: str = ""


@dataclass
class AlbumRelease:
    id: str
    title: str
    artist: str = ""
    date: str = ""
    country: str = ""
    label: str = ""
    disambiguation: str = ""
    format: str = ""
    track_count: int = 0
    artist_id: str = ""
    disc_number: int = 1
    disc_count: int = 1
    tracks: list[AlbumTrack] = field(default_factory=list)

    @property
    def year(self) -> str:
        return self.date[:4] if self.date[:4].isdigit() else ""

    def summary(self) -> dict[str, Any]:
        """What the job remembers and the dashboard shows; the tracks are looked up again for tagging."""
        values = asdict(self)
        values.pop("tracks")
        return values


def _credited(credits: Any) -> str:
    """The artists as MusicBrainz credits them, for example "Tom Petty and the Heartbreakers"."""
    if not isinstance(credits, list):
        return ""
    names = []
    for credit in credits:
        if isinstance(credit, dict):
            artist = credit.get("artist") if isinstance(credit.get("artist"), dict) else {}
            names.append(f"{credit.get('name') or artist.get('name') or ''}{credit.get('joinphrase') or ''}")
    return "".join(names).strip()


def _medium(release: dict[str, Any], discid: str, track_count: int) -> tuple[dict[str, Any], int]:
    """The medium of a release that is the CD in the drive, and how many media the release has."""
    media = [medium for medium in release.get("media") or [] if isinstance(medium, dict)]
    for medium in media:
        discs = [disc for disc in medium.get("discs") or [] if isinstance(disc, dict)]
        if discid and any(disc.get("id") == discid for disc in discs):
            return medium, len(media)
    for medium in media:
        if track_count and int(medium.get("track-count") or 0) == track_count:
            return medium, len(media)
    return (media[0] if media else {}), len(media)


def parse_release(release: dict[str, Any], discid: str = "", track_count: int = 0) -> AlbumRelease:
    medium, media_count = _medium(release, discid, track_count)
    credits = release.get("artist-credit") or []
    artist = _credited(credits)
    first_credit = credits[0] if isinstance(credits, list) and credits and isinstance(credits[0], dict) else {}
    labels = [info.get("label") for info in release.get("label-info") or [] if isinstance(info, dict)]
    tracks = []
    for track in medium.get("tracks") or []:
        if not isinstance(track, dict):
            continue
        recording = track.get("recording") if isinstance(track.get("recording"), dict) else {}
        tracks.append(
            AlbumTrack(
                position=int(track.get("position") or 0),
                title=str(track.get("title") or recording.get("title") or ""),
                artist=_credited(track.get("artist-credit") or recording.get("artist-credit")) or artist,
                length_seconds=int(track.get("length") or recording.get("length") or 0) // 1000,
                track_id=str(track.get("id") or ""),
                recording_id=str(recording.get("id") or ""),
            )
        )
    return AlbumRelease(
        id=str(release.get("id") or ""),
        title=str(release.get("title") or ""),
        artist=artist,
        date=str(release.get("date") or ""),
        country=str(release.get("country") or ""),
        label=", ".join(
            dict.fromkeys(str(label["name"]) for label in labels if isinstance(label, dict) and label.get("name"))
        ),
        disambiguation=str(release.get("disambiguation") or ""),
        format=str(medium.get("format") or ""),
        track_count=int(medium.get("track-count") or release.get("track-count") or len(tracks)),
        artist_id=str((first_credit.get("artist") or {}).get("id") or ""),
        disc_number=int(medium.get("position") or 1),
        disc_count=max(1, media_count),
        tracks=tracks,
    )


def _quoted(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def search_query(text: str) -> str:
    """A release search for "Artist - Album", or for words of the album title or the artist."""
    words = " ".join(text.split())
    artist, separator, album = words.partition(" - ")
    if separator and artist and album:
        return f'release:"{_quoted(album)}" AND artist:"{_quoted(artist)}"'
    escaped = LUCENE_SPECIAL.sub(r"\\\1", words)
    return f"release:({escaped}) OR artist:({escaped})"


def barcode_query(barcode: str) -> str:
    """A release search for a barcode as printed; a UPC-A code is the same as the EAN-13 code without its leading 0."""
    digits = re.sub(r"\D", "", barcode)
    if not 8 <= len(digits) <= 14:
        raise ValueError("A barcode has 8 to 14 digits")
    variants = [digits]
    if len(digits) == 12:
        variants.append("0" + digits)
    elif len(digits) == 13 and digits.startswith("0"):
        variants.append(digits[1:])
    return " OR ".join(f"barcode:{variant}" for variant in variants)


class MusicBrainzClient:
    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        spacing_seconds: float = 3.0,
        retry_seconds: tuple[float, ...] = BACKGROUND_RETRY_SECONDS,
        search_retry_seconds: tuple[float, ...] = SEARCH_RETRY_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._client = client
        self.spacing_seconds = spacing_seconds
        self.retry_seconds = retry_seconds
        self.search_retry_seconds = search_retry_seconds
        self._sleep = sleep
        # One request at a time with a gap, for every job and search together.
        self._lock = asyncio.Lock()
        self._next_request = 0.0
        # Answers already given, so tagging a CD never asks for what its lookup already returned.
        self._discs: dict[tuple[str, int], list[AlbumRelease]] = {}
        self._releases: dict[tuple[str, str, int], AlbumRelease] = {}

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            # Like the OMDb client: a stale proxy variable must not break lookups that work in a browser.
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(20, connect=10), follow_redirects=True, trust_env=False
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get(
        self, url: str, params: dict[str, str] | None = None, retry_seconds: tuple[float, ...] | None = None
    ) -> httpx.Response:
        waits = list(self.retry_seconds if retry_seconds is None else retry_seconds)
        while True:
            async with self._lock:
                delay = self._next_request - time.monotonic()
                if delay > 0:
                    await self._sleep(delay)
                try:
                    response = await self._http().get(url, params=params, headers=HEADERS)
                except httpx.HTTPError as error:
                    raise MusicBrainzUnavailable(f"MusicBrainz could not be reached ({error})") from error
                finally:
                    self._next_request = time.monotonic() + self.spacing_seconds
            if response.status_code not in BUSY_STATUSES:
                return response
            if not waits:
                raise MusicBrainzBusy(response.status_code)
            wait = waits.pop(0)
            retry_after = response.headers.get("Retry-After", "").strip()
            if retry_after.isdigit():
                wait = max(wait, min(float(retry_after), MAX_RETRY_AFTER_SECONDS))
            await self._sleep(wait)

    async def _json(
        self, url: str, params: dict[str, str] | None = None, retry_seconds: tuple[float, ...] | None = None
    ) -> dict[str, Any] | None:
        response = await self._get(url, params, retry_seconds)
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise MusicBrainzUnavailable(f"MusicBrainz responded with {response.status_code}")
        try:
            payload = response.json()
        except ValueError as error:
            raise MusicBrainzUnavailable("MusicBrainz sent an answer DiscDock could not read") from error
        return payload if isinstance(payload, dict) else None

    async def releases_for_disc(self, discid: str, track_count: int = 0) -> list[AlbumRelease]:
        """The releases MusicBrainz knows for a CD's DiscID, with their tracks; empty when it does not know the CD."""
        key = (discid, track_count)
        if key not in self._discs:
            payload = await self._json(
                f"{API}/discid/{quote(discid, safe='')}?inc=artist-credits+labels+recordings&fmt=json"
            )
            releases = [
                parse_release(release, discid, track_count)
                for release in (payload or {}).get("releases") or []
                if isinstance(release, dict)
            ]
            self._discs[key] = releases
            for release in releases:
                self._releases[(release.id, discid, track_count)] = release
        return list(self._discs[key])

    async def _search(self, query: str, limit: int) -> list[AlbumRelease]:
        payload = await self._json(
            f"{API}/release", {"query": query, "limit": str(limit), "fmt": "json"}, self.search_retry_seconds
        )
        releases = (payload or {}).get("releases") or []
        return [parse_release(release) for release in releases if isinstance(release, dict)]

    async def search(self, text: str, limit: int = 10) -> list[AlbumRelease]:
        return await self._search(search_query(text), limit)

    async def search_barcode(self, barcode: str, limit: int = 10) -> list[AlbumRelease]:
        """The releases with the barcode printed on the back of the album."""
        return await self._search(barcode_query(barcode), limit)

    async def release(self, release_id: str, discid: str = "", track_count: int = 0) -> AlbumRelease:
        """A release with the tracks of the medium that is the CD in the drive."""
        if not RELEASE_ID.match(release_id):
            raise ValueError("That is not a MusicBrainz release")
        key = (release_id, discid, track_count)
        if key not in self._releases:
            payload = await self._json(
                f"{API}/release/{release_id}?inc=artist-credits+labels+recordings+discids&fmt=json"
            )
            if not payload:
                raise MusicBrainzUnavailable("MusicBrainz no longer has this release")
            self._releases[key] = parse_release(payload, discid, track_count)
        return self._releases[key]

    async def front_cover(self, release_id: str) -> bytes | None:
        """The front cover from the Cover Art Archive, or None when there is none or it cannot be fetched."""
        if not RELEASE_ID.match(release_id):
            return None
        try:
            response = await self._get(f"{COVER_ART}/release/{release_id}/front-500", retry_seconds=())
        except (MusicBrainzBusy, MusicBrainzUnavailable):
            return None
        if response.status_code != 200 or not response.headers.get("content-type", "").startswith("image/"):
            return None
        return response.content if 0 < len(response.content) <= MAX_COVER_BYTES else None
