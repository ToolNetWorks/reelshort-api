"""Opt-in live tests against the real ReelShort site.

These are skipped unless explicitly selected:  pytest -m live
They are polite (few requests), metadata-only, and never touch video endpoints.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.crawler.parser import parse_movie_page, parse_movie_payload
from app.crawler.source import ReelShortSource

pytestmark = pytest.mark.live

EN_BOOK = "6a5191d8fa9f9f7a09081789"
VI_BOOK = "6a6da2cc39cbfeadc50d1e6a"


def live_settings() -> Settings:
    return Settings(
        source_requests_per_minute=10,
        source_burst=2,
        source_concurrency=1,
        source_max_retries=2,
        log_file=None,
        db_path="/tmp/opencode/live-test.sqlite3",
    )


@pytest.mark.asyncio
async def test_live_movie_page_both_samples() -> None:
    async with ReelShortSource(live_settings()) as source:
        for book_id, lang, first_chapter in (
            (EN_BOOK, "en", "1gv8oeozxn"),
            (VI_BOOK, "vi", "4h2druta3o"),
        ):
            response = await source.movie_page(book_id)
            movie = parse_movie_page(response.text, source_url=response.url)
            assert movie.book_id == book_id
            assert movie.lang == lang
            assert movie.chapter_count == 51
            assert movie.first_chapter_id == first_chapter


@pytest.mark.asyncio
async def test_live_book_info_api() -> None:
    async with ReelShortSource(live_settings()) as source:
        payload = await source.movie_info(EN_BOOK, lang="en")
        movie = parse_movie_payload(payload)
        assert movie.book_id == EN_BOOK
        assert movie.chapter_count == 51
        assert len(movie.tag_ids) > 5


@pytest.mark.asyncio
async def test_live_search_finds_editions() -> None:
    async with ReelShortSource(live_settings()) as source:
        response = await source.search_page("vi", "Thần Đèn Vĩ Đại", page=1)
        from app.crawler.parser import parse_search_page

        candidates = parse_search_page(response.text, lang_hint="vi")
        assert any(candidate.book_id == VI_BOOK for candidate in candidates)
