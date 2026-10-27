"""send_draft: a draft leaves the Drafts folder and goes out."""

from __future__ import annotations

from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from src.mcp import tools
from src.mcp.server import mcp_server
from tests.conftest import jmap_calls, jmap_error

DRAFT = {
    "accountId": "account-1",
    "list": [
        {
            "id": "d1",
            "messageId": "<draft@example.com>",
            "from": [{"email": "me@example.com", "name": None}],
            "to": [{"email": "you@example.com", "name": "You"}],
            "cc": [{"email": "cc@example.com", "name": None}],
            "bcc": [{"email": "bcc@example.com", "name": None}],
            "mailboxIds": ["mb-drafts"],
        }
    ],
    "notFound": [],
}

IDENTITIES = {
    "accountId": "account-1",
    "list": [{"id": "id-1", "email": "me@example.com", "name": None, "isDefault": True}],
    "notFound": [],
}

MAILBOXES = {
    "accountId": "account-1",
    "list": [{"id": "mb-sent", "name": "Sent", "role": "sent"}],
    "notFound": [],
}

SENT = {"created": {"send": {"id": "M1", "sendAt": "2026-10-27T10:00:00Z"}}, "notCreated": {}}


async def test_it_sends_the_draft(mock_jmap: Any, bearer: Any) -> None:
    seen = mock_jmap(
        {
            "Email/get": DRAFT,
            "Identity/get": IDENTITIES,
            "Mailbox/get": MAILBOXES,
            "EmailSubmission/set": SENT,
        }
    )

    result = await tools.send_draft(draft_id="d1")

    assert result["message_id"] == "<draft@example.com>"
    assert result["sent_at"].isoformat() == "2026-10-27T10:00:00+00:00"
    assert [address["email"] for address in result["recipients"]] == [
        "you@example.com",
        "cc@example.com",
        "bcc@example.com",
    ]
    args = jmap_calls(seen, "EmailSubmission/set")[0]
    assert args["create"] == {"send": {"identityId": "id-1", "emailId": "d1"}}
    assert args["onSuccessUpdateEmail"] == {"update": {"d1": {"mailboxIds": {"mb-sent": True}}}}


async def test_it_moves_the_draft_with_email_set_when_on_success_is_not_supported(
    mock_jmap: Any, bearer: Any
) -> None:
    seen = mock_jmap(
        {
            "Email/get": DRAFT,
            "Identity/get": IDENTITIES,
            "Mailbox/get": MAILBOXES,
            "EmailSubmission/set": [
                jmap_error(
                    "unknownArguments",
                    "unknownArguments",
                    "the argument onSuccessUpdateEmail is not supported",
                ),
                SENT,
            ],
            "Email/set": {"updated": {"d1": None}, "notFound": [], "notUpdated": {}},
        }
    )

    result = await tools.send_draft(draft_id="d1")

    assert result["message_id"] == "<draft@example.com>"
    submissions = jmap_calls(seen, "EmailSubmission/set")
    assert len(submissions) == 2
    assert "onSuccessUpdateEmail" not in submissions[1]
    update = jmap_calls(seen, "Email/set")[0]["update"]
    assert update == {"d1": {"mailboxIds": {"mb-sent": True, "mb-drafts": False}}}


async def test_it_refuses_an_unknown_draft(mock_jmap: Any, bearer: Any) -> None:
    mock_jmap(
        {
            "Email/get": {"accountId": "account-1", "list": [], "notFound": ["d1"]},
            "Identity/get": IDENTITIES,
            "Mailbox/get": MAILBOXES,
            "EmailSubmission/set": SENT,
        }
    )

    with pytest.raises(ToolError) as failure:
        await tools.send_draft(draft_id="d1")

    assert "no draft with the id" in str(failure.value)


async def test_it_refuses_an_empty_draft_id(mock_jmap: Any, bearer: Any) -> None:
    mock_jmap({"Email/get": DRAFT, "Identity/get": IDENTITIES, "Mailbox/get": MAILBOXES})

    with pytest.raises(ToolError) as failure:
        await tools.send_draft(draft_id="  ")

    assert "draft_id is required" in str(failure.value)


async def test_a_refused_submission_becomes_a_tool_error(mock_jmap: Any, bearer: Any) -> None:
    mock_jmap(
        {
            "Email/get": DRAFT,
            "Identity/get": IDENTITIES,
            "Mailbox/get": MAILBOXES,
            "EmailSubmission/set": {"created": {}, "notCreated": {"send": {"type": "forbidden"}}},
        }
    )

    with pytest.raises(ToolError) as failure:
        await tools.send_draft(draft_id="d1")

    assert "refused to send the draft" in str(failure.value)


async def test_a_method_error_becomes_a_tool_error(mock_jmap: Any, bearer: Any) -> None:
    mock_jmap(
        {
            "Email/get": DRAFT,
            "Identity/get": IDENTITIES,
            "Mailbox/get": MAILBOXES,
            "EmailSubmission/set": jmap_error("unknownMethod", "unknownMethod", "no submission"),
        }
    )

    with pytest.raises(ToolError) as failure:
        await tools.send_draft(draft_id="d1")

    assert "unknownMethod" in str(failure.value)


async def test_the_sdk_validates_the_answer(mock_jmap: Any, bearer: Any) -> None:
    mock_jmap(
        {
            "Email/get": DRAFT,
            "Identity/get": IDENTITIES,
            "Mailbox/get": MAILBOXES,
            "EmailSubmission/set": SENT,
        }
    )

    result = await mcp_server.call_tool("send_draft", {"draft_id": "d1"})

    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["message_id"] == "<draft@example.com>"
    assert result.structured_content["sent_at"] == "2026-10-27T10:00:00Z"
