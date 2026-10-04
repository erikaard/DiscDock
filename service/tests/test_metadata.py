from __future__ import annotations

from typing import ClassVar

from discdock.metadata import MetadataService, _choose_candidate, title_from_label
from discdock.models import MediaKind, MetadataCandidate


def test_omdb_detail_includes_runtime_for_main_feature_matching() -> None:
    candidate = MetadataService._candidate(
        {"Title": "The Ant Bully", "imdbID": "tt0429589", "Type": "movie"},
        {
            "Title": "The Ant Bully",
            "Year": "2006",
            "imdbID": "tt0429589",
            "Type": "movie",
            "Poster": "https://example.test/poster.jpg",
            "Runtime": "88 min",
        },
    )

    assert candidate.runtime_minutes == 88
    assert candidate.poster_url == "https://example.test/poster.jpg"


def test_drive_label_noise_is_removed_before_searching() -> None:
    assert title_from_label("BARNYARD_EN1") == ("Barnyard", "")
    assert title_from_label("QUANTUM_OF_SOLACE_F1") == ("Quantum Of Solace", "")
    assert title_from_label("TRANSFORMERS2_D1_EU") == ("Transformers 2", "")
    assert title_from_label("BIG_HERO_6") == ("Big Hero 6", "")


def test_numbered_drive_label_can_choose_a_subtitled_sequel() -> None:
    candidates = [
        MetadataCandidate(
            provider="omdb",
            provider_id="first",
            title="Transformers",
            year="2007",
            media_kind=MediaKind.MOVIE,
        ),
        MetadataCandidate(
            provider="omdb",
            provider_id="third",
            title="Transformers: Dark of the Moon",
            year="2011",
            media_kind=MediaKind.MOVIE,
        ),
        MetadataCandidate(
            provider="omdb",
            provider_id="second",
            title="Transformers: Revenge of the Fallen",
            year="2009",
            media_kind=MediaKind.MOVIE,
        ),
    ]

    selected = _choose_candidate("Transformers 2", "", candidates)

    assert selected is not None
    assert selected.provider_id == "second"


def _film(provider_id: str, title: str, year: str, runtime: int) -> MetadataCandidate:
    return MetadataCandidate(
        provider="omdb", provider_id=provider_id, title=title, year=year,
        media_kind=MediaKind.MOVIE, runtime_minutes=runtime,
    )


class _FakeOmdb(MetadataService):
    """OMDb as it answered for these labels, so the tests need no network."""

    SEARCH: ClassVar[dict[str, list[MetadataCandidate]]] = {
        # "CATS_AND_DOGS": the label's words find only a seven-minute cartoon from 1932.
        "Cats And Dogs": [_film("tt9356440", "Raining Cats and Dogs!", "2019", 0),
                          _film("tt0142154", "Cats and Dogs", "1932", 0)],
        "Cats & Dogs": [_film("tt0239395", "Cats & Dogs", "2001", 0),
                        _film("tt0117979", "The Truth About Cats & Dogs", "1996", 0)],
        # "A_KNIGHTS_TALE": without the apostrophe OMDb finds nothing at all.
        "A Knights Tale": [],
        "A Knight's Tale": [_film("tt0183790", "A Knight's Tale", "2001", 0)],
        "A Knights": [_film("tt0397931", "Bob the Builder: The Knights of Fix-A-Lot", "2003", 0)],
    }
    DETAIL: ClassVar[dict[str, MetadataCandidate]] = {
        "tt0142154": _film("tt0142154", "Cats and Dogs", "1932", 7),
        "tt0239395": _film("tt0239395", "Cats & Dogs", "2001", 87),
        "tt9356440": _film("tt9356440", "Raining Cats and Dogs!", "2019", 12),
        "tt0117979": _film("tt0117979", "The Truth About Cats & Dogs", "1996", 97),
        "tt0183790": _film("tt0183790", "A Knight's Tale", "2001", 132),
        "tt0397931": _film("tt0397931", "Bob the Builder: The Knights of Fix-A-Lot", "2003", 44),
    }

    def __init__(self) -> None:
        self.searches: list[str] = []

    def _learned_label_match(self, label):
        return None

    async def search_omdb(self, query, year="", media_kind=None):
        self.searches.append(query)
        return list(self.SEARCH.get(query, []))

    async def omdb_by_id(self, imdb_id):
        return self.DETAIL.get(imdb_id)


async def test_a_label_that_also_names_a_short_finds_the_film_on_the_disc() -> None:
    omdb = _FakeOmdb()

    # The titles on the other user's Cats & Dogs DVD: the PAL feature and a featurette.
    found = await omdb.identify("CATS_AND_DOGS", disc_minutes=[83.5, 13.9])

    assert found is not None and (found.title, found.year) == ("Cats & Dogs", "2001")
    assert "Cats & Dogs" in omdb.searches, "the & spelling is searched even though the label's words matched"


async def test_a_label_without_its_apostrophe_still_finds_the_film() -> None:
    omdb = _FakeOmdb()

    found = await omdb.identify("A_KNIGHTS_TALE", disc_minutes=[126.5, 15.0])

    assert found is not None and (found.title, found.year) == ("A Knight's Tale", "2001")
    assert "A Knights" not in omdb.searches, "the real title was found before shortening the label"


async def test_without_the_disc_an_exact_title_is_still_preferred() -> None:
    omdb = _FakeOmdb()

    # The first lookup runs while MakeMKV scans and cannot see the disc yet.
    found = await omdb.identify("CATS_AND_DOGS")

    assert found is not None and found.title in {"Cats and Dogs", "Cats & Dogs"}


def test_a_published_running_time_is_checked_against_the_disc() -> None:
    from discdock.metadata import runtime_fits

    assert runtime_fits(87, [83.5, 13.9]), "PAL plays 4% fast: 87 minutes becomes 83.5"
    assert runtime_fits(132, [126.5, 15.0])
    assert not runtime_fits(7, [83.5, 13.9]), "a seven-minute cartoon is not on a feature disc"
    assert not runtime_fits(44, [126.5, 15.0]), "nor is a 44-minute Bob the Builder"
    assert not runtime_fits(0, [90.0]), "an unknown running time fits nothing"


def test_spellings_add_what_labels_cannot_hold() -> None:
    from discdock.metadata import _spellings

    assert _spellings("Cats And Dogs") == ["Cats And Dogs", "Cats & Dogs", "Cat's And Dogs"]
    assert _spellings("A Knights Tale") == ["A Knights Tale", "A Knight's Tale"]
    assert _spellings("The Smurfs") == ["The Smurfs"], "a plural at the end is a plural"
    assert _spellings("Mass Effect") == ["Mass Effect"], "double s is not a possessive"
