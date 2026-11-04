"""Unit tests for outbound politeness: rate limiting, retries, breaker, robots."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from app.config import Settings
from app.crawler.source import ReelShortSource, SourceBlocked, SourceUnavailable


def make_settings(**overrides: object) -> Settings:
    values = {
        "source_requests_per_minute": 6000,
        "source_burst": 5,
        "source_max_retries": 3,
        "source_backoff_base_seconds": 0.01,
        "source_backoff_max_seconds": 0.05,
        "source_cooldown_on_429_seconds": 0.3,
        "source_cooldown_on_403_seconds": 0.3,
        "source_breaker_error_threshold": 3,
        "respect_robots_txt": False,
        "source_concurrency": 3,
        "db_path": "/tmp/opencode/unit-source.sqlite3",
        "log_file": None,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_token_bucket_limits_rate() -> None:
    from app.crawler.source import TokenBucket

    bucket = TokenBucket(rate_per_minute=60, burst=1)
    await bucket.acquire()
    started = time.monotonic()
    await bucket.acquire()
    elapsed = time.monotonic() - started
    assert elapsed >= 0.5, "second acquire must wait for the refill"


@pytest.mark.asyncio
async def test_retries_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, text="nope", request=request)
        return httpx.Response(200, text="ok", request=request)

    source = ReelShortSource(
        make_settings(), client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    response = await source.get("https://www.reelshort.com/api/x", kind="test")
    assert response.text == "ok"
    assert calls["n"] == 3
    assert source.stats.retries == 2
    await source.close()


@pytest.mark.asyncio
async def test_429_opens_breaker_and_defers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "1"}, text="slow down", request=request)

    source = ReelShortSource(
        make_settings(), client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(SourceUnavailable) as excinfo:
        await source.get("https://www.reelshort.com/api/y", kind="test")
    assert excinfo.value.retry_after == 1.0
    assert source.stats.throttled == 1
    assert source.stats.snapshot()["breaker_open"] is True
    # while the breaker is open no further request is attempted
    before = source.stats.requests
    with pytest.raises(SourceUnavailable):
        await source.get("https://www.reelshort.com/api/y", kind="test")
    assert source.stats.requests == before
    await source.close()


@pytest.mark.asyncio
async def test_robots_disallow_blocks_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /movie/\n", request=request)
        return httpx.Response(200, text="ok", request=request)

    source = ReelShortSource(
        make_settings(respect_robots_txt=True),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(SourceBlocked):
        await source.get("https://www.reelshort.com/movie/abc", kind="test")
    assert source.stats.blocked == 1
    await source.close()


@pytest.mark.asyncio
async def test_concurrency_is_bounded() -> None:
    state = {"active": 0, "max": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        state["active"] += 1
        state["max"] = max(state["max"], state["active"])
        await asyncio.sleep(0.05)
        state["active"] -= 1
        return httpx.Response(200, text="ok", request=request)

    transport = httpx.MockTransport(handler)
    source = ReelShortSource(
        make_settings(source_concurrency=2), client=httpx.AsyncClient(transport=transport)
    )
    await asyncio.gather(
        *[source.get(f"https://www.reelshort.com/api/{i}", kind="test") for i in range(6)]
    )
    assert state["max"] <= 2
    await source.close()
