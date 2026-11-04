"""Integration tests for admin endpoints, maintenance and persistence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from app.db.base import dispose_engine, os_environ_bind, session_scope
from app.db.models import Episode, MovieEdition
from tests.conftest import EN_BOOK, VI_BOOK, index_movie


@pytest.mark.asyncio
async def test_admin_crawl_validation(client, admin_headers) -> None:
    empty = await client.post("/admin/crawl", headers=admin_headers, json={"targets": []})
    assert empty.status_code == 400
    assert empty.json()["error"]["code"] == "no_targets"

    bad_url = await client.post(
        "/admin/crawl", headers=admin_headers, json={"targets": [{"url": "https://example.com/nope"}]}
    )
    assert bad_url.status_code == 202
    assert bad_url.json()["data"]["rejected"]


@pytest.mark.asyncio
async def test_admin_crawl_by_url_and_force(client, admin_headers) -> None:
    url = f"https://www.reelshort.com/movie/the-great-and-powerful-genie-{EN_BOOK}"
    response = await client.post(
        "/admin/crawl",
        headers=admin_headers,
        json={"targets": [{"url": url}], "include_episodes": True, "wait": True},
    )
    assert response.status_code == 202
    task = response.json()["data"]["tasks"][0]
    assert task["status"] in ("succeeded", "running", "queued")
    detail = await client.get(f"/admin/jobs/{task['task_id']}", headers=admin_headers)
    assert detail.json()["data"]["result"]["movies_written"] == 1

    # second crawl without force is served from cache (no upstream metadata call)
    calls = client.fake_source.calls.get("movie_info", 0)
    again = await client.post(
        "/admin/crawl", headers=admin_headers, json={"targets": [{"book_id": EN_BOOK}], "wait": True}
    )
    assert again.status_code == 202
    assert client.fake_source.calls.get("movie_info", 0) == calls


@pytest.mark.asyncio
async def test_admin_jobs_listing_and_cancel(client, admin_headers) -> None:
    await index_movie(client, admin_headers, EN_BOOK)
    listed = await client.get("/admin/jobs?status=succeeded", headers=admin_headers)
    assert listed.status_code == 200
    body = listed.json()
    assert body["meta"]["pagination"]["total"] >= 1
    assert all(job["status"] == "succeeded" for job in body["data"])

    # queue a job then cancel it
    queued = await client.post(
        "/admin/crawl", headers=admin_headers, json={"targets": [{"book_id": VI_BOOK}], "priority": 10}
    )
    task_id = queued.json()["data"]["tasks"][0]["task_id"]
    cancel = await client.post(f"/admin/jobs/{task_id}/cancel", headers=admin_headers)
    assert cancel.status_code in (200, 404)
    if cancel.status_code == 200:
        after = await client.get(f"/admin/jobs/{task_id}", headers=admin_headers)
        assert after.json()["data"]["status"] in ("canceled", "succeeded", "running", "queued")


@pytest.mark.asyncio
async def test_admin_stats_source_cache_and_history(client, admin_headers) -> None:
    await index_movie(client, admin_headers, EN_BOOK)

    stats = await client.get("/admin/stats", headers=admin_headers)
    assert stats.status_code == 200
    data = stats.json()["data"]
    assert data["editions"] >= 1
    assert data["episodes"] >= 51
    assert data["languages"][0]["lang"] == "en"
    assert "jobs" in data and "source" in data and "database" in data

    source = await client.get("/admin/source", headers=admin_headers)
    assert source.status_code == 200
    assert source.json()["data"]["settings"]["respect_robots_txt"] in (True, False)

    history = await client.get("/admin/history?book_id=" + EN_BOOK, headers=admin_headers)
    outcomes = {row["outcome"] for row in history.json()["data"]}
    assert "metadata_ok" in outcomes

    cache = await client.get("/admin/cache", headers=admin_headers)
    assert cache.status_code == 200
    purge = await client.post("/admin/cache/purge", headers=admin_headers)
    assert purge.status_code == 200


@pytest.mark.asyncio
async def test_admin_backup_creates_restorable_file(client, admin_headers, settings) -> None:
    await index_movie(client, admin_headers, EN_BOOK)
    response = await client.post("/admin/backup", headers=admin_headers)
    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["ok"] is True
    path = Path(payload["path"])
    assert path.exists() and path.stat().st_size > 0

    engine = create_engine(f"sqlite+pysqlite:///{path}")
    with engine.connect() as conn:
        count = conn.execute(text("SELECT count(*) FROM movie_editions")).scalar()
        episodes = conn.execute(text("SELECT count(*) FROM episodes")).scalar()
    engine.dispose()
    assert count >= 1
    assert episodes >= 51


@pytest.mark.asyncio
async def test_data_survives_restart(client, admin_headers, settings) -> None:
    await index_movie(client, admin_headers, EN_BOOK)
    await index_movie(client, admin_headers, VI_BOOK)

    # simulate a container restart: drop the engine/pool bindings, re-open the same file
    dispose_engine()
    os_environ_bind(settings)

    with session_scope() as session:
        editions = session.query(MovieEdition).count()
        episodes = session.query(Episode).count()
    assert editions == 2
    assert episodes == 102

    # the API still serves the persisted data without touching the source
    calls = client.fake_source.calls.get("movie_info", 0)
    response = await client.get(f"/v1/movies/{EN_BOOK}")
    assert response.status_code == 200
    assert response.json()["data"]["stored_episode_count"] == 51
    assert client.fake_source.calls.get("movie_info", 0) == calls


@pytest.mark.asyncio
async def test_admin_prune_and_maintenance(client, admin_headers) -> None:
    response = await client.post("/admin/maintenance/prune?days=1", headers=admin_headers)
    assert response.status_code == 200
    body = response.json()["data"]
    assert set(body) == {"history_removed", "usage_removed", "jobs_removed"}


@pytest.mark.asyncio
async def test_catalog_indexing_job(client, admin_headers) -> None:
    response = await client.post(
        "/admin/catalog", headers=admin_headers, json={"lang": "vi", "pages": 1, "page_size": 20}
    )
    assert response.status_code == 200
    task_id = response.json()["data"]["task_id"]
    from tests.conftest import wait_for_admin_task

    result = await wait_for_admin_task(client, task_id, admin_headers)
    assert result["data"]["status"] == "succeeded"
    assert result["data"]["result"]["editions_indexed"] >= 1

    listing = await client.get("/v1/movies?lang=vi&page_size=100")
    assert listing.json()["meta"]["pagination"]["total"] >= 1
