"""Persistence helpers for crawled metadata (upserts, cache, history)."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Iterable

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.crawler.parser import ParsedEpisode, ParsedMovie
from app.db.models import (
    BookAlias,
    CacheEntry,
    CatalogPageState,
    CatalogState,
    CrawlHistory,
    Episode,
    EditionMatch,
    MovieEdition,
    MovieGroup,
    new_id,
    utcnow,
)

log = logging.getLogger(__name__)


def save_checkpoint(
    session: Session,
    job: Any,
    checkpoint: dict[str, Any],
    *,
    progress: float | None = None,
) -> None:
    """Persist a job checkpoint so a cancelled/retried job can resume.

    The worker keeps one write transaction per handler run, and SQLite allows a
    single writer, so the checkpoint is committed on *that* session instead of
    a second connection. ``expire_on_commit=False`` keeps the in-memory job row
    usable afterwards.
    """
    if job is None:
        return
    try:
        job.checkpoint = dict(checkpoint)
        if progress is not None:
            job.progress = progress
        session.commit()
    except Exception as exc:  # pragma: no cover - checkpoints must never break work
        log.debug("checkpoint save failed", extra={"error": str(exc)})


def upsert_edition(session: Session, movie: ParsedMovie, *, source_kind: str = "api") -> MovieEdition:
    """Insert or update a language edition. Never invents missing values."""
    now = utcnow()
    edition = session.get(MovieEdition, movie.book_id)
    created = edition is None
    if edition is None:
        edition = MovieEdition(
            book_id=movie.book_id,
            provider="reelshort",
            book_title=movie.book_title,
            lang=movie.lang,
            discovered_at=now,
        )
        session.add(edition)

    edition.provider = edition.provider or "reelshort"
    edition.book_title = movie.book_title
    edition.lang = movie.lang
    edition.t_book_id = movie.t_book_id or edition.t_book_id
    edition.s_book_id = movie.s_book_id if movie.s_book_id is not None else edition.s_book_id
    edition.book_pic = movie.book_pic or edition.book_pic
    edition.special_desc = movie.special_desc or edition.special_desc

    # Preserve existing positive chapter_count if a listing page reports 0
    if movie.chapter_count is not None:
        if movie.chapter_count > 0 or edition.chapter_count is None:
            edition.chapter_count = movie.chapter_count

    edition.first_chapter_id = movie.first_chapter_id or edition.first_chapter_id
    edition.paid_start = movie.paid_start if movie.paid_start is not None else edition.paid_start
    edition.has_dub = movie.has_dub if movie.has_dub is not None else edition.has_dub
    edition.is_dub = movie.is_dub if movie.is_dub is not None else edition.is_dub
    edition.original_lang = movie.original_lang or edition.original_lang
    edition.is_ai = movie.is_ai if movie.is_ai is not None else edition.is_ai
    edition.is_paid = movie.is_paid if movie.is_paid is not None else edition.is_paid
    edition.book_genre = movie.book_genre if movie.book_genre is not None else edition.book_genre
    edition.book_source = movie.book_source if movie.book_source is not None else edition.book_source
    edition.book_type = movie.book_type if movie.book_type is not None else edition.book_type
    edition.update_status = movie.update_status if movie.update_status is not None else edition.update_status
    edition.publish_at = movie.publish_at if movie.publish_at is not None else edition.publish_at
    edition.online_at = movie.online_at if movie.online_at is not None else edition.online_at
    edition.read_count = movie.read_count if movie.read_count is not None else edition.read_count
    edition.collect_count = movie.collect_count if movie.collect_count is not None else edition.collect_count
    if movie.tags:
        edition.tags = movie.tags
    if movie.tag_ids:
        edition.tag_ids = movie.tag_ids
    if movie.tag_map:
        edition.tag_map = movie.tag_map
    if movie.category:
        edition.category = movie.category
    if movie.source_url:
        edition.source_url = movie.source_url
    edition.source_kind = source_kind
    edition.discovered_at = edition.discovered_at or edition.created_at or now
    edition.last_synced_at = now
    edition.fetched_at = now
    edition.raw_meta = movie.raw or {}

    if (edition.chapter_count is None or edition.chapter_count <= 0) and (edition.stored_chapter_count or 0) <= 0:
        edition.metadata_status = "METADATA_INCOMPLETE"
    else:
        edition.metadata_status = "COMPLETE"

    if created:
        session.flush()
    return edition


def upsert_episodes(
    session: Session,
    book_id: str,
    lang: str | None,
    episodes: Iterable[ParsedEpisode],
    *,
    advance_cursor: bool = True,
) -> tuple[int, int]:
    """Upsert episodes; returns (inserted, updated). Never fabricates ids."""
    inserted = updated = 0
    existing = {
        row.chapter_id: row
        for row in session.scalars(select(Episode).where(Episode.book_id == book_id))
    }
    serial_map = {row.serial_number: row for row in existing.values()}
    now = utcnow()
    max_serial = 0
    for parsed in episodes:
        if not parsed.chapter_id:
            continue
        max_serial = max(max_serial, parsed.serial_number)
        row = existing.get(parsed.chapter_id) or serial_map.get(parsed.serial_number)
        if row is None:
            row = Episode(book_id=book_id, chapter_id=parsed.chapter_id, serial_number=parsed.serial_number)
            session.add(row)
            existing[parsed.chapter_id] = row
            serial_map[parsed.serial_number] = row
            inserted += 1
        else:
            updated += 1
        row.chapter_id = parsed.chapter_id
        row.serial_number = parsed.serial_number
        row.lang = lang
        row.title = parsed.title or row.title
        row.description = parsed.description or row.description
        row.duration_seconds = (
            parsed.duration_seconds if parsed.duration_seconds is not None else row.duration_seconds
        )
        row.thumb_url = parsed.thumb_url or row.thumb_url
        row.publish_at = parsed.publish_at if parsed.publish_at is not None else row.publish_at
        row.status = parsed.status if parsed.status is not None else row.status
        row.chapter_type = parsed.chapter_type if parsed.chapter_type is not None else row.chapter_type
        row.like_count = parsed.like_count if parsed.like_count is not None else row.like_count
        row.is_preview = parsed.is_preview if parsed.is_preview is not None else row.is_preview
        row.fetched_at = now

    session.flush()
    edition = session.get(MovieEdition, book_id)
    if edition is not None:
        stored = session.scalar(
            select(func.count()).select_from(Episode).where(Episode.book_id == book_id)
        ) or 0
        edition.stored_chapter_count = int(stored)
        edition.episodes_synced_at = now
        if edition.stored_chapter_count > 0 and (edition.chapter_count is None or edition.chapter_count <= 0):
            edition.chapter_count = edition.stored_chapter_count
        if (edition.chapter_count or 0) > 0 or edition.stored_chapter_count > 0:
            edition.metadata_status = "COMPLETE"
        if advance_cursor and max_serial:
            edition.episodes_cursor = max(edition.episodes_cursor or 0, max_serial)
    return inserted, updated


def record_history(
    session: Session,
    *,
    outcome: str,
    job_id: str | None = None,
    book_id: str | None = None,
    lang: str | None = None,
    url: str | None = None,
    source_kind: str | None = None,
    http_status: int | None = None,
    detail: str | None = None,
    duration_ms: int | None = None,
    bytes_read: int | None = None,
    items_written: int = 0,
) -> None:
    session.add(
        CrawlHistory(
            job_id=job_id,
            book_id=book_id,
            lang=lang,
            url=url,
            source_kind=source_kind,
            http_status=http_status,
            outcome=outcome,
            detail=(detail or "")[:2000] or None,
            duration_ms=duration_ms,
            bytes_read=bytes_read,
            items_written=items_written,
        )
    )


# --- cache -----------------------------------------------------------------
def cache_get(session: Session, key: str, *, touch: bool = True) -> dict[str, Any] | None:
    entry = session.get(CacheEntry, key)
    if entry is None:
        return None
    if entry.expires_at is not None and entry.expires_at < utcnow():
        return None
    if touch:
        entry.hits = (entry.hits or 0) + 1
        session.flush()
    return entry.payload


def cache_set(
    session: Session, key: str, kind: str, payload: dict[str, Any], ttl: timedelta | None
) -> None:
    entry = session.get(CacheEntry, key)
    expires = utcnow() + ttl if ttl else None
    if entry is None:
        entry = CacheEntry(key=key, kind=kind, payload=payload, expires_at=expires)
        session.add(entry)
    else:
        entry.payload = payload
        entry.kind = kind
        entry.expires_at = expires
        entry.updated_at = utcnow()
    try:
        session.flush()
    except IntegrityError:  # pragma: no cover - concurrent insert
        session.rollback()
        entry = session.get(CacheEntry, key)
        if entry is not None:
            entry.payload = payload
            entry.expires_at = expires
            session.flush()


def cache_purge_expired(session: Session) -> int:
    result = session.execute(delete(CacheEntry).where(CacheEntry.expires_at < utcnow()))
    return int(result.rowcount or 0)


# --- grouping --------------------------------------------------------------
def upsert_alias(
    session: Session, alias_book_id: str, canonical_book_id: str, *, lang: str | None = None, source: str = "api"
) -> None:
    """Record that ``alias_book_id`` resolves to ``canonical_book_id`` upstream."""
    if not alias_book_id or not canonical_book_id or alias_book_id == canonical_book_id:
        return
    existing = session.get(BookAlias, alias_book_id)
    if existing is None:
        session.add(
            BookAlias(
                alias_book_id=alias_book_id,
                canonical_book_id=canonical_book_id,
                lang=lang,
                source=source,
            )
        )
    else:
        existing.canonical_book_id = canonical_book_id
        existing.lang = lang or existing.lang
    session.flush()


def resolve_alias(session: Session, book_id: str) -> str:
    """Follow alias → canonical book_id (one hop, loops guarded)."""
    seen: set[str] = set()
    current = book_id
    while current and current not in seen:
        seen.add(current)
        alias = session.get(BookAlias, current)
        if alias is None or alias.canonical_book_id in seen:
            return current
        current = alias.canonical_book_id
    return current


def ensure_group(session: Session, edition: MovieEdition, *, title: str | None = None) -> MovieGroup:
    """Attach an edition to a group, creating a single-edition group if needed."""
    if edition.movie_group_id:
        group = session.get(MovieGroup, edition.movie_group_id)
        if group is not None:
            return group
    group = MovieGroup(
        id=new_id("grp_"),
        canonical_title=title or edition.book_title,
        original_lang=edition.original_lang or edition.lang,
        canonical_book_id=edition.book_id,
        canonical_t_book_id=edition.t_book_id,
        episode_count=edition.chapter_count,
        edition_count=1,
        language_count=1,
        verified_edition_count=0,
    )
    session.add(group)
    edition.movie_group_id = group.id
    session.flush()
    return group


def refresh_group_stats(session: Session, group_id: str) -> dict[str, Any]:
    editions = list(session.scalars(select(MovieEdition).where(MovieEdition.movie_group_id == group_id)))
    group = session.get(MovieGroup, group_id)
    if group is None:
        return {}
    matches = list(session.scalars(select(EditionMatch).where(EditionMatch.movie_group_id == group_id)))
    verified = [m for m in matches if m.status == "VERIFIED"]
    group.edition_count = len(editions)
    group.language_count = len({e.lang for e in editions})
    group.verified_edition_count = len({m.target_book_id for m in verified} | {m.source_book_id for m in verified})
    if group.episode_count is None and editions:
        group.episode_count = next((e.chapter_count for e in editions if e.chapter_count), None)
    if not group.canonical_title and editions:
        group.canonical_title = editions[0].book_title
    session.flush()
    return {
        "group_id": group.id,
        "edition_count": group.edition_count,
        "language_count": group.language_count,
        "verified_edition_count": group.verified_edition_count,
    }


def link_group(session: Session, source_book_id: str, target_book_id: str) -> str | None:
    """Move ``target`` into the same group as ``source`` and register a match edge."""
    source = session.get(MovieEdition, source_book_id)
    target = session.get(MovieEdition, target_book_id)
    if source is None or target is None:
        return None
    if source.movie_group_id is None:
        ensure_group(session, source)
    source = session.get(MovieEdition, source_book_id)
    group_id = source.movie_group_id
    assert group_id is not None
    if target.movie_group_id and target.movie_group_id != group_id:
        # merge the donor group's editions into the source group
        donor = target.movie_group_id
        for edition in session.scalars(select(MovieEdition).where(MovieEdition.movie_group_id == donor)):
            edition.movie_group_id = group_id
        for match in session.scalars(select(EditionMatch).where(EditionMatch.movie_group_id == donor)):
            match.movie_group_id = group_id
        refresh_group_stats(session, donor)
    target.movie_group_id = group_id
    session.flush()
    refresh_group_stats(session, group_id)
    return group_id


# --- catalog state & page checkpoints --------------------------------------
def get_or_create_catalog_state(
    session: Session,
    lang: str,
    *,
    source_url: str | None = None,
) -> CatalogState:
    clean = lang.strip().lower()
    state = session.scalar(
        select(CatalogState).where(
            CatalogState.provider == "reelshort",
            CatalogState.locale == clean,
        )
    )
    if state is None:
        state = CatalogState(
            locale=clean,
            provider="reelshort",
            source_url=source_url,
            status="idle",
        )
        session.add(state)
        session.flush()
    elif source_url and not state.source_url:
        state.source_url = source_url
        session.flush()
    return state


def upsert_catalog_page_state(
    session: Session,
    *,
    lang: str,
    page: int,
    status: str,
    url: str | None = None,
    http_status: int | None = None,
    duration_ms: int | None = None,
    items_discovered: int | None = None,
    items_new: int | None = None,
    items_updated: int | None = None,
    items_skipped: int | None = None,
    items_incomplete: int | None = None,
    error: str | None = None,
) -> CatalogPageState:
    clean = lang.strip().lower()
    row = session.scalar(
        select(CatalogPageState).where(
            CatalogPageState.provider == "reelshort",
            CatalogPageState.locale == clean,
            CatalogPageState.page_number == page,
        )
    )
    now = utcnow()
    if row is None:
        row = CatalogPageState(
            provider="reelshort",
            locale=clean,
            page_number=page,
            status=status,
            url=url or "",
            attempts=1 if status in ("running", "succeeded", "failed") else 0,
        )
        session.add(row)
    else:
        if status == "running":
            row.attempts = (row.attempts or 0) + 1
        row.status = status
        if url:
            row.url = url
    if http_status is not None:
        row.http_status = http_status
    if duration_ms is not None:
        row.duration_ms = duration_ms
    if items_discovered is not None:
        row.items_found = items_discovered
    if items_new is not None:
        row.items_new = items_new
    if items_updated is not None:
        row.items_updated = items_updated
    if items_skipped is not None:
        row.items_skipped = items_skipped
    if items_incomplete is not None:
        row.incomplete_count = items_incomplete
    row.error = (error[:2000] if error else None) if status == "failed" else None
    if status in ("succeeded", "failed"):
        row.crawled_at = now
    row.updated_at = now
    session.flush()
    return row


def refresh_catalog_state_counts(session: Session, lang: str) -> CatalogState:
    clean = lang.strip().lower()
    state = get_or_create_catalog_state(session, clean)
    indexed_total = int(
        session.scalar(
            select(func.count()).select_from(MovieEdition).where(MovieEdition.lang == clean)
        )
        or 0
    )
    unique_total = int(
        session.scalar(
            select(func.count(func.distinct(MovieEdition.book_id))).where(MovieEdition.lang == clean)
        )
        or 0
    )
    incomplete_total = int(
        session.scalar(
            select(func.count())
            .select_from(MovieEdition)
            .where(
                MovieEdition.lang == clean,
                (MovieEdition.metadata_status == "METADATA_INCOMPLETE")
                | (func.coalesce(MovieEdition.chapter_count, 0) <= 0),
            )
        )
        or 0
    )
    enriched_total = max(0, indexed_total - incomplete_total)

    pages_completed = int(
        session.scalar(
            select(func.count())
            .select_from(CatalogPageState)
            .where(
                CatalogPageState.locale == clean,
                CatalogPageState.status == "succeeded",
            )
        )
        or 0
    )
    pages_failed = int(
        session.scalar(
            select(func.count())
            .select_from(CatalogPageState)
            .where(
                CatalogPageState.locale == clean,
                CatalogPageState.status == "failed",
            )
        )
        or 0
    )
    sum_discovered, sum_new, sum_updated, sum_skipped = session.execute(
        select(
            func.coalesce(func.sum(CatalogPageState.items_found), 0),
            func.coalesce(func.sum(CatalogPageState.items_new), 0),
            func.coalesce(func.sum(CatalogPageState.items_updated), 0),
            func.coalesce(func.sum(CatalogPageState.items_skipped), 0),
        ).where(
            CatalogPageState.locale == clean,
            CatalogPageState.status == "succeeded",
        )
    ).one()

    state.indexed_total = indexed_total
    state.unique_total = unique_total
    state.incomplete_total = incomplete_total
    state.enriched_total = enriched_total
    state.pages_completed = pages_completed
    state.pages_failed = pages_failed
    if int(sum_discovered or 0) > 0:
        state.discovered_total = int(sum_discovered)
    else:
        state.discovered_total = max(state.discovered_total or 0, indexed_total)
    if pages_completed > 0:
        state.new_movies = int(sum_new or 0)
        state.updated_movies = int(sum_updated or 0)
        state.skipped_movies = int(sum_skipped or 0)
    session.flush()
    return state
