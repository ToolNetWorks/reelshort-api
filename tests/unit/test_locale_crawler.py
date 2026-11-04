"""Unit & integration tests for the Global Language Catalog Crawler."""

import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.config import (
    REELSHORT_CATALOG_PAGE_URLS,
    REELSHORT_LOCALES,
    Settings,
    get_locale_catalog_page_url,
    get_locale_source_url,
)
from app.crawler.parser import ParsedMovie, parse_all_movies_page, parse_homepage_catalog
from app.db.base import session_scope
from app.db.models import CatalogPageState, CatalogState, CrawlJob, MovieEdition, MovieGroup
from app.main import create_app
from tests.conftest import FakeSource, build_settings, wrap_next_data


def test_reelshort_locales_mapping():
    """Verify fixed mapping requirements for all required locales."""
    expected = {
        "vi": "https://www.reelshort.com/vi",
        "en": "https://www.reelshort.com",
        "pt": "https://www.reelshort.com/pt",
        "es": "https://www.reelshort.com/es",
        "ja": "https://www.reelshort.com/ja",
        "ko": "https://www.reelshort.com/ko",
        "zh-TW": "https://www.reelshort.com/zh-TW",
        "fr": "https://www.reelshort.com/fr",
    }
    for loc, url in expected.items():
        assert loc in REELSHORT_LOCALES, f"Missing {loc} in REELSHORT_LOCALES"
        assert REELSHORT_LOCALES[loc] == url
        assert get_locale_source_url(loc) == url

    # Case insensitive lookup
    assert get_locale_source_url("VI") == "https://www.reelshort.com/vi"
    assert get_locale_source_url("zh-tw") == "https://www.reelshort.com/zh-TW"

    # Extension locales
    assert get_locale_source_url("de") == "https://www.reelshort.com/de"

    # Verify per-locale pagination URLs (Task 6)
    expected_catalog_urls = {
        "en": "https://www.reelshort.com/movie-genres/all-movies/1",
        "vi": "https://www.reelshort.com/vi/movie-genres/all-movies/2",
        "pt": "https://www.reelshort.com/pt/movie-genres/all-movies/3",
        "es": "https://www.reelshort.com/es/movie-genres/all-movies/4",
        "ja": "https://www.reelshort.com/ja/movie-genres/all-movies/5",
        "ko": "https://www.reelshort.com/ko/movie-genres/all-movies/6",
        "zh-TW": "https://www.reelshort.com/zh-TW/movie-genres/all-movies/7",
        "zh-tw": "https://www.reelshort.com/zh-TW/movie-genres/all-movies/7",
        "fr": "https://www.reelshort.com/fr/movie-genres/all-movies/8",
    }
    for loc, expected_url in expected_catalog_urls.items():
        page_num = int(expected_url.rsplit("/", 1)[-1])
        assert get_locale_catalog_page_url(loc, page_num) == expected_url


def test_parse_all_movies_page_ssr_structure():
    """Verify SSR parsing of pageProps.tagBooks.books, total, totalPage, and chapter_count=0."""
    payload = {
        "props": {
            "pageProps": {
                "total": 4799,
                "totalPage": 400,
                "tagBooks": {
                    "total": 4799,
                    "total_page": 400,
                    "books": [
                        {
                            "_id": "69cbb76f3d332d79fa0658ae",
                            "book_id": "69cbb76f3d332d79fa0658ae",
                            "t_book_id": "204200150000019999",
                            "book_title": "Homegirls Before Husbands",
                            "book_pic": "https://v-img.crazymaplestudios.com/cover1.jpg",
                            "special_desc": "Preview drama with zero chapters in listing",
                            "chapter_count": 0,
                            "first_chapter_id": "ch_preview_1",
                            "theme": ["Dramas", "Friendship"],
                            "tag": ["Dramas", "Friendship"],
                            "read_count": 0,
                            "collect_count": 0,
                            "lang": "en",
                        },
                        {
                            "_id": "699fd92b332d3b1fc80c0916",
                            "book_id": "699fd92b332d3b1fc80c0916",
                            "t_book_id": "204200150000018888",
                            "book_title": "The CEO Contract Wife",
                            "book_pic": "https://v-img.crazymaplestudios.com/cover2.jpg",
                            "special_desc": "Full drama with 85 chapters",
                            "chapter_count": 85,
                            "first_chapter_id": "ch_full_1",
                            "paid_start": 11,
                            "theme": ["Romance", "CEO"],
                            "read_count": 154000,
                            "collect_count": 9200,
                            "lang": "en",
                        },
                    ],
                },
            }
        },
        "locale": "en",
    }
    html = wrap_next_data(payload)
    parsed = parse_all_movies_page(
        html,
        "en",
        source_url="https://www.reelshort.com/movie-genres/all-movies/1",
    )
    assert parsed.total == 4799
    assert parsed.total_pages == 400
    assert len(parsed.movies) == 2

    m0 = parsed.movies[0]
    assert m0.book_id == "69cbb76f3d332d79fa0658ae"
    assert m0.chapter_count == 0
    assert m0.category == "Dramas"
    assert m0.source_url == "https://www.reelshort.com/movie/69cbb76f3d332d79fa0658ae"
    assert parsed.source_url == "https://www.reelshort.com/movie-genres/all-movies/1"

    m1 = parsed.movies[1]
    assert m1.book_id == "699fd92b332d3b1fc80c0916"
    assert m1.chapter_count == 85
    assert m1.paid_start == 11
    assert m1.read_count == 154000
    assert m1.collect_count == 9200


def test_parse_homepage_catalog_banners_and_shelves():
    """Test extracting banners, multiple shelves, and deduplication."""
    mock_payload = {
        "props": {
            "pageProps": {
                "fallback": {
                    "/api/ms/hall/webInfo": {
                        "banners": [
                            {
                                "title": "Banner Movie",
                                "jump_param": {
                                    "book_id": "book_001",
                                    "book_title": "Banner Movie Title",
                                    "t_book_id": "t_001",
                                    "book_pic": "https://example.com/banner.jpg",
                                    "special_desc": "Banner Description",
                                    "chapter_count": 50,
                                    "paid_start": 10,
                                    "start_play": {"chapter_id": "ch_001"},
                                    "theme": ["Romance"],
                                    "theme_list": [{"tag_id": "t1", "tag_name": "Romance"}],
                                },
                            }
                        ],
                        "bookShelfList": [
                            {
                                "bookshelf_name": "Mới Cập Nhật",
                                "books": [
                                    {
                                        "book_id": "book_001",  # Same as banner: test deduplication
                                        "book_title": "Banner Movie Title",
                                        "special_desc": "Richer description from shelf",
                                        "chapter_count": 50,
                                        "chapter_id": "ch_001",
                                        "theme": ["Drama"],
                                    },
                                    {
                                        "book_id": "book_002",
                                        "book_title": "Shelf Movie 2",
                                        "book_pic": "https://example.com/p2.jpg",
                                        "special_desc": "Desc 2",
                                        "chapter_count": 30,
                                        "paid_start": 8,
                                        "chapter_id": "ch_002",
                                        "theme_list": [{"tag_id": "t2", "tag_name": "Action"}],
                                    },
                                ],
                            },
                            {
                                "bookshelf_name": "BẢNG XẾP HẠNG",
                                "books": [
                                    {
                                        "book_id": "book_003",
                                        "book_title": "Shelf Movie 3",
                                        "book_pic": "https://example.com/p3.jpg",
                                        "special_desc": "Desc 3",
                                        "chapter_count": 40,
                                        "chapter_id": "ch_003",
                                    }
                                ],
                            },
                        ],
                    }
                }
            }
        },
        "locale": "vi",
    }
    html = wrap_next_data(mock_payload)
    parsed = parse_homepage_catalog(html, "vi")

    # There are 3 unique books total (book_001 was in banner and shelf 1)
    assert len(parsed) == 3
    b1 = next(p for p in parsed if p.book_id == "book_001")
    assert b1.book_title == "Banner Movie Title"
    assert b1.lang == "vi"
    assert b1.first_chapter_id == "ch_001"
    assert b1.chapter_count == 50
    assert "Romance" in b1.tags
    assert "Drama" in b1.tags

    b2 = next(p for p in parsed if p.book_id == "book_002")
    assert b2.book_title == "Shelf Movie 2"
    assert b2.category == "Mới Cập Nhật"

    b3 = next(p for p in parsed if p.book_id == "book_003")
    assert b3.book_title == "Shelf Movie 3"
    assert b3.category == "BẢNG XẾP HẠNG"


@pytest.mark.asyncio
async def test_crawl_locale_catalog_service(client):
    """Test CrawlService.crawl_locale_catalog end-to-end with fake source."""
    with session_scope() as session:
        crawl_svc = client.state.crawl
        result = await crawl_svc.crawl_locale_catalog(session, "vi")
        assert result.status == "ok"
        assert result.movies_written >= 1
        assert result.lang == "vi"

        # Verify persisted in database
        edition = session.get(MovieEdition, "6a6da2cc39cbfeadc50d1e6a")
        assert edition is not None
        assert edition.lang == "vi"
        assert edition.book_title is not None
        assert edition.category is not None
        # Group should be ensured
        assert edition.movie_group_id is not None


@pytest.mark.asyncio
async def test_full_catalog_multi_page_checkpoint_and_incomplete_metadata(client):
    """Verify multi-page full catalog crawl, deduplication, chapter_count=0 handling, and resume."""
    fake_source = client.fake_source
    fake_source.catalog_pages = {
        ("en", 1): {
            "props": {
                "pageProps": {
                    "total": 3,
                    "totalPage": 2,
                    "tagBooks": {
                        "total": 3,
                        "total_page": 2,
                        "books": [
                            {
                                "book_id": "600000000000000000000001",
                                "t_book_id": "t_1",
                                "book_title": "English Drama One",
                                "book_pic": "https://example.com/1.jpg",
                                "special_desc": "First movie",
                                "chapter_count": 60,
                                "first_chapter_id": "ch_1",
                                "paid_start": 10,
                                "theme": ["Romance"],
                                "read_count": 5000,
                                "collect_count": 200,
                                "lang": "en",
                            },
                            {
                                "book_id": "600000000000000000000002",
                                "t_book_id": "t_2",
                                "book_title": "Preview Drama Zero Chapters",
                                "book_pic": "https://example.com/2.jpg",
                                "special_desc": "Zero chapter listing",
                                "chapter_count": 0,
                                "first_chapter_id": "ch_2",
                                "theme": ["Mystery"],
                                "lang": "en",
                            },
                        ],
                    },
                }
            }
        },
        ("en", 2): {
            "props": {
                "pageProps": {
                    "total": 3,
                    "totalPage": 2,
                    "tagBooks": {
                        "total": 3,
                        "total_page": 2,
                        "books": [
                            {
                                # Duplicate of page 1 movie 1 to test deduplication
                                "book_id": "600000000000000000000001",
                                "t_book_id": "t_1",
                                "book_title": "English Drama One",
                                "chapter_count": 60,
                                "theme": ["Romance"],
                                "lang": "en",
                            },
                            {
                                "book_id": "600000000000000000000003",
                                "t_book_id": "t_3",
                                "book_title": "Deep Page Drama Three",
                                "book_pic": "https://example.com/3.jpg",
                                "special_desc": "Found on page 2",
                                "chapter_count": 45,
                                "first_chapter_id": "ch_3",
                                "theme": ["Action"],
                                "read_count": 9000,
                                "collect_count": 450,
                                "lang": "en",
                            },
                        ],
                    },
                }
            }
        },
    }

    with session_scope() as session:
        crawl_svc = client.state.crawl
        res = await crawl_svc.crawl_full_catalog(session, "en", force=True)
        assert res.status == "ok"
        assert res.extra["source_total"] == 3
        assert res.extra["total_pages"] == 2
        assert res.extra["pages_completed"] == 2
        assert res.extra["incomplete_total"] >= 1

        # Check chapter_count=0 movie is marked METADATA_INCOMPLETE
        incomplete_ed = session.get(MovieEdition, "600000000000000000000002")
        assert incomplete_ed is not None
        assert incomplete_ed.chapter_count == 0
        assert incomplete_ed.metadata_status == "METADATA_INCOMPLETE"

        complete_ed = session.get(MovieEdition, "600000000000000000000003")
        assert complete_ed is not None
        assert complete_ed.metadata_status == "COMPLETE"
        assert complete_ed.read_count == 9000
        assert complete_ed.collect_count == 450
        assert complete_ed.category == "Action"

        # Calling crawl_full_catalog again with force=False should skip due to TTL
        calls_before = fake_source.calls.get("catalog_page", 0)
        res_cached = await crawl_svc.crawl_full_catalog(session, "en", force=False)
        assert res_cached.status == "cached"
        assert fake_source.calls.get("catalog_page", 0) == calls_before

    # Verify REST API pagination & search contracts (Task 7)
    r_p1 = await client.get("/v1/movies?lang=en&page=1&page_size=2")
    assert r_p1.status_code == 200
    b_p1 = r_p1.json()
    assert b_p1["total"] == 3
    assert b_p1["page"] == 1
    assert b_p1["page_size"] == 2
    assert b_p1["has_more"] is True
    assert len(b_p1["items"]) == 2

    r_p2 = await client.get("/v1/movies?lang=en&page=2&page_size=2")
    assert r_p2.status_code == 200
    b_p2 = r_p2.json()
    assert b_p2["total"] == 3
    assert b_p2["page"] == 2
    assert b_p2["has_more"] is False
    assert len(b_p2["items"]) == 1

    # Search empty query and deep page movie
    r_search_empty = await client.get("/v1/search?q=&lang=en")
    assert r_search_empty.status_code == 200
    assert r_search_empty.json()["total"] == 3

    r_search_deep = await client.get("/v1/search?q=Deep Page Drama&lang=en")
    assert r_search_deep.status_code == 200
    assert r_search_deep.json()["total"] == 1
    assert r_search_deep.json()["items"][0]["book_id"] == "600000000000000000000003"

    # Catalog status metrics (Task 7 & Task 9)
    r_status = await client.get("/v1/catalog/status?lang=en")
    assert r_status.status_code == 200
    st = r_status.json()
    assert st["source_total"] == 3
    assert st["unique_total"] == 3
    assert st["indexed_total"] == 3
    assert st["incomplete_total"] == 1
    assert st["enriched_total"] == 2
    assert st["total_pages"] == 2
    assert st["pages_completed"] == 2
    assert st["pages_remaining"] == 0


@pytest.mark.asyncio
async def test_catalog_status_and_sync_endpoints(client):
    """Test GET /v1/catalog/status and POST /v1/catalog/sync."""
    # Initial status
    res = await client.get("/v1/catalog/status?lang=vi", headers={"X-API-Key": "test-public-key"})
    assert res.status_code == 200
    data = res.json()
    assert data["locale"] == "vi"
    assert data["movie_count"] == 0

    # Trigger sync
    res_sync = await client.post("/v1/catalog/sync?lang=vi", headers={"X-API-Key": "test-public-key"})
    assert res_sync.status_code == 200
    sync_data = res_sync.json()
    assert sync_data["status"] == "queued"
    assert sync_data["locale"] == "vi"
    assert "task_id" in sync_data

