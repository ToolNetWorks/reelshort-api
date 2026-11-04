"""Read-side queries used by the public API (pure metadata, cache friendly)."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any, Iterable

from sqlalchemy import String, func, or_, select
from sqlalchemy.orm import Session

from app.config import Settings, get_locale_catalog_page_url, get_settings
from app.crawler import store
from app.crawler.parser import normalize_title
from app.crawler.scoring import STATUS_CANDIDATE, STATUS_UNKNOWN, STATUS_VERIFIED
from app.db.models import (
    ApiUsage,
    CacheEntry,
    CatalogPageState,
    CatalogState,
    CrawlHistory,
    CrawlJob,
    EditionMatch,
    Episode,
    MovieEdition,
    MovieGroup,
    utcnow,
)


def edition_to_dict(edition: MovieEdition, *, match_status: str | None = None) -> dict[str, Any]:
    tags = list(edition.tags or [])
    genre = edition.category if edition.category and edition.category not in ("Catalog", "All Movies") else (tags[0] if tags else str(edition.book_genre) if edition.book_genre is not None else None)
    return {
        "book_id": edition.book_id,
        "lang": edition.lang,
        "language": edition.lang,
        "book_title": edition.book_title,
        "t_book_id": edition.t_book_id,
        "s_book_id": edition.s_book_id,
        "book_pic": edition.book_pic,
        "special_desc": edition.special_desc,
        "chapter_count": edition.chapter_count,
        "stored_chapter_count": edition.stored_chapter_count or 0,
        "first_chapter_id": edition.first_chapter_id,
        "paid_start": edition.paid_start,
        "is_paid": edition.is_paid,
        "has_dub": edition.has_dub,
        "is_dub": edition.is_dub,
        "original_lang": edition.original_lang,
        "category": edition.category,
        "genre": genre,
        "book_genre": edition.book_genre,
        "tags": tags,
        "read_count": edition.read_count,
        "collect_count": edition.collect_count,
        "metadata_status": edition.metadata_status or ("METADATA_INCOMPLETE" if (edition.chapter_count or 0) <= 0 else "COMPLETE"),
        "source_url": edition.source_url,
        "discovered_at": edition.discovered_at or edition.created_at,
        "last_synced_at": edition.last_synced_at or edition.fetched_at,
        "fetched_at": edition.fetched_at,
        "episodes_synced_at": edition.episodes_synced_at,
        "match_status": match_status,
    }


def movie_to_dict(
    edition: MovieEdition,
    *,
    editions: Iterable[MovieEdition | dict[str, Any]] = (),
    settings: Settings | None = None,
    original_title: str | None = None,
) -> dict[str, Any]:
    """Serialize one edition plus its sibling editions.

    ``editions`` accepts ORM rows (serialized here) or already-serialized dicts
    as produced by :func:`editions_with_status`.
    """
    settings = settings or get_settings()
    is_stale = True
    if edition.fetched_at is not None:
        is_stale = edition.fetched_at < utcnow() - timedelta(hours=settings.metadata_ttl_hours)
    stored = edition.stored_chapter_count or 0
    tags = list(edition.tags or [])
    genre = edition.category if edition.category and edition.category not in ("Catalog", "All Movies") else (tags[0] if tags else str(edition.book_genre) if edition.book_genre is not None else None)
    return {
        "group_id": edition.movie_group_id,
        "book_id": edition.book_id,
        "lang": edition.lang,
        "language": edition.lang,
        "book_title": edition.book_title,
        "original_title": original_title,
        "book_pic": edition.book_pic,
        "special_desc": edition.special_desc,
        "category": edition.category,
        "genre": genre,
        "t_book_id": edition.t_book_id,
        "s_book_id": edition.s_book_id,
        "chapter_count": edition.chapter_count,
        "episode_count": edition.chapter_count,
        "stored_episode_count": stored,
        "first_chapter_id": edition.first_chapter_id,
        "paid_start": edition.paid_start,
        "is_paid": edition.is_paid,
        "has_dub": edition.has_dub,
        "is_dub": edition.is_dub,
        "original_lang": edition.original_lang,
        "tags": tags,
        "book_genre": edition.book_genre,
        "book_source": edition.book_source,
        "update_status": edition.update_status,
        "publish_at": edition.publish_at,
        "online_at": edition.online_at,
        "read_count": edition.read_count,
        "collect_count": edition.collect_count,
        "metadata_status": edition.metadata_status or ("METADATA_INCOMPLETE" if (edition.chapter_count or 0) <= 0 else "COMPLETE"),
        "source_url": edition.source_url,
        "discovered_at": edition.discovered_at or edition.created_at,
        "last_synced_at": edition.last_synced_at or edition.fetched_at,
        "fetched_at": edition.fetched_at,
        "is_stale": is_stale,
        "editions": [e if isinstance(e, dict) else edition_to_dict(e) for e in editions],
    }


def get_edition(session: Session, book_id: str) -> MovieEdition | None:
    """Look up an edition by book_id, following alias ids to the canonical one."""
    resolved = store.resolve_alias(session, book_id)
    return session.get(MovieEdition, resolved)


def group_editions(session: Session, group_id: str | None) -> list[MovieEdition]:
    if not group_id:
        return []
    return list(
        session.scalars(
            select(MovieEdition)
            .where(MovieEdition.movie_group_id == group_id)
            .order_by(MovieEdition.lang.asc())
        )
    )


def match_status_map(session: Session, group_id: str | None) -> dict[str, str]:
    if not group_id:
        return {}
    rows = session.scalars(select(EditionMatch).where(EditionMatch.movie_group_id == group_id))
    out: dict[str, str] = {}
    for row in rows:
        for book_id in (row.source_book_id, row.target_book_id):
            current = out.get(book_id)
            priority = {"VERIFIED": 3, "CANDIDATE": 2, "UNKNOWN": 1, "REJECTED": 0}
            if current is None or priority.get(row.status, 0) > priority.get(current, 0):
                out[book_id] = row.status
    return out


def editions_with_status(session: Session, edition: MovieEdition) -> list[dict[str, Any]]:
    statuses = match_status_map(session, edition.movie_group_id)
    result = []
    for item in group_editions(session, edition.movie_group_id):
        status = statuses.get(item.book_id)
        if item.book_id == edition.book_id:
            status = status or STATUS_VERIFIED
        result.append(edition_to_dict(item, match_status=status or STATUS_UNKNOWN))
    if not result:
        result = [edition_to_dict(edition, match_status=STATUS_VERIFIED)]
    return result


def language_view(
    session: Session,
    edition: MovieEdition,
    *,
    targets: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Per-locale view: known editions plus requested-but-missing locales."""
    statuses = match_status_map(session, edition.movie_group_id)
    rows: dict[str, dict[str, Any]] = {}
    for item in group_editions(session, edition.movie_group_id):
        status = STATUS_VERIFIED if item.book_id == edition.book_id else statuses.get(item.book_id, STATUS_UNKNOWN)
        rows[item.lang] = {
            "lang": item.lang,
            "book_id": item.book_id,
            "book_title": item.book_title,
            "chapter_count": item.chapter_count,
            "stored_episode_count": item.stored_chapter_count or 0,
            "status": status,
            "confidence": None,
            "is_source": item.book_id == edition.book_id,
            "discovered_at": item.discovered_at or item.created_at,
            "note": None,
        }
    if edition.book_id not in [e.get("book_id") for e in rows.values()]:
        rows[edition.lang] = {
            "lang": edition.lang,
            "book_id": edition.book_id,
            "book_title": edition.book_title,
            "chapter_count": edition.chapter_count,
            "stored_episode_count": edition.stored_chapter_count or 0,
            "status": STATUS_VERIFIED,
            "confidence": None,
            "is_source": True,
            "discovered_at": edition.discovered_at or edition.created_at,
            "note": None,
        }
    # enrich with match confidence where available
    matches = session.scalars(
        select(EditionMatch).where(
            or_(
                EditionMatch.source_book_id == edition.book_id,
                EditionMatch.target_book_id == edition.book_id,
            )
        )
    )
    for match in matches:
        other_id = match.target_book_id if match.source_book_id == edition.book_id else match.source_book_id
        other = session.get(MovieEdition, other_id)
        if other is None:
            continue
        entry = rows.get(other.lang)
        if entry and not entry.get("is_source"):
            entry["status"] = match.status
            entry["confidence"] = match.confidence
            entry["note"] = match.note
    if targets:
        known = {row["lang"] for row in rows.values()}
        for locale in targets:
            if locale and locale not in known:
                rows[locale] = {
                    "lang": locale,
                    "book_id": None,
                    "book_title": None,
                    "chapter_count": None,
                    "stored_episode_count": 0,
                    "status": "MISSING",
                    "confidence": None,
                    "is_source": False,
                    "discovered_at": None,
                    "note": None,
                }
    return sorted(rows.values(), key=lambda item: (not item.get("is_source"), item["lang"]))


def _serialize_list_item(
    row: MovieEdition,
    *,
    edition_count: int = 1,
    available_languages: list[str] | None = None,
    requested_lang: str | None = None,
    score: float | None = None,
) -> dict[str, Any]:
    tags = list(row.tags or [])
    genre = (
        row.category
        if row.category and row.category not in ("Catalog", "All Movies")
        else (tags[0] if tags else (str(row.book_genre) if row.book_genre is not None else None))
    )
    meta_status = row.metadata_status or (
        "METADATA_INCOMPLETE"
        if (row.chapter_count is None or row.chapter_count <= 0) and (row.stored_chapter_count or 0) <= 0
        else "COMPLETE"
    )
    item: dict[str, Any] = {
        "book_id": row.book_id,
        "t_book_id": row.t_book_id,
        "lang": row.lang,
        "language": row.lang,
        "requested_lang": requested_lang or row.lang,
        "is_fallback_lang": False,
        "book_title": row.book_title,
        "book_pic": row.book_pic,
        "special_desc": row.special_desc,
        "category": row.category,
        "genre": genre,
        "book_genre": row.book_genre,
        "chapter_count": row.chapter_count,
        "stored_episode_count": row.stored_chapter_count or 0,
        "first_chapter_id": row.first_chapter_id,
        "paid_start": row.paid_start,
        "read_count": row.read_count,
        "collect_count": row.collect_count,
        "tags": tags,
        "source_url": row.source_url,
        "metadata_status": meta_status,
        "discovered_at": row.discovered_at or row.created_at,
        "last_synced_at": row.last_synced_at or row.fetched_at,
        "fetched_at": row.fetched_at,
        "group_id": row.movie_group_id or row.book_id,
        "edition_count": edition_count,
        "available_languages": available_languages if available_languages is not None else [row.lang],
    }
    if score is not None:
        item["score"] = round(score, 4)
    return item


def _group_languages(session: Session, group_ids: list[str | None]) -> dict[str, list[str]]:
    ids = list({gid for gid in group_ids if gid})
    if not ids:
        return {}
    rows = session.execute(
        select(MovieEdition.movie_group_id, MovieEdition.lang)
        .where(MovieEdition.movie_group_id.in_(ids))
    ).all()
    out: dict[str, set[str]] = {}
    for gid, l_code in rows:
        if gid and l_code:
            out.setdefault(str(gid), set()).add(str(l_code))
    return {gid: sorted(langs) for gid, langs in out.items()}


def list_movies(
    session: Session,
    *,
    lang: str | None = None,
    tag: str | None = None,
    category: str | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 20,
    order: str = "recent",
    grouped: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    clean_lang = (lang or "").strip().lower() or None
    if not grouped or clean_lang is not None:
        stmt = select(MovieEdition)
        count_stmt = select(func.count()).select_from(MovieEdition)
        if clean_lang:
            stmt = stmt.where(MovieEdition.lang == clean_lang)
            count_stmt = count_stmt.where(MovieEdition.lang == clean_lang)
        if tag:
            pattern = f"%{tag.strip()}%"
            stmt = stmt.where(MovieEdition.tags.cast(String).ilike(pattern))
            count_stmt = count_stmt.where(MovieEdition.tags.cast(String).ilike(pattern))
        if category:
            cat_clean = category.strip()
            if cat_clean.lower() == "new":
                cond = or_(
                    MovieEdition.category == "New Release",
                    MovieEdition.category.ilike("%new%"),
                    MovieEdition.category.ilike("%mới%"),
                )
            else:
                cond = or_(
                    MovieEdition.category.ilike(f"%{cat_clean}%"),
                    MovieEdition.tags.cast(String).ilike(f"%{cat_clean}%"),
                )
            stmt = stmt.where(cond)
            count_stmt = count_stmt.where(cond)
        if q and q.strip():
            needle = q.strip()
            q_pat = f"%{needle}%"
            q_cond = or_(
                MovieEdition.book_title.ilike(q_pat),
                MovieEdition.special_desc.ilike(q_pat),
                MovieEdition.tags.cast(String).ilike(q_pat),
                MovieEdition.book_id == needle,
                MovieEdition.t_book_id == needle,
            )
            stmt = stmt.where(q_cond)
            count_stmt = count_stmt.where(q_cond)

        total = int(session.scalar(count_stmt) or 0)
        if order == "title":
            stmt = stmt.order_by(MovieEdition.book_title.asc(), MovieEdition.book_id.asc())
        elif order == "episodes":
            stmt = stmt.order_by(MovieEdition.chapter_count.desc().nullslast(), MovieEdition.fetched_at.desc())
        elif order in ("top", "views"):
            stmt = stmt.order_by(
                MovieEdition.read_count.desc().nullslast(),
                MovieEdition.collect_count.desc().nullslast(),
                MovieEdition.chapter_count.desc().nullslast(),
                MovieEdition.fetched_at.desc(),
            )
        elif order == "new":
            stmt = stmt.order_by(
                (MovieEdition.category == "New Release").desc(),
                MovieEdition.publish_at.desc().nullslast(),
                MovieEdition.discovered_at.desc().nullslast(),
                MovieEdition.fetched_at.desc(),
            )
        else:
            stmt = stmt.order_by(MovieEdition.fetched_at.desc(), MovieEdition.book_id.asc())

        rows = list(session.scalars(stmt.limit(page_size).offset((page - 1) * page_size)))
        gids = [row.movie_group_id for row in rows]
        group_langs = _group_languages(session, gids)
        items = []
        for row in rows:
            langs = group_langs.get(row.movie_group_id or "", [row.lang])
            items.append(
                _serialize_list_item(
                    row,
                    edition_count=len(langs),
                    available_languages=langs,
                    requested_lang=clean_lang,
                )
            )
        return items, total

    # --- Grouped mode without a specific locale filter ---
    stmt = select(MovieEdition)
    if tag:
        pattern = f"%{tag.strip()}%"
        stmt = stmt.where(MovieEdition.tags.cast(String).ilike(pattern))
    all_editions = list(session.scalars(stmt))

    groups_map: dict[str, list[MovieEdition]] = {}
    for ed in all_editions:
        gid = ed.movie_group_id or ed.book_id
        groups_map.setdefault(gid, []).append(ed)

    grouped_items: list[dict[str, Any]] = []
    for gid, eds in groups_map.items():
        best = next((e for e in eds if e.lang == "vi"), None)
        if best is None:
            best = next((e for e in eds if e.lang == "en"), None)
        if best is None:
            best = eds[0]
        avail_langs = sorted({e.lang for e in eds})
        grouped_items.append(
            _serialize_list_item(
                best,
                edition_count=len(eds),
                available_languages=avail_langs,
                requested_lang=None,
            )
        )

    if order == "title":
        grouped_items.sort(key=lambda x: (x["book_title"] or "").casefold())
    elif order == "episodes":
        grouped_items.sort(key=lambda x: (x["chapter_count"] or 0), reverse=True)
    else:
        grouped_items.sort(key=lambda x: x["fetched_at"] or utcnow(), reverse=True)

    total = len(grouped_items)
    offset = (page - 1) * page_size
    sliced = grouped_items[offset : offset + page_size]
    return sliced, total


def _group_sizes(session: Session, group_ids: list[str | None]) -> dict[str, int]:
    ids = [gid for gid in group_ids if gid]
    if not ids:
        return {}
    rows = session.execute(
        select(MovieEdition.movie_group_id, func.count())
        .where(MovieEdition.movie_group_id.in_(ids))
        .group_by(MovieEdition.movie_group_id)
    ).all()
    return {str(gid): int(count) for gid, count in rows}


def list_episodes(
    session: Session, book_id: str, *, page: int = 1, page_size: int = 100
) -> tuple[list[Episode], int]:
    total = int(
        session.scalar(select(func.count()).select_from(Episode).where(Episode.book_id == book_id)) or 0
    )
    rows = list(
        session.scalars(
            select(Episode)
            .where(Episode.book_id == book_id)
            .order_by(Episode.serial_number.asc())
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
    )
    return rows, total


def episode_to_dict(episode: Episode) -> dict[str, Any]:
    return {
        "book_id": episode.book_id,
        "lang": episode.lang,
        "chapter_id": episode.chapter_id,
        "serial_number": episode.serial_number,
        "title": episode.title,
        "description": episode.description,
        "duration_seconds": episode.duration_seconds,
        "thumb_url": episode.thumb_url,
        "publish_at": episode.publish_at,
        "status": episode.status,
        "chapter_type": episode.chapter_type,
        "like_count": episode.like_count,
        "is_preview": episode.is_preview,
        "fetched_at": episode.fetched_at,
    }


def search_movies(
    session: Session, query: str, *, lang: str | None = None, page: int = 1, page_size: int = 20
) -> tuple[list[dict[str, Any]], int]:
    """Local (cached) search over the indexed catalogue."""
    clean_lang = (lang or "").strip().lower() or None
    needle = (query or "").strip()
    if not needle:
        return list_movies(session, lang=clean_lang, page=page, page_size=page_size)

    stmt = select(MovieEdition)
    count_stmt = select(func.count()).select_from(MovieEdition)
    if clean_lang:
        stmt = stmt.where(MovieEdition.lang == clean_lang)
        count_stmt = count_stmt.where(MovieEdition.lang == clean_lang)

    pattern = f"%{needle}%"
    condition = or_(
        MovieEdition.book_title.ilike(pattern),
        MovieEdition.special_desc.ilike(pattern),
        MovieEdition.tags.cast(String).ilike(pattern),
        MovieEdition.book_id == needle,
        MovieEdition.t_book_id == needle,
    )
    stmt = stmt.where(condition)
    count_stmt = count_stmt.where(condition)
    total = int(session.scalar(count_stmt) or 0)
    rows = list(session.scalars(stmt.order_by(MovieEdition.fetched_at.desc()).limit(1000)))
    needle_norm = normalize_title(needle)

    def score_direct(row: MovieEdition) -> float:
        import difflib

        if row.book_id == needle or (row.t_book_id and row.t_book_id == needle):
            return 1.0
        title = normalize_title(row.book_title or "")
        ratio = difflib.SequenceMatcher(None, needle_norm, title).ratio() if needle_norm else 0.5
        if needle_norm and needle_norm == title:
            return 1.0
        if needle_norm and title.startswith(needle_norm):
            return max(ratio, 0.95)
        if needle_norm and needle_norm in title:
            return max(ratio, 0.9)
        return max(ratio, 0.6)

    if rows:
        scored_pairs = sorted(((row, score_direct(row)) for row in rows), key=lambda pair: pair[1], reverse=True)
        total_matched = total
    else:
        # Fuzzy fallback when exact substring match returns 0 rows
        import difflib

        fuzzy_stmt = select(MovieEdition)
        if clean_lang:
            fuzzy_stmt = fuzzy_stmt.where(MovieEdition.lang == clean_lang)
        candidates = list(session.scalars(fuzzy_stmt.order_by(MovieEdition.fetched_at.desc()).limit(1000)))
        scored_pairs = []
        for row in candidates:
            title = normalize_title(row.book_title or "")
            ratio = difflib.SequenceMatcher(None, needle_norm, title).ratio() if needle_norm else 0.0
            if ratio >= 0.45:
                scored_pairs.append((row, ratio))
        scored_pairs.sort(key=lambda pair: pair[1], reverse=True)
        total_matched = len(scored_pairs)

    window = scored_pairs[(page - 1) * page_size : (page - 1) * page_size + page_size]
    group_langs = _group_languages(session, [row.movie_group_id for row, _s in window])
    items = [
        _serialize_list_item(
            row,
            edition_count=len(group_langs.get(row.movie_group_id or "", [row.lang])),
            available_languages=group_langs.get(row.movie_group_id or "", [row.lang]),
            requested_lang=clean_lang,
            score=sc,
        )
        for row, sc in window
    ]
    return items, total_matched


def group_detail(session: Session, group_id: str, *, targets: Iterable[str] | None = None) -> dict[str, Any] | None:
    group = session.get(MovieGroup, group_id)
    if group is None:
        return None
    editions = group_editions(session, group_id)
    statuses = match_status_map(session, group_id)
    anchor = next((e for e in editions if e.book_id == group.canonical_book_id), None) or (editions[0] if editions else None)
    languages = []
    if anchor is not None:
        languages = language_view(session, anchor, targets=targets)
    return {
        "group_id": group.id,
        "canonical_title": group.canonical_title,
        "original_lang": group.original_lang,
        "canonical_book_id": group.canonical_book_id,
        "episode_count": group.episode_count,
        "edition_count": group.edition_count or len(editions),
        "language_count": group.language_count or len({e.lang for e in editions}),
        "verified_edition_count": group.verified_edition_count,
        "editions": [edition_to_dict(e, match_status=statuses.get(e.book_id, STATUS_UNKNOWN)) for e in editions],
        "languages": languages,
    }


def catalog_status_snapshot(
    session: Session,
    lang: str,
    *,
    state_ctx: Any = None,
) -> dict[str, Any]:
    """Compute real database and crawler status metrics for `lang` (Task 7 & Task 9)."""
    clean_lang = (lang or "en").strip().lower()
    indexed_total = int(
        session.scalar(
            select(func.count()).select_from(MovieEdition).where(MovieEdition.lang == clean_lang)
        )
        or 0
    )
    unique_total = int(
        session.scalar(
            select(func.count(func.distinct(MovieEdition.book_id))).where(MovieEdition.lang == clean_lang)
        )
        or 0
    )
    incomplete_total = int(
        session.scalar(
            select(func.count())
            .select_from(MovieEdition)
            .where(
                MovieEdition.lang == clean_lang,
                (MovieEdition.metadata_status == "METADATA_INCOMPLETE")
                | (func.coalesce(MovieEdition.chapter_count, 0) <= 0),
            )
        )
        or 0
    )
    enriched_total = max(0, indexed_total - incomplete_total)

    cat_state = session.scalar(
        select(CatalogState).where(
            CatalogState.provider == "reelshort",
            CatalogState.locale == clean_lang,
        )
    )
    pages_completed = int(
        session.scalar(
            select(func.count())
            .select_from(CatalogPageState)
            .where(CatalogPageState.locale == clean_lang, CatalogPageState.status == "succeeded")
        )
        or (cat_state.pages_completed if cat_state else 0)
        or 0
    )
    pages_failed = int(
        session.scalar(
            select(func.count())
            .select_from(CatalogPageState)
            .where(CatalogPageState.locale == clean_lang, CatalogPageState.status == "failed")
        )
        or (cat_state.pages_failed if cat_state else 0)
        or 0
    )
    total_pages = int((cat_state.total_pages if cat_state else 0) or 0)
    pages_remaining = max(0, total_pages - pages_completed)
    source_total = int((cat_state.source_total if cat_state else 0) or indexed_total)
    discovered_total = max(
        int((cat_state.discovered_total if cat_state else 0) or 0),
        indexed_total,
    )
    new_movies = int((cat_state.new_movies if cat_state else 0) or 0)
    updated_movies = int((cat_state.updated_movies if cat_state else 0) or 0)
    skipped_movies = int((cat_state.skipped_movies if cat_state else 0) or 0)
    http_requests = int((cat_state.http_requests if cat_state else 0) or 0)
    crawl_duration_ms = int((cat_state.crawl_duration_ms if cat_state else 0) or 0)
    crawl_rate = float((cat_state.crawl_rate_pages_per_min if cat_state else 0.0) or 0.0)
    last_completed_page = int((cat_state.last_completed_page if cat_state else 0) or 0)

    active_job = session.scalar(
        select(CrawlJob).where(
            CrawlJob.dedupe_key == f"locale_catalog:{clean_lang}",
            CrawlJob.dedupe_active == "1",
        )
    )
    recent_job = None
    if not active_job:
        recent_job = session.scalar(
            select(CrawlJob)
            .where(CrawlJob.dedupe_key == f"locale_catalog:{clean_lang}")
            .order_by(CrawlJob.created_at.desc())
            .limit(1)
        )

    latest_fetch = session.scalar(
        select(func.max(MovieEdition.fetched_at)).where(MovieEdition.lang == clean_lang)
    )
    last_synced_dt = (
        (cat_state.last_synced_at if cat_state and cat_state.last_synced_at else None)
        or latest_fetch
    )

    if active_job:
        status = "syncing"
        worker_status = "running" if active_job.status == "running" else "queued"
    elif cat_state and cat_state.status in ("running", "paused"):
        status = cat_state.status
        worker_status = cat_state.status
    elif indexed_total > 0:
        status = "completed" if (total_pages == 0 or pages_completed >= total_pages) else "partial"
        worker_status = "idle"
    else:
        status = "idle"
        worker_status = "idle"

    if state_ctx is not None:
        try:
            pool_snap = state_ctx.pool.snapshot()
            if pool_snap.get("active", 0) > 0 and worker_status == "idle":
                worker_status = "busy"
            src_snap = state_ctx.source.stats.snapshot()
            if http_requests == 0:
                http_requests = int(src_snap.get("requests", 0) or 0)
        except Exception:
            pass

    if active_job is not None:
        progress = float(active_job.progress or 0.0)
        if total_pages > 0 and pages_completed > 0:
            progress = max(progress, round((pages_completed / total_pages) * 100.0, 1))
    elif total_pages > 0:
        progress = round(min(100.0, (pages_completed / total_pages) * 100.0), 1)
    else:
        progress = 100.0 if indexed_total > 0 else 0.0

    task_id = active_job.id if active_job else (recent_job.id if recent_job else None)
    checkpoint = (
        active_job.checkpoint
        if active_job
        else (recent_job.result if recent_job and recent_job.result else (recent_job.checkpoint if recent_job else {}))
    )

    return {
        "locale": clean_lang,
        "source_url": (cat_state.source_url if cat_state and cat_state.source_url else get_locale_catalog_page_url(clean_lang, 1)),
        "movie_count": indexed_total,
        "source_total": source_total,
        "discovered_total": discovered_total,
        "unique_total": unique_total,
        "indexed_total": indexed_total,
        "enriched_total": enriched_total,
        "incomplete_total": incomplete_total,
        "total_pages": total_pages,
        "pages_completed": pages_completed,
        "pages_remaining": pages_remaining,
        "pages_failed": pages_failed,
        "last_completed_page": last_completed_page,
        "new_movies": new_movies,
        "updated_movies": updated_movies,
        "skipped_movies": skipped_movies,
        "failed_movies": pages_failed,
        "http_requests": http_requests,
        "crawl_rate_pages_per_min": crawl_rate,
        "crawl_duration_ms": crawl_duration_ms,
        "crawl_duration_seconds": round(crawl_duration_ms / 1000.0, 1),
        "status": status,
        "worker_status": worker_status,
        "task_id": task_id,
        "progress": progress,
        "checkpoint": checkpoint or {},
        "last_error": cat_state.last_error if cat_state else None,
        "last_synced_at": last_synced_dt.isoformat() if last_synced_dt else None,
    }


def stats_snapshot(session: Session, settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    movies = int(session.scalar(select(func.count()).select_from(MovieGroup)) or 0)
    editions = int(session.scalar(select(func.count()).select_from(MovieEdition)) or 0)
    episodes = int(session.scalar(select(func.count()).select_from(Episode)) or 0)
    verified = int(
        session.scalar(select(func.count()).select_from(EditionMatch).where(EditionMatch.status == "VERIFIED")) or 0
    )
    candidates = int(
        session.scalar(select(func.count()).select_from(EditionMatch).where(EditionMatch.status == STATUS_CANDIDATE)) or 0
    )
    languages = [
        {"lang": lang, "editions": int(count), "episodes": int(episodes_count)}
        for lang, count, episodes_count in session.execute(
            select(
                MovieEdition.lang,
                func.count(MovieEdition.book_id),
                func.coalesce(func.sum(MovieEdition.stored_chapter_count), 0),
            ).group_by(MovieEdition.lang)
        ).all()
    ]
    languages.sort(key=lambda item: item["editions"], reverse=True)
    catalogs = {
        row.locale: catalog_status_snapshot(session, row.locale)
        for row in session.scalars(select(CatalogState))
    }
    cache_rows = session.execute(
        select(func.count(CacheEntry.key), func.coalesce(func.sum(CacheEntry.hits), 0))
    ).one()
    expired = int(
        session.scalar(
            select(func.count()).select_from(CacheEntry).where(CacheEntry.expires_at < utcnow())
        )
        or 0
    )
    recent = [
        {
            "id": row.id,
            "outcome": row.outcome,
            "book_id": row.book_id,
            "lang": row.lang,
            "detail": row.detail,
            "created_at": row.created_at,
        }
        for row in session.scalars(
            select(CrawlHistory).order_by(CrawlHistory.created_at.desc()).limit(10)
        )
    ]
    usage_rows = session.execute(
        select(
            ApiUsage.route,
            func.count(ApiUsage.id),
            func.avg(ApiUsage.duration_ms),
        )
        .where(ApiUsage.created_at > utcnow() - timedelta(hours=24))
        .group_by(ApiUsage.route)
        .order_by(func.count(ApiUsage.id).desc())
        .limit(15)
    ).all()
    return {
        "movies": movies,
        "editions": editions,
        "episodes": episodes,
        "groups": movies,
        "verified_matches": verified,
        "candidate_matches": candidates,
        "languages": languages,
        "catalogs": catalogs,
        "cache": {
            "entries": int(cache_rows[0] or 0),
            "hits": int(cache_rows[1] or 0),
            "expired": expired,
        },
        "recent_crawls": recent,
        "usage": {
            "top_routes_24h": [
                {"route": route, "calls": int(calls), "avg_ms": round(float(avg or 0), 1)}
                for route, calls, avg in usage_rows
            ]
        },
        "notes": {"metadata_ttl_hours": settings.metadata_ttl_hours},
    }


def count_usage(session: Session, subject: str, route_prefix: str, since: Any) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(ApiUsage)
            .where(
                ApiUsage.subject == subject,
                ApiUsage.route.like(f"{route_prefix}%"),
                ApiUsage.created_at >= since,
            )
        )
        or 0
    )


def normalise_route(path: str) -> str:
    return re.sub(r"/[A-Za-z0-9_-]{16,64}", "/{id}", path)
