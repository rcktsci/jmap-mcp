"""The search_emails tool: the messages that match a filter."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from src.mcp import tools
from tests.conftest import jmap_error

QUERY = {"accountId": "account-1", "ids": ["e1"], "total": 1, "position": 0}

EMAILS = {
    "accountId": "account-1",
    "list": [
        {
            "id": "e1",
            "threadId": "t1",
            "subject": "Report",
            "from": [{"email": "boss@example.com", "name": None}],
            "to": [{"email": "me@example.com", "name": None}],
            "receivedAt": "2026-10-20T10:00:00Z",
            "preview": "Numbers",
            "keywords": [],
            "attachments": [],
        }
    ],
    "notFound": [],
}


@pytest.mark.asyncio
async def test_search_emails_returns_the_summaries(mock_jmap: Any, bearer: Any) -> None:
    mock_jmap({"Email/query": QUERY, "Email/get": EMAILS})

    result = await tools.search_emails(text="report")

    assert result["total"] == 1
    assert result["emails"][0]["id"] == "e1"


@pytest.mark.asyncio
async def test_the_text_search_becomes_a_text_condition(
    mock_jmap: Any, bearer: Any
) -> None:
    seen = mock_jmap({"Email/query": QUERY, "Email/get": EMAILS})

    await tools.search_emails(text="report")

    body = json.loads(seen[1].content)
    assert body["methodCalls"][0][1]["filter"] == {"text": "report"}


@pytest.mark.asyncio
async def test_the_header_searches_become_header_conditions(
    mock_jmap: Any, bearer: Any
) -> None:
    seen = mock_jmap({"Email/query": QUERY, "Email/get": EMAILS})

    await tools.search_emails(subject="Report", from_address="boss", to_address="me")

    body = json.loads(seen[1].content)
    assert body["methodCalls"][0][1]["filter"] == {
        "operator": "and",
        "conditions": [
            {"header": ["subject", "Report"]},
            {"header": ["from", "boss"]},
            {"header": ["to", "me"]},
        ],
    }


@pytest.mark.asyncio
async def test_the_dates_become_conditions(mock_jmap: Any, bearer: Any) -> None:
    seen = mock_jmap({"Email/query": QUERY, "Email/get": EMAILS})

    await tools.search_emails(after=datetime(2026, 10, 1, tzinfo=timezone.utc))

    body = json.loads(seen[1].content)
    assert body["methodCalls"][0][1]["filter"] == {"after": "2026-10-01T00:00:00+00:00"}


@pytest.mark.asyncio
async def test_search_emails_without_matches_is_an_empty_page(
    mock_jmap: Any, bearer: Any
) -> None:
    mock_jmap({"Email/query": {"accountId": "account-1", "ids": [], "total": 0, "position": 0}})

    result = await tools.search_emails(text="nothing")

    assert result == {"emails": [], "total": 0, "has_more": False, "position": 0}


@pytest.mark.asyncio
async def test_a_server_without_a_full_text_index_is_an_explicit_error(
    mock_jmap: Any, bearer: Any
) -> None:
    mock_jmap(
        {
            "Email/query": jmap_error(
                "invalidFilter", "invalidFilter", "the text filter is not supported"
            )
        }
    )

    with pytest.raises(ToolError) as failure:
        await tools.search_emails(text="report")

    message = str(failure.value)
    assert "full-text" in message
    assert "subject" in message


@pytest.mark.asyncio
async def test_search_emails_turns_a_method_error_into_a_tool_error(
    mock_jmap: Any, bearer: Any
) -> None:
    mock_jmap({"Email/query": jmap_error("serverFail", "serverFail", "the query died")})

    with pytest.raises(ToolError) as failure:
        await tools.search_emails(subject="Report")

    assert "serverFail" in str(failure.value)
