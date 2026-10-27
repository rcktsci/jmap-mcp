"""The list_mailboxes tool: the folders of the account."""

from __future__ import annotations

import json
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from src.mcp import tools
from tests.conftest import jmap_error

MAILBOXES = {
    "accountId": "account-1",
    "list": [
        {
            "id": "mb-inbox",
            "name": "Inbox",
            "role": "inbox",
            "parentId": None,
            "sortOrder": 1,
            "totalEmails": 12,
            "unreadEmails": 3,
        },
        {
            "id": "mb-custom",
            "name": "Projects",
            "role": "Projects",
            "parentId": "mb-inbox",
            "sortOrder": 2,
        },
    ],
    "notFound": [],
}


@pytest.mark.asyncio
async def test_list_mailboxes_returns_the_folders(mock_jmap: Any, bearer: Any) -> None:
    mock_jmap({"Mailbox/get": MAILBOXES})

    result = await tools.list_mailboxes()

    assert result["mailboxes"] == [
        {
            "id": "mb-inbox",
            "name": "Inbox",
            "role": "inbox",
            "parent_id": None,
            "total_emails": 12,
            "unread_emails": 3,
            "sort_order": 1,
        },
        {
            "id": "mb-custom",
            "name": "Projects",
            "role": "other",
            "parent_id": "mb-inbox",
            "total_emails": 0,
            "unread_emails": 0,
            "sort_order": 2,
        },
    ]


@pytest.mark.asyncio
async def test_list_mailboxes_asks_for_the_counters(mock_jmap: Any, bearer: Any) -> None:
    seen = mock_jmap({"Mailbox/get": MAILBOXES})

    await tools.list_mailboxes()

    body = json.loads(seen[1].content)
    assert body["methodCalls"][0][0] == "Mailbox/get"
    assert body["methodCalls"][0][1]["properties"] == [
        "id",
        "name",
        "role",
        "parentId",
        "sortOrder",
        "totalEmails",
        "unreadEmails",
    ]


@pytest.mark.asyncio
async def test_list_mailboxes_without_folders_is_an_empty_answer(
    mock_jmap: Any, bearer: Any
) -> None:
    mock_jmap({"Mailbox/get": {"accountId": "account-1", "list": [], "notFound": []}})

    result = await tools.list_mailboxes()

    assert result == {"mailboxes": []}


@pytest.mark.asyncio
async def test_list_mailboxes_asks_again_when_the_counters_are_refused(
    mock_jmap: Any, bearer: Any
) -> None:
    seen = mock_jmap(
        {
            "Mailbox/get": [
                jmap_error("invalidProperties", "invalidProperties", "totalEmails is unknown"),
                MAILBOXES,
            ]
        }
    )

    result = await tools.list_mailboxes()

    assert len(seen) == 3  # the session, the refused call, the call without the counters
    assert result["mailboxes"][0]["total_emails"] == 12


@pytest.mark.asyncio
async def test_list_mailboxes_turns_a_method_error_into_a_tool_error(
    mock_jmap: Any, bearer: Any
) -> None:
    mock_jmap({"Mailbox/get": jmap_error("serverFail", "serverFail", "no folders today")})

    with pytest.raises(ToolError) as failure:
        await tools.list_mailboxes()

    assert "serverFail" in str(failure.value)
