"""The JMAP client: session, batching, method errors, upstream check, cache."""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from src.mcp.jmap import JmapClient
from tests.conftest import SESSION_DOCUMENT, jmap_response

BARE = "starlight-api-key"


def _session_handler(document: dict[str, Any] | None = None) -> Any:
    doc = document if document is not None else SESSION_DOCUMENT

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=doc)
        return jmap_response([["Identity/get", {"accountId": "account-1", "list": []}, "c0"]], state="s1")

    return handler


@pytest.mark.asyncio
async def test_the_session_document_is_parsed(settings: Any, make_transport: Any) -> None:
    transport, seen = make_transport(_session_handler())
    client = JmapClient(settings, transport=transport)

    session = await client.session(BARE)

    assert session.api_url == "https://mail.example.com/jmap/api"
    assert session.download_url == "https://mail.example.com/download/{accountId}/{blobId}/{name}"
    assert session.upload_url == "https://mail.example.com/upload/{accountId}"
    assert session.account_id == "account-1"
    assert session.capabilities == (
        "urn:ietf:params:jmap:core",
        "urn:ietf:params:jmap:mail",
        "urn:ietf:params:jmap:submission",
    )
    assert len(seen) == 1
    assert seen[0].url.path == "/jmap/session"


@pytest.mark.asyncio
async def test_the_bearer_is_forwarded_upstream(settings: Any, make_transport: Any) -> None:
    transport, seen = make_transport(_session_handler())
    client = JmapClient(settings, transport=transport)

    await client.session(BARE)

    assert seen[0].headers["authorization"] == f"Bearer {BARE}"


@pytest.mark.asyncio
async def test_the_session_falls_back_to_well_known(settings: Any, make_transport: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/jmap":
            return httpx.Response(200, json=SESSION_DOCUMENT)
        return httpx.Response(404, json={"error": "not found"})

    transport, seen = make_transport(handler)
    client = JmapClient(settings, transport=transport)

    session = await client.session(BARE)

    assert session.account_id == "account-1"
    assert [request.url.path for request in seen] == ["/jmap/session", "/.well-known/jmap"]


@pytest.mark.asyncio
async def test_method_calls_are_batched(settings: Any, make_transport: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=SESSION_DOCUMENT)
        return jmap_response(
            [
                ["Identity/get", {"accountId": "account-1", "list": []}, "c0"],
                ["Mailbox/get", {"accountId": "account-1", "list": []}, "c1"],
            ],
            state="state-1",
        )

    transport, seen = make_transport(handler)
    client = JmapClient(settings, transport=transport)
    session = await client.session(BARE)

    envelope = await client.call(
        BARE,
        session,
        [("Identity/get", {"accountId": "account-1"}), ("Mailbox/get", {"accountId": "account-1"})],
    )

    assert len(seen) == 2
    payload = json.loads(seen[1].content)
    assert payload["using"] == [
        "urn:ietf:params:jmap:core",
        "urn:ietf:params:jmap:mail",
        "urn:ietf:params:jmap:submission",
    ]
    assert payload["methodCalls"] == [
        ["Identity/get", {"accountId": "account-1"}, "c0"],
        ["Mailbox/get", {"accountId": "account-1"}, "c1"],
    ]
    assert envelope.state == "state-1"
    assert envelope.result_of("Mailbox/get") == {"accountId": "account-1", "list": []}


@pytest.mark.asyncio
async def test_a_method_error_inside_http_200_is_a_tool_error(settings: Any, make_transport: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=SESSION_DOCUMENT)
        return httpx.Response(
            200,
            json={
                "methodResponses": [
                    [
                        "error",
                        {
                            "type": "urn:ietf:params:jmap:error:unknownMethod",
                            "code": "unknownMethod",
                            "description": "The method Thread/get is not supported",
                        },
                        "c0",
                    ]
                ],
                "sessionState": "state-1",
            },
        )

    transport, _seen = make_transport(handler)
    client = JmapClient(settings, transport=transport)
    session = await client.session(BARE)

    with pytest.raises(ToolError) as failure:
        await client.call(BARE, session, [("Thread/get", {"accountId": "account-1"})])

    message = str(failure.value)
    assert "unknownMethod" in message
    assert "Thread/get" in message
    assert BARE not in message


@pytest.mark.asyncio
async def test_a_foreign_upstream_is_not_called(settings: Any, make_transport: Any) -> None:
    foreign = dict(SESSION_DOCUMENT)
    foreign["apiUrl"] = "https://evil.example.net/jmap/api"
    transport, seen = make_transport(_session_handler(foreign))
    client = JmapClient(settings, transport=transport)

    with pytest.raises(ToolError) as failure:
        await client.session(BARE)

    assert "evil.example.net" in str(failure.value)
    assert len(seen) == 1  # only the session request, no call to the foreign host


@pytest.mark.asyncio
async def test_the_session_is_cached_per_bearer(settings: Any, make_transport: Any) -> None:
    transport, seen = make_transport(_session_handler())
    client = JmapClient(settings, transport=transport)

    first = await client.session(BARE)
    second = await client.session(BARE)
    other = await client.session("another-key")

    assert first is second
    assert other is not first
    assert len(seen) == 2  # one for the first key, one for the second
    assert seen[0].headers["authorization"] == f"Bearer {BARE}"
    assert seen[1].headers["authorization"] == "Bearer another-key"


@pytest.mark.asyncio
async def test_the_cache_can_be_switched_off(settings: Any, make_transport: Any) -> None:
    off = dataclasses.replace(
        settings,
        stalwart=dataclasses.replace(settings.stalwart, session_cache_ttl_sec=0),
    )
    transport, seen = make_transport(_session_handler())
    client = JmapClient(off, transport=transport)

    await client.session(BARE)
    await client.session(BARE)

    assert len(seen) == 2


@pytest.mark.asyncio
async def test_the_cache_holds_no_bearer(settings: Any, make_transport: Any) -> None:
    transport, _seen = make_transport(_session_handler())
    client = JmapClient(settings, transport=transport)

    await client.session(BARE)

    assert all(BARE not in repr(value) for value in client._cache.values())


@pytest.mark.asyncio
async def test_a_rejected_bearer_is_reported_without_the_key(settings: Any, make_transport: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="Unauthorized")

    transport, _seen = make_transport(handler)
    client = JmapClient(settings, transport=transport)

    with pytest.raises(ToolError) as failure:
        await client.session(BARE)

    message = str(failure.value)
    assert "401" in message
    assert BARE not in message


@pytest.mark.asyncio
async def test_an_unreachable_upstream_is_reported(settings: Any, make_transport: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    transport, _seen = make_transport(handler)
    client = JmapClient(settings, transport=transport)

    with pytest.raises(ToolError) as failure:
        await client.session(BARE)

    assert "mail.example.com" in str(failure.value)
