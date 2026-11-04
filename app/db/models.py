"""SQLAlchemy models: movies, movie_editions, edition_matches, episodes,
crawl_jobs, crawl_history, cache_entries, api_usage."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON


def utcnow() -> datetime:
    """Naive UTC timestamp (SQLite stores naive DATETIME deterministically)."""
    return datetime.now(tz=timezone.utc).replace(tzinfo=None)


def new_id(prefix: str = "") -> str:
    value = uuid.uuid4().hex[:20]
    return f"{prefix}{value}" if prefix else value


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[str]: JSON}


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class MovieGroup(Base, TimestampMixin):
    """A story group: one logical movie with N language editions."""

    __tablename__ = "movies"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("grp_"))
    canonical_title: Mapped[str | None] = mapped_column(String(512))
    original_lang: Mapped[str | None] = mapped_column(String(16), index=True)
    canonical_book_id: Mapped[str | None] = mapped_column(String(64), index=True)
    canonical_t_book_id: Mapped[str | None] = mapped_column(String(64))
    edition_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    language_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    verified_edition_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    episode_count: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    editions: Mapped[list["MovieEdition"]] = relationship(back_populates="group")


class MovieEdition(Base, TimestampMixin):
    """One crawled edition of a movie (book_id is language-scoped and unique)."""

    __tablename__ = "movie_editions"
    __table_args__ = (
        UniqueConstraint("book_id", name="uq_movie_editions_book_id"),
        UniqueConstraint("provider", "lang", "book_id", name="uq_movie_editions_provider_lang_book"),
        Index("ix_movie_editions_lang_title", "lang", "book_title"),
        Index("ix_movie_editions_group_lang", "movie_group_id", "lang"),
        Index("ix_movie_editions_fetched_at", "fetched_at"),
        Index("ix_movie_editions_t_book_id", "t_book_id"),
        Index("ix_movie_editions_original_lang", "original_lang"),
        Index("ix_movie_editions_lang_read_count", "lang", "read_count"),
        Index("ix_movie_editions_metadata_status", "metadata_status"),
    )

    book_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), default="reelshort", server_default="reelshort", nullable=False)
    movie_group_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("movies.id", ondelete="SET NULL"), index=True
    )
    t_book_id: Mapped[str | None] = mapped_column(String(64))
    s_book_id: Mapped[int | None] = mapped_column(Integer)
    book_title: Mapped[str] = mapped_column(String(512), nullable=False)
    book_pic: Mapped[str | None] = mapped_column(Text)
    special_desc: Mapped[str | None] = mapped_column(Text)
    lang: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    original_lang: Mapped[str | None] = mapped_column(String(16))
    chapter_count: Mapped[int | None] = mapped_column(Integer)
    stored_chapter_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    first_chapter_id: Mapped[str | None] = mapped_column(String(64))
    paid_start: Mapped[int | None] = mapped_column(Integer)
    is_paid: Mapped[bool | None] = mapped_column(Boolean)
    has_dub: Mapped[bool | None] = mapped_column(Boolean)
    is_dub: Mapped[bool | None] = mapped_column(Boolean)
    is_ai: Mapped[bool | None] = mapped_column(Boolean)
    book_genre: Mapped[int | None] = mapped_column(Integer)
    book_source: Mapped[int | None] = mapped_column(Integer)
    book_type: Mapped[int | None] = mapped_column(Integer)
    update_status: Mapped[int | None] = mapped_column(Integer)
    publish_at: Mapped[int | None] = mapped_column(Integer)
    online_at: Mapped[int | None] = mapped_column(Integer)
    read_count: Mapped[int | None] = mapped_column(Integer)
    collect_count: Mapped[int | None] = mapped_column(Integer)
    metadata_status: Mapped[str] = mapped_column(
        String(32), default="COMPLETE", server_default="COMPLETE", nullable=False
    )
    tags: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    tag_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    tag_map: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    category: Mapped[str | None] = mapped_column(String(128), index=True)
    source_url: Mapped[str | None] = mapped_column(Text)
    source_kind: Mapped[str | None] = mapped_column(String(32))
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    episodes_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    episodes_cursor: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    raw_meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    group: Mapped[MovieGroup | None] = relationship(back_populates="editions")
    episodes: Mapped[list["Episode"]] = relationship(
        back_populates="edition", cascade="all, delete-orphan", passive_deletes=True
    )



class EditionMatch(Base, TimestampMixin):
    """A mapping edge between two language editions of the same story."""

    __tablename__ = "edition_matches"
    __table_args__ = (
        UniqueConstraint("source_book_id", "target_book_id", name="uq_edition_matches_pair"),
        Index("ix_edition_matches_status", "status"),
        Index("ix_edition_matches_group", "movie_group_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("mtc_"))
    movie_group_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("movies.id", ondelete="SET NULL"))
    source_book_id: Mapped[str] = mapped_column(String(64), ForeignKey("movie_editions.book_id", ondelete="CASCADE"))
    target_book_id: Mapped[str] = mapped_column(String(64), ForeignKey("movie_editions.book_id", ondelete="CASCADE"))
    source_lang: Mapped[str] = mapped_column(String(16))
    target_lang: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="CANDIDATE", nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    method: Mapped[str] = mapped_column(String(32), default="auto_search", nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    verified_by: Mapped[str | None] = mapped_column(String(128))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)


class BookAlias(Base, TimestampMixin):
    """An alias ``book_id`` that the official API resolves to another edition.

    ReelShort search returns several ids for the same edition (dub/legacy
    variants); ``getBookInfo`` always answers with the canonical ``book_id``.
    Recording the mapping avoids re-crawling the same story for every alias and
    lets ``/v1`` lookups by alias answer from cache.
    """

    __tablename__ = "book_aliases"
    __table_args__ = (Index("ix_book_aliases_canonical", "canonical_book_id"),)

    alias_book_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    canonical_book_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("movie_editions.book_id", ondelete="CASCADE"), nullable=False
    )
    lang: Mapped[str | None] = mapped_column(String(16))
    source: Mapped[str | None] = mapped_column(String(32))


class Episode(Base, TimestampMixin):
    """Episode metadata only. Video URLs / DRM payloads are never stored."""

    __tablename__ = "episodes"
    __table_args__ = (
        UniqueConstraint("book_id", "serial_number", name="uq_episodes_book_serial"),
        UniqueConstraint("book_id", "chapter_id", name="uq_episodes_book_chapter"),
        Index("ix_episodes_chapter_id", "chapter_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("ep_"))
    book_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("movie_editions.book_id", ondelete="CASCADE"), index=True
    )
    lang: Mapped[str | None] = mapped_column(String(16))
    chapter_id: Mapped[str] = mapped_column(String(64), nullable=False)
    serial_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str | None] = mapped_column(String(512))
    description: Mapped[str | None] = mapped_column(Text)
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    thumb_url: Mapped[str | None] = mapped_column(Text)
    publish_at: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[int | None] = mapped_column(Integer)
    chapter_type: Mapped[int | None] = mapped_column(Integer)
    like_count: Mapped[int | None] = mapped_column(Integer)
    is_preview: Mapped[bool | None] = mapped_column(Boolean)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    edition: Mapped[MovieEdition] = relationship(back_populates="episodes")


class CrawlJob(Base, TimestampMixin):
    __tablename__ = "crawl_jobs"
    __table_args__ = (
        # ``dedupe_active`` is set to "1" while a job is queued/running and set
        # to NULL once it terminates, so at most one active job exists per key
        # while terminated jobs stay in the table as history.
        UniqueConstraint("dedupe_key", "dedupe_active", name="uq_crawl_jobs_dedupe_active"),
        Index("ix_crawl_jobs_status_next_run", "status", "next_run_at"),
        Index("ix_crawl_jobs_kind", "kind"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("job_"))
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    scope: Mapped[str] = mapped_column(String(16), default="admin", nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    dedupe_active: Mapped[str | None] = mapped_column(String(8), default="1")
    status: Mapped[str] = mapped_column(String(16), default="queued", nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    progress: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    requested_by: Mapped[str | None] = mapped_column(String(128))
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CrawlHistory(Base):
    __tablename__ = "crawl_history"
    __table_args__ = (
        Index("ix_crawl_history_created", "created_at"),
        Index("ix_crawl_history_book", "book_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("ch_"))
    job_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("crawl_jobs.id", ondelete="SET NULL"))
    book_id: Mapped[str | None] = mapped_column(String(64))
    lang: Mapped[str | None] = mapped_column(String(16))
    url: Mapped[str | None] = mapped_column(Text)
    source_kind: Mapped[str | None] = mapped_column(String(32))
    http_status: Mapped[int | None] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    bytes_read: Mapped[int | None] = mapped_column(Integer)
    items_written: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class CacheEntry(Base):
    __tablename__ = "cache_entries"
    __table_args__ = (Index("ix_cache_entries_expires", "expires_at"),)

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    hits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class ApiUsage(Base):
    __tablename__ = "api_usage"
    __table_args__ = (
        Index("ix_api_usage_created", "created_at"),
        Index("ix_api_usage_subject_route", "subject", "route"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    subject: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[str] = mapped_column(String(16), default="public", nullable=False)
    route: Mapped[str] = mapped_column(String(128), nullable=False)
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[float | None] = mapped_column(Float)
    cache_hit: Mapped[bool | None] = mapped_column(Boolean)
    client_ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(Text)


class CatalogState(Base, TimestampMixin):
    """Per-locale full catalog crawl state, totals, and checkpoint."""

    __tablename__ = "catalog_states"
    __table_args__ = (
        UniqueConstraint("provider", "locale", name="uq_catalog_states_provider_locale"),
        Index("ix_catalog_states_locale", "locale"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), default="reelshort", server_default="reelshort", nullable=False)
    locale: Mapped[str] = mapped_column(String(16), nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    source_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_pages: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    page_size: Mapped[int] = mapped_column(Integer, default=12, nullable=False)
    pages_completed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pages_failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    discovered_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unique_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    indexed_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    enriched_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    incomplete_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    new_movies: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_movies: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_movies: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    http_requests: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    crawl_duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    crawl_rate_pages_per_min: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    last_completed_page: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="idle", nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CatalogPageState(Base, TimestampMixin):
    """Per-page crawl tracking and checkpoint for full catalog pagination."""

    __tablename__ = "catalog_page_states"
    __table_args__ = (
        UniqueConstraint("provider", "locale", "page_number", name="uq_catalog_page_states_provider_locale_page"),
        Index("ix_catalog_page_states_locale_status", "locale", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), default="reelshort", server_default="reelshort", nullable=False)
    locale: Mapped[str] = mapped_column(String(16), nullable=False)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
    items_found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    items_new: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    items_updated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    items_skipped: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    incomplete_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    crawled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


def schema_overview() -> dict[str, Any]:
    return {
        "tables": [
            MovieGroup.__tablename__,
            MovieEdition.__tablename__,
            BookAlias.__tablename__,
            EditionMatch.__tablename__,
            Episode.__tablename__,
            CrawlJob.__tablename__,
            CrawlHistory.__tablename__,
            CacheEntry.__tablename__,
            ApiUsage.__tablename__,
            CatalogState.__tablename__,
            CatalogPageState.__tablename__,
        ],
        "generated_at": utcnow().isoformat(),
        "count_hint": func.count().name,
    }

