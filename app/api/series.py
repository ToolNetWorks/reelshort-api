"""Downloader & Series Compatibility API (``/api/series``)."""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any, Iterator

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_state
from app.api.errors import not_found
from app.db.base import session_scope
from app.db.models import Episode, MovieEdition
from app.security import Principal, rate_limit, require_public
from app.services import library
from app.services.state import AppState

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/series", tags=["series"])


def get_db() -> Iterator[Session]:
    with session_scope() as session:
        yield session


DbSession = Annotated[Session, Depends(get_db)]
PublicKey = Annotated[Principal, Depends(require_public)]
State = Annotated[AppState, Depends(get_state)]


async def _sync_episodes_if_needed(
    state: AppState,
    session: Session,
    target: MovieEdition,
    principal_subject: str,
    *,
    timeout: float = 25.0,
) -> tuple[int, int, str, str, str | None]:
    """Ensure episodes are synced to Turso using per-book_id lock.
    
    Returns (expected, stored, sync_status, sync_message, task_id).
    """
    expected = target.chapter_count or 0
    stored = target.stored_chapter_count or 0
    task_id = None

    if stored >= expected and expected > 0:
        return expected, stored, "COMPLETED", "Đã có đầy đủ danh sách tập.", None

    lock = state.get_sync_lock(target.book_id)
    async with lock:
        session.expire_all()
        current = session.get(MovieEdition, target.book_id) or target
        expected = current.chapter_count or 0
        stored = current.stored_chapter_count or 0

        if stored >= expected and expected > 0:
            return expected, stored, "COMPLETED", "Đã có đầy đủ danh sách tập.", None

        if (stored == 0 and expected > 0) or stored < expected:
            try:
                await asyncio.wait_for(
                    state.crawl.sync_episodes(session, edition=current, force=False),
                    timeout=timeout,
                )
                session.commit()
                current = session.get(MovieEdition, target.book_id) or current
                expected = current.chapter_count or 0
                stored = current.stored_chapter_count or 0
                sync_status = "COMPLETED" if stored >= expected else "PARTIAL"
                sync_msg = "Đồng bộ danh sách tập thành công."
            except asyncio.TimeoutError:
                log.warning("episode sync timed out", extra={"book_id": target.book_id})
                sync_status = "SYNCING"
                sync_msg = "Đồng bộ mất thời gian, tác vụ đang tiếp tục chạy ngầm..."
                task_id, _ = state.pool.enqueue(
                    "movie",
                    f"movie:{target.book_id}",
                    {"book_id": target.book_id, "include_episodes": True, "force": False},
                    scope="public",
                    priority=5,
                    requested_by=principal_subject,
                )
            except Exception as exc:
                log.warning("episode sync failed", extra={"book_id": target.book_id, "error": str(exc)})
                sync_status = "PARTIAL" if stored > 0 else "FAILED"
                sync_msg = f"Không thể lấy thêm tập từ nguồn: {exc}"
                task_id, _ = state.pool.enqueue(
                    "movie",
                    f"movie:{target.book_id}",
                    {"book_id": target.book_id, "include_episodes": True, "force": False},
                    scope="public",
                    priority=5,
                    requested_by=principal_subject,
                )
            return expected, stored, sync_status, sync_msg, task_id

    return expected, stored, "IDLE", "Chưa có danh sách tập.", None


@router.get("/{series_id}", summary="Series detail with full episodes (Downloader compatible)")
async def series_detail(
    series_id: str,
    session: DbSession,
    state: State,
    principal: PublicKey,
    lang: Annotated[str | None, Query(description="Requested language edition")] = None,
) -> dict[str, Any]:
    rate_limit(f"public:{principal.subject}:series", state.settings.api_rate_limit_per_minute, 60.0)
    edition = library.get_edition(session, series_id)
    if edition is None:
        raise not_found("not_indexed", f"Series {series_id} is not indexed yet", series_id=series_id)

    # Resolve target language edition if requested
    target = edition
    if lang and edition.lang != lang:
        for candidate in library.group_editions(session, edition.movie_group_id):
            if candidate.lang == lang:
                target = candidate
                break

    # Ensure episodes are synced to Turso
    expected, stored, sync_status, sync_msg, task_id = await _sync_episodes_if_needed(
        state, session, target, principal.subject
    )

    all_eps = list(
        session.scalars(
            select(Episode).where(Episode.book_id == target.book_id).order_by(Episode.serial_number.asc())
        )
    )
    stored_serials = {e.serial_number for e in all_eps}
    missing_numbers = [n for n in range(1, expected + 1) if n not in stored_serials] if expected > 0 else []
    complete = expected > 0 and len(missing_numbers) == 0

    episodes_list = [
        {
            "number": ep.serial_number,
            "id": ep.chapter_id,
            "title": ep.title or "",
            "thumb": ep.thumb_url or "",
            "duration": ep.duration_seconds or 0,
        }
        for ep in all_eps
    ]

    editions = library.editions_with_status(session, target)
    return {
        "id": target.book_id,
        "external_id": str(target.t_book_id or ""),
        "first_chapter_id": str(target.first_chapter_id or ""),
        "paid_start": target.paid_start,
        "title": str(target.book_title or "Chưa có tên"),
        "poster": target.book_pic or "",
        "description": target.special_desc or "",
        "count": max(expected, len(episodes_list)),
        "stored_episode_count": len(episodes_list),
        "tags": target.tags or [],
        "shelves": [],
        "views": target.read_count or 0,
        "lang": target.lang,
        "group_id": target.movie_group_id or "",
        "sync_status": sync_status,
        "sync_message": sync_msg,
        "sync_task_id": task_id,
        "complete": complete,
        "missing_episodes": missing_numbers,
        "episodes": episodes_list,
        "editions": [e if isinstance(e, dict) else library.edition_to_dict(e) for e in editions],
        "source": "reelshort_api",
    }


@router.get("/{series_id}/episodes", summary="Series episode list (Downloader compatible)")
async def series_episodes(
    series_id: str,
    session: DbSession,
    state: State,
    principal: PublicKey,
    lang: Annotated[str | None, Query(description="Requested language edition")] = None,
) -> dict[str, Any]:
    rate_limit(f"public:{principal.subject}:series_eps", state.settings.api_rate_limit_per_minute * 2, 60.0)
    edition = library.get_edition(session, series_id)
    if edition is None:
        raise not_found("not_indexed", f"Series {series_id} is not indexed yet", series_id=series_id)

    target = edition
    if lang and edition.lang != lang:
        for candidate in library.group_editions(session, edition.movie_group_id):
            if candidate.lang == lang:
                target = candidate
                break

    expected, stored, sync_status, sync_msg, task_id = await _sync_episodes_if_needed(
        state, session, target, principal.subject
    )

    all_eps = list(
        session.scalars(
            select(Episode).where(Episode.book_id == target.book_id).order_by(Episode.serial_number.asc())
        )
    )
    stored_serials = {e.serial_number for e in all_eps}
    missing_numbers = [n for n in range(1, expected + 1) if n not in stored_serials] if expected > 0 else []
    complete = expected > 0 and len(missing_numbers) == 0

    episodes_list = [
        {
            "number": ep.serial_number,
            "id": ep.chapter_id,
            "title": ep.title or "",
            "thumb": ep.thumb_url or "",
            "duration": ep.duration_seconds or 0,
        }
        for ep in all_eps
    ]

    return {
        "series_id": target.book_id,
        "lang": target.lang,
        "count": len(episodes_list),
        "expected_count": expected,
        "sync_status": sync_status,
        "sync_message": sync_msg,
        "sync_task_id": task_id,
        "complete": complete,
        "missing_episodes": missing_numbers,
        "episodes": episodes_list,
    }
