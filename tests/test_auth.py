"""The bearer middleware: 401 before the MCP application, pass-through after."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from src.mcp.auth import BearerAuthMiddleware, current_bearer, fingerprint

BARE = "starlight-api-key"


async def _echo_app(scope: Any, receive: Any, send: Any) -> None:
    """Inner application: tells what it sees in the context."""
    bearer = current_bearer()
    body = json.dumps(
        {
            "bearer_present": bearer is not None,
            "fingerprint": fingerprint(bearer) if bearer else None,
        }
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://mcp.test",
    )


@pytest.mark.asyncio
async def test_request_without_header_is_401() -> None:
    async with _client(BearerAuthMiddleware(_echo_app)) as client:
        response = await client.post("/mcp", json={"jsonrpc": "2.0"})

    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}


@pytest.mark.asyncio
async def test_request_with_another_scheme_is_401() -> None:
    async with _client(BearerAuthMiddleware(_echo_app)) as client:
        response = await client.post("/mcp", headers={"authorization": "Basic dXNlcjpwYXNz"}, json={})

    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}


@pytest.mark.asyncio
async def test_bearer_without_a_value_is_401() -> None:
    async with _client(BearerAuthMiddleware(_echo_app)) as client:
        response = await client.post("/mcp", headers={"authorization": "Bearer "}, json={})

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_valid_bearer_reaches_the_application() -> None:
    async with _client(BearerAuthMiddleware(_echo_app)) as client:
        response = await client.post("/mcp", headers={"authorization": f"Bearer {BARE}"}, json={})

    assert response.status_code == 200
    payload = response.json()
    assert payload["bearer_present"] is True
    assert payload["fingerprint"] == fingerprint(BARE)


@pytest.mark.asyncio
async def test_the_bearer_is_gone_after_the_request() -> None:
    async with _client(BearerAuthMiddleware(_echo_app)) as client:
        await client.post("/mcp", headers={"authorization": f"Bearer {BARE}"}, json={})

    assert current_bearer() is None


@pytest.mark.asyncio
async def test_other_scopes_pass_through() -> None:
    seen: list[str] = []

    async def _lifespan(scope: Any, receive: Any, send: Any) -> None:
        seen.append(scope["type"])

    async with _client(BearerAuthMiddleware(_lifespan)) as client:
        await client.get("/mcp")

    assert seen == []  # the http scope was answered by the middleware


@pytest.mark.asyncio
async def test_the_bearer_is_never_written_to_disk(tmp_path: Any) -> None:
    """The middleware keeps the bearer in memory only."""

    async def _app(scope: Any, receive: Any, send: Any) -> None:
        assert current_bearer() == BARE
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    async with _client(BearerAuthMiddleware(_app)) as client:
        await client.post("/mcp", headers={"authorization": f"Bearer {BARE}"}, json={})

    written = [path for path in tmp_path.rglob("*") if path.is_file()]
    assert written == []
