"""The get_email tool: one message with its body."""

from __future__ import annotations

import json
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from mcp.server.mcpserver.exceptions import ToolError

from src.mcp import tools
from tests.conftest import jmap_error, load_published_schema

EMAIL = {
    "id": "e1",
    "threadId": "t1",
    "subject": "Report",
    "from": [{"email": "boss@example.com", "name": "Boss"}],
    "to": [{"email": "me@example.com", "name": None}],
    "cc": [{"email": "team@example.com", "name": None}],
    "bcc": [],
    "replyTo": [{"email": "reply@example.com", "name": None}],
    "receivedAt": "2026-10-20T10:00:00Z",
    "messageId": "<e1@example.com>",
    "inReplyTo": "<e0@example.com>",
    "preview": "Numbers",
    "keywords": ["$seen", "$flagged"],
    "attachments": [
        {"blobId": "b1", "name": "report.pdf", "type": "application/pdf", "size": 1234},
    ],
    "textBody": {"partId": "1", "value": "The body of the message.", "isTruncated": False},
    "bodyValues": {"1": {"value": "The body of the message.", "isTruncated": False}},
}


@pytest.mark.asyncio
async def test_get_email_returns_the_message(mock_jmap: Any, bearer: Any) -> None:
    mock_jmap({"Email/get": {"accountId": "account-1", "list": [EMAIL], "notFound": []}})

    result = await tools.get_email(email_id="e1")

    assert result["id"] == "e1"
    assert result["thread_id"] == "t1"
    assert result["subject"] == "Report"
    assert result["from"] == [{"email": "boss@example.com", "name": "Boss"}]
    assert result["cc"] == [{"email": "team@example.com", "name": None}]
    assert result["bcc"] == []
    assert result["reply_to"] == [{"email": "reply@example.com", "name": None}]
    assert result["message_id"] == "<e1@example.com>"
    assert result["in_reply_to"] == "<e0@example.com>"
    assert result["text_body"] == "The body of the message."
    assert result["truncated"] is False
    assert result["keywords"] == ["$seen", "$flagged"]
    assert result["attachments"] == [
        {
            "blob_id": "b1",
            "name": "report.pdf",
            "type": "application/pdf",
            "size": 1234,
            "is_inline": False,
        }
    ]


@pytest.mark.asyncio
async def test_the_answer_validates_against_the_output_schema(
    mock_jmap: Any, bearer: Any
) -> None:
    mock_jmap({"Email/get": {"accountId": "account-1", "list": [EMAIL], "notFound": []}})

    result = await tools.get_email(email_id="e1")

    schema = load_published_schema("get_email", "output")
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(json.loads(json.dumps(result, default=str)))


@pytest.mark.asyncio
async def test_the_body_is_taken_from_the_body_values_when_the_text_part_is_empty(
    mock_jmap: Any, bearer: Any
) -> None:
    email = dict(EMAIL, textBody={"partId": "1", "value": "", "isTruncated": False})
    mock_jmap({"Email/get": {"accountId": "account-1", "list": [email], "notFound": []}})

    result = await tools.get_email(email_id="e1")

    assert result["text_body"] == "The body of the message."


@pytest.mark.asyncio
async def test_an_html_body_becomes_text(mock_jmap: Any, bearer: Any) -> None:
    email = dict(
        EMAIL,
        textBody=None,
        htmlBody={
            "partId": "2",
            "value": "<html><style>p{color:red}</style><body><p>Hello<br>world</p></body></html>",
            "isTruncated": False,
        },
    )
    mock_jmap({"Email/get": {"accountId": "account-1", "list": [email], "notFound": []}})

    result = await tools.get_email(email_id="e1")

    assert result["text_body"] is not None
    assert "Hello" in result["text_body"]
    assert "world" in result["text_body"]
    assert "<p>" not in result["text_body"]
    assert "color:red" not in result["text_body"]


@pytest.mark.asyncio
async def test_a_long_body_is_cut_and_says_so(mock_jmap: Any, bearer: Any) -> None:
    email = dict(EMAIL, textBody={"partId": "1", "value": "x" * 500, "isTruncated": False})
    mock_jmap({"Email/get": {"accountId": "account-1", "list": [email], "notFound": []}})

    result = await tools.get_email(email_id="e1", max_body_chars=200)

    assert result["truncated"] is True
    assert result["text_body"] == "x" * 200


@pytest.mark.asyncio
async def test_an_unknown_id_is_a_tool_error(mock_jmap: Any, bearer: Any) -> None:
    mock_jmap({"Email/get": {"accountId": "account-1", "list": [], "notFound": ["e9"]}})

    with pytest.raises(ToolError) as failure:
        await tools.get_email(email_id="e9")

    assert "e9" in str(failure.value)


@pytest.mark.asyncio
async def test_get_email_turns_a_method_error_into_a_tool_error(
    mock_jmap: Any, bearer: Any
) -> None:
    mock_jmap({"Email/get": jmap_error("serverFail", "serverFail", "the message died")})

    with pytest.raises(ToolError) as failure:
        await tools.get_email(email_id="e1")

    assert "serverFail" in str(failure.value)
