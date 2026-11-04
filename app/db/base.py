"""SQLite engine/session management with WAL mode and sane pragmas."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings

log = logging.getLogger(__name__)

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _apply_pragmas(dbapi_connection, connection_record) -> None:  # noqa: ANN001
    settings = get_settings()
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={int(settings.sqlite_busy_timeout_ms)}")
        cursor.execute(f"PRAGMA cache_size=-{int(settings.sqlite_cache_kib)}")
        cursor.execute("PRAGMA temp_store=MEMORY")
        cursor.execute("PRAGMA mmap_size=268435456")
    finally:
        cursor.close()


def create_db_engine(settings: Settings | None = None) -> Engine:
    settings = settings or get_settings()
    if settings.is_turso:
        return create_engine(
            settings.database_url,
            future=True,
            echo=False,
            pool_pre_ping=True,
            connect_args={"auth_token": settings.turso_auth_token or ""},
        )

    path = Path(settings.db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        settings.database_url,
        future=True,
        echo=False,
        pool_pre_ping=True,
        connect_args={"timeout": settings.sqlite_busy_timeout_ms / 1000.0, "check_same_thread": False},
    )
    event.listen(engine, "connect", _apply_pragmas)
    return engine


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_db_engine()
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False, future=True)
    return _SessionLocal


@contextmanager
def session_scope() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine(settings: Settings | None = None) -> Engine:
    """Recreate the engine (used by tests and after config changes)."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None
    if settings is not None:
        os_environ_bind(settings)
    return get_engine()


def os_environ_bind(settings: Settings) -> None:
    """Bind an explicit Settings instance to the cached engine factory (test helper)."""
    global _engine, _SessionLocal
    _engine = create_db_engine(settings)
    _SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False, future=True)


def dispose_engine() -> None:
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


def healthcheck() -> dict[str, object]:
    """Cheap liveness probe against the database."""
    settings = get_settings()
    try:
        with get_engine().connect() as conn:
            mode = conn.execute(text("PRAGMA journal_mode")).scalar()
            tables = conn.execute(
                text("SELECT count(*) FROM sqlite_master WHERE type='table'")
            ).scalar()
        return {
            "ok": True,
            "backend": "turso" if settings.is_turso else "sqlite",
            "journal_mode": mode,
            "tables": tables,
        }
    except Exception as exc:  # pragma: no cover - defensive
        log.error("db healthcheck failed", extra={"error": str(exc)})
        return {"ok": False, "error": str(exc)}
