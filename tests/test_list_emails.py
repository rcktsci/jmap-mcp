"""The list_emails tool: the messages of a folder as summaries."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
from jsonschema import Draft202012Validator
from mcp.server.mcpserver.exceptions import ToolError

from src.mcp import tools
from src.mcp.jmap import JmapClient
from tests.conftest import jmap_error, load_published_schema

QUERY = {"accountId": "account-1", "ids": ["e1", "e2"], "total": 5, "position": 0}

EMAILS = {
    "accountId": "account-1",
    "list": [
        {
            "id": "e1",
            "threadId": "t1",
            "subject": "Hello",
            "from": [{"email": "boss@example.com", "name": "Boss"}],
            "to": [{"email": "me@example.com", "name": None}],
            "receivedAt": "2026-10-20T10:00:00Z",
            "preview": "Hi there",
            "keywords": ["$seen"],
            "attachments": [],
        },
        {
            "id": "e2",
            "threadId": None,
            "subject": None,
            "from": [],
            "to": [],
            "receivedAt": "2026-10-19T10:00:00Z",
            "preview": None,
            "keywords": ["$flagged"],
            "attachments": [{"blobId": "b1", "name": "a.pdf", "type": "application/pdf", "size": 10}],
        },
    ],
    "notFound": [],
}


@pytest.mark.asyncio
async def test_list_emails_returns_the_summaries(mock_jmap: Any, bearer: Any) -> None:
    mock_jmap({"Email/query": QUERY, "Email/get": EMAILS})

    result = await tools.list_emails()

    assert result["total"] == 5
    assert result["position"] == 0
    assert result["has_more"] is True
    assert result["emails"][0] == {
        "id": "e1",
        "thread_id": "t1",
        "subject": "Hello",
        "from": [{"email": "boss@example.com", "name": "Boss"}],
        "to": [{"email": "me@example.com", "name": None}],
        "date": datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc),
        "preview": "Hi there",
        "keywords": ["$seen"],
        "has_attachment": False,
    }
    assert result["emails"][1]["has_attachment"] is True
    assert result["emails"][1]["subject"] is None


@pytest.mark.asyncio
async def test_the_answer_validates_against_the_output_schema(
    mock_jmap: Any, bearer: Any
) -> None:
    mock_jmap({"Email/query": QUERY, "Email/get": EMAILS})

    result = await tools.list_emails()

    schema = load_published_schema("list_emails", "output")
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(json.loads(json.dumps(result, default=str)))


@pytest.mark.asyncio
async def test_list_emails_without_messages_is_an_empty_page(
    mock_jmap: Any, bearer: Any
) -> None:
    mock_jmap({"Email/query": {"accountId": "account-1", "ids": [], "total": 0, "position": 0}})

    result = await tools.list_emails()

    assert result == {"emails": [], "total": 0, "has_more": False, "position": 0}


@pytest.mark.asyncio
async def test_list_emails_paginates_by_position_and_limit(
    mock_jmap: Any, bearer: Any
) -> None:
    seen = mock_jmap({"Email/query": QUERY, "Email/get": EMAILS})

    result = await tools.list_emails(limit=2, position=2)

    body = json.loads(seen[1].content)
    query = body["methodCalls"][0][1]
    assert query["position"] == 2
    assert query["limit"] == 2
    assert query["calculateTotal"] is True
    assert result["position"] == 2
    assert result["has_more"] is True


@pytest.mark.asyncio
async def test_the_last_page_says_that_there_is_no_more(
    mock_jmap: Any, bearer: Any
) -> None:
    mock_jmap(
        {
            "Email/query": {"accountId": "account-1", "ids": ["e1"], "total": 1, "position": 0},
            "Email/get": {"accountId": "account-1", "list": [EMAILS["list"][0]], "notFound": []},
        }
    )

    result = await tools.list_emails()

    assert result["has_more"] is False


@pytest.mark.asyncio
async def test_the_limit_is_capped_by_the_server_setting(
    mock_jmap: Any, bearer: Any
) -> None:
    seen = mock_jmap({"Email/query": QUERY, "Email/get": EMAILS})

    await tools.list_emails(limit=100)

    body = json.loads(seen[1].content)
    assert body["methodCalls"][0][1]["limit"] == 50  # max_page_size of the config


@pytest.mark.asyncio
async def test_the_role_of_a_folder_is_resolved_into_its_id(
    mock_jmap: Any, bearer: Any
) -> None:
    seen = mock_jmap(
        {
            "Mailbox/get": {"accountId": "account-1", "list": [{"id": "mb-inbox", "role": "inbox"}]},
            "Email/query": QUERY,
            "Email/get": EMAILS,
        }
    )

    await tools.list_emails(mailbox_role="inbox")

    body = json.loads(seen[2].content)
    assert body["methodCalls"][0][1]["filter"] == {"inMailbox": "mb-inbox"}


@pytest.mark.asyncio
async def test_a_folder_id_is_used_as_it_is(mock_jmap: Any, bearer: Any) -> None:
    seen = mock_jmap({"Email/query": QUERY, "Email/get": EMAILS})

    await tools.list_emails(mailbox_id="mb-42")

    body = json.loads(seen[1].content)
    assert body["methodCalls"][0][1]["filter"] == {"inMailbox": "mb-42"}


@pytest.mark.asyncio
async def test_the_flags_and_the_dates_become_conditions(
    mock_jmap: Any, bearer: Any
) -> None:
    seen = mock_jmap({"Email/query": QUERY, "Email/get": EMAILS})

    await tools.list_emails(unread=True, flagged=True, has_attachment=True)

    body = json.loads(seen[1].content)
    assert body["methodCalls"][0][1]["filter"] == {
        "operator": "and",
        "conditions": [
            {"notKeyword": "$seen"},
            {"hasKeyword": "$flagged"},
            {"hasAttachment": True},
        ],
    }


@pytest.mark.asyncio
async def test_a_role_and_an_id_at_the_same_time_is_a_tool_error(
    mock_jmap: Any, bearer: Any
) -> None:
    mock_jmap({"Email/query": QUERY, "Email/get": EMAILS})

    with pytest.raises(ToolError) as failure:
        await tools.list_emails(mailbox_role="inbox", mailbox_id="mb-42")

    assert "mailbox_role" in str(failure.value)


@pytest.mark.asyncio
async def test_an_unknown_role_is_a_tool_error(mock_jmap: Any, bearer: Any) -> None:
    mock_jmap({"Mailbox/get": {"accountId": "account-1", "list": []}})

    with pytest.raises(ToolError) as failure:
        await tools.list_emails(mailbox_role="drafts")

    assert "drafts" in str(failure.value)


@pytest.mark.asyncio
async def test_a_server_without_a_total_is_a_tool_error(
    mock_jmap: Any, bearer: Any
) -> None:
    mock_jmap({"Email/query": {"accountId": "account-1", "ids": ["e1"], "position": 0}})

    with pytest.raises(ToolError) as failure:
        await tools.list_emails()

    assert "calculateTotal" in str(failure.value)


@pytest.mark.asyncio
async def test_list_emails_turns_a_method_error_into_a_tool_error(
    mock_jmap: Any, bearer: Any
) -> None:
    mock_jmap({"Email/query": jmap_error("serverFail", "serverFail", "the query died")})

    with pytest.raises(ToolError) as failure:
        await tools.list_emails()

    assert "serverFail" in str(failure.value)


@pytest.mark.asyncio
async def test_an_unexpected_failure_becomes_a_tool_error(
    settings: Any, make_transport: Any, monkeypatch: Any, bearer: Any
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("something broke deep inside")

    transport, _seen = make_transport(handler)
    monkeypatch.setattr(tools, "get_jmap_client", lambda: JmapClient(settings, transport=transport))

    with pytest.raises(ToolError) as failure:
        await tools.list_emails()

    message = str(failure.value)
    assert "something broke deep inside" not in message
    assert "starlight-api-key" not in message
