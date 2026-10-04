"""The automatic lookup, checked against labels and answers from real discs."""

from __future__ import annotations

import httpx

import discdock.metadata as metadata_module
from discdock.metadata import (
    CLOSE,
    EXACT,
    PARTIAL,
    MetadataService,
    _match,
    label_is_code,
    label_is_generic,
    title_from_label,
)
from discdock.models import MediaKind, MetadataCandidate


def test_labels_lose_what_publishers_add_but_keep_the_title() -> None:
    assert title_from_label("OBLIVION_G51") == ("Oblivion", "")
    assert title_from_label("_THE_KARATE_KID_#4C4E") == ("The Karate Kid", "")
    assert title_from_label("<FAME>") == ("Fame", "")
    assert title_from_label("Karate Kid, The") == ("The Karate Kid", ""), "MakeMKV's name for a DVD"
    assert title_from_label("WILLOW_UKAUSSCAN_24_PAL") == ("Willow", "")
    assert title_from_label("DVD_ARTHUR_AND_THE_REVENGE_OF_MA") == ("Arthur And The Revenge Of Ma", "")
    assert title_from_label("SIMPSONS_MOVIE_SE_FN") == ("Simpsons Movie", "")
    assert title_from_label("BOXTROLLS_UPT2") == ("Boxtrolls", "")
    assert title_from_label("The Boxtrolls 3D") == ("The Boxtrolls", "")
    assert title_from_label("LILOSTITCH_D6A") == ("Lilostitch", "")
    assert title_from_label("ALVIN_2_DVD3") == ("Alvin 2", ""), "the sequel number before the noise stays"
    assert title_from_label("ICE AGE 3 DVD3") == ("Ice Age 3", "")
    assert title_from_label("ROBIN_HOOD_2010") == ("Robin Hood", "2010")
    assert title_from_label("BLADE_RUNNER_2049") == ("Blade Runner 2049", ""), "no film is from 2049 yet"
    assert title_from_label("1917") == ("1917", ""), "a title that looks like a year is still the title"
    # Words that are noise in a run of noise can be a title's own last word.
    assert title_from_label("DR_NO") == ("Dr No", "")
    assert title_from_label("THE_DEN") == ("The Den", "")
    assert title_from_label("Arn - Tempelriddaren") == ("Arn Tempelriddaren", "")


def test_generic_labels_and_catalogue_codes_name_no_film() -> None:
    for label in ("DVD_VIDEO", "VIDEO_DVD", "DVDVolume", "New", "Del 1", "R_B", ""):
        assert label_is_generic(label), label
    for label in ("FROZEN", "HOP", "UP", "BIG_HERO_6", "SBO0EXW1"):
        assert not label_is_generic(label), label
    for label in ("SBO0EXW1", "AB60EXW1", "GFO-0E-XW1_DES", "MNM-0E-XW1.1_DES"):
        assert label_is_code(label), label
    for label in ("POKEMON2", "BIG_HERO_6", "TRANSFORMERS2_D1_EU", "BOXTROLLS_UPT2"):
        assert not label_is_code(label), label


def test_titles_match_however_a_label_spells_them() -> None:
    assert _match("Matrix", "The Matrix")[0] == EXACT
    assert _match("Karate Kid The", "The Karate Kid")[0] == EXACT
    assert _match("Lilostitch", "Lilo & Stitch")[0] == EXACT
    assert _match("Mr Beans Holiday", "Mr. Bean's Holiday")[0] == EXACT
    assert _match("Amelie", "Amélie")[0] == EXACT
    assert _match("Solvgruvenes Hemmelighet", "Sølvgruvenes hemmelighet")[0] == EXACT
    assert _match("Star Wars Episode 4", "Star Wars Episode IV")[0] == EXACT
    assert _match("Harry Potter Goblet Of Fire", "Harry Potter and the Goblet of Fire")[0] == CLOSE
    assert _match("Isacelliot", "Isac Elliot Dream Big: The Movie")[0] == CLOSE
    assert _match("Ducktales", "DuckTales the Movie: Treasure of the Lost Lamp")[0] == CLOSE
    assert _match("Sabeltann", "Kaptein Sabeltann")[0] == CLOSE
    assert _match("Denver", "Things to Do in Denver When You Are Dead")[0] == PARTIAL


class _Omdb(MetadataService):
    """A small OMDb: titles by search query, details by id, and a record of what was asked."""

    def __init__(self, search: dict[str, list[MetadataCandidate]], detail: list[MetadataCandidate]) -> None:
        self.search = search
        self.detail = {film.provider_id: film for film in detail}
        self.searches: list[str] = []
        self.offline = False

    def _learned_label_match(self, label):
        return None

    async def search_omdb(self, query, year="", media_kind=None):
        if self.offline:
            raise RuntimeError("OMDb could not be reached or returned an invalid response")
        self.searches.append(query)
        # Search results carry no running time or votes; only the details do.
        return [film.model_copy(update={"runtime_minutes": 0, "votes": 0}) for film in self.search.get(query, [])]

    async def omdb_by_id(self, imdb_id):
        return self.detail.get(imdb_id)


def _movie(
    provider_id: str, title: str, year: str, runtime: int, votes: int = 0, kind: MediaKind = MediaKind.MOVIE
) -> MetadataCandidate:
    return MetadataCandidate(
        provider="omdb", provider_id=provider_id, title=title, year=year, media_kind=kind,
        runtime_minutes=runtime, votes=votes,
    )


async def test_the_name_on_a_blu_ray_is_searched_before_its_label() -> None:
    film = _movie("tt1014759", "Alice in Wonderland", "2010", 108, 450_000)
    omdb = _Omdb({"Alice in Wonderland": [film]}, [film])

    found = await omdb.identify("ALICEINWONDERLAND", disc_minutes=[108.5, 4.0], disc_name="Alice in Wonderland")

    assert found is not None and found.provider_id == "tt1014759"
    assert omdb.searches[0] == "Alice in Wonderland"


async def test_a_numbered_label_counts_the_feature_films_of_its_series() -> None:
    films = [
        _movie("ia1", "Ice Age", "2002", 81, 560_000),
        _movie("ia2", "Ice Age: The Meltdown", "2006", 91, 316_000),
        _movie("ia3", "Ice Age: Dawn of the Dinosaurs", "2009", 94, 284_000),
        _movie("tv1", "Ice Age: A Mammoth Christmas", "2011", 26, 12_000),
        _movie("ia4", "Ice Age: Continental Drift", "2012", 88, 241_000),
        _movie("game", "Ice Age 2: The Meltdown", "2006", 0, 2_000, kind=MediaKind.UNKNOWN),
    ]
    omdb = _Omdb({"Ice Age": films}, films)

    fourth = await omdb.identify("ICE_AGE_4", disc_minutes=[84.5, 6.0])
    # The video game carries the number "2" but is no film, so the series is still counted.
    second = await omdb.identify("ICE_AGE_2_SE_D1", disc_minutes=[87.5])

    assert fourth is not None and fourth.provider_id == "ia4"
    assert second is not None and second.provider_id == "ia2"


async def test_a_numbered_title_on_omdb_is_taken_as_it_is() -> None:
    films = [
        _movie("sw1", "Star Wars: Episode I - The Phantom Menace", "1999", 136, 900_000),
        _movie("sw4", "Star Wars: Episode IV - A New Hope", "1977", 121, 1_500_000),
        _movie("sw5", "Star Wars: Episode V - The Empire Strikes Back", "1980", 124, 1_400_000),
    ]
    omdb = _Omdb({"Star Wars Episode Iv": films[1:2], "Star Wars Episode": films}, films)

    found = await omdb.identify("STAR_WARS_EPISODE_IV", disc_minutes=[116.5])

    assert found is not None and found.provider_id == "sw4", "not the fourth Star Wars film released"


async def test_of_films_with_the_same_name_the_one_as_long_as_the_disc_wins() -> None:
    films = [
        _movie("kk1984", "The Karate Kid", "1984", 126, 220_000),
        _movie("kk2010", "The Karate Kid", "2010", 140, 190_000),
    ]
    omdb = _Omdb({"The Karate Kid": films}, films)

    found = await omdb.identify("_THE_KARATE_KID_#4C4E", disc_minutes=[134.4], disc_name="Karate Kid, The")

    assert found is not None and found.provider_id == "kk2010"


async def test_a_well_known_film_beats_an_unknown_one_for_an_extended_cut() -> None:
    films = [
        _movie("rh2010", "Robin Hood", "2010", 140, 294_000),
        _movie("rh2025", "Robinhood", "2025", 150, 1_600),
    ]
    omdb = _Omdb({"Robin Hood": films[:1], "Robinhood": films[1:]}, films)

    found = await omdb.identify("ROBIN_HOOD", disc_minutes=[149.3])

    assert found is not None and found.provider_id == "rh2010", "the disc holds the 2010 film's extended cut"


async def test_a_series_name_on_its_film_finds_the_film_the_disc_holds() -> None:
    films = [
        _movie("dt-series", "DuckTales", "1987-1990", 0, 36_000, kind=MediaKind.SERIES),
        _movie("dt-movie", "DuckTales the Movie: Treasure of the Lost Lamp", "1990", 74, 30_000),
    ]
    omdb = _Omdb({"Ducktales": films}, films)

    found = await omdb.identify("DUCKTALES", disc_minutes=[70.7])

    assert found is not None and found.provider_id == "dt-movie", "the disc confirms the film, not the series"


async def test_a_film_that_fits_only_loosely_sends_the_search_wider() -> None:
    short = _movie("tt2012", "Never Ending Story", "2012", 108, 300)
    film = _movie("tt0088323", "The NeverEnding Story", "1984", 94, 160_000)
    omdb = _Omdb({"Never Ending Story": [short], "Neverending Story": [film]}, [short, film])

    found = await omdb.identify("NEVER_ENDING_STORY", disc_minutes=[90.0])

    assert found is not None and found.provider_id == "tt0088323"


async def test_an_extra_on_the_disc_does_not_confirm_a_tv_series() -> None:
    series = _movie("lotta-tv", "Lotta in Love", "2006-2007", 25, 70, kind=MediaKind.SERIES)
    omdb = _Omdb({"Lotta": [series]}, [series])

    # The disc's film runs 74 minutes; its 28-minute extra is no feature.
    assert await omdb.identify("LOTTA_BMG_SNF_SCN", disc_minutes=[73.7, 27.7]) is None


async def test_a_weak_match_or_a_generic_label_waits_for_a_choice() -> None:
    denver = _movie("tt0114660", "Things to Do in Denver When You Are Dead", "1995", 115, 30_000)
    omdb = _Omdb({"Denver": [denver]}, [denver])

    assert await omdb.identify("DENVER", disc_minutes=[104.0]) is None
    assert await omdb.identify("DVD_VIDEO", disc_minutes=[104.0]) is None
    assert "Dvd Video" not in omdb.searches and "Video" not in omdb.searches, "a generic label is not searched"


async def test_a_film_the_disc_contradicts_is_not_taken() -> None:
    cartoon = _movie("tt0142154", "Cats and Dogs", "1932", 7, 900)
    omdb = _Omdb({"Cats And Dogs": [cartoon]}, [cartoon])

    assert await omdb.identify("CATS_AND_DOGS", disc_minutes=[83.5, 13.9]) is None


async def test_omdb_out_of_reach_leaves_the_disc_unidentified_without_an_error() -> None:
    omdb = _Omdb({}, [])
    omdb.offline = True

    assert await omdb.identify("FROZEN", disc_minutes=[98.0]) is None


class _Secrets:
    def get(self, name: str) -> str:
        return "test-key"


class _CacheTable:
    def __init__(self) -> None:
        self.stored: list[str] = []

    def query(self, sql: str, parameters: tuple = ()) -> list:
        return []

    def execute(self, sql: str, parameters: tuple = ()) -> int:
        self.stored.append(parameters[2])
        return 1


async def test_passing_omdb_errors_are_not_cached_and_dropped_connections_are_retried(monkeypatch) -> None:
    monkeypatch.setattr(metadata_module, "OMDB_RETRY_SECONDS", (0.0, 0.0))
    answers: list[Exception | httpx.Response] = [
        httpx.ConnectError("dropped"),
        httpx.Response(200, json={"Response": "False", "Error": "Error getting data."}),
        httpx.Response(200, json={"Response": "False", "Error": "Movie not found!"}),
    ]
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.params.get("s", ""))
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    cache = _CacheTable()
    service = MetadataService(cache, _Secrets())  # type: ignore[arg-type]
    await service.client.aclose()
    service.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    first = await service._omdb_request({"s": "Frozen"})
    assert first["Error"] == "Error getting data." and len(requests) == 2, "the dropped connection was retried"
    second = await service._omdb_request({"s": "Frozen"})
    await service.close()

    assert second["Error"] == "Movie not found!" and len(requests) == 3, "the passing error was asked again"
    assert len(cache.stored) == 1 and "Movie not found!" in cache.stored[0], "only the lasting answer is kept"
