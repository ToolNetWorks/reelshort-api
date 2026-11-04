"""Refresh test fixtures from the live public ReelShort metadata endpoints.

Run manually (opt-in, network required):
    .venv/bin/python scripts/refresh_fixtures.py

Fixtures contain *metadata only* — no video URLs, stream tokens or DRM data.
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
BASE = "https://www.reelshort.com"
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0 Safari/537.36"
)

EN_BOOK = "6a5191d8fa9f9f7a09081789"
VI_BOOK = "6a6da2cc39cbfeadc50d1e6a"
ES_BOOK = "6a702f0f85d596905908bd4b"
# Search returns these ids for the same Spanish edition; the official
# getBookInfo endpoint resolves them to ES_BOOK (alias ids).
ES_ALIAS_BOOKS = ["6a95598dfe80db350c089b5e", "6a66ccd3cbbafb058e0a7a4c", "6a95598cfe80db350c089b52"]


def get(url: str, lang: str = "en") -> str:
    request = urllib.request.Request(url)
    request.add_header("User-Agent", UA)
    request.add_header("Accept", "application/json,text/html;q=0.9")
    request.add_header("lang", lang)
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return response.read().decode("utf-8", "replace")


def next_data(html: str) -> dict:
    match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>', html, re.S
    )
    if not match:
        raise RuntimeError("__NEXT_DATA__ not found")
    return json.loads(match.group(1))


def page_fixture(book_id: str, slug_hint: str) -> dict:
    html = get(f"{BASE}/movie/{book_id}")
    data = next_data(html)
    movie = data["props"]["pageProps"]["data"]
    keep = {
        "s_book_id", "book_id", "book_title", "book_type", "book_genre", "book_source",
        "book_pic", "special_desc", "lang", "t_book_id", "tag", "update_status",
        "is_preview", "read_count", "collect_count", "online_base", "total", "is_paid",
        "paid_start", "original_lang", "tag_list", "online_at", "publish_at",
        "book_sub_title", "is_dub", "is_ai", "has_dub", "fandom_urls", "book_mark",
    }
    payload = {
        "props": {"pageProps": {"data": {k: movie.get(k) for k in keep}, "__lang": movie.get("lang")}},
        "page": "/movie/[slug]",
        "query": {"slug": slug_hint},
        "buildId": data.get("buildId", "fixture"),
        "locale": data.get("locale", "en"),
    }
    return payload


def write(name: str, payload: object) -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    path = FIXTURES / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {path.relative_to(ROOT)} ({path.stat().st_size} bytes)")


def main() -> int:
    # movie pages (Next.js data payloads)
    write("next_data_en.json", page_fixture(EN_BOOK, "the-great-and-powerful-genie-" + EN_BOOK))
    time.sleep(1)
    write("next_data_vi.json", page_fixture(VI_BOOK, "than-den-vi-dai-va-toan-nang-" + VI_BOOK))

    # official JSON metadata endpoints
    for name, book, lang in (
        ("book_info_en.json", EN_BOOK, "en"),
        ("book_info_vi.json", VI_BOOK, "vi"),
        ("book_info_es.json", ES_BOOK, "es"),
    ):
        time.sleep(1)
        payload = json.loads(get(f"{BASE}/api/video/book/getBookInfo?book_id={book}", lang))
        payload["data"].pop("start_play", None)  # never store stream payloads
        write(name, payload)

    # alias ids: getBookInfo resolves them to ES_BOOK (recorded verbatim)
    for index, alias in enumerate(ES_ALIAS_BOOKS):
        time.sleep(1)
        payload = json.loads(get(f"{BASE}/api/video/book/getBookInfo?book_id={alias}", "es"))
        payload["data"].pop("start_play", None)
        write(f"book_info_es_alias{index + 1}.json", payload)

    for name, book, lang in (
        ("chapter_list_en.json", EN_BOOK, "en"),
        ("chapter_list_vi.json", VI_BOOK, "vi"),
        ("chapter_list_es.json", ES_BOOK, "es"),
    ):
        time.sleep(1)
        payload = json.loads(
            get(f"{BASE}/api/video/book/getChapterList?book_id={book}&page=1&page_size=100", lang)
        )
        for chapter in payload["data"].get("chapter_list", []):
            chapter.pop("play_info", None)
        payload["data"].pop("first_chapter", None)
        write(name, payload)

    # per-locale searches
    for name, lang, keywords in (
        ("search_vi.json", "vi", "Thần Đèn Vĩ Đại và Toàn Năng"),
        ("search_es.json", "es", "The Great and Powerful Genie"),
        ("search_en.json", "en", "The Great and Powerful Genie"),
    ):
        time.sleep(1)
        query = urllib.parse.urlencode({"keywords": keywords})
        html = get(f"{BASE}/{lang}/search?{query}", lang)
        data = next_data(html)
        page_props = data["props"]["pageProps"]
        books = page_props.get("books") or []
        write(
            name,
            {
                "props": {"pageProps": {"books": books, "total": page_props.get("total")}},
                "locale": data.get("locale", lang),
                "query": {"keywords": keywords},
            },
        )

    time.sleep(1)
    catalog = json.loads(
        get(
            f"{BASE}/api/video/book/getTagBook?tag_id=6348f5093c6ca761764d85f3&page=1&page_size=50&language=vi",
            "vi",
        )
    )
    write("tag_catalog_vi.json", catalog)

    # expected values used by assertions
    en = json.loads((FIXTURES / "next_data_en.json").read_text(encoding="utf-8"))
    vi = json.loads((FIXTURES / "next_data_vi.json").read_text(encoding="utf-8"))
    write(
        "expected.json",
        {
            "en": {
                "book_id": en["props"]["pageProps"]["data"]["book_id"],
                "lang": "en",
                "chapter_count": 51,
                "first_chapter_id": "1gv8oeozxn",
            },
            "vi": {
                "book_id": vi["props"]["pageProps"]["data"]["book_id"],
                "lang": "vi",
                "chapter_count": 51,
                "first_chapter_id": "4h2druta3o",
            },
            "es_edition_book_id": ES_BOOK,
            "es_alias_book_ids": ES_ALIAS_BOOKS,
        },
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
