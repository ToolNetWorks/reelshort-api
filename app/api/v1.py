"""Public read/discovery API (``/v1``)."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Annotated, Any, Iterator

from fastapi import APIRouter, Body, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import envelope, get_state, paginate, pagination
from app.api.errors import bad_request, not_found, unavailable
from app.config import Settings, get_settings
from app.db.base import session_scope
from app.db.models import CatalogState, CrawlJob, Episode, MovieEdition, utcnow
from app.logging_config import request_id_var
from app.schemas import DiscoverRequest
from app.security import Principal, rate_limit, require_public
from app.services import library
from app.services.state import AppState

log = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["public"])


def get_db() -> Iterator[Session]:
    with session_scope() as session:
        yield session


DbSession = Annotated[Session, Depends(get_db)]
Pager = Annotated[dict[str, int], Depends(pagination)]
PublicKey = Annotated[Principal, Depends(require_public)]
State = Annotated[AppState, Depends(get_state)]


# --- catalogue -------------------------------------------------------------
@router.get("/movies", summary="List indexed movies for a locale (cache-first, background refresh)")
def list_movies(
    session: DbSession,
    pager: Pager,
    state: State,
    principal: PublicKey,
    lang: Annotated[str | None, Query(description="Locale filter, e.g. en, vi")] = None,
    tag: Annotated[str | None, Query(description="Filter by tag name (substring)")] = None,
    category: Annotated[str | None, Query(description="Filter by category/shelf")] = None,
    q: Annotated[str | None, Query(description="Optional search filter")] = None,
    order: Annotated[str, Query(pattern="^(recent|title|episodes|top|views|new)$")] = "recent",
    grouped: Annotated[bool, Query(description="Group editions by movie_group_id (1 item per movie)")] = False,
) -> dict[str, Any]:
    rate_limit(f"public:{principal.subject}:list", state.settings.api_rate_limit_per_minute * 2, 60.0)
    clean_lang = (lang or "").strip().lower() or None

    items, total = library.list_movies(
        session,
        lang=clean_lang,
        tag=tag,
        category=category,
        q=q,
        page=pager["page"],
        page_size=pager["page_size"],
        order=order,
        grouped=grouped,
    )

    sync_meta: dict[str, Any] = {"status": "idle", "locale": clean_lang, "movie_count": total}
    is_stale = False
    cache_hit = total > 0
    catalog_counts: dict[str, int] = {
        "source_total": total,
        "discovered_total": total,
        "unique_total": total,
        "indexed_total": total,
        "enriched_total": total,
    }

    if clean_lang:
        cat_snap = library.catalog_status_snapshot(session, clean_lang, state_ctx=state)
        catalog_counts = {
            "source_total": cat_snap["source_total"],
            "discovered_total": cat_snap["discovered_total"],
            "unique_total": cat_snap["unique_total"],
            "indexed_total": cat_snap["indexed_total"],
            "enriched_total": cat_snap["enriched_total"],
        }
        active_job = session.scalar(
            select(CrawlJob).where(
                CrawlJob.dedupe_key == f"locale_catalog:{clean_lang}",
                CrawlJob.dedupe_active == "1",
            )
        )
        cat_state = session.get(CatalogState, clean_lang)
        latest_fetch = (
            (cat_state.last_completed_at if cat_state and cat_state.last_completed_at else None)
            or session.scalar(select(func.max(MovieEdition.fetched_at)).where(MovieEdition.lang == clean_lang))
        )
        is_stale = (
            latest_fetch is None
            or latest_fetch < utcnow() - timedelta(hours=state.settings.catalog_ttl_hours)
        )

        # Only trigger background crawl when index for this locale is empty or TTL has expired
        if (total == 0 and not q and not tag and not category) or (
            is_stale and state.settings.stale_while_revalidate and not q and not tag and not category
        ):
            if active_job is None:
                task_id, created = state.pool.enqueue(
                    "locale_catalog",
                    f"locale_catalog:{clean_lang}",
                    {"lang": clean_lang, "force": False, "incremental": total > 0},
                    scope="public",
                    priority=2 if total == 0 else 7,
                    requested_by=principal.subject,
                )
                sync_meta = {
                    "status": "syncing",
                    "task_id": task_id,
                    "task_created": created,
                    "locale": clean_lang,
                    "movie_count": total,
                    "is_stale": is_stale,
                }
            else:
                sync_meta = {
                    "status": "syncing",
                    "task_id": active_job.id,
                    "locale": clean_lang,
                    "movie_count": total,
                    "progress": active_job.progress,
                    "is_stale": is_stale,
                }
        elif active_job is not None:
            sync_meta = {
                "status": "syncing",
                "task_id": active_job.id,
                "locale": clean_lang,
                "movie_count": total,
                "progress": active_job.progress,
            }
        else:
            sync_meta = {
                "status": "idle",
                "locale": clean_lang,
                "movie_count": total,
                "last_synced_at": latest_fetch.isoformat() if latest_fetch else None,
            }

    pagination_info = paginate(total, pager["page"], pager["page_size"])
    resp = envelope(
        items,
        cache={"hit": cache_hit, "source": "index" if cache_hit else "none", "stale": is_stale},
        sync=sync_meta,
        pagination_info=pagination_info,
        catalog_counts=catalog_counts,
    )
    resp.update(
        {
            "items": items,
            "total": total,
            "page": pager["page"],
            "page_size": pager["page_size"],
            "has_more": pagination_info["has_more"],
            **catalog_counts,
        }
    )
    return resp


@router.get("/catalog/status", summary="Sync status and metrics of a locale catalog")
def get_catalog_status(
    session: DbSession,
    state: State,
    principal: PublicKey,
    lang: Annotated[str, Query(description="Locale, e.g. en, vi")] = "en",
) -> dict[str, Any]:
    return library.catalog_status_snapshot(session, lang, state_ctx=state)


@router.post("/catalog/sync", summary="Trigger catalog discovery/refresh for a locale")
def trigger_catalog_sync(
    session: DbSession,
    state: State,
    principal: PublicKey,
    lang: Annotated[str, Query(description="Locale to crawl, e.g. en, vi")] = "en",
    force: Annotated[bool, Query(description="Force refresh even if recently crawled")] = True,
) -> dict[str, Any]:
    rate_limit(f"public:{principal.subject}:sync", 10, 60.0)
    clean_lang = lang.strip().lower()
    task_id, created = state.pool.enqueue(
        "locale_catalog",
        f"locale_catalog:{clean_lang}",
        {"lang": clean_lang, "force": force},
        scope="public",
        priority=1,
        requested_by=principal.subject,
    )
    return {
        "task_id": task_id,
        "created": created,
        "status": "queued" if created else "already_queued",
        "locale": clean_lang,
    }


@router.get("/search", summary="Search the local index, optionally filtered by locale")
def search(
    session: DbSession,
    pager: Pager,
    state: State,
    principal: PublicKey,
    q: Annotated[str, Query(min_length=0, max_length=200)] = "",
    lang: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    rate_limit(f"public:{principal.subject}:search", state.settings.api_rate_limit_per_minute, 60.0)
    items, total = library.search_movies(
        session, q, lang=lang, page=pager["page"], page_size=pager["page_size"]
    )
    pagination_info = paginate(total, pager["page"], pager["page_size"])
    resp = envelope(
        items,
        cache={"hit": True, "source": "index"},
        pagination_info=pagination_info,
    )
    resp.update(
        {
            "items": items,
            "total": total,
            "page": pager["page"],
            "page_size": pager["page_size"],
            "has_more": pagination_info["has_more"],
        }
    )
    return resp


# --- movie detail ----------------------------------------------------------
@router.get("/movies/{book_id}", summary="Movie metadata by book_id (cache-first)")
async def get_movie(
    book_id: str,
    session: DbSession,
    state: State,
    principal: PublicKey,
    include_editions: bool = True,
    live: Annotated[bool, Query(description="Fetch immediately from the source (bounded, rate limited)")] = False,
    lang: Annotated[str | None, Query(description="Requested language edition")] = None,
) -> dict[str, Any]:
    rate_limit(f"public:{principal.subject}:movie", state.settings.api_rate_limit_per_minute, 60.0)
    edition = library.get_edition(session, book_id)
    if edition is None or live:
        task_id, created = state.pool.enqueue(
            "movie",
            f"movie:{book_id}",
            {"book_id": book_id, "include_episodes": True, "force": False},
            scope="public",
            priority=3,
            requested_by=principal.subject,
        )
        if edition is None:
            raise not_found(
                "not_indexed",
                "Movie is not indexed yet; a crawl job was queued",
                book_id=book_id,
                task_id=task_id,
                task_created=created,
                status_url=f"/v1/tasks/{task_id}",
                hint="Poll /v1/tasks/{task_id} then retry, or call again with ?live=true",
            )
        if live:
            await state.pool.wait_idle(timeout=min(20.0, state.settings.shutdown_grace_seconds))
            session.expire_all()
            edition = library.get_edition(session, book_id) or edition

    if lang and edition is not None and lang != edition.lang:
        for candidate in library.group_editions(session, edition.movie_group_id):
            if candidate.lang == lang:
                edition = candidate
                break

    # When opening movie detail, if episodes are not yet in Turso, fetch on demand
    if edition is not None and (edition.stored_chapter_count or 0) == 0 and (edition.chapter_count or 0) > 0:
        lock = state.get_sync_lock(edition.book_id)
        async with lock:
            session.expire_all()
            edition = session.get(MovieEdition, edition.book_id) or edition
            if (edition.stored_chapter_count or 0) == 0 and (edition.chapter_count or 0) > 0:
                try:
                    await asyncio.wait_for(
                        state.crawl.sync_episodes(session, edition=edition, force=False),
                        timeout=20.0,
                    )
                    session.commit()
                    edition = session.get(MovieEdition, edition.book_id) or edition
                except Exception as exc:
                    log.warning("get_movie on-demand sync deferred/failed", extra={"book_id": edition.book_id, "error": str(exc)})

    editions = library.editions_with_status(session, edition) if include_editions else []
    data = library.movie_to_dict(edition, editions=editions, settings=state.settings)
    if edition.book_id != book_id:
        # the request used an alias id; report both so callers can cache the map
        data["requested_book_id"] = book_id
        data["alias_resolved"] = True
    cache_flag: dict[str, Any] = {"hit": True, "source": "db"}
    if data["is_stale"]:
        task_id, created = state.pool.enqueue(
            "refresh",
            f"refresh:{book_id}",
            {"book_id": book_id, "include_episodes": True, "force": False},
            scope="public",
            priority=7,
        )
        cache_flag.update({"stale": True, "refresh_queued": created, "refresh_task_id": task_id})
    return envelope(data, cache=cache_flag)


@router.get("/movies/{book_id}/languages", summary="Language editions of a movie")
def movie_languages(
    book_id: str,
    session: DbSession,
    state: State,
    principal: PublicKey,
    targets: Annotated[str | None, Query(description="Comma separated locales to check, e.g. vi,es")] = None,
    discover: Annotated[bool, Query(description="Queue discovery for missing locales")] = False,
    include_unverified: bool = True,
) -> dict[str, Any]:
    rate_limit(f"public:{principal.subject}:langs", state.settings.api_rate_limit_per_minute, 60.0)
    edition = library.get_edition(session, book_id)
    if edition is None:
        raise not_found("not_indexed", "Movie is not indexed yet", book_id=book_id)
    target_list = [t.strip() for t in targets.split(",") if t.strip()] if targets else None
    rows = library.language_view(session, edition, targets=target_list)
    if not include_unverified:
        rows = [row for row in rows if row["status"] in ("VERIFIED", "MISSING")]
    task_id = None
    missing = [row["lang"] for row in rows if row["status"] == "MISSING"]
    if discover and missing:
        task_id, created = state.pool.enqueue(
            "discover",
            f"discover:{book_id}:{','.join(sorted(missing))}",
            {
                "book_id": book_id,
                "targets": missing,
                "strategies": ["search"],
                "include_movie": False,
            },
            scope="public",
            priority=6,
        )
    return envelope(
        {
            "book_id": book_id,
            "group_id": edition.movie_group_id,
            "languages": rows,
            "discovery_task_id": task_id,
        },
        cache={"hit": True, "source": "db"},
    )


@router.get("/movies/{book_id}/episodes", summary="Episode metadata (no video urls)")
async def movie_episodes(
    book_id: str,
    session: DbSession,
    state: State,
    principal: PublicKey,
    pager: Pager,
    ensure: Annotated[bool, Query(description="Queue an incremental sync when episodes are missing")] = False,
    lang: Annotated[str | None, Query(description="Request episodes of another language edition")] = None,
) -> dict[str, Any]:
    rate_limit(f"public:{principal.subject}:episodes", state.settings.api_rate_limit_per_minute * 2, 60.0)
    edition = library.get_edition(session, book_id)
    requested_edition = None
    if lang and edition is not None and lang != edition.lang:
        for candidate in library.group_editions(session, edition.movie_group_id):
            if candidate.lang == lang:
                requested_edition = candidate
                break
        if requested_edition is None:
            raise not_found(
                "no_edition_for_lang",
                f"No indexed edition for lang={lang}",
                book_id=book_id,
                lang=lang,
            )
    target = requested_edition or edition
    if target is None:
        raise not_found("not_indexed", "Movie is not indexed yet", book_id=book_id)

    expected = target.chapter_count or 0
    stored = target.stored_chapter_count or 0
    task_id = None
    sync_status = "COMPLETED" if (stored >= expected and expected > 0) else "SYNCING"
    sync_message = "Đã có đầy đủ danh sách tập." if sync_status == "COMPLETED" else "Đang kiểm tra và đồng bộ danh sách tập..."

    # On-demand sync if episodes are missing or ensure is requested (with deduplication lock)
    if (stored == 0 and expected > 0) or (ensure and stored < expected):
        lock = state.get_sync_lock(target.book_id)
        async with lock:
            session.expire_all()
            target = session.get(MovieEdition, target.book_id) or target
            expected = target.chapter_count or 0
            stored = target.stored_chapter_count or 0
            if stored < expected or stored == 0:
                try:
                    await asyncio.wait_for(
                        state.crawl.sync_episodes(session, edition=target, force=False),
                        timeout=25.0,
                    )
                    session.commit()
                    target = session.get(MovieEdition, target.book_id) or target
                    expected = target.chapter_count or 0
                    stored = target.stored_chapter_count or 0
                    sync_status = "COMPLETED" if stored >= expected else "PARTIAL"
                    sync_message = "Đồng bộ danh sách tập thành công."
                except asyncio.TimeoutError:
                    log.warning("on-demand episode sync timed out", extra={"book_id": target.book_id})
                    sync_status = "SYNCING"
                    sync_message = "Đang đồng bộ danh sách tập từ nguồn ReelShort trong tác vụ nền..."
                    task_id, _created = state.pool.enqueue(
                        "movie",
                        f"movie:{target.book_id}",
                        {"book_id": target.book_id, "include_episodes": True, "force": False},
                        scope="public",
                        priority=5,
                        requested_by=principal.subject,
                    )
                except Exception as exc:
                    log.warning("on-demand episode sync failed", extra={"book_id": target.book_id, "error": str(exc)})
                    sync_status = "PARTIAL" if stored > 0 else "FAILED"
                    sync_message = f"Không thể lấy thêm tập từ nguồn: {exc}"
                    task_id, _created = state.pool.enqueue(
                        "movie",
                        f"movie:{target.book_id}",
                        {"book_id": target.book_id, "include_episodes": True, "force": False},
                        scope="public",
                        priority=5,
                        requested_by=principal.subject,
                    )
            else:
                sync_status = "COMPLETED"
                sync_message = "Đã có đầy đủ danh sách tập."

    rows, total = library.list_episodes(
        session, target.book_id, page=pager["page"], page_size=pager["page_size"]
    )

    all_stored_serials = set(
        session.scalars(select(Episode.serial_number).where(Episode.book_id == target.book_id))
    )
    missing_episodes = [n for n in range(1, expected + 1) if n not in all_stored_serials] if expected > 0 else []
    complete = expected > 0 and len(missing_episodes) == 0

    return envelope(
        {
            "book_id": target.book_id,
            "lang": target.lang,
            "chapter_count": expected,
            "stored_episode_count": stored,
            "first_chapter_id": target.first_chapter_id,
            "sync_status": sync_status,
            "sync_message": sync_message,
            "sync_task_id": task_id,
            "complete": complete,
            "missing_episodes": missing_episodes,
            "episodes": [library.episode_to_dict(row) for row in rows],
        },
        cache={"hit": stored >= expected and expected > 0, "source": "db"},
        pagination_info=paginate(total, pager["page"], pager["page_size"]),
    )


@router.get("/groups/{group_id}", summary="Movie group with all verified editions")
def get_group(
    group_id: str,
    session: DbSession,
    state: State,
    principal: PublicKey,
    targets: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    rate_limit(f"public:{principal.subject}:group", state.settings.api_rate_limit_per_minute, 60.0)
    target_list = [t.strip() for t in targets.split(",") if t.strip()] if targets else None
    data = library.group_detail(session, group_id, targets=target_list)
    if data is None:
        raise not_found("group_not_found", "Movie group not found", group_id=group_id)
    return envelope(data, cache={"hit": True, "source": "db"})


# --- discovery -------------------------------------------------------------
@router.post("/discover", summary="Discover language editions for a movie")
def discover(
    state: State,
    principal: PublicKey,
    payload: Annotated[DiscoverRequest, Body()],
) -> dict[str, Any]:
    settings = state.settings
    rate_limit(
        f"discover:{principal.subject}",
        settings.discover_rate_limit_per_hour,
        3600.0,
        code="discover_quota_exceeded",
        message="Discovery quota exceeded for this key",
    )
    book_id = state.crawl.resolve_book_id(payload.book_id, payload.url)
    if not book_id:
        raise bad_request("invalid_target", "Provide book_id or a ReelShort movie URL")
    targets = payload.targets or []
    for locale in targets:
        if locale not in settings.discovery_locales and locale not in (
            "en", "vi", "th", "es", "fr", "de", "ja", "ko",
        ):
            raise bad_request("unsupported_locale", f"Unsupported target locale: {locale}")

    with session_scope() as session:
        edition = library.get_edition(session, book_id)
        ranked = [None]
        if edition is not None:
            rows = library.language_view(session, edition, targets=targets or None)
            if not payload.include_unverified:
                rows = [row for row in rows if row["status"] in ("VERIFIED", "MISSING")]
            ranked[0] = rows

    key_targets = ",".join(sorted(targets)) if targets else "auto"
    task_id, created = state.pool.enqueue(
        "discover",
        f"discover:{book_id}:{key_targets}",
        {
            "book_id": book_id,
            "url": payload.url,
            "targets": targets or None,
            "strategies": ["search"],
            "include_movie": edition is None,
        },
        scope="public",
        priority=4,
        requested_by=principal.subject,
    )
    return envelope(
        {
            "task_id": task_id,
            "status": "queued" if created else "already_queued",
            "book_id": book_id,
            "group_id": edition.movie_group_id if edition else None,
            "languages": ranked[0] or [],
            "queued_locales": targets or settings.discovery_locales[: settings.discovery_max_locales_per_run],
            "deduplicated": not created,
            "task_url": f"/v1/tasks/{task_id}",
        },
        cache={"hit": False, "source": "queue"},
    )


@router.get("/tasks/{task_id}", summary="Status of a public task (discovery / lazy crawl)")
def get_task(task_id: str, session: DbSession, principal: PublicKey) -> dict[str, Any]:
    job = session.get(CrawlJob, task_id)
    if job is None or job.scope != "public":
        # admin crawl jobs are never exposed on the public API
        raise not_found("task_not_found", "Task not found", task_id=task_id)
    return envelope(
        {
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
            "checkpoint": {
                k: v for k, v in (job.checkpoint or {}).items() if k in ("phase", "page", "locale", "index", "total")
            },
        },
        cache={"hit": True, "source": "db"},
    )
