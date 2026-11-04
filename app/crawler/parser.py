"""Parsers for ReelShort public HTML (__NEXT_DATA__) and JSON payloads.

Only metadata is extracted. ``play_info`` (encrypted stream payload), video
manifest URLs and any DRM material are explicitly dropped.
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>', re.S
)
_SCRIPT_RE = re.compile(r"<script[^>]*>(.*?)</script>", re.S)
_LD_JSON_RE = re.compile(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# Keys that must never be persisted (protected / streaming material).
FORBIDDEN_KEYS = {
    "play_info", "video_url", "video_path", "stream_url", "m3u8", "drm",
    "license", "license_url", "sts_token", "sts", "hls", "mpd", "token",
}
_SENSITIVE_SUFFIXES = ("_url", "_token", "_key")


@dataclass
class ParsedEpisode:
    chapter_id: str
    serial_number: int
    duration_seconds: int | None = None
    thumb_url: str | None = None
    description: str | None = None
    title: str | None = None
    publish_at: int | None = None
    status: int | None = None
    chapter_type: int | None = None
    like_count: int | None = None
    is_preview: bool | None = None


@dataclass
class ParsedMovie:
    book_id: str
    book_title: str
    lang: str
    t_book_id: str | None = None
    s_book_id: int | None = None
    book_pic: str | None = None
    special_desc: str | None = None
    chapter_count: int | None = None
    first_chapter_id: str | None = None
    paid_start: int | None = None
    has_dub: bool | None = None
    is_dub: bool | None = None
    original_lang: str | None = None
    is_paid: bool | None = None
    is_ai: bool | None = None
    book_genre: int | None = None
    book_source: int | None = None
    book_type: int | None = None
    update_status: int | None = None
    publish_at: int | None = None
    online_at: int | None = None
    read_count: int | None = None
    collect_count: int | None = None
    tags: list[str] = field(default_factory=list)
    tag_ids: list[str] = field(default_factory=list)
    tag_map: dict[str, str] = field(default_factory=dict)
    category: str | None = None
    source_url: str | None = None
    episodes: list[ParsedEpisode] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def fetched_at(self) -> datetime:
        return datetime.now(tz=timezone.utc)


@dataclass
class SearchCandidate:
    book_id: str
    book_title: str
    lang: str
    t_book_id: str | None = None
    s_book_id: int | None = None
    book_pic: str | None = None
    chapter_count: int | None = None
    is_dub: bool | None = None
    source: str = "search"
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParsedCatalogPage:
    lang: str
    page: int
    page_size: int
    total: int
    total_pages: int
    discovered_count: int
    movies: list[ParsedMovie] = field(default_factory=list)
    next_page_link: str | None = None
    source_url: str | None = None


class ParseError(ValueError):
    """Raised when a page/payload does not contain usable metadata."""


def clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = _TAG_RE.sub(" ", value)
    text = html_lib.unescape(text)
    text = _WS_RE.sub(" ", text).strip()
    return text or None


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if re.fullmatch(r"-?\d+", stripped):
            return int(stripped)
    return None


def _as_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "1", "yes"):
            return True
        if low in ("false", "0", "no", ""):
            return False
    return None


def _strip_sensitive(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``payload`` with protected/streaming keys removed."""
    clean: dict[str, Any] = {}
    for key, value in payload.items():
        lowered = key.lower()
        if lowered in FORBIDDEN_KEYS or lowered.endswith(("play_info", "stream_url")):
            continue
        if lowered.endswith(_SENSITIVE_SUFFIXES) and lowered not in ("book_pic", "video_pic", "fandom_url"):
            continue
        if isinstance(value, dict):
            clean[key] = _strip_sensitive(value)
        elif isinstance(value, list):
            clean[key] = [
                _strip_sensitive(item) if isinstance(item, dict) else item for item in value
            ]
        else:
            clean[key] = value
    return clean


def extract_next_data(html: str) -> dict[str, Any]:
    match = _NEXT_DATA_RE.search(html)
    if not match:
        raise ParseError("__NEXT_DATA__ not found in HTML")
    raw = match.group(1).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Next.js sometimes escapes the payload; try a permissive second pass.
        try:
            return json.loads(raw.replace("\\u003c", "<").replace("\\u003e", ">"))
        except json.JSONDecodeError as exc:  # pragma: no cover - malformed page
            raise ParseError(f"invalid __NEXT_DATA__ JSON: {exc}") from exc


def parse_movie_page(html: str, source_url: str | None = None) -> ParsedMovie:
    """Parse a public ReelShort movie page into :class:`ParsedMovie`."""
    data = extract_next_data(html)
    page_props = data.get("props", {}).get("pageProps", {})
    movie = page_props.get("data")
    if not isinstance(movie, dict):
        raise ParseError("pageProps.data missing — not a movie page")
    return _parse_movie_dict(movie, source_url=source_url, fallback_lang=data.get("locale"))


def parse_movie_payload(payload: dict[str, Any], source_url: str | None = None) -> ParsedMovie:
    """Parse the official ``getBookInfo`` payload."""
    if not isinstance(payload, dict):
        raise ParseError("payload is not an object")
    return _parse_movie_dict(payload, source_url=source_url)


def _parse_tag_list(movie: dict[str, Any]) -> tuple[list[str], list[str], dict[str, str]]:
    """Return (curated tags, tag ids, id→name map).

    ``tag`` holds ReelShort's short curated tags ("Playing Dumb"), while
    ``tag_list`` carries the full taxonomy **with stable ids** that are shared
    across language editions — those ids drive cross-language matching.
    """
    curated: list[str] = []
    raw_tags = movie.get("tag")
    if isinstance(raw_tags, list):
        curated = [n for n in (clean_text(t) for t in raw_tags if isinstance(t, str)) if n]
    elif isinstance(raw_tags, str):
        curated = [clean_text(raw_tags) or ""]
        curated = [c for c in curated if c]

    names: list[str] = []
    ids: list[str] = []
    mapping: dict[str, str] = {}
    tag_list = movie.get("tag_list")
    if isinstance(tag_list, list) and tag_list:
        for entry in tag_list:
            if isinstance(entry, dict):
                name = clean_text(entry.get("text") or entry.get("name"))
                tag_id = entry.get("id") or entry.get("_id")
                if tag_id:
                    tag_id = str(tag_id)
                    ids.append(tag_id)
                    if name:
                        mapping[tag_id] = name
                if name:
                    names.append(name)
            elif isinstance(entry, str):
                names.append(entry)
    names = list(dict.fromkeys(names))
    ids = list(dict.fromkeys(ids))
    tags = curated or names
    return tags, ids, mapping


def _first_int(*values: Any) -> int | None:
    """Return the first value that parses as an int (0 is a valid serial)."""
    for value in values:
        parsed = _as_int(value)
        if parsed is not None:
            return parsed
    return None


def _parse_episode(entry: dict[str, Any]) -> ParsedEpisode | None:
    chapter_id = entry.get("chapter_id") or entry.get("chapterId")
    serial = _first_int(entry.get("serial_number"), entry.get("chapter_index"), entry.get("index"))
    if not chapter_id or serial is None:
        return None
    is_preview = _as_bool(entry.get("is_preview"))
    if is_preview is None and serial == 0:
        # Upstream marks the teaser/trailer block as serial 0; it is not part
        # of ``chapter_count`` and is surfaced as a preview episode.
        is_preview = True
    return ParsedEpisode(
        chapter_id=str(chapter_id),
        serial_number=serial,
        duration_seconds=_as_int(entry.get("duration")),
        thumb_url=entry.get("video_pic") or entry.get("thumb_url") or entry.get("cover"),
        description=clean_text(entry.get("desc") or entry.get("description")),
        title=clean_text(entry.get("title") or entry.get("chapter_title")),
        publish_at=_as_int(entry.get("publish_at")),
        status=_as_int(entry.get("status")),
        chapter_type=_as_int(entry.get("chapter_type")),
        like_count=_as_int(entry.get("like_count")),
        is_preview=is_preview,
    )


def _parse_movie_dict(
    movie: dict[str, Any], source_url: str | None, fallback_lang: str | None = None
) -> ParsedMovie:
    book_id = movie.get("book_id") or movie.get("bookId") or movie.get("_id")
    title = clean_text(movie.get("book_title") or movie.get("title"))
    if not book_id:
        raise ParseError("book_id missing")
    if not title:
        raise ParseError(f"book_title missing for {book_id}")
    lang = (movie.get("lang") or fallback_lang or "").strip() or "unknown"
    names, ids, mapping = _parse_tag_list(movie)
    episodes: list[ParsedEpisode] = []
    for key in ("online_base", "chapter_list", "chapters"):
        raw_list = movie.get(key)
        if isinstance(raw_list, list):
            for entry in raw_list:
                if isinstance(entry, dict):
                    parsed = _parse_episode(entry)
                    if parsed:
                        episodes.append(parsed)
    episodes.sort(key=lambda e: e.serial_number)

    first_chapter_id = movie.get("first_chapter_id")
    if not first_chapter_id:
        first = movie.get("first_chapter")
        if isinstance(first, dict):
            first_chapter_id = first.get("chapter_id")
    if not first_chapter_id:
        start_play = movie.get("start_play")
        if isinstance(start_play, dict) and len(episodes) <= 1:
            first_chapter_id = start_play.get("chapter_id")
    if not first_chapter_id and episodes:
        first_chapter_id = episodes[0].chapter_id

    chapter_count = _first_int(movie.get("chapter_count"), movie.get("total"))
    if (chapter_count is None or chapter_count == 0) and episodes:
        chapter_count = len(episodes)

    raw = _strip_sensitive(movie)
    raw.pop("online_base", None)
    raw.pop("chapter_list", None)

    return ParsedMovie(
        book_id=str(book_id),
        book_title=title,
        lang=lang,
        t_book_id=str(movie["t_book_id"]) if movie.get("t_book_id") is not None else None,
        s_book_id=_as_int(movie.get("s_book_id")),
        book_pic=movie.get("book_pic"),
        special_desc=clean_text(movie.get("special_desc")),
        chapter_count=chapter_count,
        first_chapter_id=str(first_chapter_id) if first_chapter_id else None,
        paid_start=_as_int(movie.get("paid_start")),
        has_dub=_as_bool(movie.get("has_dub")),
        is_dub=_as_bool(movie.get("is_dub")),
        original_lang=(movie.get("original_lang") or None),
        is_paid=_as_bool(movie.get("is_paid")),
        is_ai=_as_bool(movie.get("is_ai")),
        book_genre=_as_int(movie.get("book_genre")),
        book_source=_as_int(movie.get("book_source")),
        book_type=_as_int(movie.get("book_type")),
        update_status=_as_int(movie.get("update_status")),
        publish_at=_as_int(movie.get("publish_at")),
        online_at=_as_int(movie.get("online_at")),
        read_count=_as_int(movie.get("read_count")),
        collect_count=_as_int(movie.get("collect_count")),
        tags=names,
        tag_ids=ids,
        tag_map=mapping,
        source_url=source_url,
        episodes=episodes,
        raw=raw,
    )


def parse_chapter_list(payload: dict[str, Any]) -> tuple[list[ParsedEpisode], dict[str, Any]]:
    """Parse ``getChapterList`` into episodes plus book-level attributes."""
    raw_list = payload.get("chapter_list") or payload.get("list") or []
    episodes: list[ParsedEpisode] = []
    for entry in raw_list:
        if isinstance(entry, dict):
            parsed = _parse_episode(entry)
            if parsed:
                episodes.append(parsed)
    episodes.sort(key=lambda e: e.serial_number)
    info = {
        "book_id": payload.get("book_id"),
        "t_book_id": payload.get("t_book_id"),
        "book_title": clean_text(payload.get("book_title")),
        "book_pic": payload.get("book_pic"),
        "lang": payload.get("lang"),
        "chapter_count": _as_int(payload.get("chapter_count")) or _as_int(payload.get("total")),
        "has_dub": _as_bool(payload.get("has_dub")),
        "book_source": _as_int(payload.get("book_source")),
        "publish_at": _as_int(payload.get("publish_at")),
        "collect_count": _as_int(payload.get("collect_count")),
        "page": _as_int(payload.get("page")) or 1,
        "page_size": _as_int(payload.get("page_size")) or len(episodes),
    }
    first = payload.get("first_chapter")
    if isinstance(first, dict):
        parsed_first = _parse_episode(first)
        if parsed_first:
            info["first_chapter_id"] = parsed_first.chapter_id
    return episodes, info


def parse_search_page(html: str, lang_hint: str | None = None) -> list[SearchCandidate]:
    """Parse the SSR search page (``/{lang}/search?keywords=``) results."""
    data = extract_next_data(html)
    page_props = data.get("props", {}).get("pageProps", {})
    books = page_props.get("books") or []
    fallback_lang = lang_hint or data.get("locale")
    out: list[SearchCandidate] = []
    for entry in books:
        if not isinstance(entry, dict):
            continue
        book_id = entry.get("book_id") or entry.get("_id")
        title = clean_text(entry.get("book_title") or entry.get("title"))
        if not book_id or not title:
            continue
        out.append(
            SearchCandidate(
                book_id=str(book_id),
                book_title=title,
                lang=(entry.get("lang") or fallback_lang or "unknown"),
                t_book_id=str(entry["t_book_id"]) if entry.get("t_book_id") is not None else None,
                s_book_id=_as_int(entry.get("s_book_id")),
                book_pic=entry.get("book_pic"),
                chapter_count=_as_int(entry.get("chapter_count")) or _as_int(entry.get("total")),
                is_dub=_as_bool(entry.get("is_dub")),
                source="search",
                extra={
                    "total_available": page_props.get("total"),
                    "keywords": page_props.get("keywords"),
                },
            )
        )
    return out


def parse_tag_catalog(payload: dict[str, Any], lang: str) -> tuple[list[SearchCandidate], int]:
    """Parse the official ``getTagBook`` catalogue into candidates."""
    books = payload.get("books") or []
    out: list[SearchCandidate] = []
    for entry in books:
        if not isinstance(entry, dict):
            continue
        book_id = entry.get("book_id") or entry.get("_id")
        title = clean_text(entry.get("book_title"))
        if not book_id or not title:
            continue
        out.append(
            SearchCandidate(
                book_id=str(book_id),
                book_title=title,
                lang=lang,
                t_book_id=str(entry["t_book_id"]) if entry.get("t_book_id") is not None else None,
                s_book_id=_as_int(entry.get("s_book_id")),
                book_pic=entry.get("book_pic"),
                chapter_count=_as_int(entry.get("chapter_count")),
                source="tag_catalog",
                extra={
                    "tag_names": entry.get("tag") or [],
                    "tag_lang": entry.get("tag_lang") or [],
                    "book_genre": _as_int(entry.get("book_genre")),
                    "book_source": _as_int(entry.get("book_source")),
                    "first_chapter_id": entry.get("chapter_id"),
                    "read_count": _as_int(entry.get("read_count")),
                },
            )
        )
    total = _as_int(payload.get("total_items")) or len(out)
    return out, total


def extract_ld_json(html: str) -> list[dict[str, Any]]:
    """Extract schema.org data (used as a secondary title/episode-count source)."""
    out: list[dict[str, Any]] = []
    for match in _LD_JSON_RE.finditer(html):
        try:
            parsed = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        graph = parsed.get("@graph") if isinstance(parsed, dict) else None
        if isinstance(graph, list):
            out.extend(item for item in graph if isinstance(item, dict))
        elif isinstance(parsed, dict):
            out.append(parsed)
    return out


def extract_book_id_from_url(url: str) -> str | None:
    """Accept /movie/<slug>-<book_id>, /movie/<book_id>, query ?book_id=."""
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    for key in ("book_id", "bookId", "id"):
        if key in query and query[key]:
            return query[key][0]
    match = re.search(r"/(?:movie|drama)/([^/?#]+)", parsed.path)
    if not match:
        return None
    slug = match.group(1)
    candidate = slug.rsplit("-", 1)[-1]
    if re.fullmatch(r"[0-9a-f]{16,32}", candidate):
        return candidate
    if re.fullmatch(r"[0-9a-f]{16,32}", slug):
        return slug
    return None


def normalize_title(title: str) -> str:
    text = html_lib.unescape(title or "").lower()
    text = re.sub(r"[^\w\s]+", " ", text, flags=re.UNICODE)
    text = _WS_RE.sub(" ", text).strip()
    return text


def parse_homepage_catalog(html: str, target_lang: str) -> list[ParsedMovie]:
    """Parse a ReelShort homepage HTML (SSR __NEXT_DATA__) into a deduplicated list of ParsedMovie.

    Extracts:
    * Banners (from jump_param)
    * All shelves in bookShelfList (New Releases, Rankings, themed shelves, recommendations)
    Deduplicates by book_id and preserves tags, chapter_count, description, poster, and shelf category.
    """
    clean_lang = target_lang.strip().lower()
    data = extract_next_data(html)
    page_props = data.get("props", {}).get("pageProps", {})
    fallback = page_props.get("fallback") or {}

    # Find the webInfo block in fallback
    web_info = None
    if isinstance(fallback, dict):
        web_info = fallback.get("/api/ms/hall/webInfo")
        if not web_info:
            for k, v in fallback.items():
                if isinstance(v, dict) and ("banners" in v or "bookShelfList" in v):
                    web_info = v
                    break

    if not isinstance(web_info, dict):
        if "banners" in page_props or "bookShelfList" in page_props:
            web_info = page_props
        else:
            raise ParseError("No hall/webInfo catalog data found in homepage HTML")

    banners = web_info.get("banners") or []
    shelves = web_info.get("bookShelfList") or []

    items_by_id: dict[str, dict[str, Any]] = {}

    def _register_book(raw_book: dict[str, Any], default_category: str | None = None) -> None:
        bid = raw_book.get("book_id") or raw_book.get("bookId") or raw_book.get("_id")
        if not bid:
            return
        bid = str(bid).strip()
        title = clean_text(raw_book.get("book_title") or raw_book.get("title"))
        if not title:
            return

        pic = raw_book.get("book_pic") or raw_book.get("default_pic") or raw_book.get("pic")
        desc = clean_text(raw_book.get("special_desc") or raw_book.get("desc") or raw_book.get("description"))
        chapter_count = _first_int(raw_book.get("chapter_count"), raw_book.get("total"))
        paid_start = _first_int(raw_book.get("paid_start"))
        t_book_id = str(raw_book["t_book_id"]) if raw_book.get("t_book_id") is not None else None
        s_book_id = _as_int(raw_book.get("s_book_id"))
        book_genre = _as_int(raw_book.get("book_genre"))
        book_source = _as_int(raw_book.get("book_source"))
        book_type = _as_int(raw_book.get("book_type"))
        is_paid = _as_bool(raw_book.get("is_paid"))
        has_dub = _as_bool(raw_book.get("has_dub"))
        is_dub = _as_bool(raw_book.get("is_dub"))
        read_count = _first_int(raw_book.get("read_count"))
        collect_count = _first_int(raw_book.get("collect_count"))

        start_play = raw_book.get("start_play") if isinstance(raw_book.get("start_play"), dict) else {}
        first_chapter_id = raw_book.get("chapter_id") or start_play.get("chapter_id") or raw_book.get("first_chapter_id")
        if first_chapter_id:
            first_chapter_id = str(first_chapter_id).strip()

        curated_tags, tag_ids, tag_mapping = _parse_tag_list(raw_book)
        theme = raw_book.get("theme")
        if isinstance(theme, list):
            for t in theme:
                ct = clean_text(t) if isinstance(t, str) else None
                if ct and ct not in curated_tags:
                    curated_tags.append(ct)
        theme_list = raw_book.get("theme_list")
        if isinstance(theme_list, list):
            for t in theme_list:
                if isinstance(t, dict):
                    t_id = str(t.get("tag_id")) if t.get("tag_id") else None
                    t_name = clean_text(t.get("tag_name"))
                    if t_name and t_name not in curated_tags:
                        curated_tags.append(t_name)
                    if t_id and t_name:
                        tag_mapping[t_id] = t_name
                    if t_id and t_id not in tag_ids:
                        tag_ids.append(t_id)

        category = default_category or raw_book.get("category") or "Catalog"

        if bid not in items_by_id:
            items_by_id[bid] = {
                "book_id": bid,
                "book_title": title,
                "lang": clean_lang,
                "t_book_id": t_book_id,
                "s_book_id": s_book_id,
                "book_pic": pic,
                "special_desc": desc,
                "chapter_count": chapter_count,
                "first_chapter_id": first_chapter_id,
                "paid_start": paid_start,
                "has_dub": has_dub,
                "is_dub": is_dub,
                "is_paid": is_paid,
                "book_genre": book_genre,
                "book_source": book_source,
                "book_type": book_type,
                "read_count": read_count,
                "collect_count": collect_count,
                "tags": curated_tags,
                "tag_ids": tag_ids,
                "tag_map": tag_mapping,
                "category": category,
                "raw": _strip_sensitive(raw_book),
            }
        else:
            existing = items_by_id[bid]
            if not existing["book_pic"] and pic:
                existing["book_pic"] = pic
            if not existing["special_desc"] and desc:
                existing["special_desc"] = desc
            if not existing["first_chapter_id"] and first_chapter_id:
                existing["first_chapter_id"] = first_chapter_id
            if not existing["chapter_count"] and chapter_count:
                existing["chapter_count"] = chapter_count
            if not existing["t_book_id"] and t_book_id:
                existing["t_book_id"] = t_book_id
            if not existing["paid_start"] and paid_start:
                existing["paid_start"] = paid_start
            for t in curated_tags:
                if t not in existing["tags"]:
                    existing["tags"].append(t)
            for tid in tag_ids:
                if tid not in existing["tag_ids"]:
                    existing["tag_ids"].append(tid)
            existing["tag_map"].update(tag_mapping)

    for banner in banners:
        if not isinstance(banner, dict):
            continue
        jp = banner.get("jump_param")
        if isinstance(jp, dict):
            _register_book(jp, default_category="Banner")
        elif banner.get("book_id"):
            _register_book(banner, default_category="Banner")

    for idx, shelf in enumerate(shelves):
        if not isinstance(shelf, dict):
            continue
        shelf_name = clean_text(shelf.get("bookshelf_name") or shelf.get("title") or shelf.get("name")) or f"Shelf {idx + 1}"
        books = shelf.get("books") or shelf.get("bookList") or []
        if isinstance(books, list):
            for bk in books:
                if isinstance(bk, dict):
                    _register_book(bk, default_category=shelf_name)

    locale_seg = "zh-TW" if clean_lang == "zh-tw" else clean_lang
    source_prefix = f"https://www.reelshort.com/{locale_seg}" if clean_lang != "en" else "https://www.reelshort.com"
    out: list[ParsedMovie] = []
    for bid, item in items_by_id.items():
        pm = ParsedMovie(
            book_id=item["book_id"],
            book_title=item["book_title"],
            lang=clean_lang,
            t_book_id=item["t_book_id"],
            s_book_id=item["s_book_id"],
            book_pic=item["book_pic"],
            special_desc=item["special_desc"],
            chapter_count=item["chapter_count"],
            first_chapter_id=item["first_chapter_id"],
            paid_start=item["paid_start"],
            has_dub=item["has_dub"],
            is_dub=item["is_dub"],
            is_paid=item["is_paid"],
            book_genre=item["book_genre"],
            book_source=item["book_source"],
            book_type=item["book_type"],
            read_count=item["read_count"],
            collect_count=item["collect_count"],
            tags=item["tags"],
            tag_ids=item["tag_ids"],
            tag_map=item["tag_map"],
            category=item["category"],
            source_url=f"{source_prefix}/movie/{bid}",
            raw=item["raw"],
        )
        out.append(pm)
    return out


def parse_all_movies_page(
    html: str,
    target_lang: str,
    *,
    source_url: str | None = None,
) -> ParsedCatalogPage:
    """Parse a ReelShort `/movie-genres/all-movies/{page}` SSR HTML page.

    Extracts `pageProps.tagBooks.books`, `pageProps.total`, and `pageProps.totalPage`
    from `script#__NEXT_DATA__`, preserving `chapter_count = 0` so incomplete
    metadata items can be flagged and queued for enrichment.
    """
    clean_lang = target_lang.strip().lower()
    data = extract_next_data(html)
    page_props = data.get("props", {}).get("pageProps", {})
    if not isinstance(page_props, dict):
        raise ParseError("pageProps missing in __NEXT_DATA__")

    tag_books = page_props.get("tagBooks")
    raw_books = None
    if isinstance(tag_books, dict):
        raw_books = tag_books.get("books")
    if raw_books is None:
        raw_books = page_props.get("books")
    if not isinstance(raw_books, list):
        raise ParseError("pageProps.tagBooks.books missing — not an all-movies catalog page")

    page_num = _first_int(
        page_props.get("page"),
        tag_books.get("page") if isinstance(tag_books, dict) else None,
    ) or 1
    page_size = _first_int(
        page_props.get("pageSize"),
        page_props.get("page_size"),
        tag_books.get("pageSize") if isinstance(tag_books, dict) else None,
    ) or 12
    total = _first_int(
        page_props.get("total"),
        tag_books.get("total") if isinstance(tag_books, dict) else None,
        tag_books.get("total_items") if isinstance(tag_books, dict) else None,
    )
    if total is None:
        total = len(raw_books)

    total_pages = _first_int(
        page_props.get("totalPage"),
        page_props.get("total_pages"),
        tag_books.get("totalPage") if isinstance(tag_books, dict) else None,
    )
    if total_pages is None or total_pages <= 0:
        total_pages = max(1, (total + max(page_size, 1) - 1) // max(page_size, 1)) if total > 0 else 1

    next_page_link = page_props.get("nextPageLink") if isinstance(page_props.get("nextPageLink"), str) else None
    locale_seg = "zh-TW" if clean_lang == "zh-tw" else clean_lang
    source_prefix = f"https://www.reelshort.com/{locale_seg}" if clean_lang != "en" else "https://www.reelshort.com"

    discovered_count = 0
    by_book_id: dict[str, ParsedMovie] = {}

    for raw_book in raw_books:
        if not isinstance(raw_book, dict):
            continue
        bid = raw_book.get("book_id") or raw_book.get("bookId") or raw_book.get("_id")
        if not bid:
            continue
        bid = str(bid).strip()
        title = clean_text(raw_book.get("book_title") or raw_book.get("title"))
        if not title:
            continue

        discovered_count += 1
        pic = raw_book.get("book_pic") or raw_book.get("default_pic") or raw_book.get("pic")
        desc = clean_text(raw_book.get("special_desc") or raw_book.get("desc") or raw_book.get("description"))
        chapter_count = _first_int(raw_book.get("chapter_count"), raw_book.get("total"))
        paid_start = _first_int(raw_book.get("paid_start"))
        t_book_id = str(raw_book["t_book_id"]) if raw_book.get("t_book_id") is not None else None
        s_book_id = _as_int(raw_book.get("s_book_id"))
        book_genre = _as_int(raw_book.get("book_genre"))
        book_source = _as_int(raw_book.get("book_source"))
        book_type = _as_int(raw_book.get("book_type"))
        update_status = _as_int(raw_book.get("update_status"))
        publish_at = _as_int(raw_book.get("publish_at"))
        online_at = _as_int(raw_book.get("online_at"))
        is_paid = _as_bool(raw_book.get("is_paid"))
        has_dub = _as_bool(raw_book.get("has_dub"))
        is_dub = _as_bool(raw_book.get("is_dub"))
        is_ai = _as_bool(raw_book.get("is_ai"))
        original_lang = (raw_book.get("original_lang") or None)
        read_count = _first_int(raw_book.get("read_count"))
        collect_count = _first_int(raw_book.get("collect_count"))

        start_play = raw_book.get("start_play") if isinstance(raw_book.get("start_play"), dict) else {}
        first_chapter_id = (
            raw_book.get("first_chapter_id")
            or raw_book.get("chapter_id")
            or start_play.get("chapter_id")
        )
        if first_chapter_id:
            first_chapter_id = str(first_chapter_id).strip()

        curated_tags, tag_ids, tag_mapping = _parse_tag_list(raw_book)
        theme = raw_book.get("theme")
        if isinstance(theme, list):
            for t in theme:
                ct = clean_text(t) if isinstance(t, str) else None
                if ct and ct not in curated_tags:
                    curated_tags.append(ct)
        theme_list = raw_book.get("theme_list")
        if isinstance(theme_list, list):
            for t in theme_list:
                if isinstance(t, dict):
                    t_id = str(t.get("tag_id")) if t.get("tag_id") else None
                    t_name = clean_text(t.get("tag_name"))
                    if t_name and t_name not in curated_tags:
                        curated_tags.append(t_name)
                    if t_id and t_name:
                        tag_mapping[t_id] = t_name
                    if t_id and t_id not in tag_ids:
                        tag_ids.append(t_id)

        book_mark = raw_book.get("book_mark") if isinstance(raw_book.get("book_mark"), dict) else {}
        mark_text = clean_text(book_mark.get("text")) if book_mark else None
        if mark_text and mark_text.lower() == "new":
            category = "New Release"
        elif raw_book.get("category"):
            category = clean_text(str(raw_book.get("category")))
        elif curated_tags:
            category = curated_tags[0]
        else:
            category = "All Movies"

        movie_url = f"{source_prefix}/movie/{bid}"
        pm = ParsedMovie(
            book_id=bid,
            book_title=title,
            lang=clean_lang,
            t_book_id=t_book_id,
            s_book_id=s_book_id,
            book_pic=pic,
            special_desc=desc,
            chapter_count=chapter_count,
            first_chapter_id=first_chapter_id,
            paid_start=paid_start,
            has_dub=has_dub,
            is_dub=is_dub,
            original_lang=original_lang,
            is_paid=is_paid,
            is_ai=is_ai,
            book_genre=book_genre,
            book_source=book_source,
            book_type=book_type,
            update_status=update_status,
            publish_at=publish_at,
            online_at=online_at,
            read_count=read_count,
            collect_count=collect_count,
            tags=curated_tags,
            tag_ids=tag_ids,
            tag_map=tag_mapping,
            category=category,
            source_url=movie_url,
            raw=_strip_sensitive(raw_book),
        )
        if bid not in by_book_id:
            by_book_id[bid] = pm

    return ParsedCatalogPage(
        lang=clean_lang,
        page=page_num,
        page_size=page_size,
        total=total,
        total_pages=total_pages,
        discovered_count=discovered_count,
        movies=list(by_book_id.values()),
        next_page_link=next_page_link,
        source_url=source_url,
    )

