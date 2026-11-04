"""Shared pytest fixtures: isolated SQLite DB, fake source, ASGI test client."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, AsyncIterator
from unittest.mock import patch

import asyncio
import httpx
import pytest

os.environ["TURSO_DATABASE_URL"] = ""
os.environ["TURSO_AUTH_TOKEN"] = ""

from app.config import Settings
from app.crawler.source import SourceNotFound, SourceResponse, SourceStats, TokenBucket
from app.db.base import os_environ_bind
from app.main import create_app

FIXTURES = Path(__file__).parent / "fixtures"

EN_BOOK = "6a5191d8fa9f9f7a09081789"
VI_BOOK = "6a6da2cc39cbfeadc50d1e6a"
ES_BOOK = "6a702f0f85d596905908bd4b"
# Official search returns these ids for the same Spanish edition; getBookInfo
# resolves each of them to ES_BOOK.
ES_ALIAS_BOOKS = [
    "6a95598dfe80db350c089b5e",
    "6a66ccd3cbbafb058e0a7a4c",
    "6a95598cfe80db350c089b52",
]

ADMIN_USER = "admin"
ADMIN_PASSWORD = "test-password"
ADMIN_TOKEN = "test-token"
PUBLIC_KEY = "test-public-key"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def wrap_next_data(payload: dict[str, Any]) -> str:
    return (
        "<!DOCTYPE html><html><head><title>fixture</title></head><body>"
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload, ensure_ascii=False)
        + "</script></body></html>"
    )


class FakeSource:
    """Deterministic stand-in for :class:`app.crawler.source.ReelShortSource`.

    Serves the recorded metadata fixtures and counts calls so tests can assert
    that the crawler is cache-first and does not hammer the source.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base_url = settings.source_base_url
        self.stats = SourceStats()
        self._bucket = TokenBucket(600, 60)
        self.calls: dict[str, int] = {}
        self.failures: dict[str, Exception] = {}
        self.delays: dict[str, float] = {}
        self.next_data = {
            EN_BOOK: load_fixture("next_data_en.json"),
            VI_BOOK: load_fixture("next_data_vi.json"),
        }
        self.book_info = {
            EN_BOOK: load_fixture("book_info_en.json")["data"],
            VI_BOOK: load_fixture("book_info_vi.json")["data"],
            ES_BOOK: load_fixture("book_info_es.json")["data"],
        }
        # alias ids answer with the canonical edition payload, like upstream
        for index, alias in enumerate(ES_ALIAS_BOOKS):
            self.book_info[alias] = load_fixture(f"book_info_es_alias{index + 1}.json")["data"]
        self.chapters = {
            EN_BOOK: load_fixture("chapter_list_en.json")["data"],
            VI_BOOK: load_fixture("chapter_list_vi.json")["data"],
            ES_BOOK: load_fixture("chapter_list_es.json")["data"],
        }
        self.searches = {
            ("vi", "Thần Đèn Vĩ Đại và Toàn Năng"): "search_vi.json",
            ("es", "The Great and Powerful Genie"): "search_es.json",
            ("en", "The Great and Powerful Genie"): "search_en.json",
        }
        self.catalog = load_fixture("tag_catalog_vi.json")

    # --- lifecycle ---------------------------------------------------------
    async def start(self) -> None:  # pragma: no cover - trivial
        return None

    async def close(self) -> None:  # pragma: no cover - trivial
        return None

    def _record(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1
        self.stats.requests += 1
        self.stats.by_kind[name] = self.stats.by_kind.get(name, 0) + 1
        self.stats.last_status = 200
        if name in self.failures:
            raise self.failures[name]

    # --- endpoints ---------------------------------------------------------
    async def movie_page(self, book_id: str) -> SourceResponse:
        self._record("movie_page")
        payload = self.next_data.get(book_id)
        if payload is None:
            raise SourceNotFound(f"no fixture for {book_id}")
        html = wrap_next_data(payload)
        return SourceResponse(
            url=f"{self.base_url}/movie/{book_id}",
            status=200,
            text=html,
            elapsed_ms=5,
            kind="movie_page",
        )

    async def movie_info(self, book_id: str, lang: str | None = None) -> dict[str, Any]:
        await asyncio.sleep(self.delays.get("movie_info", 0.0))
        self._record("movie_info")
        payload = self.book_info.get(book_id)
        if payload is None:
            raise SourceNotFound(f"no fixture for {book_id}")
        return json.loads(json.dumps(payload, ensure_ascii=False))

    async def chapter_list(
        self, book_id: str, page: int = 1, page_size: int = 100, lang: str | None = None
    ) -> dict[str, Any]:
        self._record("chapter_list")
        payload = self.chapters.get(book_id)
        if payload is None:
            raise SourceNotFound(f"no fixture for {book_id}")
        chapters = payload["chapter_list"]
        start = (page - 1) * page_size
        window = chapters[start : start + page_size]
        result = dict(payload)
        result["chapter_list"] = window
        result["page"] = page
        result["page_size"] = page_size
        return result

    async def search_page(self, lang: str, keywords: str, page: int = 1) -> SourceResponse:
        self._record("search")
        fixture = self.searches.get((lang, keywords))
        if fixture is None:
            payload = {"props": {"pageProps": {"books": [], "total": 0}}, "locale": lang}
        else:
            payload = load_fixture(fixture)
        html = wrap_next_data(
            {
                "props": {"pageProps": {"books": payload["props"]["pageProps"].get("books", [])}},
                "locale": payload.get("locale", lang),
            }
        )
        return SourceResponse(
            url=f"{self.base_url}/{lang}/search?keywords={keywords}",
            status=200,
            text=html,
            elapsed_ms=7,
            kind="search",
        )

    async def tag_catalog(
        self, tag_id: str, lang: str, page: int = 1, page_size: int = 50
    ) -> dict[str, Any]:
        self._record("tag_catalog")
        books = self.catalog["data"]["books"]
        start = (page - 1) * page_size
        return {
            "books": books[start : start + page_size],
            "total_items": self.catalog["data"]["total_items"],
            "page": page,
            "page_size": page_size,
        }

    async def tag_list(self, lang: str, book_type: int = 1) -> dict[str, Any]:  # pragma: no cover
        self._record("tag_list")
        return {"items": []}

    async def homepage(self, lang: str) -> SourceResponse:
        self._record("homepage")
        sample_bid = VI_BOOK if lang == "vi" else (EN_BOOK if lang == "en" else ES_BOOK)
        sample_books = [
            {
                "book_id": sample_bid,
                "book_title": "Sample Drama",
                "chapter_count": 20,
                "chapter_id": "ch_sample_1",
                "special_desc": "Sample Description",
                "book_pic": "https://example.com/pic.jpg",
                "paid_start": 5,
                "theme": ["Drama"],
                "theme_list": [{"tag_id": "tag_1", "tag_name": "Drama"}],
            }
        ]
        payload = {
            "props": {
                "pageProps": {
                    "fallback": {
                        "/api/ms/hall/webInfo": {
                            "banners": [
                                {
                                    "jump_param": {
                                        "book_id": sample_bid,
                                        "book_title": "Sample Drama",
                                        "chapter_count": 20,
                                        "book_pic": "https://example.com/banner.jpg",
                                    }
                                }
                            ],
                            "bookShelfList": [
                                {
                                    "bookshelf_name": "New Releases",
                                    "books": sample_books,
                                }
                            ],
                        }
                    }
                }
            },
            "locale": lang,
        }
        html = wrap_next_data(payload)
        return SourceResponse(
            url=f"{self.base_url}/{lang}",
            status=200,
            text=html,
            elapsed_ms=5,
            kind="homepage",
        )

    async def catalog_page(self, lang: str, page: int = 1) -> SourceResponse:
        self._record("catalog_page")
        custom_pages = getattr(self, "catalog_pages", {}).get((lang, page))
        if custom_pages is not None:
            html = wrap_next_data(custom_pages)
            return SourceResponse(
                url=f"{self.base_url}/movie-genres/all-movies/{page}",
                status=200,
                text=html,
                elapsed_ms=5,
                kind="catalog_page",
            )
        sample_bid = VI_BOOK if lang == "vi" else (EN_BOOK if lang == "en" else ES_BOOK)
        books = [
            {
                "_id": sample_bid,
                "book_id": sample_bid,
                "t_book_id": f"t_{sample_bid}",
                "book_title": "Sample Catalog Drama",
                "chapter_count": 20,
                "first_chapter_id": "ch_sample_1",
                "special_desc": "Sample Catalog Description",
                "book_pic": "https://example.com/pic.jpg",
                "paid_start": 5,
                "read_count": 1200,
                "collect_count": 300,
                "theme": ["Drama", "Romance"],
                "tag": ["Drama", "Romance"],
                "lang": lang,
            }
        ]
        payload = {
            "props": {
                "pageProps": {
                    "total": 1,
                    "totalPage": 1,
                    "tagBooks": {
                        "books": books,
                        "total": 1,
                        "total_page": 1,
                    },
                }
            },
            "locale": lang,
        }
        html = wrap_next_data(payload)
        return SourceResponse(
            url=f"{self.base_url}/movie-genres/all-movies/{page}",
            status=200,
            text=html,
            elapsed_ms=5,
            kind="catalog_page",
        )

    async def sitemap(self) -> str | None:  # pragma: no cover
        return None


def build_settings(tmp_path: Path, **overrides: Any) -> Settings:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    values: dict[str, Any] = {
        "db_path": str(data_dir / "test.sqlite3"),
        "data_dir": str(data_dir),
        "backup_dir": str(data_dir / "backups"),
        "log_file": None,
        "log_level": "WARNING",
        "environment": "test",
        "admin_username": ADMIN_USER,
        "admin_password": ADMIN_PASSWORD,
        "admin_token": ADMIN_TOKEN,
        "public_api_keys": [PUBLIC_KEY],
        "public_auth_enabled": False,
        "worker_concurrency": 2,
        "scheduler_enabled": False,
        "source_requests_per_minute": 6000,
        "source_concurrency": 4,
        "worker_poll_seconds": 0.05,
        "job_max_attempts": 3,
        "discovery_max_locales_per_run": 4,
        "discovery_search_pages": 1,
        "discovery_tag_scan_pages": 1,
        "discovery_max_candidates": 12,
        "turso_database_url": None,
        "turso_auth_token": None,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    """The inbound limiter is process-global; keep tests independent."""
    from app.security import limiter

    limiter.reset()


@pytest.fixture()
def migrated_settings(tmp_path: Path) -> Settings:
    """Settings pointing at a fresh SQLite file with the real Alembic schema."""
    settings = build_settings(tmp_path)
    apply_migrations(settings)
    return settings


def apply_migrations(settings: Settings) -> None:
    """Run the production migrations against ``settings.db_path`` (test fidelity)."""
    from alembic import command
    from alembic.config import Config

    os_environ_bind(settings)
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", settings.database_url)
    with patch.dict(os.environ, {"DB_PATH": settings.db_path, "TURSO_DATABASE_URL": "", "TURSO_AUTH_TOKEN": ""}):
        command.upgrade(config, "head")
    os_environ_bind(settings)


@pytest.fixture()
def settings(migrated_settings: Settings) -> Settings:
    return migrated_settings


@pytest.fixture()
def fake_source(settings: Settings) -> FakeSource:
    return FakeSource(settings)


@pytest.fixture()
async def client(settings: Settings, fake_source: FakeSource) -> AsyncIterator[httpx.AsyncClient]:
    os_environ_bind(settings)
    app = create_app(settings)
    app.state.source_factory = lambda _settings: fake_source
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            http.app = app  # type: ignore[attr-defined]
            http.state = app.state.ctx  # type: ignore[attr-defined]
            http.fake_source = fake_source  # type: ignore[attr-defined]
            yield http


@pytest.fixture()
def admin_headers() -> dict[str, str]:
    return {"X-Admin-Token": ADMIN_TOKEN}


async def wait_for_task(
    client: httpx.AsyncClient, task_id: str, *, timeout: float = 15.0, headers: dict[str, str] | None = None
) -> dict[str, Any]:
    """Poll an admin/public task until it reaches a terminal state."""
    import asyncio
    import time as _time

    deadline = _time.monotonic() + timeout
    path = f"/v1/tasks/{task_id}"
    last: dict[str, Any] = {}
    while _time.monotonic() < deadline:
        response = await client.get(path, headers=headers or {})
        payload = response.json()
        last = payload
        task = payload.get("data") or {}
        if task.get("status") in ("succeeded", "failed", "canceled"):
            return payload
        await asyncio.sleep(0.05)
    return last


async def wait_for_admin_task(
    client: httpx.AsyncClient, task_id: str, headers: dict[str, str], *, timeout: float = 15.0
) -> dict[str, Any]:
    import asyncio
    import time as _time

    deadline = _time.monotonic() + timeout
    last: dict[str, Any] = {}
    while _time.monotonic() < deadline:
        response = await client.get(f"/admin/jobs/{task_id}", headers=headers)
        last = response.json()
        task = last.get("data") or {}
        if task.get("status") in ("succeeded", "failed", "canceled"):
            return last
        await asyncio.sleep(0.05)
    return last


async def index_movie(
    client: httpx.AsyncClient, headers: dict[str, str], book_id: str, *, force: bool = False
) -> str:
    """Queue a crawl through the admin API and wait for completion."""
    response = await client.post(
        "/admin/crawl",
        headers=headers,
        json={"targets": [{"book_id": book_id}], "force": force, "include_episodes": True},
    )
    assert response.status_code == 202, response.text
    task_id = response.json()["data"]["tasks"][0]["task_id"]
    payload = await wait_for_admin_task(client, task_id, headers)
    assert payload["data"]["status"] == "succeeded", payload
    return task_id
