from __future__ import annotations

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
