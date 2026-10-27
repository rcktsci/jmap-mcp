"""The whoami tool: the answer of the schema and the published schemas."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from jsonschema import Draft202012Validator
from mcp.server.mcpserver.exceptions import ToolError

from src.mcp import auth, tools
from src.mcp.jmap import JmapClient
from src.mcp.server import mcp_server
from tests.conftest import SESSION_DOCUMENT, load_published_schema

BARE = "starlight-api-key"

IDENTITIES = {
    "accountId": "account-1",
    "list": [
        {
            "id": "id-1",
            "name": "Main",
            "email": "me@example.com",
            "replyTo": [{"email": "reply@example.com", "name": "Reply"}],
            "isDefault": True,
        },
        {
            "id": "id-2",
            "name": None,
            "email": "alias@example.com",
            "replyTo": [],
        },
    ],
    "notFound": [],
}


def _handler(identities: Any = IDENTITIES, state: str | None = None) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=SESSION_DOCUMENT)
        return httpx.Response(
            200,
            json={"methodResponses": [["Identity/get", identities, "c0"]], "sessionState": state},
        )

    return handler


@pytest.fixture
def bearer() -> Any:
    marker = auth.set_bearer(BARE)
    yield BARE
    auth.reset_bearer(marker)


@pytest.fixture
def jmap_client(settings: Any, make_transport: Any, monkeypatch: Any) -> Any:
    def build(handler: Any) -> JmapClient:
        transport, _seen = make_transport(handler)
        client = JmapClient(settings, transport=transport)
        monkeypatch.setattr(tools, "get_jmap_client", lambda: client)
        return client

    return build


@pytest.mark.asyncio
async def test_whoami_returns_the_fields_of_the_schema(jmap_client: Any, bearer: Any) -> None:
    jmap_client(_handler())

    result = await tools.whoami()

    assert set(result) == {"account_id", "email", "identities", "capabilities", "state"}
    assert result["account_id"] == "account-1"
    assert result["email"] == "me@example.com"
    assert result["state"] is None
    assert result["capabilities"] == [
        "urn:ietf:params:jmap:core",
        "urn:ietf:params:jmap:mail",
        "urn:ietf:params:jmap:submission",
    ]


@pytest.mark.asyncio
async def test_whoami_returns_the_identities(jmap_client: Any, bearer: Any) -> None:
    jmap_client(_handler())

    result = await tools.whoami()

    assert result["identities"] == [
        {
            "id": "id-1",
            "email": "me@example.com",
            "name": "Main",
            "reply_to": [{"email": "reply@example.com", "name": "Reply"}],
            "is_default": True,
        },
        {
            "id": "id-2",
            "email": "alias@example.com",
            "name": None,
            "reply_to": [],
            "is_default": False,
        },
    ]


@pytest.mark.asyncio
async def test_whoami_returns_the_state_of_the_answer(jmap_client: Any, bearer: Any) -> None:
    jmap_client(_handler(state="account-state-42"))

    result = await tools.whoami()

    assert result["state"] == "account-state-42"


@pytest.mark.asyncio
async def test_whoami_without_identities_is_a_tool_error(jmap_client: Any, bearer: Any) -> None:
    jmap_client(_handler(identities={"accountId": "account-1", "list": [], "notFound": []}))

    with pytest.raises(ToolError) as failure:
        await tools.whoami()

    assert "identity" in str(failure.value)


@pytest.mark.asyncio
async def test_whoami_without_a_bearer_is_a_tool_error() -> None:
    assert auth.current_bearer() is None

    with pytest.raises(ToolError) as failure:
        await tools.whoami()

    assert "authorization" in str(failure.value)


@pytest.mark.asyncio
async def test_the_answer_validates_against_the_output_schema(jmap_client: Any, bearer: Any) -> None:
    jmap_client(_handler())

    result = await tools.whoami()

    schema = load_published_schema("whoami", "output")
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(json.loads(json.dumps(result)))


@pytest.mark.asyncio
async def test_an_unexpected_failure_becomes_a_tool_error(
    settings: Any, make_transport: Any, monkeypatch: Any, bearer: Any
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("something broke deep inside")

    transport, _seen = make_transport(handler)
    monkeypatch.setattr(tools, "get_jmap_client", lambda: JmapClient(settings, transport=transport))

    with pytest.raises(ToolError) as failure:
        await tools.whoami()

    message = str(failure.value)
    assert "something broke deep inside" not in message
    assert BARE not in message


@pytest.mark.asyncio
async def test_whoami_is_published() -> None:
    published = await mcp_server.list_tools()

    assert "whoami" in [tool.name for tool in published]


@pytest.mark.asyncio
async def test_the_published_schemas_match_the_documentation() -> None:
    published = (await mcp_server.list_tools())[0]

    assert published.input_schema == load_published_schema("whoami", "input")
    assert published.output_schema == load_published_schema("whoami", "output")
