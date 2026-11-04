"""Integration tests: crawling, cache-first behaviour, dedupe, edition mapping."""

from __future__ import annotations

import asyncio
import json

import pytest

from app.db.base import session_scope
from app.db.models import CrawlJob, Episode, MovieEdition
from tests.conftest import (
    EN_BOOK,
    ES_ALIAS_BOOKS,
    ES_BOOK,
    VI_BOOK,
    index_movie,
    load_fixture,
    wait_for_admin_task,
    wait_for_task,
)


@pytest.mark.asyncio
async def test_crawl_english_sample_metadata(client, admin_headers) -> None:
    await index_movie(client, admin_headers, EN_BOOK)

    response = await client.get(f"/v1/movies/{EN_BOOK}")
    assert response.status_code == 200
    movie = response.json()["data"]

    expected = load_fixture("expected.json")["en"]
    assert movie["book_id"] == expected["book_id"]
    assert movie["lang"] == "en"
    assert movie["chapter_count"] == 51
    assert movie["episode_count"] == 51
    assert movie["stored_episode_count"] == 51
    assert movie["first_chapter_id"] == expected["first_chapter_id"]
    assert movie["t_book_id"] == "204200150000005664"
    assert movie["s_book_id"] == 136032
    assert movie["paid_start"] == 10
    assert movie["has_dub"] is False
    assert "Playing Dumb" in movie["tags"]
    assert movie["source_url"].endswith(f"/movie/{EN_BOOK}")

    # cache-first: metadata + episodes came from a single upstream metadata call
    assert client.fake_source.calls.get("movie_info") == 1
    assert not client.fake_source.calls.get("chapter_list"), "inline episodes must avoid extra calls"


@pytest.mark.asyncio
async def test_crawl_vietnamese_sample_metadata(client, admin_headers) -> None:
    await index_movie(client, admin_headers, VI_BOOK)

    response = await client.get(f"/v1/movies/{VI_BOOK}")
    movie = response.json()["data"]
    assert movie["lang"] == "vi"
    assert movie["book_title"] == "Thần Đèn Vĩ Đại và Toàn Năng"
    assert movie["chapter_count"] == 51
    assert movie["first_chapter_id"] == "4h2druta3o"
    assert movie["is_dub"] is True
    assert movie["original_lang"] == "en"

    episodes = await client.get(f"/v1/movies/{VI_BOOK}/episodes?page_size=100")
    payload = episodes.json()["data"]
    assert payload["stored_episode_count"] == 51
    assert payload["chapter_count"] == 51
    assert payload["complete"] is True
    assert len(payload["episodes"]) == 51
    assert payload["episodes"][0]["chapter_id"] == "4h2druta3o"


@pytest.mark.asyncio
async def test_episode_ids_are_never_fabricated(client, admin_headers) -> None:
    await index_movie(client, admin_headers, EN_BOOK)
    await index_movie(client, admin_headers, VI_BOOK)

    fixture_en = {c["chapter_id"] for c in load_fixture("chapter_list_en.json")["data"]["chapter_list"]}
    fixture_vi = {c["chapter_id"] for c in load_fixture("chapter_list_vi.json")["data"]["chapter_list"]}
    with session_scope() as session:
        stored_en = {
            row.chapter_id for row in session.query(Episode).filter(Episode.book_id == EN_BOOK).all()
        }
        stored_vi = {
            row.chapter_id for row in session.query(Episode).filter(Episode.book_id == VI_BOOK).all()
        }
    assert stored_en == fixture_en
    assert stored_vi == fixture_vi
    assert len(stored_en) == 51 and len(stored_vi) == 51


@pytest.mark.asyncio
async def test_lazy_crawl_and_concurrent_dedupe(client) -> None:
    calls_before = client.fake_source.calls.get("movie_info", 0)

    results = await asyncio.gather(*[client.get(f"/v1/movies/{VI_BOOK}") for _ in range(6)])
    task_ids = set()
    for response in results:
        assert response.status_code in (200, 404)
        if response.status_code == 404:
            details = response.json()["error"]["details"]
            assert response.json()["error"]["code"] == "not_indexed"
            task_ids.add(details["task_id"])
    assert len(task_ids) == 1, f"concurrent requests must dedupe to one crawl job: {task_ids}"

    task_id = task_ids.pop()
    payload = await wait_for_task(client, task_id)
    assert payload["data"]["status"] == "succeeded"
    assert client.fake_source.calls.get("movie_info", 0) == calls_before + 1

    cached = await client.get(f"/v1/movies/{VI_BOOK}")
    assert cached.status_code == 200
    assert cached.json()["data"]["lang"] == "vi"
    assert client.fake_source.calls.get("movie_info", 0) == calls_before + 1  # served from cache


@pytest.mark.asyncio
async def test_movies_list_and_search(client, admin_headers) -> None:
    await index_movie(client, admin_headers, EN_BOOK)
    await index_movie(client, admin_headers, VI_BOOK)

    vi_list = await client.get("/v1/movies?lang=vi")
    assert vi_list.status_code == 200
    data = vi_list.json()["data"]
    assert len(data) == 1
    assert data[0]["book_id"] == VI_BOOK
    assert vi_list.json()["meta"]["pagination"]["total"] == 1

    en_list = await client.get("/v1/movies?lang=en")
    assert en_list.json()["meta"]["pagination"]["total"] == 1

    search = await client.get("/v1/search?q=genie&lang=en")
    hits = search.json()["data"]
    assert any(hit["book_id"] == EN_BOOK for hit in hits)
    assert all(hit["score"] is not None for hit in hits)

    by_id = await client.get(f"/v1/search?q={VI_BOOK}")
    assert by_id.json()["data"][0]["book_id"] == VI_BOOK


@pytest.mark.asyncio
async def test_discovery_maps_language_editions(client, admin_headers) -> None:
    await index_movie(client, admin_headers, EN_BOOK)
    await index_movie(client, admin_headers, VI_BOOK)

    # Discovery from the English edition towards vi, using the Vietnamese title as a hint
    response = await client.post(
        "/admin/discover",
        headers=admin_headers,
        json={
            "book_id": EN_BOOK,
            "targets": ["vi"],
            "title_hints": ["Thần Đèn Vĩ Đại và Toàn Năng"],
            "strategies": ["search"],
        },
    )
    assert response.status_code == 200, response.text
    task_id = response.json()["data"]["task_id"]
    result = await wait_for_admin_task(client, task_id, admin_headers)
    assert result["data"]["status"] == "succeeded", result

    candidates = await client.get("/admin/editions/candidates", headers=admin_headers)
    matches = candidates.json()["data"]
    assert matches, "discovery must produce a match row"
    match = matches[0]
    assert {match["source"]["book_id"], match["target"]["book_id"]} == {EN_BOOK, VI_BOOK}
    assert match["status"] == "VERIFIED", match["evidence"]
    # evidence-based score: tag Jaccard 1.0 + equal episode count + official dub
    # relation + t_book tail match (+ title/genre/publish signals)
    assert match["confidence"] >= 0.80, match["evidence"]
    assert match["evidence"]["tag_ids_jaccard"] == 1.0

    languages = await client.get(f"/v1/movies/{EN_BOOK}/languages?targets=vi,es")
    assert languages.status_code == 200
    rows = {row["lang"]: row for row in languages.json()["data"]["languages"]}
    assert rows["vi"]["book_id"] == VI_BOOK
    assert rows["vi"]["chapter_count"] == 51
    assert rows["es"]["status"] == "MISSING"

    group = await client.get(f"/v1/movies/{EN_BOOK}")
    group_id = group.json()["data"]["group_id"]
    assert group_id

    group_detail = await client.get(f"/v1/groups/{group_id}")
    assert group_detail.status_code == 200
    body = group_detail.json()["data"]
    ids = {edition["book_id"] for edition in body["editions"]}
    assert {EN_BOOK, VI_BOOK} <= ids
    assert body["language_count"] == 2


@pytest.mark.asyncio
async def test_manual_verification_promotes_match(client, admin_headers) -> None:
    await index_movie(client, admin_headers, EN_BOOK)
    await index_movie(client, admin_headers, VI_BOOK)

    merge = await client.post(
        "/admin/editions/merge",
        headers=admin_headers,
        json={"source_book_id": EN_BOOK, "target_book_id": VI_BOOK, "status": "CANDIDATE", "note": "initial"},
    )
    assert merge.status_code == 200
    match_id = merge.json()["data"]["match_id"]

    verify = await client.post(
        f"/admin/editions/{match_id}/verify",
        headers=admin_headers,
        json={"status": "VERIFIED", "note": "both are The Great and Powerful Genie (51 eps)"},
    )
    assert verify.status_code == 200
    assert verify.json()["data"]["status"] == "VERIFIED"

    languages = await client.get(f"/v1/movies/{VI_BOOK}/languages?targets=en")
    rows = {row["lang"]: row for row in languages.json()["data"]["languages"]}
    assert rows["en"]["status"] == "VERIFIED"

    stats = await client.get("/admin/stats", headers=admin_headers)
    assert stats.json()["data"]["verified_matches"] >= 1


@pytest.mark.asyncio
async def test_stale_metadata_triggers_single_refresh(client, admin_headers) -> None:
    await index_movie(client, admin_headers, EN_BOOK)
    calls_before = client.fake_source.calls.get("movie_info", 0)

    with session_scope() as session:
        edition = session.get(MovieEdition, EN_BOOK)
        from datetime import timedelta

        from app.db.models import utcnow

        edition.fetched_at = utcnow() - timedelta(hours=100)
        session.flush()

    first = await client.get(f"/v1/movies/{EN_BOOK}")
    assert first.status_code == 200
    assert first.json()["data"]["is_stale"] is True
    second = await client.get(f"/v1/movies/{EN_BOOK}")
    third = await client.get(f"/v1/movies/{EN_BOOK}")
    assert second.json()["meta"]["cache"].get("refresh_queued") in (False, None)
    assert third.status_code == 200

    await asyncio.sleep(0.3)
    assert client.fake_source.calls.get("movie_info", 0) == calls_before + 1


@pytest.mark.asyncio
async def test_unknown_book_returns_task_not_data(client) -> None:
    response = await client.get("/v1/movies/ffffffffffffffffffffffff")
    assert response.status_code == 404
    body = response.json()
    assert body["data"] is None
    assert body["error"]["code"] == "not_indexed"
    assert body["error"]["details"]["task_id"].startswith("job_")
    assert body["error"]["details"]["status_url"].startswith("/v1/tasks/")


@pytest.mark.asyncio
async def test_public_task_endpoint_hides_admin_jobs(client, admin_headers) -> None:
    response = await client.post(
        "/admin/crawl",
        headers=admin_headers,
        json={"targets": [{"book_id": EN_BOOK}], "include_episodes": False},
    )
    admin_task_id = response.json()["data"]["tasks"][0]["task_id"]

    public_view = await client.get(f"/v1/tasks/{admin_task_id}")
    assert public_view.status_code == 404
    assert public_view.json()["error"]["code"] == "task_not_found"

    admin_view = await client.get(f"/admin/jobs/{admin_task_id}", headers=admin_headers)
    assert admin_view.status_code == 200
    assert admin_view.json()["data"]["scope"] == "admin"


@pytest.mark.asyncio
async def test_alias_book_id_resolves_to_canonical_edition(client, admin_headers) -> None:
    """ReelShort search returns alias ids; the API must not re-crawl forever."""
    alias = ES_ALIAS_BOOKS[0]

    # an alias id is crawled once and stored under the canonical edition id
    await index_movie(client, admin_headers, alias)
    response = await client.get(f"/v1/movies/{alias}")
    assert response.status_code == 200
    movie = response.json()["data"]
    assert movie["book_id"] == ES_BOOK
    assert movie["lang"] == "es"
    assert movie["requested_book_id"] == alias
    assert movie["alias_resolved"] is True
    assert movie["stored_episode_count"] == 51

    with session_scope() as session:
        from app.db.models import BookAlias

        row = session.get(BookAlias, alias)
        assert row is not None and row.canonical_book_id == ES_BOOK

    # re-requesting the same alias is a pure cache hit: no extra upstream call
    calls = client.fake_source.calls.get("movie_info", 0)
    again = await client.get(f"/v1/movies/{alias}")
    assert again.status_code == 200
    assert again.json()["data"]["book_id"] == ES_BOOK
    assert client.fake_source.calls.get("movie_info", 0) == calls

    # an *unknown* alias is crawled lazily once, then resolves to the same edition
    other = ES_ALIAS_BOOKS[1]
    lazy = await client.get(f"/v1/movies/{other}")
    assert lazy.status_code == 404
    task_id = lazy.json()["error"]["details"]["task_id"]
    assert (await wait_for_task(client, task_id))["data"]["status"] == "succeeded"

    resolved = await client.get(f"/v1/movies/{other}")
    assert resolved.status_code == 200
    body = resolved.json()["data"]
    assert body["book_id"] == ES_BOOK and body["requested_book_id"] == other
    with session_scope() as session:
        from app.db.models import BookAlias

        assert session.get(BookAlias, other).canonical_book_id == ES_BOOK
        # both aliases point at one single edition row (no duplicate crawl)
        assert len(session.query(MovieEdition).filter(MovieEdition.lang == "es").all()) == 1

    # episodes/languages also resolve through the alias
    episodes = await client.get(f"/v1/movies/{alias}/episodes?page_size=100")
    assert episodes.status_code == 200
    body = episodes.json()["data"]
    assert body["book_id"] == ES_BOOK and len(body["episodes"]) == 51


@pytest.mark.asyncio
async def test_discovery_endpoint_is_rate_limited(client, settings) -> None:
    settings.discover_rate_limit_per_hour = 2
    responses = []
    for _ in range(4):
        responses.append(
            await client.post("/v1/discover", json={"book_id": EN_BOOK, "targets": ["vi"]})
        )
    codes = [response.status_code for response in responses]
    assert 429 in codes, codes
    throttled = next(r for r in responses if r.status_code == 429)
    assert throttled.json()["error"]["code"] == "discover_quota_exceeded"


@pytest.mark.asyncio
async def test_job_timeout_keeps_resume_checkpoint(client, admin_headers, settings) -> None:
    """A job killed by its timeout must leave behind committed progress."""
    settings.job_timeout_seconds = 0.4
    settings.job_max_attempts = 2
    client.fake_source.delays["movie_info"] = 1.5

    response = await client.post(
        "/admin/crawl",
        headers=admin_headers,
        json={"targets": [{"book_id": EN_BOOK}], "include_episodes": False},
    )
    task_id = response.json()["data"]["tasks"][0]["task_id"]

    detail: dict = {}
    for _ in range(60):
        detail = (await client.get(f"/admin/jobs/{task_id}", headers=admin_headers)).json()["data"]
        if detail.get("error") and "timed out" in detail["error"]:
            break
        await asyncio.sleep(0.1)
    else:
        pytest.fail(f"job never reported a timeout: {detail}")

    assert "timed out" in detail["error"]
    # the checkpoint written before the upstream call survived the cancellation
    assert detail["checkpoint"]["book_id"] == EN_BOOK
    assert detail["checkpoint"]["phase"] in ("metadata", "retry")
    assert detail["checkpoint"].get("method") == "api"

    with session_scope() as session:
        job = session.get(CrawlJob, task_id)
        assert job is not None
        assert (job.checkpoint or {}).get("book_id") == EN_BOOK


@pytest.mark.asyncio
async def test_discovery_only_fetches_plausible_candidates(client, admin_headers) -> None:
    """The catalogue scan must not fetch metadata for unrelated books."""
    await index_movie(client, admin_headers, EN_BOOK)
    catalog_size = len(load_fixture("tag_catalog_vi.json")["data"]["books"])

    response = await client.post(
        "/admin/discover",
        headers=admin_headers,
        json={
            "book_id": EN_BOOK,
            "targets": ["vi"],
            "title_hints": ["Thần Đèn Vĩ Đại và Toàn Năng"],
            "strategies": ["search", "related"],
        },
    )
    task_id = response.json()["data"]["task_id"]
    result = await wait_for_admin_task(client, task_id, admin_headers, timeout=30.0)
    assert result["data"]["status"] == "succeeded", result

    locale_result = result["data"]["result"]["locales"]["vi"]
    assert locale_result["candidates_considered"] < catalog_size
    # only candidates carrying an identity signal were fetched at all
    fetched = client.fake_source.calls.get("movie_info", 0)
    assert 1 <= fetched <= locale_result["candidates_considered"] + 1
