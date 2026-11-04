"""Async HTTP client for the public ReelShort web endpoints.

Design notes
------------
* Metadata only: the client never requests video manifests, stream tokens or
  DRM payloads, and the parser drops ``play_info``/``video_url`` fields.
* Polite by construction: global token bucket, bounded concurrency, robots.txt
  enforcement, retry with exponential backoff + jitter, and a circuit breaker
  that pauses the whole source when it starts answering 429/403.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from app.config import Settings, get_settings

log = logging.getLogger(__name__)


class SourceError(RuntimeError):
    """Base class for source failures."""


class SourceUnavailable(SourceError):
    """The source asked us to back off (429/403/5xx) — caller should reschedule."""

    def __init__(self, message: str, *, retry_after: float | None = None, status: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.status = status


class SourceBlocked(SourceError):
    """robots.txt (or policy) forbids fetching this URL."""


class SourceNotFound(SourceError):
    """The requested book/episode does not exist upstream."""


@dataclass
class SourceResponse:
    url: str
    status: int
    text: str
    elapsed_ms: int
    kind: str
    from_cache: bool = False


class TokenBucket:
    def __init__(self, rate_per_minute: float, burst: float) -> None:
        self.rate_per_second = max(rate_per_minute, 0.1) / 60.0
        self.capacity = max(burst, 1.0)
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while True:
            async with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate_per_second)
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self.rate_per_second
            if time.monotonic() > deadline:
                raise SourceUnavailable("rate limiter timeout: outbound budget exhausted")
            await asyncio.sleep(min(wait, 2.0))

    @property
    def available_tokens(self) -> float:
        return round(self._tokens, 2)


@dataclass
class SourceStats:
    requests: int = 0
    ok: int = 0
    errors: int = 0
    retries: int = 0
    throttled: int = 0
    blocked: int = 0
    bytes_read: int = 0
    last_status: int | None = None
    last_error: str | None = None
    breaker_open_until: float = 0.0
    consecutive_errors: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)

    def snapshot(self) -> dict[str, object]:
        now = time.time()
        return {
            "requests": self.requests,
            "ok": self.ok,
            "errors": self.errors,
            "retries": self.retries,
            "throttled": self.throttled,
            "blocked": self.blocked,
            "bytes_read": self.bytes_read,
            "last_status": self.last_status,
            "last_error": self.last_error,
            "breaker_open": self.breaker_open_until > now,
            "breaker_cooldown_seconds": max(0, round(self.breaker_open_until - now, 1)),
            "consecutive_errors": self.consecutive_errors,
            "by_kind": dict(self.by_kind),
        }


class ReelShortSource:
    """Public ReelShort metadata source."""

    def __init__(self, settings: Settings | None = None, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings or get_settings()
        self.base_url = self.settings.source_base_url.rstrip("/")
        self.stats = SourceStats()
        self._client = client
        self._own_client = client is None
        self._bucket = TokenBucket(self.settings.source_requests_per_minute, self.settings.source_burst)
        self._semaphore = asyncio.Semaphore(max(1, self.settings.source_concurrency))
        self._robots: dict[str, tuple[float, RobotFileParser | None]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._breaker_lock = asyncio.Lock()

    # --- lifecycle ---------------------------------------------------------
    async def __aenter__(self) -> "ReelShortSource":
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.settings.source_timeout_seconds),
                headers={
                    "User-Agent": self.settings.user_agent,
                    "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                follow_redirects=True,
                limits=httpx.Limits(
                    max_connections=max(4, self.settings.source_concurrency * 2),
                    max_keepalive_connections=max(2, self.settings.source_concurrency),
                ),
            )

    async def close(self) -> None:
        if self._client is not None and self._own_client:
            await self._client.aclose()
            self._client = None

    # --- helpers -----------------------------------------------------------
    def _breaker_open(self) -> bool:
        return time.time() < self.stats.breaker_open_until

    async def _open_breaker(self, seconds: float) -> None:
        async with self._breaker_lock:
            until = time.time() + seconds
            if until > self.stats.breaker_open_until:
                self.stats.breaker_open_until = until
                log.warning(
                    "source circuit breaker open",
                    extra={"cooldown_seconds": seconds, "until": until},
                )

    def _host_lock(self, host: str) -> asyncio.Lock:
        return self._locks.setdefault(host, asyncio.Lock())

    async def _robots_allows(self, url: str) -> bool:
        if not self.settings.respect_robots_txt:
            return True
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        cached = self._robots.get(origin)
        now = time.monotonic()
        if cached and now - cached[0] < self.settings.robots_cache_seconds:
            parser = cached[1]
        else:
            parser = None
            try:
                async with self._semaphore:
                    await self._bucket.acquire()
                    resp = await self._client.get(f"{origin}/robots.txt", timeout=10.0)
                if resp.status_code == 200:
                    parser = RobotFileParser()
                    parser.parse(resp.text.splitlines())
            except Exception as exc:  # pragma: no cover - network flake
                log.warning("robots.txt fetch failed", extra={"origin": origin, "error": str(exc)})
                parser = None
            self._robots[origin] = (now, parser)
        if parser is None:
            return True  # fail-open: robots unavailable, keep default politeness
        return parser.can_fetch(self.settings.user_agent, url)

    def _backoff_seconds(self, attempt: int, retry_after: float | None = None) -> float:
        if retry_after is not None:
            return min(max(retry_after, 0.5), self.settings.source_backoff_max_seconds)
        base = self.settings.source_backoff_base_seconds * (2 ** max(attempt - 1, 0))
        return min(base, self.settings.source_backoff_max_seconds) * (0.75 + random.random() * 0.5)

    async def get(
        self,
        url: str,
        *,
        kind: str,
        params: dict[str, object] | None = None,
        lang: str | None = None,
        allow_status: tuple[int, ...] = (200,),
        raise_on_404: bool = True,
    ) -> SourceResponse:
        if not await self._robots_allows(url):
            self.stats.blocked += 1
            raise SourceBlocked(f"robots.txt disallows {url}")
        if self._breaker_open():
            wait = round(self.stats.breaker_open_until - time.time(), 1)
            raise SourceUnavailable(f"source in cooldown for another {wait}s")

        last_error: Exception | None = None
        for attempt in range(1, self.settings.source_max_retries + 1):
            await self._bucket.acquire()
            headers: dict[str, str] = {}
            if lang:
                headers["lang"] = lang
                headers["Referer"] = f"{self.base_url}/"
            started = time.monotonic()
            try:
                async with self._semaphore:
                    resp = await self._client.get(url, params=params, headers=headers)
                elapsed_ms = int((time.monotonic() - started) * 1000)
                self.stats.requests += 1
                self.stats.by_kind[kind] = self.stats.by_kind.get(kind, 0) + 1
                self.stats.last_status = resp.status_code
                self.stats.bytes_read += len(resp.content)

                if resp.status_code in allow_status:
                    self.stats.ok += 1
                    self.stats.consecutive_errors = 0
                    return SourceResponse(
                        url=str(resp.url), status=resp.status_code, text=resp.text,
                        elapsed_ms=elapsed_ms, kind=kind,
                    )
                if resp.status_code == 404:
                    if raise_on_404:
                        raise SourceNotFound(f"404 for {url}")
                    self.stats.ok += 1
                    return SourceResponse(
                        url=str(resp.url), status=404, text=resp.text, elapsed_ms=elapsed_ms, kind=kind
                    )
                if resp.status_code in (429, 403):
                    self.stats.throttled += 1
                    self.stats.errors += 1
                    self.stats.consecutive_errors += 1
                    retry_after = _parse_retry_after(resp.headers.get("retry-after"))
                    cooldown = (
                        self.settings.source_cooldown_on_429_seconds
                        if resp.status_code == 429
                        else self.settings.source_cooldown_on_403_seconds
                    )
                    await self._open_breaker(retry_after or cooldown)
                    raise SourceUnavailable(
                        f"source returned {resp.status_code}", retry_after=retry_after or cooldown,
                        status=resp.status_code,
                    )
                if resp.status_code >= 500 or resp.status_code == 408:
                    last_error = SourceUnavailable(f"upstream {resp.status_code}", status=resp.status_code)
                    self.stats.errors += 1
                    self.stats.consecutive_errors += 1
                    if self.stats.consecutive_errors >= self.settings.source_breaker_error_threshold:
                        await self._open_breaker(self.settings.source_cooldown_on_429_seconds)
                    if attempt < self.settings.source_max_retries:
                        self.stats.retries += 1
                        await asyncio.sleep(self._backoff_seconds(attempt))
                        continue
                    raise last_error
                # any other non-success (4xx)
                raise SourceError(f"unexpected status {resp.status_code} for {url}")
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                elapsed_ms = int((time.monotonic() - started) * 1000)
                self.stats.errors += 1
                self.stats.consecutive_errors += 1
                self.stats.last_error = str(exc)
                last_error = SourceUnavailable(f"transport error: {exc}")
                if self.stats.consecutive_errors >= self.settings.source_breaker_error_threshold:
                    await self._open_breaker(self.settings.source_cooldown_on_429_seconds)
                if attempt < self.settings.source_max_retries:
                    self.stats.retries += 1
                    await asyncio.sleep(self._backoff_seconds(attempt))
                    continue
                raise last_error from exc
            except SourceError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                last_error = SourceError(str(exc))
                if attempt < self.settings.source_max_retries:
                    self.stats.retries += 1
                    await asyncio.sleep(self._backoff_seconds(attempt))
                    continue
                raise last_error from exc
        raise last_error or SourceUnavailable("unknown source failure")

    # --- concrete endpoints -------------------------------------------------
    async def movie_page(self, book_id: str) -> SourceResponse:
        """Public movie page HTML (slug-less; the site resolves and redirects)."""
        url = f"{self.base_url}/movie/{book_id}"
        return await self.get(url, kind="movie_page")

    async def movie_info(self, book_id: str, lang: str | None = None) -> dict:
        """Official public JSON metadata endpoint."""
        url = f"{self.base_url}/api/video/book/getBookInfo"
        resp = await self.get(url, kind="book_info", params={"book_id": book_id}, lang=lang)
        return _json_payload(resp, url)

    async def chapter_list(self, book_id: str, page: int = 1, page_size: int = 100, lang: str | None = None) -> dict:
        url = f"{self.base_url}/api/video/book/getChapterList"
        resp = await self.get(
            url, kind="chapter_list",
            params={"book_id": book_id, "page": page, "page_size": page_size}, lang=lang,
        )
        return _json_payload(resp, url)

    async def search_page(self, lang: str, keywords: str, page: int = 1) -> SourceResponse:
        """Official per-locale search (SSR HTML, parsed from __NEXT_DATA__)."""
        locale = "" if lang == "en" else f"/{lang}"
        url = f"{self.base_url}{locale}/search"
        params: dict[str, object] = {"keywords": keywords}
        if page > 1:
            params["page"] = page
        return await self.get(url, kind="search", params=params)

    async def tag_catalog(
        self, tag_id: str, lang: str, page: int = 1, page_size: int = 50
    ) -> dict:
        """Official per-language tag catalogue listing (metadata only)."""
        url = f"{self.base_url}/api/video/book/getTagBook"
        resp = await self.get(
            url, kind="tag_catalog",
            params={"tag_id": tag_id, "page": page, "page_size": page_size, "language": lang},
            lang=lang,
        )
        return _json_payload(resp, url)

    async def tag_list(self, lang: str, book_type: int = 1) -> dict:
        url = f"{self.base_url}/api/video/book/getTagList"
        resp = await self.get(url, kind="tag_list", params={"book_type": book_type, "language": lang}, lang=lang)
        return _json_payload(resp, url)

    async def homepage(self, lang: str) -> SourceResponse:
        """Fetch the public locale homepage containing SSR hall/webInfo catalog data."""
        from app.config import get_locale_source_url

        url = get_locale_source_url(lang, self.base_url)
        return await self.get(url, kind="homepage", lang=lang)

    async def catalog_page(self, lang: str, page: int = 1) -> SourceResponse:
        """Fetch a paginated SSR all-movies catalog page (`/movie-genres/all-movies/{page}`)."""
        from app.config import get_locale_catalog_page_url

        url = get_locale_catalog_page_url(lang, page=page, base_url=self.base_url)
        return await self.get(url, kind="catalog_page", lang=lang)

    async def sitemap(self) -> str | None:
        resp = await self.get(f"{self.base_url}/sitemap.xml", kind="sitemap", raise_on_404=False)
        return resp.text if resp.status == 200 else None


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if re.fullmatch(r"\d+", value):
        return float(value)
    return None


def _json_payload(resp: SourceResponse, url: str) -> dict:
    import json

    try:
        payload = json.loads(resp.text)
    except json.JSONDecodeError as exc:
        raise SourceError(f"invalid JSON from {url}") from exc
    if isinstance(payload, dict) and payload.get("code") not in (None, 0):
        code = payload.get("code")
        msg = payload.get("msg") or "unknown error"
        if code in (404,):
            raise SourceNotFound(f"{url}: {msg}")
        if code in (405, 101):
            raise SourceError(f"{url}: upstream rejected request ({code} {msg})")
        raise SourceError(f"{url}: upstream error ({code} {msg})")
    data = payload.get("data") if isinstance(payload, dict) else None
    if data is None:
        raise SourceNotFound(f"{url}: empty payload")
    return data
