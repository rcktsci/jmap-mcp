"""The get_thread tool: the messages of a conversation."""

from __future__ import annotations

import json
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from src.mcp import tools
from tests.conftest import jmap_error

THREAD = {
    "accountId": "account-1",
    "list": [
        {
            "id": "t1",
            "name": "Report",
            "emailIds": ["e1", "e2"],
            "totalEmails": 2,
        }
    ],
    "notFound": [],
}

EMAILS = {
    "accountId": "account-1",
    "list": [
        {
            "id": "e2",
            "threadId": "t1",
            "subject": "Re: Report",
            "from": [{"email": "me@example.com", "name": None}],
            "to": [{"email": "boss@example.com", "name": None}],
            "receivedAt": "2026-10-21T10:00:00Z",
            "preview": "And the second part",
            "keywords": [],
            "attachments": [],
            "textBody": {"partId": "1", "value": "And the second part", "isTruncated": False},
        },
        {
            "id": "e1",
            "threadId": "t1",
            "subject": "Report",
            "from": [{"email": "boss@example.com", "name": None}],
            "to": [{"email": "me@example.com", "name": None}],
            "receivedAt": "2026-10-20T10:00:00Z",
            "preview": "The first part",
            "keywords": [],
            "attachments": [],
            "textBody": {"partId": "1", "value": "The first part", "isTruncated": False},
        },
    ],
    "notFound": [],
}


@pytest.mark.asyncio
async def test_get_thread_returns_the_messages_oldest_first(
    mock_jmap: Any, bearer: Any
) -> None:
    mock_jmap({"Thread/get": THREAD, "Email/get": EMAILS})

    result = await tools.get_thread(thread_id="t1")

    assert result["thread_id"] == "t1"
    assert result["subject"] == "Report"
    assert result["message_count"] == 2
    assert [email["id"] for email in result["emails"]] == ["e1", "e2"]


@pytest.mark.asyncio
async def test_the_bodies_are_not_read_by_default(mock_jmap: Any, bearer: Any) -> None:
    seen = mock_jmap({"Thread/get": THREAD, "Email/get": EMAILS})

    result = await tools.get_thread(thread_id="t1")

    body = json.loads(seen[2].content)
    assert "textBody" not in body["methodCalls"][0][1]["properties"]
    assert all(email["text_body"] is None for email in result["emails"])
    assert all(email["truncated"] is False for email in result["emails"])


@pytest.mark.asyncio
async def test_the_bodies_are_read_on_request(mock_jmap: Any, bearer: Any) -> None:
    seen = mock_jmap({"Thread/get": THREAD, "Email/get": EMAILS})

    result = await tools.get_thread(thread_id="t1", include_bodies=True)

    body = json.loads(seen[2].content)
    assert "textBody" in body["methodCalls"][0][1]["properties"]
    assert body["methodCalls"][0][1]["bodyProperties"]["maxLength"] == 4000
    assert result["emails"][0]["text_body"] == "The first part"


@pytest.mark.asyncio
async def test_the_newest_messages_are_kept_when_the_thread_is_longer(
    mock_jmap: Any, bearer: Any
) -> None:
    mock_jmap(
        {
            "Thread/get": {
                "accountId": "account-1",
                "list": [
                    {
                        "id": "t1",
                        "name": "Report",
                        "emailIds": ["e1", "e2", "e3"],
                        "totalEmails": 3,
                    }
                ],
                "notFound": [],
            },
            "Email/get": {
                "accountId": "account-1",
                "list": [EMAILS["list"][0], EMAILS["list"][1]],
                "notFound": [],
            },
        }
    )

    result = await tools.get_thread(thread_id="t1", max_messages=2)

    assert result["message_count"] == 3
    assert len(result["emails"]) == 2


@pytest.mark.asyncio
async def test_an_empty_conversation_is_an_empty_answer(mock_jmap: Any, bearer: Any) -> None:
    mock_jmap(
        {
            "Thread/get": {
                "accountId": "account-1",
                "list": [{"id": "t1", "name": "Report", "emailIds": [], "totalEmails": 0}],
                "notFound": [],
            }
        }
    )

    result = await tools.get_thread(thread_id="t1")

    assert result == {"thread_id": "t1", "subject": "Report", "message_count": 0, "emails": []}


@pytest.mark.asyncio
async def test_a_server_without_thread_get_falls_back_to_the_email_query(
    mock_jmap: Any, bearer: Any
) -> None:
    seen = mock_jmap(
        {
            "Thread/get": jmap_error(
                "unknownMethod", "unknownMethod", "The method Thread/get is not supported"
            ),
            "Email/query": {"accountId": "account-1", "ids": ["e2", "e1"], "total": 2, "position": 0},
            "Email/get": EMAILS,
        }
    )

    result = await tools.get_thread(thread_id="t1")

    body = json.loads(seen[2].content)
    assert body["methodCalls"][0][0] == "Email/query"
    assert body["methodCalls"][0][1]["filter"] == {"inThread": "t1"}
    assert result["message_count"] == 2
    assert [email["id"] for email in result["emails"]] == ["e1", "e2"]
    assert result["subject"] == "Report"


@pytest.mark.asyncio
async def test_an_unknown_conversation_is_a_tool_error(mock_jmap: Any, bearer: Any) -> None:
    mock_jmap({"Thread/get": {"accountId": "account-1", "list": [], "notFound": ["t9"]}})

    with pytest.raises(ToolError) as failure:
        await tools.get_thread(thread_id="t9")

    assert "t9" in str(failure.value)


@pytest.mark.asyncio
async def test_get_thread_turns_a_method_error_into_a_tool_error(
    mock_jmap: Any, bearer: Any
) -> None:
    mock_jmap({"Thread/get": jmap_error("serverFail", "serverFail", "the thread died")})

    with pytest.raises(ToolError) as failure:
        await tools.get_thread(thread_id="t1")

    assert "serverFail" in str(failure.value)
