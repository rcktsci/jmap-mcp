"""The list_identities tool: the sender identities of the account."""

from __future__ import annotations

from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from src.mcp import tools
from tests.conftest import jmap_error

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
        {"id": "id-2", "name": None, "email": "alias@example.com", "replyTo": []},
    ],
    "notFound": [],
}


@pytest.mark.asyncio
async def test_list_identities_returns_the_identities(mock_jmap: Any, bearer: Any) -> None:
    mock_jmap({"Identity/get": IDENTITIES})

    result = await tools.list_identities()

    assert result == {
        "identities": [
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
    }


@pytest.mark.asyncio
async def test_list_identities_without_identities_is_an_empty_answer(
    mock_jmap: Any, bearer: Any
) -> None:
    mock_jmap({"Identity/get": {"accountId": "account-1", "list": [], "notFound": []}})

    result = await tools.list_identities()

    assert result == {"identities": []}


@pytest.mark.asyncio
async def test_list_identities_marks_the_first_identity_as_the_default(
    mock_jmap: Any, bearer: Any
) -> None:
    mock_jmap(
        {
            "Identity/get": {
                "accountId": "account-1",
                "list": [{"id": "id-1", "email": "me@example.com"}],
                "notFound": [],
            }
        }
    )

    result = await tools.list_identities()

    assert result["identities"][0]["is_default"] is True


@pytest.mark.asyncio
async def test_list_identities_turns_a_method_error_into_a_tool_error(
    mock_jmap: Any, bearer: Any
) -> None:
    mock_jmap(
        {
            "Identity/get": jmap_error(
                "serverFail", "serverFail", "the identity store is not available"
            )
        }
    )

    with pytest.raises(ToolError) as failure:
        await tools.list_identities()

    message = str(failure.value)
    assert "serverFail" in message
    assert "identity" in message.lower()


@pytest.mark.asyncio
async def test_list_identities_without_a_bearer_is_a_tool_error() -> None:
    with pytest.raises(ToolError) as failure:
        await tools.list_identities()

    assert "authorization" in str(failure.value)
