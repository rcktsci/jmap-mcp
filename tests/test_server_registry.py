"""The published tools: all fifteen of them and their schemas."""

from __future__ import annotations

from typing import Any

import pytest

from src.mcp.server import INSTRUCTIONS, mcp_server
from tests.conftest import load_published_schema

TOOLS = [
    "whoami",
    "list_identities",
    "list_mailboxes",
    "list_emails",
    "search_emails",
    "count_emails",
    "get_email",
    "get_thread",
    "set_flags",
    "move_emails",
    "create_draft",
    "send_draft",
    "list_changes",
    "download_attachment",
    "upload_attachment",
]


@pytest.mark.asyncio
async def test_the_fifteen_tools_are_published() -> None:
    published = await mcp_server.list_tools()

    assert [tool.name for tool in published] == TOOLS


@pytest.mark.asyncio
async def test_every_tool_has_a_description() -> None:
    published = await mcp_server.list_tools()

    assert all(tool.description and tool.description.strip() for tool in published)


@pytest.mark.asyncio
async def test_the_published_schemas_match_the_documentation() -> None:
    published = await mcp_server.list_tools()

    for tool in published:
        assert tool.input_schema == load_published_schema(tool.name, "input")
        assert tool.output_schema == load_published_schema(tool.name, "output")


def test_the_instructions_teach_the_agent_the_rules() -> None:
    for rule in ("whoami", "list_mailboxes", "count_emails", "get_email", "untrusted"):
        assert rule in INSTRUCTIONS


@pytest.mark.asyncio
async def test_the_sdk_validates_the_answer_of_a_tool(mock_jmap: Any, bearer: Any) -> None:
    mock_jmap(
        {
            "Mailbox/get": {
                "accountId": "account-1",
                "list": [
                    {
                        "id": "mb-inbox",
                        "name": "Inbox",
                        "role": "inbox",
                        "parentId": None,
                        "sortOrder": 1,
                        "totalEmails": 3,
                        "unreadEmails": 1,
                    }
                ],
                "notFound": [],
            }
        }
    )

    result = await mcp_server.call_tool("list_mailboxes", {})

    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["mailboxes"][0]["id"] == "mb-inbox"
