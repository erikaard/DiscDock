from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from .database import Database, utc_now
from .models import MediaKind, MetadataCandidate
from .secrets import SecretStore

YEAR_PATTERN = re.compile(r"(?:^|[\s._(-])((?:19|20)\d{2})(?:$|[\s._)-])")
TRAILING_LABEL_NOISE = re.compile(
    r"(?:\s+(?:(?:disc|disk|dvd|bd|cd|d|f|r)\s*\d+|"
    r"(?:en|eng|no|nor|de|ger|fr|fre|es|spa|it|ita|nl|dut|se|swe|dk|dan|fi|fin)\d*|"
    r"eu|uk|us))+$",
    re.IGNORECASE,
)
ATTACHED_NUMBER = re.compile(r"(?<=[A-Za-z])(?=\d+\b)")
RUNTIME_PATTERN = re.compile(r"(\d+)\s*min", re.IGNORECASE)
NON_ALNUM = re.compile(r"[^a-z0-9]+")
TRAILING_SEQUENCE = re.compile(r"^(.*?)[\s._-]+(\d{1,2})$")


def title_from_label(label: str) -> tuple[str, str]:
    cleaned = label.strip().replace("_", " ").replace(".", " ")
    match = YEAR_PATTERN.search(cleaned)
    year = match.group(1) if match else ""
    if match:
        cleaned = cleaned[: match.start()] + " " + cleaned[match.end() :]
    cleaned = TRAILING_LABEL_NOISE.sub("", cleaned)
    cleaned = ATTACHED_NUMBER.sub(" ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -_()")
    return cleaned.title(), year


def _title_key(value: str) -> str:
    return NON_ALNUM.sub("", value.casefold())


def _sequence_hint(title: str) -> tuple[str, int]:
    match = TRAILING_SEQUENCE.match(title.strip())
    if not match:
        return title, 0
    return match.group(1).strip(), int(match.group(2))


def _year_number(value: str) -> int:
    match = re.match(r"(?:19|20)\d{2}", value)
    return int(match.group(0)) if match else 9999


def _choose_candidate(
    title: str, year: str, candidates: list[MetadataCandidate]
) -> MetadataCandidate | None:
    if not candidates:
        return None
    target_key = _title_key(title)
    exact = [candidate for candidate in candidates if _title_key(candidate.title) == target_key]
    if exact:
        if year:
            return min(exact, key=lambda candidate: candidate.year[:4] != year[:4])
        return exact[0]

    base_title, sequence = _sequence_hint(title)
    base_key = _title_key(base_title)
    if sequence > 0 and base_key:
        # Labels such as TRANSFORMERS2 often mean the second modern film even
        # though the published title has a subtitle rather than the digit 2.
        franchise = {
            candidate.provider_id: candidate
            for candidate in candidates
            if candidate.media_kind == MediaKind.MOVIE
            and _title_key(candidate.title).startswith(base_key)
            and _year_number(candidate.year) != 9999
        }
        chronological = sorted(
            franchise.values(), key=lambda candidate: (_year_number(candidate.year), candidate.title)
        )
        if sequence <= len(chronological):
            return chronological[sequence - 1]

    target_tokens = set(re.findall(r"[a-z0-9]+", title.casefold()))

    def score(candidate: MetadataCandidate) -> tuple[int, int, int]:
        candidate_tokens = set(re.findall(r"[a-z0-9]+", candidate.title.casefold()))
        overlap = len(target_tokens & candidate_tokens)
        contains = int(bool(target_key and target_key in _title_key(candidate.title)))
        year_match = int(bool(year and candidate.year.startswith(year[:4])))
        return (year_match, contains, overlap)

    return max(candidates, key=score)


class MetadataService:
    def __init__(self, database: Database, secrets: SecretStore):
        self.database = database
        self.secrets = secrets
        # The service is local-only; do not inherit stale proxy variables from a
        # launcher or shell, which can make metadata fail while browsers work.
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(10, connect=5), follow_redirects=True, trust_env=False
        )

    async def close(self) -> None:
        await self.client.aclose()

    def _cached(self, key: str) -> Any | None:
        rows = self.database.query(
            "SELECT response_json,expires_at FROM metadata_cache WHERE cache_key=?", (key,)
        )
        if not rows:
            return None
        try:
            if datetime.fromisoformat(rows[0]["expires_at"]) <= datetime.now(UTC):
                return None
            return json.loads(rows[0]["response_json"])
        except (ValueError, json.JSONDecodeError):
            return None

    def _store_cache(self, key: str, provider: str, value: Any, hours: int = 168) -> None:
        expires = (datetime.now(UTC) + timedelta(hours=hours)).isoformat(timespec="seconds")
        self.database.execute(
            """INSERT INTO metadata_cache(cache_key,provider,response_json,expires_at,created_at) VALUES(?,?,?,?,?)
               ON CONFLICT(cache_key) DO UPDATE SET response_json=excluded.response_json,expires_at=excluded.expires_at,created_at=excluded.created_at""",
            (key, provider, json.dumps(value, ensure_ascii=False), expires, utc_now()),
        )

    def _learned_label_match(self, label: str) -> MetadataCandidate | None:
        """Reuse a correction for another disc with the same normalized label."""
        label_title, _ = title_from_label(label)
        label_key = _title_key(label_title)
        if not label_key:
            return None
        rows = self.database.query(
            """SELECT disc_label,metadata_json FROM jobs
               WHERE metadata_json LIKE '%\"user_selected\": true%'
               ORDER BY updated_at DESC LIMIT 100"""
        )
        for row in rows:
            previous_title, _ = title_from_label(str(row.get("disc_label") or ""))
            if _title_key(previous_title) != label_key:
                continue
            try:
                metadata = json.loads(row.get("metadata_json") or "{}")
                return MetadataCandidate.model_validate(metadata)
            except (ValueError, json.JSONDecodeError):
                continue
        return None

    async def _omdb_request(self, params: dict[str, str]) -> dict[str, Any]:
        key = self.secrets.get("omdb_api_key")
        if not key:
            return {"Response": "False", "Error": "OMDb API key is not configured"}
        key_identity = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
        cache_key = f"omdb:{key_identity}:" + json.dumps(params, sort_keys=True)
        cached = self._cached(cache_key)
        if cached is not None:
            return cached
        try:
            response = await self.client.get("https://www.omdbapi.com/", params={"apikey": key, **params})
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise RuntimeError("OMDb could not be reached or returned an invalid response") from error
        self._store_cache(cache_key, "omdb", payload)
        return payload

    @staticmethod
    def _candidate(item: dict[str, Any], detail: dict[str, Any] | None = None) -> MetadataCandidate:
        source = detail or item
        type_name = str(source.get("Type") or item.get("Type") or "").lower()
        media_kind = (
            MediaKind.SERIES
            if type_name in {"series", "episode"}
            else MediaKind.MOVIE
            if type_name == "movie"
            else MediaKind.UNKNOWN
        )
        poster = str(source.get("Poster") or item.get("Poster") or "")
        runtime_match = RUNTIME_PATTERN.search(str(source.get("Runtime") or ""))
        return MetadataCandidate(
            provider="omdb",
            provider_id=str(source.get("imdbID") or item.get("imdbID") or ""),
            title=str(source.get("Title") or item.get("Title") or ""),
            year=str(source.get("Year") or item.get("Year") or ""),
            media_kind=media_kind,
            poster_url="" if poster == "N/A" else poster,
            plot="" if source.get("Plot") == "N/A" else str(source.get("Plot") or ""),
            runtime_minutes=int(runtime_match.group(1)) if runtime_match else 0,
        )

    async def search_omdb(
        self, query: str, year: str = "", media_kind: MediaKind | None = None
    ) -> list[MetadataCandidate]:
        params = {"s": query}
        if year:
            params["y"] = year[:4]
        if media_kind in {MediaKind.MOVIE, MediaKind.SERIES}:
            params["type"] = "movie" if media_kind == MediaKind.MOVIE else "series"
        payload = await self._omdb_request(params)
        if str(payload.get("Response")).lower() != "true":
            return []
        return [self._candidate(item) for item in payload.get("Search", [])[:10]]

    async def omdb_by_id(self, imdb_id: str) -> MetadataCandidate | None:
        payload = await self._omdb_request({"i": imdb_id, "plot": "short"})
        return self._candidate(payload, payload) if str(payload.get("Response")).lower() == "true" else None

    async def identify(self, label: str, media_kind: MediaKind | None = None) -> MetadataCandidate | None:
        learned = self._learned_label_match(label)
        if learned:
            return learned
        title, year = title_from_label(label)
        if not title:
            return None
        attempts: list[tuple[str, str]] = []
        if year:
            attempts.extend([(title, year), (title, str(int(year) - 1))])
        attempts.append((title, ""))
        base_title, sequence = _sequence_hint(title)
        if sequence > 0 and base_title:
            attempts.append((base_title, ""))
        words = title.split()
        while len(words) > 2:
            words.pop()
            attempts.append((" ".join(words), ""))
        seen: set[tuple[str, str]] = set()
        candidates: dict[str, MetadataCandidate] = {}
        for query, candidate_year in attempts:
            if (query, candidate_year) in seen:
                continue
            seen.add((query, candidate_year))
            try:
                results = await self.search_omdb(query, candidate_year, media_kind)
            except (httpx.HTTPError, RuntimeError, ValueError):
                return None
            for result in results:
                candidates[result.provider_id] = result
            if any(_title_key(result.title) == _title_key(title) for result in results):
                break
            await asyncio.sleep(0)
        selected = _choose_candidate(title, year, list(candidates.values()))
        if not selected:
            return None
        detail = await self.omdb_by_id(selected.provider_id)
        return detail or selected

    async def test_omdb(self, supplied_key: str | None = None) -> dict[str, Any]:
        if supplied_key:
            try:
                response = await self.client.get(
                    "https://www.omdbapi.com/", params={"apikey": supplied_key, "i": "tt0111161"}
                )
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError) as error:
                raise RuntimeError("OMDb could not be reached or returned an invalid response") from error
        else:
            payload = await self._omdb_request({"i": "tt0111161"})
        return {
            "ok": str(payload.get("Response")).lower() == "true",
            "message": payload.get("Error", "OMDb connection succeeded"),
        }
