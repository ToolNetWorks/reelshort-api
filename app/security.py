"""Authentication (public API keys vs admin credentials) and inbound rate limits."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from app.config import Settings, get_settings
from app.db.base import session_scope
from app.db.models import ApiUsage

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Principal:
    subject: str
    role: str  # "public" | "admin" | "anonymous"
    api_key_id: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class SlidingWindowLimiter:
    """In-process sliding-window limiter (per subject, per bucket)."""

    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, limit: int, window_seconds: float) -> tuple[bool, int, float]:
        now = time.monotonic()
        bucket = self._events[key]
        cutoff = now - window_seconds
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= limit:
            retry_after = max(0.5, bucket[0] + window_seconds - now)
            return False, 0, retry_after
        bucket.append(now)
        return True, limit - len(bucket), 0.0

    def reset(self, key: str | None = None) -> None:
        if key is None:
            self._events.clear()
        else:
            self._events.pop(key, None)


limiter = SlidingWindowLimiter()


def hash_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _extract_api_key(
    x_api_key: str | None, authorization: str | None
) -> str | None:
    if x_api_key:
        return x_api_key.strip()
    if authorization:
        parts = authorization.split(None, 1)
        if len(parts) == 2 and parts[0].lower() in ("bearer", "apikey", "token"):
            return parts[1].strip()
    return None


def client_ip(request: Request, settings: Settings) -> str:
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def require_public(
    request: Request,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    authorization: Annotated[str | None, Header()] = None,
    settings: Annotated[Settings, Depends(get_settings)] = None,  # type: ignore[assignment]
) -> Principal:
    """Public endpoints: anonymous on loopback, API key required when enabled."""
    settings = settings or get_settings()
    provided = _extract_api_key(x_api_key, authorization)
    ip = client_ip(request, settings)

    if provided and any(hmac.compare_digest(provided, key) for key in settings.public_api_keys):
        request.state.subject = f"key:{hash_key(provided)}"
        request.state.role = "public"
        return Principal(subject=request.state.subject, role="public", api_key_id=hash_key(provided))

    if settings.public_auth_enabled:
        if not provided:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "unauthorized", "message": "Missing API key (X-API-Key header)"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "invalid_api_key", "message": "Invalid API key"},
        )
    if provided:
        # unknown key supplied while auth is disabled → treat as anonymous but flag
        log.info("unknown public api key supplied", extra={"key_id": hash_key(provided), "ip": ip})
    request.state.subject = f"anon:{ip}"
    request.state.role = "anonymous"
    return Principal(subject=f"anon:{ip}", role="anonymous")


def _check_basic(authorization: str, settings: Settings) -> Principal | None:
    if not authorization.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(authorization.split(None, 1)[1]).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, IndexError):
        return None
    if ":" not in decoded:
        return None
    username, password = decoded.split(":", 1)
    user_ok = bool(settings.admin_username) and hmac.compare_digest(username, settings.admin_username)
    pass_ok = bool(settings.admin_password) and hmac.compare_digest(password, settings.admin_password)
    if user_ok and pass_ok:
        return Principal(subject=f"admin:{username}", role="admin")
    return None


def require_admin(
    request: Request,
    x_admin_token: Annotated[str | None, Header(alias="X-Admin-Token")] = None,
    authorization: Annotated[str | None, Header()] = None,
    settings: Annotated[Settings, Depends(get_settings)] = None,  # type: ignore[assignment]
) -> Principal:
    """Admin endpoints: always authenticated (Basic credentials or admin token)."""
    settings = settings or get_settings()
    if x_admin_token and settings.admin_token and hmac.compare_digest(x_admin_token, settings.admin_token):
        request.state.subject = "admin:token"
        request.state.role = "admin"
        return Principal(subject="admin:token", role="admin")
    if authorization:
        principal = _check_basic(authorization, settings)
        if principal is not None:
            request.state.subject = principal.subject
            request.state.role = "admin"
            return principal
    log.warning("admin authentication failed", extra={"ip": client_ip(request, settings)})
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "admin_unauthorized", "message": "Admin credentials required"},
        headers={"WWW-Authenticate": 'Basic realm="reelshort-admin"'},
    )


def rate_limit(
    key: str,
    limit: int,
    window_seconds: float,
    *,
    code: str = "rate_limited",
    message: str = "Too many requests",
) -> None:
    allowed, _remaining, retry_after = limiter.check(key, limit, window_seconds)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": code, "message": message, "retry_after_seconds": round(retry_after, 1)},
            headers={"Retry-After": str(int(retry_after) + 1)},
        )


def record_usage(
    *,
    subject: str,
    role: str,
    route: str,
    method: str,
    status_code: int | None,
    duration_ms: float | None,
    cache_hit: bool | None,
    ip: str | None,
    user_agent: str | None,
) -> None:
    """Persist a usage row (best effort; never breaks the request path)."""
    try:
        with session_scope() as session:
            session.add(
                ApiUsage(
                    subject=subject[:128],
                    role=role,
                    route=route[:128],
                    method=method[:8],
                    status_code=status_code,
                    duration_ms=duration_ms,
                    cache_hit=cache_hit,
                    client_ip=(ip or "")[:64] or None,
                    user_agent=(user_agent or "")[:400] or None,
                )
            )
    except Exception as exc:  # pragma: no cover - metrics must never fail requests
        log.debug("usage recording failed", extra={"error": str(exc)})
