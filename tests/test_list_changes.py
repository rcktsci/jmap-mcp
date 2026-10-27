"""The list_changes tool: what changed since a state token."""

from __future__ import annotations

import json
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from src.mcp import tools
from tests.conftest import jmap_error

CHANGES = {
    "accountId": "account-1",
    "oldState": "state-1",
    "newState": "state-2",
    "hasMoreChanges": False,
    "created": ["e3"],
    "updated": ["e2"],
    "destroyed": ["e1"],
}


@pytest.mark.asyncio
async def test_list_changes_returns_the_changes(mock_jmap: Any, bearer: Any) -> None:
    mock_jmap({"Email/changes": CHANGES})

    result = await tools.list_changes(since_state="state-1")

    assert result == {
        "created": ["e3"],
        "updated": ["e2"],
        "destroyed": ["e1"],
        "state": "state-2",
        "has_more_changes": False,
    }


@pytest.mark.asyncio
async def test_the_state_and_the_limit_are_sent_to_the_server(
    mock_jmap: Any, bearer: Any
) -> None:
    seen = mock_jmap({"Email/changes": CHANGES})

    await tools.list_changes(since_state="state-1", max_changes=100)

    body = json.loads(seen[1].content)
    assert body["methodCalls"][0][0] == "Email/changes"
    assert body["methodCalls"][0][1]["sinceState"] == "state-1"
    assert body["methodCalls"][0][1]["maxChanges"] == 100


@pytest.mark.asyncio
async def test_without_changes_the_lists_are_empty(mock_jmap: Any, bearer: Any) -> None:
    mock_jmap(
        {
            "Email/changes": {
                "accountId": "account-1",
                "oldState": "state-1",
                "newState": "state-1",
                "hasMoreChanges": False,
                "created": [],
                "updated": [],
                "destroyed": [],
            }
        }
    )

    result = await tools.list_changes(since_state="state-1")

    assert result == {
        "created": [],
        "updated": [],
        "destroyed": [],
        "state": "state-1",
        "has_more_changes": False,
    }


@pytest.mark.asyncio
async def test_more_changes_are_reported(mock_jmap: Any, bearer: Any) -> None:
    mock_jmap({"Email/changes": dict(CHANGES, hasMoreChanges=True)})

    result = await tools.list_changes(since_state="state-1")

    assert result["has_more_changes"] is True


@pytest.mark.asyncio
async def test_a_server_without_a_new_state_is_a_tool_error(
    mock_jmap: Any, bearer: Any
) -> None:
    mock_jmap({"Email/changes": dict(CHANGES, newState=None)})

    with pytest.raises(ToolError) as failure:
        await tools.list_changes(since_state="state-1")

    assert "delta sync" in str(failure.value)


@pytest.mark.asyncio
async def test_an_empty_state_is_a_tool_error(mock_jmap: Any, bearer: Any) -> None:
    mock_jmap({"Email/changes": CHANGES})

    with pytest.raises(ToolError) as failure:
        await tools.list_changes(since_state="  ")

    assert "since_state" in str(failure.value)


@pytest.mark.asyncio
async def test_list_changes_turns_a_method_error_into_a_tool_error(
    mock_jmap: Any, bearer: Any
) -> None:
    mock_jmap({"Email/changes": jmap_error("serverFail", "serverFail", "the changes died")})

    with pytest.raises(ToolError) as failure:
        await tools.list_changes(since_state="state-1")

    assert "serverFail" in str(failure.value)
