from __future__ import annotations

import httpx
import pytest

from discdock.musicbrainz import (
    MusicBrainzBusy,
    MusicBrainzClient,
    barcode_query,
    parse_release,
    search_query,
)

DISCID = "ybzdi.je6cuccUFvEROdOIHCLlQ-"
FIRST = "403427d8-6201-4831-a346-f7d910eead70"
SECOND = "338e72dc-1cf5-4757-b874-a8cab8aa877a"


def release_json(release_id: str, disambiguation: str, date: str) -> dict:
    return {
        "id": release_id,
        "title": "Into the Great Wide Open",
        "date": date,
        "country": "XE",
        "disambiguation": disambiguation,
        "artist-credit": [
            {"name": "Tom Petty", "joinphrase": " and ", "artist": {"id": "5ca3f318-d028-4151-ac73-78e2b2d6cdcc"}},
            {"name": "The Heartbreakers", "joinphrase": "", "artist": {"id": "c7ea3ec3-1f31-4d77-bd8b-7a4e8a1fc2fd"}},
        ],
        "label-info": [{"label": {"name": "MCA"}}, {"label": None}, {"label": {"name": "MCA"}}],
        "media": [
            {
                "position": 1,
                "format": "CD",
                "track-count": 2,
                "discs": [{"id": DISCID}],
                "tracks": [
                    {"id": "track-1", "position": 1, "title": "Learning to Fly", "length": 242000, "recording": {"id": "recording-1"}},
                    {"id": "track-2", "position": 2, "title": "Kings Highway", "length": 184000, "recording": {"id": "recording-2"}},
                ],
            }
        ],
    }


def client_for(handler) -> MusicBrainzClient:
    return MusicBrainzClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)), spacing_seconds=0, retry_seconds=(0, 0)
    )


@pytest.mark.asyncio
async def test_a_cd_is_looked_up_by_its_discid() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"releases": [release_json(FIRST, "BIEM / MCPS", "1991"), release_json(SECOND, "GEMA / BIEM", "1991-07")]},
        )

    releases = await client_for(handler).releases_for_disc(DISCID, 2)

    assert requests[0].url.path == f"/ws/2/discid/{DISCID}"
    assert "recordings" in str(requests[0].url)
    assert requests[0].headers["User-Agent"].startswith("DiscDock/"), "MusicBrainz turns anonymous clients away first"
    assert [release.id for release in releases] == [FIRST, SECOND]
    first = releases[0]
    assert (first.artist, first.label, first.year, first.disambiguation) == (
        "Tom Petty and The Heartbreakers",
        "MCA",
        "1991",
        "BIEM / MCPS",
    )
    assert [(track.position, track.title, track.length_seconds) for track in first.tracks] == [
        (1, "Learning to Fly", 242),
        (2, "Kings Highway", 184),
    ]
    assert "tracks" not in first.summary()


@pytest.mark.asyncio
async def test_a_busy_musicbrainz_is_asked_again() -> None:
    answers = [httpx.Response(503), httpx.Response(200, json={"releases": [release_json(FIRST, "", "1991")]})]

    releases = await client_for(lambda request: answers.pop(0)).releases_for_disc(DISCID)

    assert [release.id for release in releases] == [FIRST]
    assert not answers


@pytest.mark.asyncio
async def test_musicbrainz_that_stays_busy_is_reported_with_its_status() -> None:
    with pytest.raises(MusicBrainzBusy) as caught:
        await client_for(lambda request: httpx.Response(503)).releases_for_disc(DISCID)

    assert caught.value.status == 503


@pytest.mark.asyncio
async def test_a_cd_musicbrainz_does_not_know_has_no_releases() -> None:
    assert await client_for(lambda request: httpx.Response(404)).releases_for_disc(DISCID) == []


def test_the_medium_of_a_set_is_the_one_with_the_cds_discid() -> None:
    release = release_json(FIRST, "", "1991")
    second_disc = {
        "position": 2,
        "format": "CD",
        "track-count": 1,
        "discs": [{"id": "another-disc-id"}],
        "tracks": [{"id": "track-3", "position": 1, "title": "Bonus", "recording": {"id": "recording-3"}}],
    }
    release["media"].append(second_disc)

    parsed = parse_release(release, "another-disc-id", 1)

    assert (parsed.disc_number, parsed.disc_count, [track.title for track in parsed.tracks]) == (2, 2, ["Bonus"])


def test_searches_for_artist_and_album_or_for_any_words() -> None:
    assert search_query("Tom Petty - Into the Great Wide Open") == (
        'release:"Into the Great Wide Open" AND artist:"Tom Petty"'
    )
    assert search_query("AC/DC  Back in Black") == r"release:(AC\/DC Back in Black) OR artist:(AC\/DC Back in Black)"


@pytest.mark.asyncio
async def test_a_cd_costs_one_request_to_musicbrainz() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"releases": [release_json(FIRST, "", "1991")]})

    client = client_for(handler)
    await client.releases_for_disc(DISCID, 2)
    again = await client.releases_for_disc(DISCID, 2)
    for_the_tags = await client.release(FIRST, DISCID, 2)

    assert len(requests) == 1
    assert [release.id for release in again] == [FIRST]
    assert [track.title for track in for_the_tags.tracks] == ["Learning to Fly", "Kings Highway"]


@pytest.mark.asyncio
async def test_a_busy_musicbrainz_gets_the_time_it_asks_for() -> None:
    waits: list[float] = []
    answers = [httpx.Response(503, headers={"Retry-After": "20"}), httpx.Response(200, json={"releases": []})]

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    client = MusicBrainzClient(
        httpx.AsyncClient(transport=httpx.MockTransport(lambda request: answers.pop(0))),
        spacing_seconds=0,
        retry_seconds=(10,),
        sleep=sleep,
    )

    assert await client.releases_for_disc(DISCID) == []
    assert waits == [20]


@pytest.mark.asyncio
async def test_a_search_in_the_dashboard_does_not_wait_long_for_a_busy_musicbrainz() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503)

    async def sleep(seconds: float) -> None:
        del seconds

    client = MusicBrainzClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        spacing_seconds=0,
        retry_seconds=(10, 30, 60),
        search_retry_seconds=(4,),
        sleep=sleep,
    )

    with pytest.raises(MusicBrainzBusy):
        await client.search("Tom Petty - Into the Great Wide Open")
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_a_barcode_is_searched_as_printed_and_as_upc_or_ean() -> None:
    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        queries.append(request.url.params["query"])
        return httpx.Response(200, json={"releases": [release_json(FIRST, "", "1991")]})

    releases = await client_for(handler).search_barcode("0 602527 12345 6")

    assert [release.id for release in releases] == [FIRST]
    assert queries == ["barcode:0602527123456 OR barcode:602527123456"]
    assert barcode_query("602527123456") == "barcode:602527123456 OR barcode:0602527123456"
    with pytest.raises(ValueError):
        barcode_query("12345")
