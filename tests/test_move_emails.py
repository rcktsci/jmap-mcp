"""move_emails: the folder of one or more messages."""

from __future__ import annotations

from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from src.mcp import tools
from tests.conftest import jmap_calls, jmap_error

MAILBOXES = {
    "accountId": "account-1",
    "list": [
        {"id": "mb-inbox", "name": "Inbox", "role": "inbox"},
        {"id": "mb-trash", "name": "Trash", "role": "trash"},
    ],
    "notFound": [],
}

EMAILS = {
    "accountId": "account-1",
    "list": [
        {"id": "m1", "mailboxIds": ["mb-inbox"]},
        {"id": "m2", "mailboxIds": ["mb-inbox", "mb-trash"]},
    ],
    "notFound": ["m3"],
}


async def test_it_moves_by_role(mock_jmap: Any, bearer: Any) -> None:
    seen = mock_jmap(
        {
            "Mailbox/get": MAILBOXES,
            "Email/get": EMAILS,
            "Email/set": {"updated": {"m1": None, "m2": None}, "notFound": [], "notUpdated": {}},
        }
    )

    result = await tools.move_emails(ids=["m1", "m2"], mailbox_role="trash")

    assert result["moved"] == ["m1", "m2"]
    update = jmap_calls(seen, "Email/set")[0]["update"]
    # The message leaves the folders it is in - mailboxIds is the whole set.
    # RFC 8621 §4.6: the patch syntax of RFC 8620 §5.3 - the new folder is
    # added, the old ones are removed.
    assert update["m1"] == {"mailboxIds/mb-trash": True, "mailboxIds/mb-inbox": None}
    assert update["m2"] == {"mailboxIds/mb-trash": True, "mailboxIds/mb-inbox": None}


async def test_a_message_that_does_not_exist_is_a_tool_error(mock_jmap: Any, bearer: Any) -> None:
    mock_jmap(
        {
            "Mailbox/get": MAILBOXES,
            "Email/get": EMAILS,
            "Email/set": {"updated": {"m1": None, "m2": None}, "notFound": ["m3"], "notUpdated": {}},
        }
    )

    with pytest.raises(ToolError) as failure:
        await tools.move_emails(ids=["m1", "m2", "m3"], mailbox_role="trash")

    message = str(failure.value)
    assert "m3" in message
    assert "do not exist" in message


async def test_it_moves_by_mailbox_id(mock_jmap: Any, bearer: Any) -> None:
    seen = mock_jmap(
        {
            "Email/get": EMAILS,
            "Email/set": {"updated": {"m1": None}, "notFound": [], "notUpdated": {}},
        }
    )

    result = await tools.move_emails(ids=["m1"], mailbox_id="mb-trash")

    assert result["moved"] == ["m1"]
    update = jmap_calls(seen, "Email/set")[0]["update"]
    assert update == {"m1": {"mailboxIds/mb-trash": True, "mailboxIds/mb-inbox": None}}


async def test_it_refuses_two_targets(mock_jmap: Any, bearer: Any) -> None:
    mock_jmap({"Mailbox/get": MAILBOXES, "Email/get": EMAILS, "Email/set": {}})

    with pytest.raises(ToolError) as failure:
        await tools.move_emails(ids=["m1"], mailbox_role="trash", mailbox_id="mb-trash")

    assert "not both" in str(failure.value)


async def test_it_refuses_a_call_without_a_target(mock_jmap: Any, bearer: Any) -> None:
    mock_jmap({"Mailbox/get": MAILBOXES, "Email/get": EMAILS, "Email/set": {}})

    with pytest.raises(ToolError) as failure:
        await tools.move_emails(ids=["m1"])

    assert "exactly one target is required" in str(failure.value)


async def test_a_null_target_is_not_a_target(mock_jmap: Any, bearer: Any) -> None:
    mock_jmap({"Mailbox/get": MAILBOXES, "Email/get": EMAILS, "Email/set": {}})

    with pytest.raises(ToolError) as failure:
        await tools.move_emails(ids=["m1"], mailbox_role=None, mailbox_id=None)

    assert "exactly one target is required" in str(failure.value)


async def test_a_method_error_becomes_a_tool_error(mock_jmap: Any, bearer: Any) -> None:
    mock_jmap(
        {
            "Mailbox/get": MAILBOXES,
            "Email/get": EMAILS,
            "Email/set": jmap_error("invalidArguments", "invalidArguments", "no such mailbox"),
        }
    )

    with pytest.raises(ToolError) as failure:
        await tools.move_emails(ids=["m1"], mailbox_role="trash")

    assert "invalidArguments" in str(failure.value)


async def test_a_refused_move_becomes_a_tool_error(mock_jmap: Any, bearer: Any) -> None:
    mock_jmap(
        {
            "Mailbox/get": MAILBOXES,
            "Email/get": EMAILS,
            "Email/set": {
                "updated": {},
                "notFound": [],
                "notUpdated": {"m1": {"type": "forbidden", "code": "notAllowed"}},
            },
        }
    )

    with pytest.raises(ToolError) as failure:
        await tools.move_emails(ids=["m1"], mailbox_role="trash")

    assert "m1" in str(failure.value)


async def test_it_refuses_a_call_without_ids(mock_jmap: Any, bearer: Any) -> None:
    mock_jmap({"Mailbox/get": MAILBOXES, "Email/get": EMAILS, "Email/set": {}})

    with pytest.raises(ToolError) as failure:
        await tools.move_emails(ids=[], mailbox_role="trash")

    assert "ids is required" in str(failure.value)
