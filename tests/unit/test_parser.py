"""Unit tests for the ReelShort metadata parsers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.crawler.parser import (
    ParseError,
    extract_book_id_from_url,
    extract_next_data,
    normalize_title,
    parse_chapter_list,
    parse_movie_page,
    parse_movie_payload,
    parse_search_page,
    parse_tag_catalog,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_parse_movie_page_english() -> None:
    payload = load("next_data_en.json")
    html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'
    movie = parse_movie_page(html, source_url="https://www.reelshort.com/movie/the-great-and-powerful-genie")
    assert movie.book_id == "6a5191d8fa9f9f7a09081789"
    assert movie.lang == "en"
    assert movie.chapter_count == 51
    assert movie.first_chapter_id == "1gv8oeozxn"
    assert movie.t_book_id == "204200150000005664"
    assert movie.s_book_id == 136032
    assert movie.book_title == "The Great and Powerful Genie"
    assert movie.paid_start == 10
    assert movie.has_dub is False
    assert movie.tags == ["Playing Dumb"], "curated tags come from the `tag` field"
    assert len(movie.tag_ids) == 26, "the full tag taxonomy ids drive cross-language matching"
    assert movie.tag_map[movie.tag_ids[0]] == "Male"
    assert len(movie.episodes) == 51
    assert movie.episodes[0].serial_number == 1


def test_parse_movie_page_vietnamese() -> None:
    payload = load("next_data_vi.json")
    html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'
    movie = parse_movie_page(html)
    assert movie.book_id == "6a6da2cc39cbfeadc50d1e6a"
    assert movie.lang == "vi"
    assert movie.chapter_count == 51
    assert movie.first_chapter_id == "4h2druta3o"
    assert movie.t_book_id == "244201150000005664"
    assert movie.is_dub is True
    assert movie.original_lang == "en"


def test_no_invented_values_when_fields_missing() -> None:
    minimal = {"props": {"pageProps": {"data": {"book_id": "abc123def456abc123def456", "book_title": "Tiny"}}}}
    html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(minimal)}</script>'
    movie = parse_movie_page(html)
    assert movie.lang == "unknown"
    assert movie.chapter_count is None
    assert movie.first_chapter_id is None
    assert movie.paid_start is None
    assert movie.t_book_id is None
    assert movie.tags == []


def test_missing_next_data_raises() -> None:
    with pytest.raises(ParseError):
        extract_next_data("<html><body>nothing</body></html>")


def test_movie_payload_drops_play_info() -> None:
    payload = load("book_info_en.json")["data"]
    payload["start_play"] = {"chapter_id": "x", "play_info": "SECRET-STREAM-TOKEN"}
    movie = parse_movie_payload(payload)
    assert movie.chapter_count == 51
    serialized = json.dumps(movie.raw)
    assert "SECRET-STREAM-TOKEN" not in serialized
    assert "play_info" not in serialized


def test_chapter_list_parse_and_cap() -> None:
    payload = load("chapter_list_en.json")["data"]
    for chapter in payload["chapter_list"]:
        chapter["play_info"] = "SECRET"
    episodes, info = parse_chapter_list(payload)
    assert info["chapter_count"] == 51
    assert len(episodes) == 51
    assert episodes[0].chapter_id == "1gv8oeozxn"
    assert episodes[-1].serial_number == 51
    assert all("SECRET" not in (episode.description or "") for episode in episodes)
    assert episodes[0].duration_seconds == 283


def test_chapter_list_keeps_serial_zero_teaser() -> None:
    """Serial 0 is a real upstream teaser row: keep the id and flag it as a preview."""
    payload = {
        "book_id": "68fb5b66bf28974a1f03e4ab",
        "chapter_count": 61,
        "total": 62,
        "chapter_list": [
            {
                "chapter_id": "ukfanv4sxz",
                "serial_number": 0,
                "duration": 66,
                "video_pic": "https://v-mps.crazymaplestudios.com/cover.jpg",
                "desc": "teaser",
                "play_info": "SECRET",
            },
            {
                "chapter_id": "081w9q180e",
                "serial_number": 1,
                "duration": 139,
                "desc": "real episode",
            },
        ],
    }
    episodes, info = parse_chapter_list(payload)
    assert info["chapter_count"] == 61
    assert [episode.serial_number for episode in episodes] == [0, 1]
    teaser = episodes[0]
    assert teaser.chapter_id == "ukfanv4sxz"
    assert teaser.is_preview is True
    assert teaser.duration_seconds == 66
    assert episodes[1].is_preview is None, "regular episodes keep upstream's missing flag as None"
    # a zero `serial_number` must never make the parser fall through to other keys
    payload_zero_index = {
        "chapter_list": [{"chapter_id": "aaa111", "serial_number": 0, "index": 17}],
    }
    episodes, _ = parse_chapter_list(payload_zero_index)
    assert [episode.serial_number for episode in episodes] == [0]


def test_parse_search_page_returns_candidates() -> None:
    payload = load("search_vi.json")
    html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'
    candidates = parse_search_page(html, lang_hint="vi")
    ids = {candidate.book_id for candidate in candidates}
    assert "6a6da2cc39cbfeadc50d1e6a" in ids
    assert all(candidate.lang == "vi" for candidate in candidates)
    assert all(candidate.source == "search" for candidate in candidates)


def test_parse_tag_catalog() -> None:
    payload = load("tag_catalog_vi.json")["data"]
    books, total = parse_tag_catalog(payload, "vi")
    assert total > 0
    assert books
    assert all(book.lang == "vi" for book in books)
    assert all("chapter_count" in book.extra or book.chapter_count is not None for book in books)


def test_extract_book_id_from_url_variants() -> None:
    assert extract_book_id_from_url("https://www.reelshort.com/movie/the-great-and-powerful-genie-6a5191d8fa9f9f7a09081789") == "6a5191d8fa9f9f7a09081789"
    assert extract_book_id_from_url("https://www.reelshort.com/vi/movie/6a6da2cc39cbfeadc50d1e6a") == "6a6da2cc39cbfeadc50d1e6a"
    assert extract_book_id_from_url("https://www.reelshort.com/movie/x?book_id=6a6da2cc39cbfeadc50d1e6a") == "6a6da2cc39cbfeadc50d1e6a"
    assert extract_book_id_from_url("https://www.reelshort.com/not-a-movie") is None


def test_normalize_title() -> None:
    assert normalize_title("The Great & Powerful Genie!") == "the great powerful genie"
    assert normalize_title("Thần Đèn Vĩ Đại và Toàn Năng") == "thần đèn vĩ đại và toàn năng"
