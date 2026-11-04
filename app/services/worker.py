"""Background worker pool + scheduler (bounded concurrency, resumable jobs)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import timedelta
from typing import Any, Awaitable, Callable

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.config import Settings, get_settings
from app.crawler import store
from app.crawler.parser import parse_tag_catalog
from app.crawler.service import CrawlService
from app.crawler.source import ReelShortSource, SourceUnavailable
from app.db.base import session_scope
from app.db.models import CatalogState, CrawlJob, MovieEdition, utcnow
from app.services.jobs import JobManager

log = logging.getLogger(__name__)

MAX_DEFERRALS = 12


class WorkerPool:
    def __init__(
        self,
        source: ReelShortSource,
        *,
        settings: Settings | None = None,
        jobs: JobManager | None = None,
        crawl_service: CrawlService | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.source = source
        self.jobs = jobs or JobManager(self.settings)
        self.crawl = crawl_service or CrawlService(source, self.settings)
        self._tasks: list[asyncio.Task[None]] = []
        self._stop = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._active = 0
        self._scheduler_task: asyncio.Task[None] | None = None
        self.last_error: str | None = None
        self.started_at = utcnow()
        self.jobs_processed = 0
        self.jobs_failed = 0
        self._handlers: dict[str, Callable[[CrawlJob], Awaitable[dict[str, Any]]]] = {
            "movie": self._handle_movie,
            "refresh": self._handle_movie,
            "discover": self._handle_discover,
            "catalog": self._handle_catalog,
            "locale_catalog": self._handle_locale_catalog,
            "enrich_movie": self._handle_enrich_movie,
        }

    # --- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        self._stop.clear()
        # recover jobs interrupted by a restart (checkpoint preserved)
        with session_scope() as session:
            recovered = self.jobs.recover_orphans(session)
        if recovered:
            log.info("requeued interrupted jobs", extra={"count": recovered})
        for index in range(max(1, self.settings.worker_concurrency)):
            self._tasks.append(asyncio.create_task(self._worker_loop(f"worker-{index + 1}")))
        if self.settings.scheduler_enabled:
            self._scheduler_task = asyncio.create_task(self._scheduler_loop())
        log.info(
            "worker pool started",
            extra={"workers": len(self._tasks), "scheduler": self.settings.scheduler_enabled},
        )

    async def stop(self, timeout: float | None = None) -> None:
        self._stop.set()
        if self._scheduler_task is not None:
            self._scheduler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._scheduler_task
            self._scheduler_task = None
        for task in self._tasks:
            task.cancel()
        grace = timeout if timeout is not None else self.settings.shutdown_grace_seconds
        if self._tasks:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(*self._tasks, return_exceptions=True), timeout=max(1.0, grace)
                )
        self._tasks.clear()
        log.info("worker pool stopped", extra={"processed": self.jobs_processed, "failed": self.jobs_failed})

    async def wait_idle(self, timeout: float = 10.0) -> bool:
        try:
            await asyncio.wait_for(self._idle.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # --- worker loop -------------------------------------------------------
    async def _worker_loop(self, worker_id: str) -> None:
        while not self._stop.is_set():
            job: CrawlJob | None = None
            kind = "movie"
            job_id = ""
            payload: dict[str, Any] = {}
            try:
                with session_scope() as session:
                    job = self.jobs.claim_next(session, worker_id=worker_id)
                    if job is None:
                        self._idle.set()
                        await asyncio.sleep(self.settings.worker_poll_seconds)
                        continue
                    self._idle.clear()
                    payload = dict(job.payload or {})
                    kind = job.kind
                    job_id = job.id
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - db hiccup
                log.exception("job claim failed", extra={"worker": worker_id, "error": str(exc)})
                await asyncio.sleep(1.0)
                continue

            self._active += 1
            try:
                handler = self._handlers.get(kind)
                if handler is None:
                    raise RuntimeError(f"no handler registered for kind={kind}")
                effect = asyncio.Event()
                result = await self._run_with_timeout(
                    self._invoke(handler, job_id, payload, effect), job_id, kind
                )
                with session_scope() as session:
                    fresh = session.get(CrawlJob, job_id)
                    if fresh is None:
                        continue
                    if effect.is_set():
                        continue  # handler already transitioned the job
                    self.jobs.succeed(session, fresh, result)
                self.jobs_processed += 1
            except SourceUnavailable as exc:
                with session_scope() as session:
                    fresh = session.get(CrawlJob, job_id)
                    if fresh is not None and fresh.status == "running":
                        deferrals = int((fresh.checkpoint or {}).get("deferrals", 0)) + 1
                        fresh.checkpoint = {**(fresh.checkpoint or {}), "deferrals": deferrals}
                        if deferrals <= MAX_DEFERRALS:
                            fresh.attempts = max(0, fresh.attempts - 1)
                            self.jobs.defer(
                                session, fresh, exc.retry_after or 300.0, f"source unavailable: {exc}"
                            )
                        else:
                            self.jobs.fail(session, fresh, f"source unavailable too long: {exc}")
                log.warning("job deferred", extra={"job_id": job_id, "reason": str(exc)[:200]})
            except asyncio.TimeoutError:
                with session_scope() as session:
                    fresh = session.get(CrawlJob, job_id)
                    if fresh is not None and fresh.status == "running":
                        limit = self._timeout_for_kind(kind)
                        self.jobs.fail(session, fresh, f"job timed out after {limit}s")
                self.jobs_failed += 1
            except asyncio.CancelledError:
                self._active -= 1
                self._idle.set()
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("job failed", extra={"job_id": job_id, "kind": kind})
                with session_scope() as session:
                    fresh = session.get(CrawlJob, job_id)
                    if fresh is not None and fresh.status == "running":
                        self.jobs.fail(session, fresh, f"{type(exc).__name__}: {exc}")
                self.jobs_failed += 1
            finally:
                self._active -= 1
                if self._active <= 0:
                    self._idle.set()

    def _timeout_for_kind(self, kind: str) -> float:
        if kind == "discover":
            return self.settings.discovery_job_timeout_seconds
        if kind in ("locale_catalog", "catalog"):
            return self.settings.catalog_job_timeout_seconds
        return self.settings.job_timeout_seconds

    async def _invoke(
        self, handler: Callable[[CrawlJob], Awaitable[dict[str, Any]]], job_id: str, payload: dict[str, Any], effect: asyncio.Event
    ) -> dict[str, Any]:
        with session_scope() as session:
            job = session.get(CrawlJob, job_id)
            if job is None or job.status != "running":
                effect.set()
                return {}
            # re-attach the merged payload so handlers see fresh dict
            job.payload = payload
            try:
                return await handler(job)
            finally:
                session.flush()

    async def _run_with_timeout(
        self, coro: Awaitable[dict[str, Any]], job_id: str, kind: str = "movie"
    ) -> dict[str, Any]:
        timeout = self._timeout_for_kind(kind)
        return await asyncio.wait_for(coro, timeout=timeout)

    # --- handlers ----------------------------------------------------------
    async def _handle_movie(self, job: CrawlJob) -> dict[str, Any]:
        payload = job.payload or {}
        with session_scope() as session:
            local = session.get(CrawlJob, job.id)
            result = await self.crawl.crawl_movie(
                session,
                book_id=payload.get("book_id"),
                url=payload.get("url"),
                include_episodes=bool(payload.get("include_episodes", True)),
                force=bool(payload.get("force", False)),
                job=local,
                lang_hint=payload.get("lang_hint"),
            )
            summary = result.as_dict()
            if payload.get("discover"):
                summary["discovery"] = await self.crawl.discover(
                    session,
                    book_id=result.book_id or payload.get("book_id") or "",
                    targets=payload.get("targets"),
                    title_hints=payload.get("title_hints"),
                    strategies=payload.get("strategies") or ["search"],
                    job=local,
                )
            return summary

    async def _handle_discover(self, job: CrawlJob) -> dict[str, Any]:
        payload = job.payload or {}
        book_id = payload.get("book_id")
        with session_scope() as session:
            local = session.get(CrawlJob, job.id)
            if payload.get("include_movie") and (payload.get("url") or book_id):
                await self.crawl.crawl_movie(
                    session,
                    book_id=book_id,
                    url=payload.get("url"),
                    include_episodes=bool(payload.get("include_episodes", True)),
                    force=False,
                    job=local,
                )
            return await self.crawl.discover(
                session,
                book_id=book_id,
                targets=payload.get("targets"),
                title_hints=payload.get("title_hints"),
                strategies=payload.get("strategies") or ["search"],
                job=local,
            )

    async def _handle_catalog(self, job: CrawlJob) -> dict[str, Any]:
        """Index a bounded slice of the official per-locale catalogue."""
        payload = job.payload or {}
        lang = payload.get("lang") or "en"
        pages = int(payload.get("pages") or 1)
        page_size = int(payload.get("page_size") or 50)
        indexed = 0
        for page in range(1, pages + 1):
            with session_scope() as session:
                local = session.get(CrawlJob, job.id)
                if local is not None:
                    self.crawl._checkpoint(
                        session,
                        local,
                        {"phase": "catalog", "lang": lang, "page": page, "pages": pages},
                        progress=round(page / max(pages, 1) * 100, 1),
                    )
                payload_data = await self.source.tag_catalog(
                    self.settings.catalog_tag_id, lang, page=page, page_size=page_size
                )
                books, total = parse_tag_catalog(payload_data, lang)
                written = 0
                for book in books:
                    if session.get(MovieEdition, book.book_id) is not None:
                        continue
                    from app.crawler.parser import ParsedMovie

                    parsed = ParsedMovie(
                        book_id=book.book_id,
                        book_title=book.book_title,
                        lang=lang,
                        t_book_id=book.t_book_id,
                        book_pic=book.book_pic,
                        chapter_count=book.chapter_count,
                        first_chapter_id=book.extra.get("first_chapter_id"),
                        book_genre=book.extra.get("book_genre"),
                        book_source=book.extra.get("book_source"),
                        tags=list(book.extra.get("tag_names") or []),
                        source_url=f"{self.source.base_url}/movie/{book.book_id}",
                    )
                    edition = store.upsert_edition(session, parsed, source_kind="catalog")
                    if edition.movie_group_id is None:
                        store.ensure_group(session, edition)
                    written += 1
                indexed += written
                store.record_history(
                    session,
                    outcome="catalog_page",
                    job_id=job.id,
                    lang=lang,
                    source_kind="catalog",
                    detail=f"page={page} total={total}",
                    items_written=written,
                )
        return {"lang": lang, "pages": pages, "editions_indexed": indexed}

    async def _handle_locale_catalog(self, job: CrawlJob) -> dict[str, Any]:
        payload = job.payload or {}
        lang = str(payload.get("lang") or "vi").strip().lower()
        force = bool(payload.get("force", False))
        max_pages = payload.get("max_pages")
        incremental = bool(payload.get("incremental", False))
        reset_pages = bool(payload.get("reset_pages", False))
        with session_scope() as session:
            local = session.get(CrawlJob, job.id)
            result = await self.crawl.crawl_locale_catalog(
                session,
                lang,
                job=local,
                force=force,
                max_pages=int(max_pages) if max_pages is not None else None,
                incremental=incremental,
                reset_pages=reset_pages,
            )
            return {
                "locale": lang,
                "status": result.status or "ok",
                "movies_written": result.movies_written,
                **result.extra,
            }

    async def _handle_enrich_movie(self, job: CrawlJob) -> dict[str, Any]:
        payload = job.payload or {}
        book_id = str(payload.get("book_id") or "").strip()
        if not book_id:
            return {"status": "skipped", "reason": "missing_book_id"}
        with session_scope() as session:
            local = session.get(CrawlJob, job.id)
            result = await self.crawl.enrich_movie_metadata(
                session,
                book_id=book_id,
                lang_hint=payload.get("lang"),
                job=local,
            )
            return result.as_dict()

    # --- scheduler ---------------------------------------------------------
    async def _scheduler_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.sleep(max(30.0, self.settings.scheduler_interval_seconds))
                await self.run_scheduler_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive
                log.exception("scheduler cycle failed")

    async def run_scheduler_cycle(self) -> dict[str, int]:
        """Refresh stale editions, enrich METADATA_INCOMPLETE rows, sync expired catalogs, and purge cache."""
        enqueued = 0
        enriched_enqueued = 0
        catalogs_enqueued = 0
        with session_scope() as session:
            stale = self.crawl.stale_book_ids(session, self.settings.scheduler_batch_size)
            for book_id in stale:
                job, created = self.jobs.create(
                    session,
                    kind="refresh",
                    dedupe_key=f"refresh:{book_id}",
                    payload={"book_id": book_id, "include_episodes": True, "force": False},
                    scope="scheduler",
                    priority=8,
                )
                if created:
                    enqueued += 1

            incomplete = self.crawl.incomplete_book_ids(session, self.settings.enrichment_batch_size)
            for book_id in incomplete:
                job, created = self.jobs.create(
                    session,
                    kind="enrich_movie",
                    dedupe_key=f"enrich_movie:{book_id}",
                    payload={"book_id": book_id},
                    scope="scheduler",
                    priority=9,
                )
                if created:
                    enriched_enqueued += 1

            missing_eps = self.crawl.missing_episodes_book_ids(session, self.settings.enrichment_batch_size)
            for book_id in missing_eps:
                job, created = self.jobs.create(
                    session,
                    kind="movie",
                    dedupe_key=f"movie:{book_id}",
                    payload={"book_id": book_id, "include_episodes": True, "force": False},
                    scope="scheduler",
                    priority=9,
                )
                if created:
                    enriched_enqueued += 1

            if self.settings.auto_sync_enabled:
                catalog_threshold = utcnow() - timedelta(hours=self.settings.catalog_ttl_hours)
                for loc in self.settings.auto_sync_locales:
                    clean_loc = loc.strip().lower()
                    cat_state = session.scalar(
                        select(CatalogState).where(
                            CatalogState.provider == "reelshort",
                            CatalogState.locale == clean_loc,
                        )
                    )
                    if cat_state is not None and cat_state.last_synced_at is not None:
                        if cat_state.last_synced_at < catalog_threshold:
                            _j, created = self.jobs.create(
                                session,
                                kind="locale_catalog",
                                dedupe_key=f"locale_catalog:{clean_loc}",
                                payload={"lang": clean_loc, "force": True, "incremental": True},
                                scope="scheduler",
                                priority=6,
                            )
                            if created:
                                catalogs_enqueued += 1

            purged = store.cache_purge_expired(session)
        if enqueued or enriched_enqueued or catalogs_enqueued or purged:
            log.info(
                "scheduler cycle",
                extra={
                    "refreshes": enqueued,
                    "enrichments": enriched_enqueued,
                    "catalogs": catalogs_enqueued,
                    "cache_purged": purged,
                },
            )
        return {
            "refreshes_enqueued": enqueued,
            "enrichments_enqueued": enriched_enqueued,
            "catalogs_enqueued": catalogs_enqueued,
            "cache_entries_purged": purged,
        }

    # --- stats -------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        with session_scope() as session:
            counts = self.jobs.counts(session)
            queue_depth = int(
                session.scalar(select(func.count()).select_from(CrawlJob).where(CrawlJob.status == "queued")) or 0
            )
            running = int(
                session.scalar(select(func.count()).select_from(CrawlJob).where(CrawlJob.status == "running")) or 0
            )
        return {
            "workers": len(self._tasks),
            "active": self._active,
            "queue_depth": queue_depth,
            "running": running,
            "jobs_processed": self.jobs_processed,
            "jobs_failed": self.jobs_failed,
            "by_status": counts,
            "last_error": self.last_error,
            "idle": self._active == 0,
        }

    def enqueue(self, kind: str, dedupe_key: str, payload: dict[str, Any], **kwargs: Any) -> tuple[str, bool]:
        """API-side helper: create a job and return (task_id, created)."""
        with session_scope() as session:
            try:
                job, created = self.jobs.create(
                    session, kind=kind, dedupe_key=dedupe_key, payload=payload, **kwargs
                )
                return job.id, created
            except IntegrityError:  # pragma: no cover - race
                session.rollback()
                existing = session.scalar(
                    select(CrawlJob).where(
                        CrawlJob.dedupe_key == dedupe_key, CrawlJob.dedupe_active == "1"
                    )
                )
                if existing is not None:
                    return existing.id, False
                raise


def enqueue_after_delay(pool: WorkerPool, seconds: float, **kwargs: Any) -> None:
    """Small helper used by endpoints that want a delayed enqueue (tests/ops)."""

    async def _later() -> None:
        await asyncio.sleep(seconds)
        pool.enqueue(**kwargs)

    asyncio.create_task(_later())


def setup_scheduler_defaults(pool: WorkerPool) -> None:
    """No-op placeholder kept for symmetry / future cron-like entries."""
    pool.settings.scheduler_min_age_hours = max(pool.settings.scheduler_min_age_hours, 1.0)


def next_refresh_at(hours: float) -> Any:
    return utcnow() + timedelta(hours=hours)
