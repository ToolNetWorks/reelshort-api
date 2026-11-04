"""Health, readiness and metrics-friendly endpoints."""

from __future__ import annotations

import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from app.api.deps import envelope, get_state
from app.config import Settings, get_settings
from app.db.base import healthcheck as db_healthcheck
from app.db.models import utcnow
from app.services.state import AppState

router = APIRouter(tags=["health"])


@router.get("/health", summary="Liveness + dependency snapshot")
def health(state: Annotated[AppState, Depends(get_state)], settings: Annotated[Settings, Depends(get_settings)]) -> dict[str, Any]:
    db = db_healthcheck()
    queue = state.pool.snapshot()
    source = state.source.stats.snapshot()
    degraded = not db.get("ok") or queue.get("last_error") is not None
    return {
        "status": "degraded" if degraded else "ok",
        "version": settings.app_version,
        "environment": settings.environment,
        "database": db,
        "queue": queue,
        "source": {
            "breaker_open": source.get("breaker_open"),
            "last_status": source.get("last_status"),
            "requests": source.get("requests"),
            "errors": source.get("errors"),
            "throttled": source.get("throttled"),
        },
        "uptime_seconds": state.uptime_seconds,
        "server_time": int(time.time()),
    }


@router.get("/health/live", summary="Minimal liveness probe")
def live() -> dict[str, Any]:
    return {"status": "ok", "server_time": int(time.time())}


@router.get("/health/ready", summary="Readiness probe (database reachable)")
def ready(state: Annotated[AppState, Depends(get_state)]) -> dict[str, Any]:
    db = db_healthcheck()
    return {"status": "ready" if db.get("ok") else "not_ready", "database": db}


@router.get("/v1/meta", summary="Service metadata (locales, TTLs, limits)")
def meta(state: Annotated[AppState, Depends(get_state)]) -> dict[str, Any]:
    settings = state.settings
    return envelope(
        {
            "name": settings.app_name,
            "version": settings.app_version,
            "environment": settings.environment,
            "source": settings.source_base_url,
            "supported_locales": list(settings.discovery_locales),
            "freshness": {
                "metadata_ttl_hours": settings.metadata_ttl_hours,
                "search_cache_ttl_minutes": settings.search_cache_ttl_minutes,
                "edition_ttl_hours": settings.edition_ttl_hours,
            },
            "limits": {
                "api_rate_limit_per_minute": settings.api_rate_limit_per_minute,
                "discover_rate_limit_per_hour": settings.discover_rate_limit_per_hour,
                "max_page_size": settings.max_page_size,
            },
            "policy": {
                "metadata_only": True,
                "stores_video_urls": False,
                "respects_robots_txt": settings.respect_robots_txt,
                "scheduled_refresh": settings.scheduler_enabled,
            },
            "generated_at": utcnow().isoformat(),
        },
        cache={"hit": True, "source": "static"},
    )
