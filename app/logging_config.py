"""Structured (JSON) logging setup with request-id correlation."""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

_RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename", "module",
    "exc_info", "exc_text", "stack_info", "lineno", "funcName", "created", "msecs",
    "relativeCreated", "thread", "threadName", "processName", "process", "taskName",
    "message", "asctime",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        base = super().format(record)
        return f"{base} request_id={request_id_var.get()}"


def setup_logging(level: str = "INFO", log_file: str | None = None, force_json: bool = True) -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    for handler in list(root.handlers):
        root.removeHandler(handler)

    stream = logging.StreamHandler(sys.stdout)
    if force_json:
        stream.setFormatter(JsonFormatter())
    else:
        stream.setFormatter(ConsoleFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(stream)

    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=20 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(JsonFormatter())
        root.addHandler(file_handler)

    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, root.level))
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)


def configure_from_settings(log_level: str, log_file: str | None) -> None:
    force_json = os.getenv("LOG_FORMAT", "json").lower() != "text"
    setup_logging(level=log_level, log_file=log_file, force_json=force_json)
