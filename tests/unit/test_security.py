"""Unit tests for authentication separation and inbound rate limiting."""

from __future__ import annotations

import base64
import time

import pytest

from app.config import Settings
from app.main import create_app
from app.security import SlidingWindowLimiter, hash_key

from tests.conftest import ADMIN_PASSWORD, ADMIN_TOKEN, ADMIN_USER, PUBLIC_KEY, build_settings


@pytest.fixture()
def app(tmp_path):
    from tests.conftest import apply_migrations

    settings = build_settings(tmp_path, public_auth_enabled=True)
    apply_migrations(settings)
    return create_app(settings)


@pytest.mark.asyncio
async def test_public_endpoint_requires_key_when_enabled(app) -> None:
    import httpx

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/v1/movies")
            assert response.status_code == 401
            assert response.json()["error"]["code"] == "unauthorized"

            ok = await client.get("/v1/movies", headers={"X-API-Key": PUBLIC_KEY})
            assert ok.status_code == 200

            bad = await client.get("/v1/movies", headers={"X-API-Key": "nope"})
            assert bad.status_code == 403
            assert bad.json()["error"]["code"] == "invalid_api_key"


@pytest.mark.asyncio
async def test_admin_endpoints_always_require_auth(app) -> None:
    import httpx

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            for path in ("/admin/stats", "/admin/jobs"):
                response = await client.get(path)
                assert response.status_code == 401, path
                assert response.json()["error"]["code"] == "admin_unauthorized"
            # public key must not grant admin access
            response = await client.get("/admin/stats", headers={"X-API-Key": PUBLIC_KEY})
            assert response.status_code == 401

            token_ok = await client.get("/admin/stats", headers={"X-Admin-Token": ADMIN_TOKEN})
            assert token_ok.status_code == 200

            basic = base64.b64encode(f"{ADMIN_USER}:{ADMIN_PASSWORD}".encode()).decode()
            basic_ok = await client.get("/admin/stats", headers={"Authorization": f"Basic {basic}"})
            assert basic_ok.status_code == 200

            wrong = base64.b64encode(f"{ADMIN_USER}:wrong".encode()).decode()
            bad = await client.get("/admin/stats", headers={"Authorization": f"Basic {wrong}"})
            assert bad.status_code == 401


@pytest.mark.asyncio
async def test_public_responses_do_not_leak_secrets(app) -> None:
    import httpx

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        headers = {"X-API-Key": PUBLIC_KEY}
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            for path in ("/health", "/v1/meta", "/v1/movies", "/v1/search?q=genie"):
                response = await client.get(path, headers=headers)
                body = response.text
                assert ADMIN_PASSWORD not in body
                assert ADMIN_TOKEN not in body
                assert PUBLIC_KEY not in body
                assert "play_info" not in body


def test_sliding_window_limiter() -> None:
    limiter = SlidingWindowLimiter()
    for _ in range(3):
        allowed, remaining, _ = limiter.check("k", 3, 60)
        assert allowed
    allowed, remaining, retry_after = limiter.check("k", 3, 60)
    assert allowed is False
    assert remaining == 0
    assert retry_after > 0
    limiter.reset()
    allowed, _, _ = limiter.check("k", 3, 60)
    assert allowed is True


def test_hash_key_is_stable_and_short() -> None:
    assert hash_key("abc") == hash_key("abc")
    assert len(hash_key("abc")) == 12


def test_admin_configured_flag() -> None:
    assert Settings(admin_password="x").admin_configured is True
    assert Settings(admin_token="y").admin_configured is True
    assert Settings(admin_password="", admin_token="").admin_configured is False
