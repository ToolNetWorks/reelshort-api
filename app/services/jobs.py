"""Job queue backed by SQLite (dedupe, retries, checkpoints, priorities)."""

from __future__ import annotations

import logging
import random
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import CrawlJob, utcnow

log = logging.getLogger(__name__)

TERMINAL_STATUSES = ("succeeded", "failed", "canceled")


class JobManager:
    """Create / claim / complete jobs with concurrency-safe deduplication."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    # --- creation ----------------------------------------------------------
    def create(
        self,
        session: Session,
        *,
        kind: str,
        dedupe_key: str,
        payload: dict[str, Any] | None = None,
        scope: str = "admin",
        priority: int = 5,
        max_attempts: int | None = None,
        run_after: Any = None,
        requested_by: str | None = None,
    ) -> tuple[CrawlJob, bool]:
        """Create a job or return the existing active job for ``dedupe_key``."""
        dedupe_key = dedupe_key[:255]
        existing = session.scalar(
            select(CrawlJob).where(
                CrawlJob.dedupe_key == dedupe_key,
                CrawlJob.dedupe_active == "1",
            )
        )
        if existing is not None:
            return existing, False

        job = CrawlJob(
            kind=kind,
            scope=scope,
            dedupe_key=dedupe_key,
            dedupe_active="1",
            status="queued",
            priority=max(1, min(int(priority), 10)),
            payload=payload or {},
            max_attempts=max_attempts or self.settings.job_max_attempts,
            requested_by=requested_by,
            run_after=run_after or utcnow(),
            next_run_at=run_after or utcnow(),
        )
        session.add(job)
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            existing = session.scalar(
                select(CrawlJob).where(
                    CrawlJob.dedupe_key == dedupe_key, CrawlJob.dedupe_active == "1"
                )
            )
            if existing is not None:
                return existing, False
            raise
        return job, True

    # --- claiming ----------------------------------------------------------
    def claim_next(self, session: Session, *, worker_id: str) -> CrawlJob | None:
        now = utcnow()
        job = session.scalar(
            select(CrawlJob)
            .where(CrawlJob.status == "queued", CrawlJob.next_run_at <= now)
            .order_by(CrawlJob.priority.asc(), CrawlJob.next_run_at.asc(), CrawlJob.created_at.asc())
            .limit(1)
        )
        if job is None:
            return None
        merged_cp = {**(job.checkpoint or {}), "worker": worker_id, "phase": "starting"}
        updated = session.execute(
            update(CrawlJob)
            .where(CrawlJob.id == job.id, CrawlJob.status == "queued")
            .values(
                status="running",
                started_at=utcnow(),
                attempts=CrawlJob.attempts + 1,
                checkpoint=merged_cp,
            )
        )
        if updated.rowcount != 1:  # another worker won the race
            session.rollback()
            return None
        session.commit()
        session.refresh(job)
        return job

    # --- state transitions -------------------------------------------------
    def succeed(self, session: Session, job: CrawlJob, result: dict[str, Any]) -> None:
        job.status = "succeeded"
        job.result = result or {}
        job.error = None
        job.progress = 100.0
        job.finished_at = utcnow()
        job.dedupe_active = None
        job.checkpoint = {**(job.checkpoint or {}), "phase": "done"}
        session.flush()

    def fail(
        self, session: Session, job: CrawlJob, error: str, *, retry_after_seconds: float | None = None
    ) -> str:
        """Record a failure; returns the resulting status."""
        job.error = (error or "unknown error")[:4000]
        if job.attempts < job.max_attempts:
            delay = retry_after_seconds
            if delay is None:
                base = self.settings.source_backoff_base_seconds * (2 ** max(job.attempts - 1, 0))
                delay = min(base, self.settings.source_backoff_max_seconds)
                delay *= 0.75 + random.random() * 0.5
            job.status = "queued"
            job.next_run_at = utcnow() + timedelta(seconds=max(delay, 1.0))
            job.checkpoint = {**(job.checkpoint or {}), "phase": "retry", "retry_in_seconds": round(delay, 1)}
            session.flush()
            return "queued"
        job.status = "failed"
        job.finished_at = utcnow()
        job.dedupe_active = None
        session.flush()
        return "failed"

    def defer(self, session: Session, job: CrawlJob, seconds: float, reason: str) -> None:
        job.status = "queued"
        job.next_run_at = utcnow() + timedelta(seconds=max(seconds, 1.0))
        job.checkpoint = {**(job.checkpoint or {}), "phase": "deferred", "reason": reason[:500]}
        session.flush()

    def cancel(self, session: Session, job_id: str) -> bool:
        job = session.get(CrawlJob, job_id)
        if job is None or job.status in TERMINAL_STATUSES:
            return False
        job.status = "canceled"
        job.finished_at = utcnow()
        job.dedupe_active = None
        session.flush()
        return True

    def recover_orphans(self, session: Session) -> int:
        """Jobs left 'running' by a crash/restart are requeued (checkpoint kept)."""
        stale = list(session.scalars(select(CrawlJob).where(CrawlJob.status == "running")))
        now = utcnow()
        for job in stale:
            job.status = "queued"
            job.next_run_at = now
            job.checkpoint = {**(job.checkpoint or {}), "phase": "resumed_after_restart"}
        session.flush()
        return len(stale)

    # --- queries -----------------------------------------------------------
    def get(self, session: Session, job_id: str) -> CrawlJob | None:
        return session.get(CrawlJob, job_id)

    def counts(self, session: Session) -> dict[str, int]:
        rows = session.execute(
            select(CrawlJob.status, func.count()).group_by(CrawlJob.status)
        ).all()
        return {str(status): int(count) for status, count in rows}

    def list_jobs(
        self,
        session: Session,
        *,
        status: str | None = None,
        kind: str | None = None,
        scope: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[CrawlJob], int]:
        stmt = select(CrawlJob)
        count_stmt = select(func.count()).select_from(CrawlJob)
        if status:
            stmt = stmt.where(CrawlJob.status == status)
            count_stmt = count_stmt.where(CrawlJob.status == status)
        if kind:
            stmt = stmt.where(CrawlJob.kind == kind)
            count_stmt = count_stmt.where(CrawlJob.kind == kind)
        if scope:
            stmt = stmt.where(CrawlJob.scope == scope)
            count_stmt = count_stmt.where(CrawlJob.scope == scope)
        total = int(session.scalar(count_stmt) or 0)
        rows = list(
            session.scalars(
                stmt.order_by(CrawlJob.created_at.desc(), CrawlJob.priority.asc())
                .limit(max(1, min(limit, 200)))
                .offset(max(0, offset))
            )
        )
        return rows, total
