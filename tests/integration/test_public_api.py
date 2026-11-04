"""Integration tests for the public contract used by ToolNet Drama Downloader."""

from __future__ import annotations

import pytest

from tests.conftest import EN_BOOK, VI_BOOK, index_movie


@pytest.mark.asyncio
async def test_meta_endpoint_describes_contract(client) -> None:
    response = await client.get("/v1/meta")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["policy"]["metadata_only"] is True
    assert data["policy"]["stores_video_urls"] is False
    assert "vi" in data["supported_locales"]
    assert data["freshness"]["metadata_ttl_hours"] > 0


@pytest.mark.asyncio
async def test_health_endpoints(client) -> None:
    for path in ("/health", "/health/live", "/health/ready", "/healthz"):
        response = await client.get(path)
        assert response.status_code == 200, path
    health = (await client.get("/health")).json()
    assert health["status"] in ("ok", "degraded")
    assert health["database"]["ok"] is True
    assert "queue" in health and "source" in health


@pytest.mark.asyncio
async def test_catalog_and_pagination_contract(client, admin_headers) -> None:
    await index_movie(client, admin_headers, EN_BOOK)
    await index_movie(client, admin_headers, VI_BOOK)

    response = await client.get("/v1/movies?page=1&page_size=1")
    body = response.json()
    assert len(body["data"]) == 1
    pagination = body["meta"]["pagination"]
    assert pagination["page"] == 1
    assert pagination["page_size"] == 1
    assert pagination["total"] == 2
    assert pagination["total_pages"] == 2
    assert pagination["has_next"] is True

    page2 = await client.get("/v1/movies?page=2&page_size=1")
    assert page2.json()["data"][0]["book_id"] != body["data"][0]["book_id"]

    too_big = await client.get("/v1/movies?page_size=1000")
    assert too_big.status_code == 422 or too_big.status_code == 400


@pytest.mark.asyncio
async def test_episode_pagination_and_incomplete_flag(client, admin_headers) -> None:
    await index_movie(client, admin_headers, EN_BOOK)
    response = await client.get(f"/v1/movies/{EN_BOOK}/episodes?page=1&page_size=10")
    body = response.json()
    assert len(body["data"]["episodes"]) == 10
    assert body["meta"]["pagination"]["total"] == 51
    assert body["data"]["episodes"][0]["serial_number"] == 1
    assert body["data"]["episodes"][-1]["serial_number"] == 10

    last_page = await client.get(f"/v1/movies/{EN_BOOK}/episodes?page=6&page_size=10")
    episodes = last_page.json()["data"]["episodes"]
    assert len(episodes) == 1
    assert episodes[0]["serial_number"] == 51


@pytest.mark.asyncio
async def test_episodes_can_be_requested_per_language(client, admin_headers) -> None:
    await index_movie(client, admin_headers, EN_BOOK)
    await index_movie(client, admin_headers, VI_BOOK)
    await client.post(
        "/admin/editions/merge",
        headers=admin_headers,
        json={"source_book_id": EN_BOOK, "target_book_id": VI_BOOK, "status": "VERIFIED"},
    )
    response = await client.get(f"/v1/movies/{EN_BOOK}/episodes?lang=vi&page_size=5")
    body = response.json()["data"]
    assert body["lang"] == "vi"
    assert body["book_id"] == VI_BOOK
    assert body["episodes"][0]["chapter_id"] == "4h2druta3o"

    missing = await client.get(f"/v1/movies/{EN_BOOK}/episodes?lang=th")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "no_edition_for_lang"


@pytest.mark.asyncio
async def test_episodes_never_contain_video_urls(client, admin_headers) -> None:
    await index_movie(client, admin_headers, EN_BOOK)
    response = await client.get(f"/v1/movies/{EN_BOOK}/episodes?page_size=100")
    raw = response.text
    for forbidden in ("play_info", "video_url", "m3u8", "drm", "license"):
        assert forbidden not in raw


@pytest.mark.asyncio
async def test_error_envelope_shape(client) -> None:
    response = await client.get("/v1/movies/does-not-exist")
    body = response.json()
    assert response.status_code == 404
    assert body["data"] is None
    assert set(body.keys()) == {"data", "error", "meta"}
    assert body["error"]["code"]
    assert body["meta"]["server_time"] > 0
    assert body["meta"]["request_id"]

    not_found = await client.get("/v1/nope")
    assert not_found.status_code == 404
    assert not_found.json()["error"]["code"] == "http_error"


@pytest.mark.asyncio
async def test_request_id_header_round_trip(client) -> None:
    response = await client.get("/v1/meta", headers={"X-Request-Id": "abc123"})
    assert response.headers["x-request-id"] == "abc123"
    assert response.json()["meta"]["request_id"] == "abc123"


@pytest.mark.asyncio
async def test_episodes_on_demand_sync(client, admin_headers) -> None:
    # 1. Index movie without episodes
    await client.post(
        "/admin/crawl",
        headers=admin_headers,
        json={"targets": [{"book_id": EN_BOOK}], "force": True, "include_episodes": False, "wait": True},
    )
    from app.db.base import session_scope
    from app.db.models import MovieEdition
    with session_scope() as session:
        ed = session.get(MovieEdition, EN_BOOK)
        assert ed.stored_chapter_count == 0

    # 2. Access episodes endpoint directly -> triggers on-demand sync
    response = await client.get(f"/v1/movies/{EN_BOOK}/episodes?page_size=100")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["stored_episode_count"] == 51
    assert len(data["episodes"]) == 51
    assert data["complete"] is True
    assert data["missing_episodes"] == []
    assert data["episodes"][0]["chapter_id"] == "1gv8oeozxn"


@pytest.mark.asyncio
async def test_movie_detail_language_resolution(client, admin_headers) -> None:
    await index_movie(client, admin_headers, EN_BOOK)
    await index_movie(client, admin_headers, VI_BOOK)
    await client.post(
        "/admin/editions/merge",
        headers=admin_headers,
        json={"source_book_id": EN_BOOK, "target_book_id": VI_BOOK, "status": "VERIFIED"},
    )
    # Query EN_BOOK with lang=vi resolves to Vietnamese edition
    res = await client.get(f"/v1/movies/{EN_BOOK}?lang=vi")
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["book_id"] == VI_BOOK
    assert data["lang"] == "vi"


@pytest.mark.asyncio
async def test_missing_episodes_detection(client, admin_headers) -> None:
    from app.db.base import session_scope
    from app.db.models import Episode, MovieEdition

    # Insert a test edition with chapter_count=3 but only serial 1 and 3 in DB
    test_id = "test_partial_movie_1"
    with session_scope() as session:
        session.add(
            MovieEdition(
                book_id=test_id,
                book_title="Test Partial Drama",
                lang="en",
                chapter_count=3,
                stored_chapter_count=2,
            )
        )
        session.add(Episode(book_id=test_id, chapter_id="ch_1", serial_number=1))
        session.add(Episode(book_id=test_id, chapter_id="ch_3", serial_number=3))

    res = await client.get(f"/v1/movies/{test_id}/episodes")
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["chapter_count"] == 3
    assert data["stored_episode_count"] == 2
    assert data["complete"] is False
    assert data["missing_episodes"] == [2]


@pytest.mark.asyncio
async def test_series_compatibility_endpoints(client, admin_headers) -> None:
    await index_movie(client, admin_headers, EN_BOOK)

    # Test /api/series/{series_id}
    res = await client.get(f"/api/series/{EN_BOOK}")
    assert res.status_code == 200
    data = res.json()
    assert data["id"] == EN_BOOK
    assert data["count"] == 51
    assert len(data["episodes"]) == 51
    assert data["episodes"][0]["number"] == 1
    assert data["episodes"][0]["id"] == "1gv8oeozxn"

    # Test /api/series/{series_id}/episodes
    res_eps = await client.get(f"/api/series/{EN_BOOK}/episodes")
    assert res_eps.status_code == 200
    data_eps = res_eps.json()
    assert data_eps["series_id"] == EN_BOOK
    assert data_eps["count"] == 51
    assert data_eps["complete"] is True
    assert data_eps["episodes"][0]["id"] == "1gv8oeozxn"


