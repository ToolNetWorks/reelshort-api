"""Admin API (``/admin``) — authenticated, never exposed publicly."""

from __future__ import annotations

import logging
from typing import Annotated, Any, Iterator

from fastapi import APIRouter, Body, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import envelope, paginate, pagination
from app.api.errors import bad_request, not_found
from app.db.base import session_scope
from app.db.models import CrawlHistory, CrawlJob, EditionMatch, MovieEdition, utcnow
from app.schemas import AdminCrawlRequest, VerifyEditionRequest
from app.security import Principal, rate_limit, require_admin
from app.services import library, maintenance
from app.services.state import AppState
from app.api.deps import get_state

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])


def get_db() -> Iterator[Session]:
    with session_scope() as session:
        yield session


DbSession = Annotated[Session, Depends(get_db)]
Pager = Annotated[dict[str, int], Depends(pagination)]
Admin = Annotated[Principal, Depends(require_admin)]
State = Annotated[AppState, Depends(get_state)]


class MergeRequest(BaseModel):
    source_book_id: str
    target_book_id: str
    status: str = Field(default="VERIFIED", pattern="^(VERIFIED|CANDIDATE|UNKNOWN)$")
    note: str | None = None


class ManualDiscoverRequest(BaseModel):
    book_id: str | None = None
    url: str | None = None
    targets: list[str] = Field(default_factory=list)
    title_hints: list[str] = Field(default_factory=list)
    strategies: list[str] = Field(default_factory=lambda: ["search"])
    include_movie: bool = True
    include_episodes: bool = True
    wait: bool = False


class RefreshCatalogRequest(BaseModel):
    lang: str = "en"
    pages: int = Field(default=1, ge=1, le=20)
    page_size: int = Field(default=50, ge=10, le=100)


def _job_to_dict(job: CrawlJob, *, admin: bool = True) -> dict[str, Any]:
    data = {
        "task_id": job.id,
        "kind": job.kind,
        "status": job.status,
        "progress": job.progress,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "error": job.error,
        "result": job.result or {},
        "checkpoint": job.checkpoint or {},
    }
    if admin:
        data.update(
            {
                "scope": job.scope,
                "priority": job.priority,
                "dedupe_key": job.dedupe_key,
                "payload": job.payload or {},
                "requested_by": job.requested_by,
                "next_run_at": job.next_run_at,
            }
        )
    return data


# --- crawling --------------------------------------------------------------
@router.post("/crawl", summary="Queue crawl jobs for book ids / URLs", status_code=status.HTTP_202_ACCEPTED)
async def start_crawl(
    session: DbSession,
    state: State,
    admin: Admin,
    payload: Annotated[AdminCrawlRequest, Body()],
) -> dict[str, Any]:
    rate_limit(
        f"admin-crawl:{admin.subject}",
        state.settings.crawl_rate_limit_per_hour,
        3600.0,
        code="crawl_quota_exceeded",
        message="Crawl quota exceeded",
    )
    if not payload.targets:
        raise bad_request("no_targets", "Provide at least one target")
    tasks: list[dict[str, Any]] = []
    deduplicated: list[str] = []
    rejected: list[dict[str, Any]] = []
    for target in payload.targets:
        book_id = state.crawl.resolve_book_id(target.book_id, target.url)
        if not book_id:
            rejected.append({"target": target.model_dump(), "reason": "invalid_target"})
            continue
        job_payload = {
            "book_id": book_id,
            "url": target.url,
            "lang_hint": target.lang,
            "include_episodes": payload.include_episodes,
            "force": payload.force,
            "discover": payload.discover,
            "targets": payload.langs,
            "strategies": ["search"],
        }
        job, created = state.jobs.create(
            session,
            kind="movie",
            dedupe_key=f"movie:{book_id}",
            payload=job_payload,
            scope="admin",
            priority=payload.priority,
            requested_by=admin.subject,
        )
        if not created:
            deduplicated.append(job.id)
        tasks.append(_job_to_dict(job))
    session.commit()
    if payload.wait and tasks:
        import asyncio

        await_ids = [task["task_id"] for task in tasks]
        deadline = 25.0
        step = 0.4
        waited = 0.0
        while waited < deadline:
            await asyncio.sleep(step)
            waited += step
            session.expire_all()
            pending = [
                job_id
                for job_id in await_ids
                if (session.get(CrawlJob, job_id) is None)
                or (session.get(CrawlJob, job_id).status in ("queued", "running"))
            ]
            if not pending:
                break
        session.expire_all()
        tasks = [
            _job_to_dict(session.get(CrawlJob, task["task_id"]))
            for task in tasks
            if session.get(CrawlJob, task["task_id"]) is not None
        ]
    return envelope(
        {"tasks": tasks, "deduplicated": deduplicated, "rejected": rejected},
        cache={"hit": False, "source": "queue"},
    )


@router.post("/discover", summary="Run edition discovery with optional title hints")
def manual_discover(
    session: DbSession, state: State, admin: Admin, payload: Annotated[ManualDiscoverRequest, Body()]
) -> dict[str, Any]:
    book_id = state.crawl.resolve_book_id(payload.book_id, payload.url)
    if not book_id:
        raise bad_request("invalid_target", "Provide book_id or url")
    edition = library.get_edition(session, book_id)
    job, created = state.jobs.create(
        session,
        kind="discover",
        dedupe_key=f"discover:{book_id}:{','.join(sorted(payload.targets)) or 'auto'}",
        payload={
            "book_id": book_id,
            "url": payload.url,
            "targets": payload.targets or None,
            "title_hints": payload.title_hints,
            "strategies": payload.strategies,
            "include_movie": payload.include_movie and edition is None,
        },
        scope="admin",
        priority=4,
        requested_by=admin.subject,
    )
    session.commit()
    return envelope(
        {
            "task_id": job.id,
            "status": "queued" if created else "already_queued",
            "book_id": book_id,
            "group_id": edition.movie_group_id if edition else None,
            "deduplicated": not created,
        },
        cache={"hit": False, "source": "queue"},
    )


@router.post("/catalog", summary="Index a bounded slice of the official per-locale catalogue")
def refresh_catalog(
    session: DbSession, state: State, admin: Admin, payload: Annotated[RefreshCatalogRequest, Body()]
) -> dict[str, Any]:
    job, created = state.jobs.create(
        session,
        kind="catalog",
        dedupe_key=f"catalog:{payload.lang}:{payload.pages}",
        payload={"lang": payload.lang, "pages": payload.pages, "page_size": payload.page_size},
        scope="admin",
        priority=8,
        requested_by=admin.subject,
    )
    session.commit()
    return envelope(
        {"task_id": job.id, "status": "queued" if created else "already_queued", "deduplicated": not created},
        cache={"hit": False, "source": "queue"},
    )


@router.post("/catalog/sync", summary="Trigger full or incremental locale catalog crawl (authenticated admin)")
def admin_sync_locale_catalog(
    session: DbSession,
    state: State,
    admin: Admin,
    lang: Annotated[str, Query(description="Locale to sync, e.g. en, vi")] = "en",
    force: Annotated[bool, Query(description="Force sync even if within TTL")] = True,
    reset_pages: Annotated[bool, Query(description="Reset all page checkpoints and rescan from page 1")] = False,
    max_pages: Annotated[int | None, Query(ge=1, le=5000, description="Optional cap on pages")] = None,
) -> dict[str, Any]:
    clean_lang = lang.strip().lower()
    job, created = state.jobs.create(
        session,
        kind="locale_catalog",
        dedupe_key=f"locale_catalog:{clean_lang}",
        payload={
            "lang": clean_lang,
            "force": force,
            "reset_pages": reset_pages,
            "max_pages": max_pages,
        },
        scope="admin",
        priority=1,
        requested_by=admin.subject,
    )
    session.commit()
    return envelope(
        {
            "task_id": job.id,
            "created": created,
            "status": "queued" if created else "already_queued",
            "locale": clean_lang,
            "deduplicated": not created,
        },
        cache={"hit": False, "source": "queue"},
    )


@router.get("/catalog/status", summary="Full catalog crawl dashboard metrics for a locale (authenticated admin)")
def admin_catalog_status(
    session: DbSession,
    state: State,
    admin: Admin,
    lang: Annotated[str, Query(description="Locale, e.g. en, vi")] = "en",
) -> dict[str, Any]:
    return envelope(
        library.catalog_status_snapshot(session, lang, state_ctx=state),
        cache={"hit": False},
    )


# --- monitoring ------------------------------------------------------------
@router.get("/stats", summary="Aggregate statistics")
def stats(session: DbSession, state: State, admin: Admin) -> dict[str, Any]:
    import time
    from datetime import timedelta

    from sqlalchemy import func

    from app.db.models import ApiUsage, MovieEdition, utcnow

    data = library.stats_snapshot(session, state.settings)
    data["jobs"] = state.pool.snapshot()
    data["source"] = state.source.stats.snapshot()
    data["usage"]["subjects_24h"] = int(
        session.scalar(
            select(func.count(func.distinct(ApiUsage.subject))).where(
                ApiUsage.created_at >= utcnow() - timedelta(hours=24)
            )
        )
        or 0
    )
    data["generated_at"] = int(time.time())
    data["database"] = maintenance.database_size(session, state.settings)
    data["editions_unmatched"] = int(
        session.scalar(
            select(func.count()).select_from(MovieEdition).where(MovieEdition.movie_group_id.is_(None))
        )
        or 0
    )
    return envelope(data, cache={"hit": False})


@router.get("/jobs", summary="List crawl jobs")
def list_jobs(
    session: DbSession,
    state: State,
    admin: Admin,
    pager: Pager,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    kind: Annotated[str | None, Query()] = None,
    scope: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    rows, total = state.jobs.list_jobs(
        session,
        status=status_filter,
        kind=kind,
        scope=scope,
        limit=pager["page_size"],
        offset=(pager["page"] - 1) * pager["page_size"],
    )
    return envelope(
        [_job_to_dict(job) for job in rows],
        pagination_info=paginate(total, pager["page"], pager["page_size"]),
        cache={"hit": False},
    )


@router.get("/jobs/{job_id}", summary="Crawl job detail")
def job_detail(job_id: str, session: DbSession, admin: Admin) -> dict[str, Any]:
    job = session.get(CrawlJob, job_id)
    if job is None:
        raise not_found("job_not_found", "Job not found", job_id=job_id)
    return envelope(_job_to_dict(job), cache={"hit": False})


@router.post("/jobs/{job_id}/cancel", summary="Cancel a queued/running job")
def cancel_job(job_id: str, session: DbSession, state: State, admin: Admin) -> dict[str, Any]:
    ok = state.jobs.cancel(session, job_id)
    session.commit()
    if not ok:
        raise not_found("job_not_found", "Job not found or already finished", job_id=job_id)
    return envelope({"task_id": job_id, "status": "canceled"}, cache={"hit": False})


@router.post("/jobs/{job_id}/retry", summary="Requeue a finished job")
def retry_job(job_id: str, session: DbSession, state: State, admin: Admin) -> dict[str, Any]:
    job = session.get(CrawlJob, job_id)
    if job is None:
        raise not_found("job_not_found", "Job not found", job_id=job_id)
    if job.status in ("queued", "running"):
        raise bad_request("job_active", "Job is already active", job_id=job_id)
    job.status = "queued"
    job.dedupe_active = "1"
    job.attempts = 0
    job.error = None
    job.finished_at = None
    from app.db.models import utcnow

    job.next_run_at = utcnow()
    session.flush()
    session.commit()
    return envelope({"task_id": job_id, "status": "queued"}, cache={"hit": False})


@router.get("/history", summary="Recent crawl history")
def history(
    session: DbSession,
    admin: Admin,
    pager: Pager,
    book_id: Annotated[str | None, Query()] = None,
    outcome: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    from sqlalchemy import func

    filters = []
    if book_id:
        filters.append(CrawlHistory.book_id == book_id)
    if outcome:
        filters.append(CrawlHistory.outcome == outcome)
    total = int(
        session.scalar(select(func.count()).select_from(CrawlHistory).where(*filters)) or 0
    )
    rows = list(
        session.scalars(
            select(CrawlHistory)
            .where(*filters)
            .order_by(CrawlHistory.created_at.desc())
            .limit(pager["page_size"])
            .offset((pager["page"] - 1) * pager["page_size"])
        )
    )
    return envelope(
        [
            {
                "id": row.id,
                "job_id": row.job_id,
                "book_id": row.book_id,
                "lang": row.lang,
                "outcome": row.outcome,
                "http_status": row.http_status,
                "source_kind": row.source_kind,
                "detail": row.detail,
                "items_written": row.items_written,
                "duration_ms": row.duration_ms,
                "created_at": row.created_at,
            }
            for row in rows
        ],
        pagination_info=paginate(total, pager["page"], pager["page_size"]),
        cache={"hit": False},
    )


@router.get("/source", summary="Upstream source health (rates, breaker)")
def source_status(state: State, admin: Admin) -> dict[str, Any]:
    return envelope(
        {
            "stats": state.source.stats.snapshot(),
            "settings": {
                "requests_per_minute": state.settings.source_requests_per_minute,
                "concurrency": state.settings.source_concurrency,
                "max_retries": state.settings.source_max_retries,
                "respect_robots_txt": state.settings.respect_robots_txt,
            },
            "budget_tokens": state.source._bucket.available_tokens,  # noqa: SLF001 - ops visibility
        },
        cache={"hit": False},
    )


# --- editions --------------------------------------------------------------
@router.get("/editions/candidates", summary="Candidate / verified edition matches")
def edition_candidates(
    session: DbSession,
    admin: Admin,
    pager: Pager,
    status_filter: Annotated[str | None, Query(alias="status", pattern="^(VERIFIED|CANDIDATE|UNKNOWN|REJECTED)$")] = None,
) -> dict[str, Any]:
    from sqlalchemy import func

    filters = [EditionMatch.status == status_filter] if status_filter else []
    total = int(session.scalar(select(func.count()).select_from(EditionMatch).where(*filters)) or 0)
    rows = list(
        session.scalars(
            select(EditionMatch)
            .where(*filters)
            .order_by(EditionMatch.confidence.desc(), EditionMatch.created_at.desc())
            .limit(pager["page_size"])
            .offset((pager["page"] - 1) * pager["page_size"])
        )
    )
    items = []
    for row in rows:
        source = session.get(MovieEdition, row.source_book_id)
        target = session.get(MovieEdition, row.target_book_id)
        items.append(
            {
                "match_id": row.id,
                "group_id": row.movie_group_id,
                "source": {"book_id": row.source_book_id, "lang": row.source_lang,
                           "title": source.book_title if source else None},
                "target": {"book_id": row.target_book_id, "lang": row.target_lang,
                           "title": target.book_title if target else None},
                "status": row.status,
                "confidence": row.confidence,
                "method": row.method,
                "evidence": row.evidence,
                "verified_by": row.verified_by,
                "verified_at": row.verified_at,
                "note": row.note,
                "created_at": row.created_at,
            }
        )
    return envelope(items, pagination_info=paginate(total, pager["page"], pager["page_size"]), cache={"hit": False})


@router.post("/editions/{match_id}/verify", summary="Manually verify / reject a match")
def verify_match(
    match_id: str, session: DbSession, admin: Admin, payload: Annotated[VerifyEditionRequest, Body()]
) -> dict[str, Any]:
    from app.db.models import utcnow

    match = session.get(EditionMatch, match_id)
    if match is None:
        raise not_found("match_not_found", "Match not found", match_id=match_id)
    match.status = payload.status
    if payload.confidence is not None:
        match.confidence = payload.confidence
    if payload.note:
        match.note = payload.note
    match.verified_by = admin.subject
    match.verified_at = utcnow()
    match.evidence = {**(match.evidence or {}), "manual_verification": True, "verified_by": admin.subject}
    if payload.status in ("VERIFIED", "CANDIDATE"):
        from app.crawler.store import link_group, refresh_group_stats

        group_id = link_group(session, match.source_book_id, match.target_book_id)
        match.movie_group_id = group_id
        if group_id:
            refresh_group_stats(session, group_id)
    session.commit()
    return envelope(
        {"match_id": match.id, "status": match.status, "confidence": match.confidence,
         "group_id": match.movie_group_id},
        cache={"hit": False},
    )


@router.post("/editions/merge", summary="Manually group two editions")
def merge_editions(session: DbSession, admin: Admin, payload: Annotated[MergeRequest, Body()]) -> dict[str, Any]:
    from app.crawler.store import link_group, refresh_group_stats
    from app.db.models import MovieEdition, utcnow

    source = session.get(MovieEdition, payload.source_book_id)
    target = session.get(MovieEdition, payload.target_book_id)
    if source is None or target is None:
        raise not_found(
            "edition_not_found",
            "One or both editions are not indexed",
            source_indexed=source is not None,
            target_indexed=target is not None,
        )
    match = session.scalar(
        select(EditionMatch).where(
            EditionMatch.source_book_id == payload.source_book_id,
            EditionMatch.target_book_id == payload.target_book_id,
        )
    )
    if match is None:
        match = EditionMatch(
            source_book_id=payload.source_book_id,
            target_book_id=payload.target_book_id,
            source_lang=source.lang,
            target_lang=target.lang,
            status=payload.status,
            confidence=1.0 if payload.status == "VERIFIED" else 0.5,
            method="manual",
            evidence={"manual": True, "verified_by": admin.subject},
            note=payload.note,
            verified_by=admin.subject if payload.status == "VERIFIED" else None,
            verified_at=utcnow() if payload.status == "VERIFIED" else None,
        )
        session.add(match)
        session.flush()
    else:
        match.status = payload.status
        match.method = "manual"
        match.note = payload.note or match.note
        match.verified_by = admin.subject
        match.verified_at = utcnow()
        match.evidence = {**(match.evidence or {}), "manual": True}
    group_id = link_group(session, payload.source_book_id, payload.target_book_id)
    match.movie_group_id = group_id
    if group_id:
        refresh_group_stats(session, group_id)
    session.commit()
    return envelope(
        {"match_id": match.id, "group_id": group_id, "status": match.status}, cache={"hit": False}
    )


# --- maintenance -----------------------------------------------------------
@router.get("/cache", summary="Cache summary")
def cache_summary(session: DbSession, admin: Admin) -> dict[str, Any]:
    from sqlalchemy import func

    from app.db.models import CacheEntry, utcnow

    rows = session.execute(
        select(CacheEntry.kind, func.count(CacheEntry.key), func.coalesce(func.sum(CacheEntry.hits), 0))
        .group_by(CacheEntry.kind)
    ).all()
    expired = int(
        session.scalar(select(func.count()).select_from(CacheEntry).where(CacheEntry.expires_at < utcnow())) or 0
    )
    return envelope(
        {
            "by_kind": [
                {"kind": kind, "entries": int(count), "hits": int(hits)} for kind, count, hits in rows
            ],
            "expired": expired,
        },
        cache={"hit": False},
    )


@router.post("/cache/purge", summary="Purge expired cache entries")
def cache_purge(session: DbSession, admin: Admin) -> dict[str, Any]:
    from app.crawler.store import cache_purge_expired

    removed = cache_purge_expired(session)
    session.commit()
    return envelope({"expired_removed": removed}, cache={"hit": False})


@router.post("/backup", summary="Create a consistent SQLite backup")
def backup(state: State, admin: Admin) -> dict[str, Any]:
    result = maintenance.sqlite_backup(state.settings, label="admin")
    return envelope(result, cache={"hit": False})


@router.post("/maintenance/prune", summary="Prune old history / usage rows")
def prune(session: DbSession, admin: Admin, days: Annotated[int, Query(ge=1, le=3650)] = 30) -> dict[str, Any]:
    result = maintenance.prune_history(session, days=days)
    session.commit()
    return envelope(result, cache={"hit": False})
