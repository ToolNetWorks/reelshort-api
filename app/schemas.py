"""Pydantic schemas for the public and admin APIs (stable, documented shapes)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ErrorBody(BaseModel):
    code: str = Field(description="Machine readable error code")
    message: str = Field(description="Human readable message")
    details: dict[str, Any] = Field(default_factory=dict)


class Meta(BaseModel):
    request_id: str | None = None
    server_time: int
    cache: dict[str, Any] = Field(default_factory=dict)
    pagination: dict[str, Any] | None = None


class Envelope(BaseModel, Generic[T]):
    """Uniform response envelope: exactly one of ``data``/``error`` is set."""

    data: T | None = None
    error: ErrorBody | None = None
    meta: Meta


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    environment: str
    database: dict[str, Any]
    queue: dict[str, Any]
    source: dict[str, Any]
    uptime_seconds: float
    server_time: int


class TagOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str | None = None
    name: str
    category_id: str | None = None


class EditionOut(BaseModel):
    book_id: str
    lang: str
    book_title: str
    t_book_id: str | None = None
    s_book_id: int | None = None
    book_pic: str | None = None
    chapter_count: int | None = None
    stored_chapter_count: int = 0
    first_chapter_id: str | None = None
    paid_start: int | None = None
    is_paid: bool | None = None
    has_dub: bool | None = None
    is_dub: bool | None = None
    original_lang: str | None = None
    tags: list[str] = Field(default_factory=list)
    source_url: str | None = None
    fetched_at: datetime | None = None
    episodes_synced_at: datetime | None = None
    match_status: str | None = Field(
        default=None, description="Verification status of this edition inside its movie group"
    )


class MovieOut(BaseModel):
    group_id: str | None = None
    book_id: str
    lang: str
    book_title: str
    original_title: str | None = None
    book_pic: str | None = None
    special_desc: str | None = None
    t_book_id: str | None = None
    s_book_id: int | None = None
    chapter_count: int | None = None
    episode_count: int | None = None
    stored_episode_count: int = 0
    first_chapter_id: str | None = None
    paid_start: int | None = None
    is_paid: bool | None = None
    has_dub: bool | None = None
    is_dub: bool | None = None
    original_lang: str | None = None
    tags: list[str] = Field(default_factory=list)
    book_genre: int | None = None
    book_source: int | None = None
    update_status: int | None = None
    publish_at: int | None = None
    online_at: int | None = None
    source_url: str | None = None
    fetched_at: datetime | None = None
    is_stale: bool = False
    editions: list[EditionOut] = Field(default_factory=list)


class MovieListItem(BaseModel):
    book_id: str
    lang: str
    book_title: str
    book_pic: str | None = None
    chapter_count: int | None = None
    stored_episode_count: int = 0
    tags: list[str] = Field(default_factory=list)
    group_id: str | None = None
    edition_count: int = 1
    fetched_at: datetime | None = None


class EpisodeOut(BaseModel):
    book_id: str
    lang: str | None = None
    chapter_id: str
    serial_number: int
    title: str | None = None
    description: str | None = None
    duration_seconds: int | None = None
    thumb_url: str | None = None
    publish_at: int | None = None
    status: int | None = None
    chapter_type: int | None = None
    like_count: int | None = None
    is_preview: bool | None = None
    fetched_at: datetime | None = None


class LanguageEditionOut(BaseModel):
    lang: str
    book_id: str | None = None
    book_title: str | None = None
    chapter_count: int | None = None
    stored_episode_count: int = 0
    status: Literal["VERIFIED", "CANDIDATE", "UNKNOWN", "MISSING"]
    confidence: float | None = None
    is_source: bool = False
    discovered_at: datetime | None = None
    note: str | None = None


class GroupOut(BaseModel):
    group_id: str
    canonical_title: str | None = None
    original_lang: str | None = None
    canonical_book_id: str | None = None
    episode_count: int | None = None
    edition_count: int
    language_count: int
    verified_edition_count: int
    editions: list[EditionOut] = Field(default_factory=list)
    languages: list[LanguageEditionOut] = Field(default_factory=list)


class SearchHit(BaseModel):
    book_id: str
    lang: str
    book_title: str
    book_pic: str | None = None
    chapter_count: int | None = None
    tags: list[str] = Field(default_factory=list)
    score: float | None = None
    group_id: str | None = None


class DiscoverRequest(BaseModel):
    book_id: str | None = Field(default=None, description="ReelShort book_id to discover editions for")
    url: str | None = Field(default=None, description="ReelShort movie URL (https://www.reelshort.com/movie/...)")
    targets: list[str] | None = Field(
        default=None, description="Locales to look for, e.g. ['vi','es']. Defaults to configured set."
    )
    include_unverified: bool = Field(
        default=True, description="Include CANDIDATE matches in the response"
    )


class DiscoverResponse(BaseModel):
    task_id: str | None = None
    status: str
    book_id: str | None = None
    group_id: str | None = None
    languages: list[LanguageEditionOut] = Field(default_factory=list)
    queued_locales: list[str] = Field(default_factory=list)
    deduplicated: bool = False


class TaskOut(BaseModel):
    task_id: str
    kind: str
    status: Literal["queued", "running", "succeeded", "failed", "canceled", "retrying"]
    progress: float
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    attempts: int
    max_attempts: int
    error: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    checkpoint: dict[str, Any] = Field(default_factory=dict)


class AdminJobOut(TaskOut):
    scope: str
    priority: int
    dedupe_key: str
    payload: dict[str, Any] = Field(default_factory=dict)
    requested_by: str | None = None
    next_run_at: datetime


class CrawlTarget(BaseModel):
    book_id: str | None = None
    url: str | None = None
    lang: str | None = Field(default=None, description="Optional locale hint for URL-less book ids")


class AdminCrawlRequest(BaseModel):
    targets: list[CrawlTarget] = Field(default_factory=list, max_length=50)
    langs: list[str] | None = Field(default=None, description="Locales for discovery-style multi-lang crawl")
    include_episodes: bool = True
    discover: bool = Field(default=False, description="Also run edition discovery for each target")
    force: bool = Field(default=False, description="Ignore freshness TTL and crawl immediately")
    priority: int = Field(default=5, ge=1, le=10)
    wait: bool = Field(default=False, description="Wait for completion (bounded by request timeout)")


class AdminCrawlResponse(BaseModel):
    tasks: list[TaskOut] = Field(default_factory=list)
    deduplicated: list[str] = Field(default_factory=list)
    rejected: list[dict[str, Any]] = Field(default_factory=list)


class VerifyEditionRequest(BaseModel):
    status: Literal["VERIFIED", "CANDIDATE", "REJECTED"]
    note: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class AdminStatsResponse(BaseModel):
    movies: int
    editions: int
    episodes: int
    groups: int
    verified_matches: int
    candidate_matches: int
    languages: list[dict[str, Any]]
    jobs: dict[str, Any]
    cache: dict[str, Any]
    source: dict[str, Any]
    usage: dict[str, Any]
    generated_at: int
