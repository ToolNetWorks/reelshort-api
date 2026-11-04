"""Unit tests for the SQLite-backed job queue (dedupe, retries, resume)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.db.base import get_engine, os_environ_bind, session_scope
from app.db.models import Base, CrawlJob, utcnow
from app.services.jobs import JobManager


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    config = Settings(
        db_path=str(tmp_path / "jobs.sqlite3"),
        log_file=None,
        job_max_attempts=2,
        source_backoff_base_seconds=0.01,
        source_backoff_max_seconds=0.02,
    )
    os_environ_bind(config)
    Base.metadata.create_all(get_engine())
    return config


def test_dedupe_single_active_job(settings: Settings) -> None:
    manager = JobManager(settings)
    with session_scope() as session:
        job, created = manager.create(session, kind="movie", dedupe_key="movie:abc", payload={"book_id": "abc"})
        assert created is True
        same, created_again = manager.create(
            session, kind="movie", dedupe_key="movie:abc", payload={"book_id": "abc"}
        )
        assert created_again is False
        assert same.id == job.id


def test_claim_and_retry_backoff(settings: Settings) -> None:
    manager = JobManager(settings)
    with session_scope() as session:
        job, _ = manager.create(session, kind="movie", dedupe_key="movie:x", payload={})
        job_id = job.id
    with session_scope() as session:
        claimed = manager.claim_next(session, worker_id="w1")
        assert claimed is not None and claimed.id == job_id
        assert claimed.attempts == 1
    with session_scope() as session:
        job = session.get(CrawlJob, job_id)
        status = manager.fail(session, job, "boom")
        assert status == "queued"
        assert job.next_run_at > utcnow()
        assert job.checkpoint["phase"] == "retry"
    with session_scope() as session:
        job = session.get(CrawlJob, job_id)
        job.next_run_at = utcnow()
    with session_scope() as session:
        claimed = manager.claim_next(session, worker_id="w1")
        assert claimed is not None
        assert claimed.attempts == 2
    with session_scope() as session:
        job = session.get(CrawlJob, job_id)
        status = manager.fail(session, job, "boom again")
        assert status == "failed"
        assert job.dedupe_active is None


def test_completed_job_allows_new_job_same_key(settings: Settings) -> None:
    manager = JobManager(settings)
    with session_scope() as session:
        job, _ = manager.create(session, kind="movie", dedupe_key="movie:y", payload={})
        manager.succeed(session, job, {"ok": True})
    with session_scope() as session:
        new_job, created = manager.create(session, kind="movie", dedupe_key="movie:y", payload={})
        assert created is True
        assert new_job.id != job.id


def test_recover_orphan_running_jobs(settings: Settings) -> None:
    manager = JobManager(settings)
    with session_scope() as session:
        job, _ = manager.create(session, kind="movie", dedupe_key="movie:z", payload={})
        job_id = job.id
    with session_scope() as session:
        manager.claim_next(session, worker_id="w1")
    with session_scope() as session:
        recovered = manager.recover_orphans(session)
        assert recovered == 1
    with session_scope() as session:
        job = session.get(CrawlJob, job_id)
        assert job.status == "queued"
        assert job.checkpoint["phase"] == "resumed_after_restart"


def test_priority_order(settings: Settings) -> None:
    manager = JobManager(settings)
    with session_scope() as session:
        manager.create(session, kind="movie", dedupe_key="low", payload={}, priority=9)
        manager.create(session, kind="movie", dedupe_key="high", payload={}, priority=1)
    with session_scope() as session:
        first = manager.claim_next(session, worker_id="w1")
        assert first.dedupe_key == "high"
