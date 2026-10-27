"""The count_emails tool: the number of messages that match a filter."""

from __future__ import annotations

import json
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from src.mcp import tools
from tests.conftest import jmap_error


@pytest.mark.asyncio
async def test_count_emails_returns_the_total(mock_jmap: Any, bearer: Any) -> None:
    mock_jmap({"Email/query": {"accountId": "account-1", "ids": ["e1"], "total": 42, "position": 0}})

    result = await tools.count_emails()

    assert result == {"count": 42}


@pytest.mark.asyncio
async def test_count_emails_asks_for_one_message_and_the_total(
    mock_jmap: Any, bearer: Any
) -> None:
    seen = mock_jmap({"Email/query": {"accountId": "account-1", "ids": [], "total": 0}})

    await tools.count_emails()

    body = json.loads(seen[1].content)
    query = body["methodCalls"][0][1]
    assert query["limit"] == 1
    assert query["calculateTotal"] is True
    assert query["position"] == 0
    assert len(body["methodCalls"]) == 1  # no Email/get: the counter loads no message


@pytest.mark.asyncio
async def test_count_emails_uses_the_same_filters_as_the_search(
    mock_jmap: Any, bearer: Any
) -> None:
    seen = mock_jmap({"Email/query": {"accountId": "account-1", "ids": [], "total": 0}})

    await tools.count_emails(text="report", unread=True, mailbox_id="mb-1")

    body = json.loads(seen[1].content)
    assert body["methodCalls"][0][1]["filter"] == {
        "operator": "and",
        "conditions": [{"inMailbox": "mb-1"}, {"notKeyword": "$seen"}, {"text": "report"}],
    }


@pytest.mark.asyncio
async def test_count_emails_without_matches_is_zero(mock_jmap: Any, bearer: Any) -> None:
    mock_jmap({"Email/query": {"accountId": "account-1", "ids": [], "total": 0}})

    result = await tools.count_emails()

    assert result == {"count": 0}


@pytest.mark.asyncio
async def test_a_server_without_a_total_is_a_tool_error(mock_jmap: Any, bearer: Any) -> None:
    mock_jmap({"Email/query": {"accountId": "account-1", "ids": ["e1"], "position": 0}})

    with pytest.raises(ToolError) as failure:
        await tools.count_emails()

    assert "calculateTotal" in str(failure.value)


@pytest.mark.asyncio
async def test_count_emails_turns_a_method_error_into_a_tool_error(
    mock_jmap: Any, bearer: Any
) -> None:
    mock_jmap({"Email/query": jmap_error("serverFail", "serverFail", "the query died")})

    with pytest.raises(ToolError) as failure:
        await tools.count_emails()

    assert "serverFail" in str(failure.value)
