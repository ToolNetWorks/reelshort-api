"""Shared application state wired up during the FastAPI lifespan."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime

from app.config import Settings
from app.crawler.service import CrawlService
from app.crawler.source import ReelShortSource
from app.db.models import utcnow
from app.services.jobs import JobManager
from app.services.worker import WorkerPool


@dataclass
class AppState:
    settings: Settings
    source: ReelShortSource
    crawl: CrawlService
    jobs: JobManager
    pool: WorkerPool
    started_at: datetime = field(default_factory=utcnow)
    _sync_locks: dict[str, asyncio.Lock] = field(default_factory=dict)

    def get_sync_lock(self, book_id: str) -> asyncio.Lock:
        lock = self._sync_locks.get(book_id)
        if lock is None:
            lock = asyncio.Lock()
            self._sync_locks[book_id] = lock
        return lock

    @property
    def uptime_seconds(self) -> float:
        return round((utcnow() - self.started_at).total_seconds(), 3)
