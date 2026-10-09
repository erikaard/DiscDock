from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import unicodedata
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from .database import Database, utc_now
from .models import MediaKind, MetadataCandidate
from .secrets import SecretStore

RUNTIME_PATTERN = re.compile(r"(\d+)\s*min", re.IGNORECASE)
YEAR_WORD = re.compile(r"(?:19|20)\d{2}")
HASH_SUFFIX = re.compile(r"#[0-9a-f]{4}\s*$", re.IGNORECASE)
ATTACHED_NUMBER = re.compile(r"^([A-Za-z]{3,})(\d{1,2})$")
# How many candidates of one kind of match have their details looked up when choosing between them.
MAX_RUNTIME_CHECKS = 6
# Sequels are counted among at most this many films that start with the label's words.
MAX_FRANCHISE_CHECKS = 8
# OMDb answers worth remembering. Others, such as "Request limit reached!" or "Error
# getting data.", are passing problems that must not stick to a title for a week.
CACHEABLE_ERRORS = {"movie not found!", "series not found!", "too many results.", "incorrect imdb id."}
OMDB_RETRY_SECONDS = (1.0, 3.0)

ARTICLES = {"the", "a", "an"}
# Little words that say nothing about which film a label means.
STOPWORDS = ARTICLES | {"and", "of", "in", "on", "at", "to", "for", "as", "with", "from", "by", "or", "vs", "is"}
# A label made only of these names no film: DVD_VIDEO, VIDEO_DVD, DVDVolume, New, Del 1.
GENERIC_WORDS = {
    "dvd", "video", "dvdvideo", "videodvd", "dvdvolume", "volume", "vol", "new", "disc", "disk", "del", "part",
    "movie", "movies", "film", "bd", "bdrom", "bluray", "bdmv", "untitled", "mydvd", "mydisc", "nolabel",
    "cdrom", "dvdrom", "label", "title", "default", "logical", "volumeid", "data", "backup", "my",
}
# What publishers put after the title: formats, editions, regions and languages.
NOISE_WORDS = {
    "dvd", "bd", "bluray", "uhd", "hd", "3d", "2d", "4k", "ws", "fs", "widescreen", "fullscreen", "pal", "ntsc",
    "se", "ce", "le", "fn", "snf", "bmg", "special", "edition", "collectors", "collector", "ultimate",
    "deluxe", "anniversary", "remastered", "theatrical", "extended", "unrated", "uncut", "rental",
    "eu", "uk", "us", "usa", "aus", "scan", "scn", "nordic", "nor", "swe", "sv", "dk", "den", "dan",
    "fin", "fi", "ger", "de", "fr", "fre", "es", "spa", "it", "ita", "nl", "dut", "en", "eng", "dts", "ac3",
}
# Also ordinary words ("Us", "It", "The Den"): noise only next to other noise.
AMBIGUOUS_NOISE = {"us", "it", "den", "dan", "es", "de", "se", "le", "en", "fi", "fn", "ce"}
NOISE_TOKEN = re.compile(
    r"(?:d|f|g|r|cd|bd|dvd|disc|disk|en|eng|nor|upt|side)\d+[a-z]?"  # D1, F1, G51, R2, DVD3, EN1, UPT2
    r"|[a-z]\d+[a-z]"  # D6A
    r"|\d+x\d+"  # 16X9
    r"|(?:uk|usa|us|aus|eu|scan|scn|nordic|nor|swe|dk|fin){2,}"  # UKAUSSCAN
)
LEADING_NOISE = {"dvd", "bd", "bluray", "disc", "disk"}
ROMAN = {"ii": 2, "iii": 3, "iv": 4, "vi": 6, "vii": 7, "viii": 8, "ix": 9}
FOLDED_LETTERS = str.maketrans({"ø": "o", "æ": "ae", "œ": "oe", "ß": "ss", "ð": "d", "þ": "th", "ł": "l", "đ": "d"})

# How closely a candidate's title matches the disc, best first.
EXACT, SEQUEL, CLOSE, PARTIAL, UNRELATED = range(5)


def _label_words(label: str) -> list[str]:
    text = HASH_SUFFIX.sub("", label.strip())
    # Brackets only set an edition apart: "The Karate Kid (Special Edition)".
    text = re.sub(r"[_.<>()\[\]{}#|*]+", " ", text)
    return [word for word in text.split() if any(character.isalnum() for character in word)]


def _is_noise(word: str) -> bool:
    lowered = word.casefold().strip(",:").replace("-", "")
    return lowered in NOISE_WORDS or bool(NOISE_TOKEN.fullmatch(lowered))


def _without_trailing_noise(words: list[str]) -> list[str]:
    start = len(words)
    while start > 0 and (_is_noise(words[start - 1]) or words[start - 1].isdigit()):
        start -= 1
    # A number before the noise is the title's: ALVIN_2_DVD3 is Alvin 2.
    while start < len(words) and words[start].isdigit():
        start += 1
    run = words[start:]
    if not run or start == 0:
        return words
    if len(run) == 1 and run[0].casefold() in AMBIGUOUS_NOISE:
        return words
    return words[:start]


def title_from_label(label: str) -> tuple[str, str]:
    """The film title and year a disc label spells out, without the publisher's additions.

    ``GUARDIANS_OF_THE_GALAXY_G51`` is "Guardians Of The Galaxy", ``<THE_SMURFS>#6E3C``
    "The Smurfs", ``Karate Kid, The`` "The Karate Kid" and ``ROBIN_HOOD_2010`` "Robin
    Hood" from 2010. A number that cannot be a release year stays in the title, so
    ``BLADE_RUNNER_2049`` and ``1917`` keep theirs.
    """
    words = _label_words(label)
    year = ""
    latest = datetime.now(UTC).year + 1
    for index in range(len(words) - 1, 0, -1):
        if YEAR_WORD.fullmatch(words[index]) and int(words[index]) <= latest:
            year = words.pop(index)
            break
    while len(words) > 1 and words[0].casefold() in LEADING_NOISE:
        words.pop(0)
    words = _without_trailing_noise(words)
    if len(words) > 1 and words[-1].casefold() in ARTICLES:
        words.insert(0, words.pop())
        words[-1] = words[-1].rstrip(",")
    split: list[str] = []
    for word in words:
        attached = ATTACHED_NUMBER.match(word)
        split.extend(attached.groups() if attached else [word])
    title = " ".join(word for word in split if word not in {"-", ","}).strip(" -,:")
    # Labels in capitals get ordinary capitals; a name the disc wrote itself keeps its own.
    if title == title.upper():
        title = title.title()
    return title, year


def label_is_generic(label: str) -> bool:
    """Whether a disc label names no film at all, such as ``DVD_VIDEO`` or ``New``."""
    words = [word.casefold() for word in _label_words(label)]
    return all(word in GENERIC_WORDS or word.isdigit() or len(word) == 1 for word in words)


def label_is_code(label: str) -> bool:
    """A publisher's catalogue code such as ``SBO0EXW1`` or ``GFO-0E-XW1_DES``: specific, but no title."""
    words = [word.casefold().replace("-", "") for word in _label_words(label)]
    codes = [word for word in words if re.search(r"[a-z]\d+[a-z]|\d[a-z]+\d", word) and not _is_noise(word)]
    return bool(codes) and all(word in codes or _is_noise(word) or len(word) <= 3 for word in words)


def _fold(value: str) -> str:
    text = value.casefold().replace("&", " and ").replace("'", "").replace("’", "")
    text = unicodedata.normalize("NFKD", text.translate(FOLDED_LETTERS))
    return "".join(character for character in text if not unicodedata.combining(character))


def _tokens(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", _fold(value))


def _keys(value: str) -> set[str]:
    """Ways of writing a title that all mean the same film.

    Case, punctuation and accents never matter ("Amélie", "AMELIE"), "&" is "and"
    or nothing at all ("LILOSTITCH"), a leading or trailing article is optional
    ("MATRIX", "Karate Kid, The") and II is 2.
    """
    words = _tokens(value)
    if not words:
        return set()
    variants = [words, [str(ROMAN.get(word, word)) for word in words]]
    variants += [[word for word in variant if word != "and"] for variant in list(variants)]
    keys: set[str] = set()
    for variant in variants:
        if not variant:
            continue
        keys.add("".join(variant))
        if len(variant) > 1 and variant[0] in ARTICLES:
            keys.add("".join(variant[1:]))
        if len(variant) > 1 and variant[-1] in ARTICLES:
            keys.add("".join(variant[:-1]))
    return keys


def _title_key(value: str) -> str:
    return "".join(_tokens(value))


def _significant(value: str) -> set[str]:
    return {word for word in _tokens(value) if word not in STOPWORDS}


def _match(label_title: str, candidate_title: str) -> tuple[int, float]:
    """How well a candidate's title fits the label (EXACT, CLOSE, PARTIAL or UNRELATED), and
    the share of the label's significant words it contains."""
    label_keys, candidate_keys = _keys(label_title), _keys(candidate_title)
    if label_keys & candidate_keys:
        return EXACT, 1.0
    label_words, candidate_words = _significant(label_title), _significant(candidate_title)
    if not label_words:
        return UNRELATED, 0.0
    overlap = len(label_words & candidate_words)
    coverage = overlap / len(label_words)
    joined = _title_key(label_title)
    # A label cut short, several words written as one (ISACELLIOT), or a series' name on its
    # film (DUCKTALES for "DuckTales the Movie"): the published title starts with it.
    if (len(label_words) >= 2 or len(joined) >= 6) and any(
        key.startswith(label_key) for key in candidate_keys for label_key in label_keys
    ):
        return CLOSE, 1.0
    # Every word of the label is in a title not much longer: "Charlie And The Chocolate Factory",
    # "Kaptein Sabeltann" for SABELTANN, but not "Things to Do in Denver When You're Dead" for DENVER.
    if coverage == 1.0 and overlap / max(1, len(candidate_words)) >= 0.5:
        return CLOSE, coverage
    return (PARTIAL, coverage) if overlap else (UNRELATED, 0.0)


def runtime_fits(runtime_minutes: float, disc_minutes: list[float]) -> bool:
    """Whether a film of this published running time could be one of the titles on the disc.

    PAL discs play about four percent fast, so both clocks count. Fifteen percent
    either way allows for a different cut of the same film without letting a
    short extra pass for the feature.
    """
    if runtime_minutes <= 0:
        return False
    targets = (runtime_minutes, runtime_minutes * 24 / 25)
    return any(abs(minutes - target) <= target * 0.15 for minutes in disc_minutes for target in targets)


def _runtime_distance(runtime_minutes: float, disc_minutes: list[float]) -> float:
    """How far a running time is from the closest title on the disc, as a share of it (either clock)."""
    if runtime_minutes <= 0 or not disc_minutes:
        return 1.0
    targets = (runtime_minutes, runtime_minutes * 24 / 25)
    return min(abs(minutes - target) / target for minutes in disc_minutes for target in targets)


def _spellings(title: str) -> list[str]:
    """The title as the disc label wrote it, and the spellings OMDb is likely to use instead.

    Labels cannot hold "&" or apostrophes, and OMDb's search is literal about both:
    "Cats And Dogs" finds a 1932 cartoon, "A Knights Tale" finds nothing at all.
    """
    words = title.split()
    variants = [title]
    ampersand = " ".join("&" if word.casefold() == "and" else word for word in words)
    if ampersand != title:
        variants.append(ampersand)
    possessive = " ".join(
        f"{word[:-1]}'s"
        if index < len(words) - 1 and len(word) > 3 and word[-1:] in {"s", "S"} and not word.lower().endswith("ss")
        else word
        for index, word in enumerate(words)
    )
    if possessive != title:
        variants.append(possessive)
    return variants


def _sequence_hint(title: str) -> tuple[str, int]:
    """``("Ice Age", 4)`` for "Ice Age 4"; a trailing Roman numeral counts too ("Balto II")."""
    words = title.split()
    if len(words) < 2:
        return title, 0
    last = words[-1].casefold()
    number = int(last) if last.isdigit() and len(last) <= 2 else ROMAN.get(last, 0)
    return (" ".join(words[:-1]), number) if number else (title, 0)


def _year_number(value: str) -> int:
    match = re.match(r"(?:19|20)\d{2}", value)
    return int(match.group(0)) if match else 9999


def _year_score(year: str, candidate: MetadataCandidate) -> int:
    """A label's year is often the disc's release, a year after the film's."""
    if not year:
        return 0
    if candidate.year.startswith(year[:4]):
        return 2
    return 1 if year[:4].isdigit() and candidate.year.startswith(str(int(year[:4]) - 1)) else 0


def _series_root(title: str) -> str:
    """What a film series' entries share: "Ice Age" for "Ice Age: Continental Drift"."""
    head = re.split(r":| - ", title, maxsplit=1)[0]
    words = _tokens(head)
    if len(words) > 1 and (words[-1].isdigit() or words[-1] in ROMAN):
        words = words[:-1]
    return "".join(words)


def _sequel_pick(
    title: str, candidates: list[MetadataCandidate], disc_minutes: list[float] | None = None
) -> MetadataCandidate | None:
    """The film a numbered label means: ICE_AGE_4 is the fourth Ice Age film.

    Published titles rarely carry the number ("Ice Age: Continental Drift"), so the
    films starting with the label's words are grouped by series, short films and
    specials that cannot be on the disc are left out, and the series with enough
    films is counted in order of release.
    """
    base, number = _sequence_hint(title)
    base_keys = _keys(base)
    if number <= 0 or not base_keys:
        return None
    groups: dict[str, list[MetadataCandidate]] = {}
    for candidate in candidates:
        if candidate.media_kind != MediaKind.MOVIE or _year_number(candidate.year) == 9999:
            continue
        if not any(key.startswith(base_key) for key in _keys(candidate.title) for base_key in base_keys):
            continue
        minutes = candidate.runtime_minutes
        if disc_minutes:
            # Only films the disc could hold are counted, so a TV special of unknown
            # length does not take the place of the third film.
            if not runtime_fits(minutes, disc_minutes):
                continue
        elif minutes and minutes < 60:
            continue
        groups.setdefault(_series_root(candidate.title), []).append(candidate)
    series = [films for films in groups.values() if len(films) >= number]
    if not series:
        return None
    chosen = max(series, key=lambda films: (len(films), -min(_year_number(film.year) for film in films)))
    return sorted(chosen, key=lambda film: (_year_number(film.year), film.title))[number - 1]


def _numbered_title_found(title: str, candidates: list[MetadataCandidate]) -> bool:
    """Whether a close match already carries the label's number: "Star Wars: Episode IV - A New Hope"."""
    _, number = _sequence_hint(title)
    if not number:
        return False
    spelled = {str(number), *(roman for roman, value in ROMAN.items() if value == number)}
    return any(
        candidate.media_kind == MediaKind.MOVIE
        and _match(title, candidate.title)[0] <= CLOSE
        and spelled & set(_tokens(candidate.title))
        for candidate in candidates
    )


def _rank_candidates(title: str, year: str, candidates: list[MetadataCandidate]) -> list[MetadataCandidate]:
    """OMDb's candidates for a disc label, most likely first."""
    sequel = None if _numbered_title_found(title, candidates) else _sequel_pick(title, candidates)
    order = {candidate.provider_id: index for index, candidate in enumerate(candidates)}

    def rank(candidate: MetadataCandidate) -> tuple[int, int, float, int]:
        tier, coverage = _match(title, candidate.title)
        if sequel is not None and candidate.provider_id == sequel.provider_id and tier != EXACT:
            tier = SEQUEL
        return tier, -_year_score(year, candidate), -coverage, order[candidate.provider_id]

    return sorted(candidates, key=rank)


def _choose_candidate(
    title: str, year: str, candidates: list[MetadataCandidate]
) -> MetadataCandidate | None:
    ranked = _rank_candidates(title, year, candidates)
    return ranked[0] if ranked else None


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
        if label_is_generic(label):
            # Every "DVD_VIDEO" disc is a different film; one correction says nothing about the next.
            return None
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
        for attempt in range(len(OMDB_RETRY_SECONDS) + 1):
            try:
                response = await self.client.get("https://www.omdbapi.com/", params={"apikey": key, **params})
                if response.status_code in {429, 500, 502, 503, 504} and attempt < len(OMDB_RETRY_SECONDS):
                    await asyncio.sleep(OMDB_RETRY_SECONDS[attempt])
                    continue
                response.raise_for_status()
                payload = response.json()
                break
            except httpx.TransportError as error:
                # A dropped connection or a timeout is worth another try; a refused key is not.
                if attempt < len(OMDB_RETRY_SECONDS):
                    await asyncio.sleep(OMDB_RETRY_SECONDS[attempt])
                    continue
                raise RuntimeError("OMDb could not be reached or returned an invalid response") from error
            except (httpx.HTTPError, ValueError) as error:
                raise RuntimeError("OMDb could not be reached or returned an invalid response") from error
        if not isinstance(payload, dict):
            return {"Response": "False", "Error": "OMDb returned an invalid response"}
        if (
            str(payload.get("Response")).lower() == "true"
            or str(payload.get("Error") or "").casefold() in CACHEABLE_ERRORS
        ):
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
        votes = re.sub(r"\D", "", str(source.get("imdbVotes") or ""))
        return MetadataCandidate(
            provider="omdb",
            provider_id=str(source.get("imdbID") or item.get("imdbID") or ""),
            title=str(source.get("Title") or item.get("Title") or ""),
            year=str(source.get("Year") or item.get("Year") or ""),
            media_kind=media_kind,
            poster_url="" if poster == "N/A" else poster,
            plot="" if source.get("Plot") == "N/A" else str(source.get("Plot") or ""),
            runtime_minutes=int(runtime_match.group(1)) if runtime_match else 0,
            votes=int(votes) if votes else 0,
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
        return [self._candidate(item) for item in payload.get("Search", [])[:10] if isinstance(item, dict)]

    async def omdb_by_id(self, imdb_id: str) -> MetadataCandidate | None:
        payload = await self._omdb_request({"i": imdb_id, "plot": "short"})
        return self._candidate(payload, payload) if str(payload.get("Response")).lower() == "true" else None

    async def identify(
        self,
        label: str,
        media_kind: MediaKind | None = None,
        disc_minutes: list[float] | None = None,
        disc_name: str = "",
    ) -> MetadataCandidate | None:
        """The film a disc most likely is, or None when nothing fits well enough to rip under its name.

        ``disc_name`` is the name MakeMKV read from the disc itself; a Blu-ray's is
        usually the published title ("Alice in Wonderland" for ALICEINWONDERLAND), so
        it is searched first. With ``disc_minutes``, the lengths of the titles on the
        disc, a candidate whose published running time fits none of them is passed
        over for one that does: a label that names a short and a feature alike must
        not end up as the short, with an extra ripped to match it. A loose match,
        sharing only some of the label's words, is only taken when the disc confirms
        it; otherwise the disc waits for someone to pick the title. OMDb being
        unreachable never raises: the lookup then uses what it has, or finds nothing.
        """
        learned = self._learned_label_match(label) if label else None
        if learned:
            return learned
        names: list[tuple[str, str]] = []
        for raw in (disc_name, label):
            if not raw or label_is_generic(raw) or label_is_code(raw):
                continue
            title, year = title_from_label(raw)
            if title and not any(_keys(title) & _keys(known) for known, _ in names):
                names.append((title, year))
        if not names:
            return None
        # The film is among the disc's long titles; a short extra must not confirm a TV episode.
        longest = max(disc_minutes or [0])
        features = [minutes for minutes in disc_minutes or [] if minutes >= longest / 2]
        lookup = _Lookup(self, names, media_kind, features)
        return await lookup.run()

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


class _Lookup:
    """One identification: the searches for each name a disc goes by, then the choice."""

    def __init__(
        self,
        service: MetadataService,
        names: list[tuple[str, str]],
        media_kind: MediaKind | None,
        disc_minutes: list[float],
    ):
        self.service = service
        self.names = names
        self.media_kind = media_kind
        self.disc_minutes = disc_minutes
        self.candidates: dict[str, MetadataCandidate] = {}
        self.details: dict[str, MetadataCandidate] = {}
        self.seen: set[tuple[str, str]] = set()
        self.offline = False

    async def search(self, query: str, year: str = "") -> list[MetadataCandidate]:
        if self.offline or (query, year) in self.seen or not query.strip():
            return []
        self.seen.add((query, year))
        try:
            results = await self.service.search_omdb(query, year, self.media_kind)
        except (httpx.HTTPError, RuntimeError, ValueError):
            # OMDb is unreachable: stop asking, and choose from what came back so far.
            self.offline = True
            return []
        # OMDb also lists video games ("Ice Age 2: The Meltdown"), which no disc here holds.
        results = [result for result in results if result.provider_id and result.media_kind != MediaKind.UNKNOWN]
        for result in results:
            self.candidates.setdefault(result.provider_id, result)
        return results

    async def detail(self, candidate: MetadataCandidate) -> MetadataCandidate:
        if candidate.provider_id in self.details:
            return self.details[candidate.provider_id]
        found = None
        if not self.offline:
            try:
                found = await self.service.omdb_by_id(candidate.provider_id)
            except (httpx.HTTPError, RuntimeError, ValueError):
                self.offline = True
        self.details[candidate.provider_id] = found or candidate
        return self.details[candidate.provider_id]

    def found_exact(self, title: str) -> bool:
        return any(_match(title, candidate.title)[0] == EXACT for candidate in self.candidates.values())

    async def gather(self, title: str, year: str) -> None:
        """Search until OMDb returns the title itself, trying what labels cannot spell."""
        years = [year, str(int(year) - 1), ""] if year else [""]
        # The label's own words and the "&" spelling both, so the right film is among
        # the candidates even when a different one carries the label's exact words.
        # The apostrophe is a guess, tried only when nothing matched yet.
        for index, spelling in enumerate(_spellings(title)):
            if index > 0 and "'" in spelling and self.found_exact(title):
                continue
            for candidate_year in years:
                results = await self.search(spelling, candidate_year)
                if any(_match(title, result.title)[0] == EXACT for result in results):
                    break
        if self.found_exact(title):
            return
        words = title.split()
        fallbacks: list[str] = []
        base, number = _sequence_hint(title)
        if number:
            fallbacks.append(base)
        shorter = list(words)
        while len(shorter) > 2:
            shorter.pop()
            fallbacks.append(" ".join(shorter))
        for query in fallbacks:
            await self.search(query)
            if self.found_exact(title):
                return
        joined = _title_key(title)
        if len(words) == 1 and joined.isalpha() and len(joined) >= 8:
            # One word that is really several: PELLEPOLITIBIL, ISACELLIOT.
            for length in range(4, min(8, len(joined) - 2)):
                await self.search(words[0][:length])
                if any(_match(title, candidate.title)[0] <= CLOSE for candidate in self.candidates.values()):
                    return

    async def widen(self, title: str) -> None:
        """More searches for a disc whose running times fit nothing found so far.

        "Never Ending Story" is a 2012 short on OMDb; the film is "The NeverEnding
        Story", which only a search with the two words joined finds.
        """
        words = title.split()
        base, number = _sequence_hint(title)
        if number:
            await self.search(base)
        if 2 <= len(words) <= 4 and not number:
            for index in range(len(words) - 1):
                together = [*words[:index], words[index] + words[index + 1].lower(), *words[index + 2 :]]
                await self.search(" ".join(together))
        significant = [word for word in words if word.casefold() not in STOPWORDS]
        if 2 <= len(significant) < len(words):
            await self.search(" ".join(significant))
        shorter = list(words)
        while len(shorter) > 2:
            shorter.pop()
            await self.search(" ".join(shorter))

    def rank(self, candidate: MetadataCandidate, sequels: set[str]) -> tuple[int, float, int]:
        """The best (tier, coverage, year score) any of the disc's names gives a candidate."""
        best = (UNRELATED, 0.0, 0)
        for title, year in self.names:
            tier, coverage = _match(title, candidate.title)
            if candidate.provider_id in sequels and tier != EXACT:
                tier = SEQUEL
            score = (tier, coverage, _year_score(year, candidate))
            if (score[0], -score[1], -score[2]) < (best[0], -best[1], -best[2]):
                best = score
        return best

    def confident(self, tier: int, coverage: float) -> bool:
        if tier < PARTIAL:
            return True
        # A loose match needs most of a label of several words, and the disc to agree.
        words = max(len(_significant(title)) for title, _ in self.names)
        return bool(self.disc_minutes) and words >= 2 and coverage >= 2 / 3

    async def run(self) -> MetadataCandidate | None:
        for title, year in self.names:
            await self.gather(title, year)
        choice, confirmed = await self.choose()
        # A loose fit is worth a wider search: the 2012 short "Never Ending Story" runs 108
        # minutes, near enough to the disc's 90, but "The NeverEnding Story" runs 94.
        loose = choice is not None and _runtime_distance(choice.runtime_minutes, self.disc_minutes) > 0.05
        if self.disc_minutes and (not confirmed or loose) and not self.offline:
            for title, _ in self.names:
                await self.widen(title)
            wider, wider_confirmed = await self.choose()
            if wider_confirmed or choice is None:
                choice = wider
        return choice

    def confirmed(self, candidate: MetadataCandidate) -> bool:
        """Whether one of the disc's titles runs as long as OMDb says the film does."""
        return not self.disc_minutes or runtime_fits(candidate.runtime_minutes, self.disc_minutes)

    def unconfirmed_ok(self, candidate: MetadataCandidate, tier: int) -> bool:
        """A film whose running time OMDb does not know can still be taken on its name alone,
        if that name matches well: the title itself, or most of a label of several words."""
        if candidate.runtime_minutes > 0:
            return False
        words = max(len(_significant(title)) for title, _ in self.names)
        return tier in {EXACT, SEQUEL} or (tier == CLOSE and words >= 2)

    async def sequels(self, candidates: list[MetadataCandidate]) -> set[str]:
        """The films numbered labels mean, when no film on OMDb carries the number itself."""
        picks: set[str] = set()
        for title, _ in self.names:
            base, number = _sequence_hint(title)
            if not number or self.found_exact(title):
                continue
            spelled = {str(number), *(roman for roman, value in ROMAN.items() if value == number)}
            numbered = [
                candidate
                for candidate in candidates
                if candidate.media_kind == MediaKind.MOVIE
                and _match(title, candidate.title)[0] <= CLOSE
                and spelled & set(_tokens(candidate.title))
            ]
            # "Star Wars: Episode IV - A New Hope" carries its number. "Ice Age 3: T4 Movie
            # Special" does too, but is no feature film, so the series is counted after all.
            numbered_films = [await self.detail(candidate) for candidate in numbered[:MAX_RUNTIME_CHECKS]]
            if any(film.runtime_minutes > 0 and self.confirmed(film) for film in numbered_films):
                continue
            # Counting a series needs to know which of its entries are feature films.
            members = sorted(
                (
                    candidate
                    for candidate in candidates
                    if candidate.media_kind == MediaKind.MOVIE
                    and any(key.startswith(base_key) for key in _keys(candidate.title) for base_key in _keys(base))
                ),
                key=lambda candidate: _year_number(candidate.year),
            )
            detailed = [await self.detail(candidate) for candidate in members[:MAX_FRANCHISE_CHECKS]]
            pick = _sequel_pick(title, detailed, self.disc_minutes)
            if pick is not None:
                picks.add(pick.provider_id)
        return picks

    async def choose(self) -> tuple[MetadataCandidate | None, bool]:
        """The best candidate, and whether the disc's running times confirm it.

        Films the disc confirms come first, however their names compare with one the
        disc does not: a DUCKTALES disc holding a 74-minute film is "DuckTales the
        Movie", not the TV series named exactly DuckTales. Only when the disc confirms
        nothing is a film of unknown length taken on a good match of its name. A film
        the disc contradicts is never taken: better to ask than to rip the wrong titles.
        """
        if not self.candidates:
            return None, False
        candidates = list(self.candidates.values())
        sequels = await self.sequels(candidates)
        order = {provider_id: index for index, provider_id in enumerate(self.candidates)}
        ranks = {candidate.provider_id: self.rank(candidate, sequels) for candidate in candidates}
        ranked = sorted(
            candidates,
            key=lambda candidate: (
                ranks[candidate.provider_id][0],
                -ranks[candidate.provider_id][1],
                -ranks[candidate.provider_id][2],
                order[candidate.provider_id],
            ),
        )
        def best(choices: list[MetadataCandidate]) -> MetadataCandidate:
            return max(
                choices,
                key=lambda candidate: (
                    ranks[candidate.provider_id][2],
                    ranks[candidate.provider_id][1],
                    # Of films with the same name, the one as long as the disc's and the one
                    # people know: ten times the votes outweigh three percent of running time.
                    # The Karate Kid of 2010 (the disc's length), not 1984; the Robin Hood of
                    # 2010 for its extended cut, not an unknown 2025 film of the cut's length.
                    math.log10(candidate.votes + 1)
                    - _runtime_distance(candidate.runtime_minutes, self.disc_minutes) / 0.03,
                ),
            )

        tiers: list[tuple[int, list[MetadataCandidate]]] = []
        for tier in (EXACT, SEQUEL, CLOSE, PARTIAL):
            group = [candidate for candidate in ranked if ranks[candidate.provider_id][0] == tier]
            if not group:
                continue
            # Details are looked up one kind of match at a time, and only until the disc confirms one.
            detailed = [await self.detail(candidate) for candidate in group[:MAX_RUNTIME_CHECKS]]
            confident = [c for c in detailed if self.confident(tier, ranks[c.provider_id][1])]
            tiers.append((tier, confident))
            confirmed = [c for c in confident if c.runtime_minutes > 0 and self.confirmed(c)]
            if confirmed or (not self.disc_minutes and confident):
                return best(confirmed or confident), True
        for tier, group in tiers:
            unconfirmed = [candidate for candidate in group if self.unconfirmed_ok(candidate, tier)]
            if unconfirmed:
                return best(unconfirmed), False
        return None, False
