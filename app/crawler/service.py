"""Crawl orchestration: cache-first reads, incremental episode sync,
checkpoint/resume, history recording and polite failure handling."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.config import Settings, get_locale_catalog_page_url, get_locale_source_url, get_settings
from app.crawler import store
from app.crawler.discovery import EditionDiscovery
from app.crawler.parser import (
    ParseError,
    ParsedCatalogPage,
    ParsedMovie,
    extract_book_id_from_url,
    parse_all_movies_page,
    parse_chapter_list,
    parse_homepage_catalog,
    parse_movie_page,
    parse_movie_payload,
)
from app.crawler.source import (
    ReelShortSource,
    SourceBlocked,
    SourceError,
    SourceNotFound,
    SourceUnavailable,
)
from app.db.models import CatalogPageState, CatalogState, CrawlJob, Episode, MovieEdition, utcnow

log = logging.getLogger(__name__)

MAX_CHAPTER_PAGES = 20


@dataclass
class CrawlResult:
    book_id: str | None
    lang: str | None = None
    status: str = "ok"
    movies_written: int = 0
    episodes_inserted: int = 0
    episodes_updated: int = 0
    requests: int = 0
    source_kind: str = "api"
    deferred: bool = False
    detail: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "book_id": self.book_id,
            "lang": self.lang,
            "status": self.status,
            "movies_written": self.movies_written,
            "episodes_inserted": self.episodes_inserted,
            "episodes_updated": self.episodes_updated,
            "requests": self.requests,
            "source_kind": self.source_kind,
            "deferred": self.deferred,
            "detail": self.detail,
            **self.extra,
        }


class CrawlService:
    def __init__(self, source: ReelShortSource, settings: Settings | None = None) -> None:
        self.source = source
        self.settings = settings or get_settings()
        self.discovery = EditionDiscovery(source, self.settings)

    # --- helpers -----------------------------------------------------------
    @staticmethod
    def _checkpoint(
        session: Session, job: Any, checkpoint: dict[str, Any], *, progress: float | None = None
    ) -> None:
        """Record progress on the job row and commit it (resume-safe)."""
        store.save_checkpoint(session, job, checkpoint, progress=progress)

    @staticmethod
    def resolve_book_id(book_id: str | None, url: str | None) -> str | None:
        if book_id:
            return book_id.strip()
        if url:
            return extract_book_id_from_url(url)
        return None

    def is_fresh(self, edition: MovieEdition, *, ttl_hours: float | None = None) -> bool:
        if edition.fetched_at is None:
            return False
        ttl = self.settings.metadata_ttl_hours if ttl_hours is None else ttl_hours
        return edition.fetched_at >= utcnow() - timedelta(hours=ttl)

    @staticmethod
    def _episodes_complete(session: Session, edition: MovieEdition) -> bool:
        expected = edition.chapter_count or 0
        if expected <= 0:
            return False
        stored = int(
            session.scalar(select(func.count()).select_from(Episode).where(Episode.book_id == edition.book_id))
            or 0
        )
        return stored >= expected

    # --- main entry point --------------------------------------------------
    async def crawl_movie(
        self,
        session: Session,
        *,
        book_id: str | None = None,
        url: str | None = None,
        include_episodes: bool = True,
        force: bool = False,
        job: CrawlJob | None = None,
        lang_hint: str | None = None,
    ) -> CrawlResult:
        resolved = self.resolve_book_id(book_id, url)
        if not resolved:
            raise SourceError("a book_id or a ReelShort movie URL is required")

        # An alias id (ReelShort search returns several ids per edition) maps to
        # the canonical edition that was actually crawled.
        resolved = store.resolve_alias(session, resolved)
        edition = session.get(MovieEdition, resolved)
        if edition is not None and not force and self.is_fresh(edition):
            if not include_episodes or self._episodes_complete(session, edition):
                # cache hit: neither metadata nor episodes need an upstream call
                return CrawlResult(book_id=resolved, lang=edition.lang, status="cached", movies_written=0)
            # fresh metadata but incomplete episodes → incremental episode sync only
            result = CrawlResult(book_id=resolved, lang=edition.lang, source_kind="cache")
            episodes_result = await self.sync_episodes(session, edition=edition, force=False, job=job)
            result.episodes_inserted += episodes_result.episodes_inserted
            result.episodes_updated += episodes_result.episodes_updated
            result.requests += episodes_result.requests
            result.detail = f"incremental episode sync for {edition.book_id}"
            store.record_history(
                session,
                outcome="episodes_incremental",
                job_id=job.id if job else None,
                book_id=edition.book_id,
                lang=edition.lang,
                source_kind="api",
                detail=f"inserted={result.episodes_inserted} updated={result.episodes_updated}",
                items_written=result.episodes_inserted,
            )
            return result

        result = CrawlResult(book_id=resolved)
        source_url = url or f"{self.source.base_url}/movie/{resolved}"
        started = time.monotonic()
        parsed: ParsedMovie | None = None
        source_kind = "api"
        try:
            if job is not None:
                self._checkpoint(session, job, {"phase": "metadata", "book_id": resolved, "method": "api"})
            payload = await self.source.movie_info(resolved, lang=lang_hint)
            parsed = parse_movie_payload(payload, source_url=source_url)
            result.requests += 1
        except (SourceNotFound, ParseError, SourceError) as exc:
            # fall back to the public HTML page (still official metadata)
            log.info("api metadata unavailable, falling back to page", extra={"book_id": resolved, "error": str(exc)})
            source_kind = "page"
            try:
                if job is not None:
                    self._checkpoint(session, job, {"phase": "metadata", "book_id": resolved, "method": "page"})
                page = await self.source.movie_page(resolved)
                parsed = parse_movie_page(page.text, source_url=page.url)
                result.requests += 1
                source_url = page.url
            except Exception as page_exc:
                store.record_history(
                    session,
                    outcome="metadata_failed",
                    job_id=job.id if job else None,
                    book_id=resolved,
                    lang=lang_hint,
                    url=source_url,
                    source_kind="page",
                    detail=f"{type(page_exc).__name__}: {page_exc}",
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
                raise

        assert parsed is not None
        edition = store.upsert_edition(session, parsed, source_kind=source_kind)
        if parsed.book_id != resolved:
            # the official API resolves alias ids to the canonical edition;
            # recorded after the edition exists (FK to movie_editions.book_id)
            store.upsert_alias(session, resolved, parsed.book_id, lang=parsed.lang, source=source_kind)
            log.info(
                "alias book_id resolved",
                extra={"alias_book_id": resolved, "book_id": parsed.book_id, "lang": parsed.lang},
            )
            result.extra["alias_of"] = parsed.book_id
        result.lang = parsed.lang
        result.source_kind = source_kind
        result.movies_written = 1
        store.record_history(
            session,
            outcome="metadata_ok",
            job_id=job.id if job else None,
            book_id=parsed.book_id,
            lang=parsed.lang,
            url=parsed.source_url,
            source_kind=source_kind,
            http_status=200,
            detail=f"chapter_count={parsed.chapter_count}",
            duration_ms=int((time.monotonic() - started) * 1000),
            items_written=1,
        )
        session.flush()

        # ensure the edition belongs to a group (single-edition group at minimum)
        if edition.movie_group_id is None:
            store.ensure_group(session, edition)
            session.flush()

        if include_episodes:
            try:
                episodes_result = await self.sync_episodes(
                    session, edition=edition, parsed=parsed, force=force, job=job
                )
                result.episodes_inserted += episodes_result.episodes_inserted
                result.episodes_updated += episodes_result.episodes_updated
                result.requests += episodes_result.requests
            except SourceUnavailable as exc:
                result.deferred = True
                result.detail = f"episode sync deferred: {exc}"
                if job is not None:
                    job.checkpoint = {
                        "phase": "episodes",
                        "book_id": parsed.book_id,
                        "resume_cursor": edition.episodes_cursor,
                        "reason": str(exc),
                    }
                log.warning("episode sync deferred", extra={"book_id": parsed.book_id, "error": str(exc)})
                raise
        return result

    # --- episode sync ------------------------------------------------------
    async def sync_episodes(
        self,
        session: Session,
        *,
        edition: MovieEdition,
        parsed: ParsedMovie | None = None,
        force: bool = False,
        job: CrawlJob | None = None,
    ) -> CrawlResult:
        result = CrawlResult(book_id=edition.book_id, lang=edition.lang)
        stored = int(
            session.scalar(
                select(func.count()).select_from(Episode).where(Episode.book_id == edition.book_id)
            )
            or 0
        )
        expected = edition.chapter_count or (parsed.chapter_count if parsed else None)
        cursor = int(edition.episodes_cursor or 0)

        if not force and expected is not None and stored >= expected:
            return result  # already complete — no upstream call at all

        # Fast path: the metadata payload already carried the full episode list.
        if parsed is not None and parsed.episodes:
            inline_complete = expected is not None and len(parsed.episodes) >= expected
            if inline_complete or (not force and stored == 0 and expected is None):
                inserted, updated = store.upsert_episodes(
                    session, edition.book_id, edition.lang, parsed.episodes
                )
                result.episodes_inserted += inserted
                result.episodes_updated += updated
                store.record_history(
                    session,
                    outcome="episodes_inline",
                    job_id=job.id if job else None,
                    book_id=edition.book_id,
                    lang=edition.lang,
                    source_kind="api",
                    items_written=inserted,
                )
                session.flush()
                return result

        page_size = 100
        start_serial = cursor
        pages = 0
        while True:
            pages += 1
            if pages > MAX_CHAPTER_PAGES:
                log.warning("chapter pagination capped", extra={"book_id": edition.book_id})
                break
            page = max(1, (start_serial // page_size) + 1)
            if job is not None:
                self._checkpoint(
                    session,
                    job,
                    {
                        "phase": "episodes",
                        "book_id": edition.book_id,
                        "page": page,
                        "page_size": page_size,
                        "cursor": start_serial,
                        "stored": stored,
                        "expected": expected,
                    },
                    progress=round(min(99.0, (stored / expected) * 100), 1) if expected else None,
                )
            payload = await self.source.chapter_list(edition.book_id, page=page, page_size=page_size, lang=edition.lang)
            result.requests += 1
            episodes, info = parse_chapter_list(payload)
            if not episodes:
                break
            if info.get("chapter_count") and (edition.chapter_count in (None, 0)):
                edition.chapter_count = int(info["chapter_count"])
            if info.get("book_title") and not edition.book_title:
                edition.book_title = str(info["book_title"])
            inserted, updated = store.upsert_episodes(session, edition.book_id, edition.lang, episodes)
            result.episodes_inserted += inserted
            result.episodes_updated += updated
            store.record_history(
                session,
                outcome="episodes_page",
                job_id=job.id if job else None,
                book_id=edition.book_id,
                lang=edition.lang,
                source_kind="api",
                detail=f"page={page} size={page_size}",
                items_written=inserted,
            )
            session.flush()
            max_serial = max(e.serial_number for e in episodes)
            if max_serial <= start_serial and not force:
                break
            start_serial = max_serial
            expected = edition.chapter_count or expected
            if expected is not None and start_serial >= expected:
                break
            if len(episodes) < page_size:
                break
        return result

    # --- discovery entry point --------------------------------------------
    async def discover(
        self,
        session: Session,
        *,
        book_id: str,
        targets: list[str] | None = None,
        title_hints: list[str] | None = None,
        strategies: list[str] | None = None,
        job: CrawlJob | None = None,
    ) -> dict[str, Any]:
        outcome = await self.discovery.discover(
            session,
            source_book_id=book_id,
            targets=targets,
            title_hints=title_hints or [],
            strategies=strategies or ("search",),
            job=job,
        )
        store.record_history(
            session,
            outcome="discovery_done",
            job_id=job.id if job else None,
            book_id=book_id,
            detail=f"created={outcome.created_matches} updated={outcome.updated_matches}",
            items_written=outcome.created_matches,
        )
        return outcome.as_dict()

    # --- locale catalog crawling ------------------------------------------
    @staticmethod
    def _catalog_state_summary(state: CatalogState) -> dict[str, Any]:
        return {
            "source_total": state.source_total or 0,
            "discovered_total": state.discovered_total or 0,
            "unique_total": state.unique_total or 0,
            "indexed_total": state.indexed_total or 0,
            "enriched_total": state.enriched_total or 0,
            "incomplete_total": state.incomplete_total or 0,
            "total_pages": state.total_pages or 0,
            "pages_completed": state.pages_completed or 0,
            "pages_failed": state.pages_failed or 0,
            "pages_remaining": max(0, (state.total_pages or 0) - (state.pages_completed or 0)),
            "new_editions": state.new_movies or 0,
            "updated_editions": state.updated_movies or 0,
            "skipped_editions": state.skipped_movies or 0,
            "http_requests": state.http_requests or 0,
            "crawl_duration_ms": state.crawl_duration_ms or 0,
            "crawl_rate_pages_per_min": state.crawl_rate_pages_per_min or 0.0,
            "last_completed_page": state.last_completed_page or 0,
            "total_found": state.discovered_total or state.indexed_total or 0,
        }

    @staticmethod
    def _store_catalog_page(
        session: Session,
        clean_lang: str,
        page_num: int,
        parsed_page: ParsedCatalogPage,
        http_status: int,
        elapsed_ms: int,
        seen_in_run: set[str],
        incomplete_ids: list[str],
    ) -> tuple[int, int, int, int]:
        book_ids = [pm.book_id for pm in parsed_page.movies]
        if book_ids:
            # Warm identity map in a single query
            list(session.scalars(select(MovieEdition).where(MovieEdition.book_id.in_(book_ids))))

        p_new = 0
        p_updated = 0
        p_skipped = 0
        p_incomplete = 0

        for pm in parsed_page.movies:
            if pm.book_id in seen_in_run:
                p_skipped += 1
                continue
            seen_in_run.add(pm.book_id)

            existing = session.get(MovieEdition, pm.book_id)
            if existing is None:
                p_new += 1
            else:
                p_updated += 1

            edition = store.upsert_edition(session, pm, source_kind="catalog_all_movies")
            if edition.movie_group_id is None:
                store.ensure_group(session, edition)
            if edition.metadata_status == "METADATA_INCOMPLETE":
                p_incomplete += 1
                if edition.book_id not in incomplete_ids:
                    incomplete_ids.append(edition.book_id)

        store.upsert_catalog_page_state(
            session,
            lang=clean_lang,
            page=page_num,
            status="succeeded",
            url=parsed_page.source_url,
            http_status=http_status,
            duration_ms=elapsed_ms,
            items_discovered=parsed_page.discovered_count,
            items_new=p_new,
            items_updated=p_updated,
            items_skipped=p_skipped,
            items_incomplete=p_incomplete,
        )
        return p_new, p_updated, p_skipped, p_incomplete

    async def crawl_locale_catalog(
        self,
        session: Session,
        lang: str,
        *,
        job: CrawlJob | None = None,
        force: bool = False,
        max_pages: int | None = None,
        incremental: bool = False,
        reset_pages: bool = False,
    ) -> CrawlResult:
        """Crawl full paginated catalog when supported by source, else fallback to homepage."""
        if hasattr(self.source, "catalog_page"):
            return await self.crawl_full_catalog(
                session,
                lang,
                job=job,
                force=force,
                max_pages=max_pages,
                incremental=incremental,
                reset_pages=reset_pages,
            )
        return await self.crawl_locale_catalog_homepage(session, lang, job=job, force=force)

    async def crawl_full_catalog(
        self,
        session: Session,
        lang: str,
        *,
        job: CrawlJob | None = None,
        force: bool = False,
        max_pages: int | None = None,
        incremental: bool = False,
        reset_pages: bool = False,
    ) -> CrawlResult:
        """Crawl the full paginated SSR catalog (`/movie-genres/all-movies/{page}`) for `lang`."""
        clean_lang = lang.strip().lower()
        first_url = get_locale_catalog_page_url(clean_lang, page=1, base_url=self.source.base_url)
        state = store.get_or_create_catalog_state(session, clean_lang, source_url=first_url)

        # Guard clause: skip full scan if catalog is already complete and within TTL
        if (
            not force
            and state.last_synced_at is not None
            and (state.total_pages or 0) > 0
            and (state.pages_completed or 0) >= (state.total_pages or 0)
            and state.last_synced_at >= utcnow() - timedelta(hours=self.settings.catalog_ttl_hours)
        ):
            state = store.refresh_catalog_state_counts(session, clean_lang)
            session.commit()
            return CrawlResult(
                book_id=None,
                lang=clean_lang,
                status="cached",
                movies_written=0,
                requests=0,
                source_kind="catalog_all_movies",
                detail="catalog fresh within TTL",
                extra=self._catalog_state_summary(state),
            )

        started = time.monotonic()
        now = utcnow()
        state.status = "running"
        state.started_at = state.started_at or now
        state.last_error = None
        session.commit()

        result = CrawlResult(book_id=None, lang=clean_lang, source_kind="catalog_all_movies")

        if job is not None:
            self._checkpoint(
                session,
                job,
                {"phase": "fetch_page_1", "locale": clean_lang, "page": 1, "url": first_url},
                progress=max(1.0, float(job.progress or 0.0)),
            )

        # 1. Fetch page 1 and discover dynamic total & totalPage
        t0 = time.monotonic()
        try:
            resp1 = await self.source.catalog_page(clean_lang, page=1)
            result.requests += 1
        except Exception as exc:
            state.status = "paused" if isinstance(exc, SourceUnavailable) else "failed"
            state.last_error = f"{type(exc).__name__}: {exc}"
            store.upsert_catalog_page_state(
                session,
                lang=clean_lang,
                page=1,
                status="failed",
                url=first_url,
                error=state.last_error,
            )
            store.record_history(
                session,
                outcome="catalog_page_failed",
                job_id=job.id if job else None,
                lang=clean_lang,
                url=first_url,
                source_kind="catalog_all_movies",
                detail=f"page=1 {state.last_error}",
                duration_ms=int((time.monotonic() - t0) * 1000),
            )
            store.refresh_catalog_state_counts(session, clean_lang)
            session.commit()
            raise

        p1_ms = int((time.monotonic() - t0) * 1000)
        try:
            parsed_p1 = parse_all_movies_page(resp1.text, clean_lang, source_url=resp1.url)
        except ParseError:
            # Fallback if page is a homepage fixture
            return await self.crawl_locale_catalog_homepage(session, clean_lang, job=job, force=force)

        total_pages = max(1, parsed_p1.total_pages)
        target_pages = min(total_pages, max_pages) if (max_pages and max_pages > 0) else total_pages

        state.source_total = parsed_p1.total
        state.total_pages = total_pages
        state.page_size = parsed_p1.page_size
        state.source_url = first_url

        if reset_pages:
            session.execute(
                update(CatalogPageState)
                .where(
                    CatalogPageState.provider == "reelshort",
                    CatalogPageState.locale == clean_lang,
                )
                .values(status="pending", error=None)
            )
            session.flush()

        # Ensure CatalogPageState rows exist for 1..target_pages
        existing_page_rows = {
            row.page_number: row
            for row in session.scalars(
                select(CatalogPageState).where(
                    CatalogPageState.provider == "reelshort",
                    CatalogPageState.locale == clean_lang,
                )
            )
        }
        for p in range(1, target_pages + 1):
            if p not in existing_page_rows:
                p_url = get_locale_catalog_page_url(clean_lang, page=p, base_url=self.source.base_url)
                row = CatalogPageState(
                    provider="reelshort",
                    locale=clean_lang,
                    page_number=p,
                    status="pending",
                    url=p_url,
                )
                session.add(row)
                existing_page_rows[p] = row
        session.flush()

        seen_in_run: set[str] = set()
        incomplete_ids: list[str] = []
        run_new = 0
        run_updated = 0
        run_skipped = 0

        # Store page 1
        p1_new, p1_upd, p1_skip, _p1_inc = self._store_catalog_page(
            session, clean_lang, 1, parsed_p1, resp1.status, p1_ms, seen_in_run, incomplete_ids
        )
        run_new += p1_new
        run_updated += p1_upd
        run_skipped += p1_skip
        state.http_requests = (state.http_requests or 0) + 1
        state.last_completed_page = max(state.last_completed_page or 0, 1)
        state = store.refresh_catalog_state_counts(session, clean_lang)

        pct = round((min(state.pages_completed, target_pages) / max(target_pages, 1)) * 100.0, 1)
        self._checkpoint(
            session,
            job,
            {
                "phase": "crawling_pages",
                "locale": clean_lang,
                "page": 1,
                "total": target_pages,
                "pages_completed": state.pages_completed,
                "indexed_total": state.indexed_total,
                "source_total": state.source_total,
            },
            progress=pct,
        )
        session.commit()

        # Determine remaining pages 2..target_pages
        pending_pages = [
            p
            for p in range(2, target_pages + 1)
            if existing_page_rows[p].status != "succeeded"
        ]
        incremental_catchup = False
        if not pending_pages and (incremental or force) and p1_new > 0 and target_pages > 1:
            # New movies were discovered on page 1; walk subsequent pages until caught up
            pending_pages = list(range(2, target_pages + 1))
            incremental_catchup = True

        batch_size = max(1, int(self.settings.source_concurrency))

        async def _fetch_page(page_no: int) -> tuple[int, Any, ParsedCatalogPage, int]:
            t_start = time.monotonic()
            resp = await self.source.catalog_page(clean_lang, page=page_no)
            dt_ms = int((time.monotonic() - t_start) * 1000)
            parsed = parse_all_movies_page(resp.text, clean_lang, source_url=resp.url)
            return page_no, resp, parsed, dt_ms

        idx = 0
        while idx < len(pending_pages):
            batch = pending_pages[idx : idx + batch_size]
            idx += len(batch)

            for p in batch:
                p_url = get_locale_catalog_page_url(clean_lang, page=p, base_url=self.source.base_url)
                store.upsert_catalog_page_state(
                    session, lang=clean_lang, page=p, status="running", url=p_url
                )
            session.commit()

            outcomes = await asyncio.gather(*(_fetch_page(p) for p in batch), return_exceptions=True)
            stop_incremental = False

            for p, outcome in zip(batch, outcomes):
                p_url = get_locale_catalog_page_url(clean_lang, page=p, base_url=self.source.base_url)
                if isinstance(outcome, Exception):
                    err_text = f"{type(outcome).__name__}: {outcome}"
                    store.upsert_catalog_page_state(
                        session,
                        lang=clean_lang,
                        page=p,
                        status="failed",
                        url=p_url,
                        error=err_text,
                    )
                    store.record_history(
                        session,
                        outcome="catalog_page_failed",
                        job_id=job.id if job else None,
                        lang=clean_lang,
                        url=p_url,
                        source_kind="catalog_all_movies",
                        detail=f"page={p} {err_text}",
                    )
                    state.last_error = f"page {p}: {err_text}"
                    if isinstance(outcome, (SourceUnavailable, SourceBlocked)):
                        state.status = "paused"
                        elapsed_ms = int((time.monotonic() - started) * 1000)
                        state.crawl_duration_ms = (state.crawl_duration_ms or 0) + elapsed_ms
                        state = store.refresh_catalog_state_counts(session, clean_lang)
                        self._checkpoint(
                            session,
                            job,
                            {
                                "phase": "paused",
                                "locale": clean_lang,
                                "page": p,
                                "total": target_pages,
                                "pages_completed": state.pages_completed,
                                "reason": err_text,
                            },
                            progress=round((min(state.pages_completed, target_pages) / max(target_pages, 1)) * 100.0, 1),
                        )
                        session.commit()
                        raise outcome
                    continue

                p_no, resp, parsed_p, dt_ms = outcome
                result.requests += 1
                state.http_requests = (state.http_requests or 0) + 1

                # Dynamic totalPage expansion if ReelShort added new pages mid-crawl
                if parsed_p.total_pages > total_pages:
                    old_total_pages = total_pages
                    total_pages = parsed_p.total_pages
                    state.total_pages = total_pages
                    state.source_total = max(state.source_total or 0, parsed_p.total)
                    new_target = min(total_pages, max_pages) if (max_pages and max_pages > 0) else total_pages
                    if new_target > target_pages:
                        for new_p in range(target_pages + 1, new_target + 1):
                            if new_p not in existing_page_rows:
                                new_url = get_locale_catalog_page_url(clean_lang, page=new_p, base_url=self.source.base_url)
                                new_row = CatalogPageState(
                                    provider="reelshort",
                                    locale=clean_lang,
                                    page_number=new_p,
                                    status="pending",
                                    url=new_url,
                                )
                                session.add(new_row)
                                existing_page_rows[new_p] = new_row
                            if new_p not in pending_pages:
                                pending_pages.append(new_p)
                        target_pages = new_target
                        log.info(
                            "catalog total_pages expanded dynamically",
                            extra={"lang": clean_lang, "old": old_total_pages, "new": total_pages},
                        )

                p_new, p_upd, p_skip, _p_inc = self._store_catalog_page(
                    session, clean_lang, p_no, parsed_p, resp.status, dt_ms, seen_in_run, incomplete_ids
                )
                run_new += p_new
                run_updated += p_upd
                run_skipped += p_skip
                state.last_completed_page = p_no

                if incremental_catchup and p_new == 0:
                    stop_incremental = True

            elapsed_ms = int((time.monotonic() - started) * 1000)
            state.crawl_rate_pages_per_min = round(
                (result.requests / max(elapsed_ms / 60000.0, 0.001)), 2
            )
            state = store.refresh_catalog_state_counts(session, clean_lang)
            pct = round((min(state.pages_completed, target_pages) / max(target_pages, 1)) * 100.0, 1)
            self._checkpoint(
                session,
                job,
                {
                    "phase": "crawling_pages",
                    "locale": clean_lang,
                    "page": state.last_completed_page,
                    "total": target_pages,
                    "pages_completed": state.pages_completed,
                    "indexed_total": state.indexed_total,
                    "source_total": state.source_total,
                },
                progress=pct,
            )
            session.commit()

            if stop_incremental:
                break

        # Queue bounded low-priority enrichment jobs for METADATA_INCOMPLETE movies
        if incomplete_ids:
            from app.services.jobs import JobManager

            jm = JobManager(self.settings)
            limit = max(1, int(self.settings.enrichment_batch_size) * 4)
            for bid in incomplete_ids[:limit]:
                jm.create(
                    session,
                    kind="enrich_movie",
                    dedupe_key=f"enrich_movie:{bid}",
                    payload={"book_id": bid, "lang": clean_lang},
                    scope="scheduler",
                    priority=9,
                )

        total_elapsed_ms = int((time.monotonic() - started) * 1000)
        state.crawl_duration_ms = (state.crawl_duration_ms or 0) + total_elapsed_ms
        state.crawl_rate_pages_per_min = round(
            (result.requests / max(total_elapsed_ms / 60000.0, 0.001)), 2
        )
        state = store.refresh_catalog_state_counts(session, clean_lang)
        if state.pages_failed == 0 and state.pages_completed >= target_pages:
            state.status = "completed"
            state.last_synced_at = utcnow()
        else:
            state.status = "partial"

        store.record_history(
            session,
            outcome="catalog_full_ok" if state.status == "completed" else "catalog_full_partial",
            job_id=job.id if job else None,
            lang=clean_lang,
            url=first_url,
            source_kind="catalog_all_movies",
            http_status=200,
            detail=(
                f"pages={state.pages_completed}/{state.total_pages} "
                f"indexed={state.indexed_total} new={run_new} updated={run_updated}"
            ),
            duration_ms=total_elapsed_ms,
            items_written=run_new,
        )
        session.commit()

        result.movies_written = run_new
        result.extra = {
            **self._catalog_state_summary(state),
            "run_new_editions": run_new,
            "run_updated_editions": run_updated,
            "run_skipped_editions": run_skipped,
        }
        return result

    async def crawl_locale_catalog_homepage(
        self,
        session: Session,
        lang: str,
        *,
        job: CrawlJob | None = None,
        force: bool = False,
    ) -> CrawlResult:
        clean_lang = lang.strip().lower()
        url = get_locale_source_url(clean_lang, self.source.base_url)
        started = time.monotonic()
        result = CrawlResult(book_id=None, lang=clean_lang, source_kind="catalog_homepage")

        if job is not None:
            self._checkpoint(
                session,
                job,
                {"phase": "fetch_homepage", "lang": clean_lang, "url": url},
                progress=10.0,
            )

        try:
            page = await self.source.homepage(clean_lang)
            result.requests += 1
        except Exception as exc:
            store.record_history(
                session,
                outcome="catalog_homepage_failed",
                job_id=job.id if job else None,
                lang=clean_lang,
                url=url,
                source_kind="catalog_homepage",
                detail=f"{type(exc).__name__}: {exc}",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            raise

        if job is not None:
            self._checkpoint(
                session,
                job,
                {"phase": "parse_catalog", "lang": clean_lang, "url": url},
                progress=40.0,
            )

        parsed_movies = parse_homepage_catalog(page.text, clean_lang)
        total_found = len(parsed_movies)
        new_count = 0
        updated_count = 0

        for idx, pm in enumerate(parsed_movies):
            existing = session.get(MovieEdition, pm.book_id)
            if existing is None:
                new_count += 1
            else:
                updated_count += 1

            edition = store.upsert_edition(session, pm, source_kind="catalog_homepage")
            if edition.movie_group_id is None:
                store.ensure_group(session, edition)

            if (idx + 1) % 25 == 0 or idx == total_found - 1:
                session.flush()
                if job is not None:
                    pct = 40.0 + round(((idx + 1) / max(total_found, 1)) * 55.0, 1)
                    self._checkpoint(
                        session,
                        job,
                        {
                            "phase": "storing_movies",
                            "lang": clean_lang,
                            "processed": idx + 1,
                            "total": total_found,
                            "new": new_count,
                            "updated": updated_count,
                        },
                        progress=pct,
                    )

        store.record_history(
            session,
            outcome="catalog_homepage_ok",
            job_id=job.id if job else None,
            lang=clean_lang,
            url=url,
            source_kind="catalog_homepage",
            http_status=200,
            detail=f"found={total_found} new={new_count} updated={updated_count}",
            duration_ms=int((time.monotonic() - started) * 1000),
            items_written=new_count,
        )

        result.movies_written = new_count
        result.extra = {
            "total_found": total_found,
            "new_editions": new_count,
            "updated_editions": updated_count,
        }
        return result

    async def enrich_movie_metadata(
        self,
        session: Session,
        *,
        book_id: str,
        lang_hint: str | None = None,
        job: CrawlJob | None = None,
    ) -> CrawlResult:
        """Enrich an edition marked METADATA_INCOMPLETE using detail page/API metadata."""
        edition = session.get(MovieEdition, book_id)
        lang = (edition.lang if edition else None) or lang_hint
        res = await self.crawl_movie(
            session,
            book_id=book_id,
            include_episodes=False,
            force=True,
            job=job,
            lang_hint=lang,
        )
        if lang:
            store.refresh_catalog_state_counts(session, lang)
        return res

    # --- freshness / staleness --------------------------------------------
    def stale_book_ids(self, session: Session, limit: int) -> list[str]:
        threshold = utcnow() - timedelta(hours=self.settings.scheduler_min_age_hours)
        stmt = (
            select(MovieEdition.book_id)
            .where(MovieEdition.fetched_at < threshold)
            .order_by(MovieEdition.fetched_at.asc())
            .limit(limit)
        )
        return [row for row in session.scalars(stmt)]

    def incomplete_book_ids(self, session: Session, limit: int) -> list[str]:
        threshold = utcnow() - timedelta(hours=self.settings.metadata_ttl_hours)
        stmt = (
            select(MovieEdition.book_id)
            .where(
                MovieEdition.metadata_status == "METADATA_INCOMPLETE",
                (MovieEdition.episodes_synced_at.is_(None)) | (MovieEdition.episodes_synced_at < threshold),
            )
            .order_by(MovieEdition.fetched_at.asc())
            .limit(limit)
        )
        return [row for row in session.scalars(stmt)]

    def missing_episodes_book_ids(self, session: Session, limit: int) -> list[str]:
        threshold = utcnow() - timedelta(hours=self.settings.metadata_ttl_hours)
        stmt = (
            select(MovieEdition.book_id)
            .where(
                MovieEdition.chapter_count > 0,
                MovieEdition.stored_chapter_count < MovieEdition.chapter_count,
                (MovieEdition.episodes_synced_at.is_(None)) | (MovieEdition.episodes_synced_at < threshold),
            )
            .order_by(MovieEdition.fetched_at.asc())
            .limit(limit)
        )
        return [row for row in session.scalars(stmt)]


__all__ = [
    "CrawlService",
    "CrawlResult",
    "SourceBlocked",
    "SourceError",
    "SourceNotFound",
    "SourceUnavailable",
]
