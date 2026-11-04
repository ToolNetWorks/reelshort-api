"""ToolNet ReelShort Metadata API — FastAPI application entrypoint."""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import admin as admin_api
from app.api import health as health_api
from app.api import series as series_api
from app.api import v1 as v1_api
from app.api.errors import register_error_handlers
from app.config import Settings, get_settings
from app.crawler.service import CrawlService
from app.crawler.source import ReelShortSource
from app.db.base import dispose_engine, get_session_factory, os_environ_bind
from app.db.models import utcnow
from app.logging_config import configure_from_settings, request_id_var
from app.security import client_ip, record_usage
from app.services.jobs import JobManager
from app.services.state import AppState
from app.services.worker import WorkerPool

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    configure_from_settings(settings.log_level, settings.log_file)
    # bind the database engine to *this* app's settings (test/dev isolation)
    os_environ_bind(settings)
    if settings.admin_auth_enabled and not settings.admin_configured:
        log.warning("admin authentication is enabled but no credentials are configured — all admin calls will be rejected")

    source = app.state.source_factory(settings) if getattr(app.state, "source_factory", None) else ReelShortSource(settings)
    await source.start()
    jobs = JobManager(settings)
    crawl = CrawlService(source, settings)
    pool = WorkerPool(source, settings=settings, jobs=jobs, crawl_service=crawl)
    ctx = AppState(settings=settings, source=source, crawl=crawl, jobs=jobs, pool=pool)
    app.state.ctx = ctx
    await pool.start()
    log.info(
        "service started",
        extra={
            "version": settings.app_version,
            "source": settings.source_base_url,
            "db_path": settings.db_path,
            "workers": settings.worker_concurrency,
        },
    )
    try:
        yield
    finally:
        log.info("shutdown requested — draining workers")
        await pool.stop(timeout=settings.shutdown_grace_seconds)
        await source.close()
        dispose_engine()
        log.info("shutdown complete")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="ToolNet ReelShort Metadata API",
        version=settings.app_version,
        description=(
            "Self-hosted metadata API for ReelShort language editions.\n\n"
            "* Metadata only: no video URLs, no DRM/stream payloads.\n"
            "* Cache-first with TTL, background refresh and bounded concurrency.\n"
            "* Public key auth (`X-API-Key`) is separate from admin credentials "
            "(HTTP Basic or `X-Admin-Token`).\n"
            "* Admin crawl tasks are never exposed on public endpoints."
        ),
        openapi_url="/openapi.json" if settings.docs_enabled else None,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    # Every `Depends(get_settings)` must resolve *this* app's settings, not the
    # process-wide cached singleton (tests and multi-app setups rely on this).
    app.dependency_overrides[get_settings] = lambda: settings

    register_error_handlers(app)
    app.include_router(health_api.router)
    app.include_router(v1_api.router)
    app.include_router(series_api.router)
    app.include_router(admin_api.router)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if not settings.environment.startswith("prod") else [],
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def observability(request: Request, call_next):  # noqa: ANN001, ANN202
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status_code = 500
        cache_hit: bool | None = None
        subject = "anonymous"
        role = "anonymous"
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["x-request-id"] = request_id
            subject = getattr(request.state, "subject", subject)
            role = getattr(request.state, "role", role)
            cache_hit = getattr(request.state, "cache_hit", None)
            return response
        except Exception:
            raise
        finally:
            duration_ms = (time.perf_counter() - started) * 1000
            path = request.url.path
            if not path.startswith("/health"):
                log.info(
                    "request",
                    extra={
                        "method": request.method,
                        "path": path,
                        "status": status_code,
                        "duration_ms": round(duration_ms, 2),
                        "client_ip": client_ip(request, settings),
                    },
                )
                if path.startswith(("/v1", "/admin")):
                    record_usage(
                        subject=subject,
                        role=role,
                        route=path,
                        method=request.method,
                        status_code=status_code,
                        duration_ms=round(duration_ms, 2),
                        cache_hit=cache_hit,
                        ip=client_ip(request, settings),
                        user_agent=request.headers.get("user-agent"),
                    )
            request_id_var.reset(token)

    @app.get("/", include_in_schema=False)
    def root() -> JSONResponse:
        return JSONResponse(
            {
                "name": settings.app_name,
                "version": settings.app_version,
                "docs": "/docs" if settings.docs_enabled else None,
                "health": "/health",
                "endpoints": {
                    "public": [
                        "GET /v1/meta",
                        "GET /v1/movies",
                        "GET /v1/movies/{book_id}",
                        "GET /v1/movies/{book_id}/languages",
                        "GET /v1/movies/{book_id}/episodes",
                        "GET /v1/search",
                        "GET /v1/groups/{group_id}",
                        "POST /v1/discover",
                        "GET /v1/tasks/{task_id}",
                    ],
                    "admin": [
                        "POST /admin/crawl",
                        "POST /admin/discover",
                        "GET /admin/stats",
                        "GET /admin/jobs",
                        "GET /admin/editions/candidates",
                        "POST /admin/editions/{match_id}/verify",
                        "POST /admin/editions/merge",
                        "POST /admin/backup",
                    ],
                },
                "policy": "metadata only — no video download",
                "server_time": int(time.time()),
            }
        )

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        """Compatibility alias (some probes use /healthz)."""
        return {"status": "ok"}

    return app


app = create_app()


def _session_factory():  # pragma: no cover - re-export for tooling
    return get_session_factory()


__all__ = ["app", "create_app", "utcnow"]
