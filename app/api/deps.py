"""FastAPI dependencies: shared state, pagination, envelope helpers."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Annotated, Any, Callable

from fastapi import Depends, Query, Request

from app.api.errors import bad_request
from app.config import Settings, get_settings
from app.logging_config import request_id_var
from app.services.state import AppState

CacheFlag = dict[str, Any]


def get_state(request: Request) -> AppState:
    state = getattr(request.app.state, "ctx", None)
    if state is None:  # pragma: no cover - app not fully started
        raise RuntimeError("application state is not initialised")
    return state


def get_source(request: Request):  # noqa: ANN201 - circular friendliness
    return get_state(request).source


def get_pool(request: Request):
    return get_state(request).pool


def get_crawl(request: Request):
    return get_state(request).crawl


def get_jobs(request: Request):
    return get_state(request).jobs


def pagination(
    page: Annotated[int, Query(ge=1, le=100000, description="1-based page number")] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, description="Items per page")] = 20,
    settings: Annotated[Settings, Depends(get_settings)] = None,  # type: ignore[assignment]
) -> dict[str, int]:
    settings = settings or get_settings()
    if page_size > settings.max_page_size:
        raise bad_request("page_size_too_large", f"page_size must be <= {settings.max_page_size}")
    return {"page": page, "page_size": page_size}


def meta(
    *,
    request_id: str | None = None,
    cache: CacheFlag | None = None,
    pagination_info: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    res = {
        "request_id": request_id or request_id_var.get(),
        "server_time": int(time.time()),
        "cache": cache or {"hit": False},
        "pagination": pagination_info,
    }
    res.update(extra)
    return res


def paginate(total: int, page: int, page_size: int) -> dict[str, Any]:
    total_pages = (total + page_size - 1) // page_size if page_size else 0
    has_more = page < total_pages
    return {
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
        "has_next": has_more,
        "has_more": has_more,
        "has_prev": page > 1,
    }


def envelope(
    data: Any,
    *,
    cache: CacheFlag | None = None,
    pagination_info: dict[str, Any] | None = None,
    request_id: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "data": data,
        "error": None,
        "meta": meta(
            cache=cache,
            pagination_info=pagination_info,
            request_id=request_id,
            **extra,
        ),
    }


def iso(value: datetime | None) -> str | None:
    return value.isoformat() + "Z" if value is not None else None


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


__all__ = [
    "get_state",
    "get_source",
    "get_pool",
    "get_crawl",
    "get_jobs",
    "pagination",
    "meta",
    "paginate",
    "envelope",
    "iso",
    "clamp",
    "Callable",
]
