"""Backup / maintenance helpers (SQLite online backup, pruning)."""

from __future__ import annotations

import logging
import sqlite3
from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import ApiUsage, CrawlHistory, CrawlJob, utcnow

log = logging.getLogger(__name__)


def sqlite_backup(settings: Settings | None = None, *, label: str = "manual") -> dict[str, object]:
    """Consistent online backup using the SQLite backup API (WAL-safe)."""
    settings = settings or get_settings()
    src_path = Path(settings.db_path)
    if not src_path.exists():
        return {"ok": False, "error": f"database not found: {src_path}"}
    backup_dir = Path(settings.backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"{src_path.stem}-{label}-{stamp}.sqlite3"
    source = sqlite3.connect(str(src_path))
    try:
        dest = sqlite3.connect(str(target))
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()
    _prune_backups(backup_dir, settings.backup_keep)
    return {"ok": True, "path": str(target), "bytes": target.stat().st_size, "label": label}


def _prune_backups(directory: Path, keep: int) -> list[str]:
    files = sorted(directory.glob("*.sqlite3"), key=lambda p: p.stat().st_mtime, reverse=True)
    removed: list[str] = []
    for old in files[max(keep, 1):]:
        try:
            old.unlink()
            removed.append(str(old))
        except OSError:  # pragma: no cover
            continue
    return removed


def prune_history(session: Session, *, days: int = 30) -> dict[str, int]:
    cutoff = utcnow() - timedelta(days=days)
    history = session.execute(delete(CrawlHistory).where(CrawlHistory.created_at < cutoff)).rowcount or 0
    usage = session.execute(delete(ApiUsage).where(ApiUsage.created_at < cutoff)).rowcount or 0
    jobs = session.execute(
        delete(CrawlJob).where(
            CrawlJob.status.in_(("succeeded", "failed", "canceled")),
            CrawlJob.finished_at.is_not(None),
            CrawlJob.finished_at < cutoff,
        )
    ).rowcount or 0
    session.flush()
    return {"history_removed": int(history), "usage_removed": int(usage), "jobs_removed": int(jobs)}


def database_size(session: Session, settings: Settings | None = None) -> dict[str, object]:
    settings = settings or get_settings()
    path = Path(settings.db_path)
    page_count = session.execute(func.count())  # placeholder to keep session alive semantics
    del page_count
    size = path.stat().st_size if path.exists() else 0
    wal = Path(f"{settings.db_path}-wal")
    return {
        "path": str(path),
        "bytes": size,
        "wal_bytes": wal.stat().st_size if wal.exists() else 0,
        "jobs": int(session.scalar(select(func.count()).select_from(CrawlJob)) or 0),
    }
