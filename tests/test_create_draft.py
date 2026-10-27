"""create_draft: a new draft in the Drafts folder."""

from __future__ import annotations

from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from src.mcp import tools
from tests.conftest import jmap_calls, jmap_error

IDENTITIES = {
    "accountId": "account-1",
    "list": [
        {
            "id": "id-1",
            "email": "me@example.com",
            "name": "Me",
            "replyTo": [{"email": "reply@example.com", "name": None}],
            "isDefault": True,
        }
    ],
    "notFound": [],
}

MAILBOXES = {
    "accountId": "account-1",
    "list": [{"id": "mb-drafts", "name": "Drafts", "role": "drafts"}],
    "notFound": [],
}

CREATED = {"created": {"draft": {"id": "d1", "threadId": "t1"}}, "notCreated": {}}


async def test_it_creates_a_draft(mock_jmap: Any, bearer: Any) -> None:
    seen = mock_jmap(
        {
            "Identity/get": IDENTITIES,
            "Mailbox/get": MAILBOXES,
            "Email/set": CREATED,
        }
    )

    result = await tools.create_draft(to=["you@example.com"], subject="Hi", body="Hello")

    assert result == {"draft_id": "d1", "thread_id": "t1"}
    email = jmap_calls(seen, "Email/set")[0]["create"]["draft"]
    assert email["mailboxIds"] == {"mb-drafts": True}
    assert email["keywords"] == {"$draft": True}
    assert email["from"] == [{"email": "me@example.com", "name": "Me"}]
    assert email["to"] == [{"email": "you@example.com"}]
    assert email["subject"] == "Hi"
    assert email["textBody"] == {"value": "Hello"}
    assert "htmlBody" not in email
    assert "cc" not in email


async def test_it_takes_cc_bcc_and_html(mock_jmap: Any, bearer: Any) -> None:
    seen = mock_jmap(
        {
            "Identity/get": IDENTITIES,
            "Mailbox/get": MAILBOXES,
            "Email/set": CREATED,
        }
    )

    await tools.create_draft(
        to=["You <you@example.com>"],
        subject="Hi",
        body="Hello",
        cc=["cc@example.com"],
        bcc=["bcc@example.com"],
        body_html="<p>Hello</p>",
    )

    email = jmap_calls(seen, "Email/set")[0]["create"]["draft"]
    assert email["to"] == [{"email": "you@example.com", "name": "You"}]
    assert email["cc"] == [{"email": "cc@example.com"}]
    assert email["bcc"] == [{"email": "bcc@example.com"}]
    assert email["htmlBody"] == {"value": "<p>Hello</p>", "type": "text/html"}


async def test_it_attaches_the_uploaded_blobs(mock_jmap: Any, bearer: Any) -> None:
    seen = mock_jmap(
        {
            "Identity/get": IDENTITIES,
            "Mailbox/get": MAILBOXES,
            "Email/set": CREATED,
        }
    )

    await tools.create_draft(
        to=["you@example.com"],
        subject="Hi",
        body="Hello",
        attachments=[{"blob_id": "blob-1"}],
    )

    email = jmap_calls(seen, "Email/set")[0]["create"]["draft"]
    assert email["attachments"] == [{"blobId": "blob-1"}]


async def test_it_makes_a_reply(mock_jmap: Any, bearer: Any) -> None:
    seen = mock_jmap(
        {
            "Identity/get": IDENTITIES,
            "Mailbox/get": MAILBOXES,
            "Email/get": {
                "accountId": "account-1",
                "list": [
                    {
                        "id": "m1",
                        "messageId": "<orig@example.com>",
                        "references": ["<older@example.com>"],
                    }
                ],
                "notFound": [],
            },
            "Email/set": CREATED,
        }
    )

    await tools.create_draft(
        to=["you@example.com"], subject="Re: Hello", body="Answer", in_reply_to="m1"
    )

    email = jmap_calls(seen, "Email/set")[0]["create"]["draft"]
    assert email["inReplyTo"] == "<orig@example.com>"
    assert email["references"] == ["<older@example.com>", "<orig@example.com>"]
    assert email["subject"] == "Re: Hello"


async def test_it_prefixes_the_subject_of_a_reply(mock_jmap: Any, bearer: Any) -> None:
    seen = mock_jmap(
        {
            "Identity/get": IDENTITIES,
            "Mailbox/get": MAILBOXES,
            "Email/get": {
                "accountId": "account-1",
                "list": [{"id": "m1", "messageId": "<orig@example.com>"}],
                "notFound": [],
            },
            "Email/set": CREATED,
        }
    )

    await tools.create_draft(to=["you@example.com"], subject="Hello", body="Answer", in_reply_to="m1")

    email = jmap_calls(seen, "Email/set")[0]["create"]["draft"]
    assert email["subject"] == "Re: Hello"


async def test_it_uses_the_given_identity(mock_jmap: Any, bearer: Any) -> None:
    seen = mock_jmap(
        {
            "Identity/get": {
                "accountId": "account-1",
                "list": [
                    {"id": "id-1", "email": "me@example.com", "name": None, "isDefault": True},
                    {"id": "id-2", "email": "alias@example.com", "name": "Alias", "isDefault": False},
                ],
                "notFound": [],
            },
            "Mailbox/get": MAILBOXES,
            "Email/set": CREATED,
        }
    )

    await tools.create_draft(
        to=["you@example.com"], subject="Hi", body="Hello", from_identity_id="id-2"
    )

    email = jmap_calls(seen, "Email/set")[0]["create"]["draft"]
    assert email["from"] == [{"email": "alias@example.com", "name": "Alias"}]


async def test_it_refuses_an_unknown_identity(mock_jmap: Any, bearer: Any) -> None:
    mock_jmap({"Identity/get": IDENTITIES, "Mailbox/get": MAILBOXES, "Email/set": CREATED})

    with pytest.raises(ToolError) as failure:
        await tools.create_draft(
            to=["you@example.com"], subject="Hi", body="Hello", from_identity_id="id-9"
        )

    assert "list_identities" in str(failure.value)


async def test_it_refuses_a_recipient_that_is_not_an_address(mock_jmap: Any, bearer: Any) -> None:
    mock_jmap({"Identity/get": IDENTITIES, "Mailbox/get": MAILBOXES, "Email/set": CREATED})

    with pytest.raises(ToolError) as failure:
        await tools.create_draft(to=["not-an-address"], subject="Hi", body="Hello")

    assert "not an email address" in str(failure.value)


async def test_a_method_error_becomes_a_tool_error(mock_jmap: Any, bearer: Any) -> None:
    mock_jmap(
        {
            "Identity/get": IDENTITIES,
            "Mailbox/get": MAILBOXES,
            "Email/set": jmap_error("invalidArguments", "invalidArguments", "bad recipient"),
        }
    )

    with pytest.raises(ToolError) as failure:
        await tools.create_draft(to=["you@example.com"], subject="Hi", body="Hello")

    assert "invalidArguments" in str(failure.value)


async def test_a_refused_draft_becomes_a_tool_error(mock_jmap: Any, bearer: Any) -> None:
    mock_jmap(
        {
            "Identity/get": IDENTITIES,
            "Mailbox/get": MAILBOXES,
            "Email/set": {"created": {}, "notCreated": {"draft": {"type": "forbidden"}}},
        }
    )

    with pytest.raises(ToolError) as failure:
        await tools.create_draft(to=["you@example.com"], subject="Hi", body="Hello")

    assert "refused to create the draft" in str(failure.value)
