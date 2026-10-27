"""Tests of the live probe (tools/live_check.py) against a mock Stalwart.

The probe itself talks to a live server; these tests give it a mock upstream
so that the shape of the report, the verdicts and the promise that the bearer
never reaches the output are checked without a network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from src.mcp.jmap import JmapClient
from tools.live_check import (
    CHECKS,
    ERROR,
    OK,
    SKIPPED,
    UNSUPPORTED,
    CheckResult,
    Options,
    Probe,
    build_settings,
    check_10_body,
    check_12_state,
    main,
    print_summary,
    print_table,
    run_checks,
)

BARE = "API_live-check-key"

SESSION_DOCUMENT: dict[str, Any] = {
    "capabilities": {
        "urn:ietf:params:jmap:core": {},
        "urn:ietf:params:jmap:mail": {},
        "urn:ietf:params:jmap:submission": {},
    },
    "accounts": {"account-1": {"name": "me", "isPersonal": True}},
    "primaryAccounts": {"urn:ietf:params:jmap:mail": "account-1"},
    "username": "me@example.com",
    "apiUrl": "https://mail.example.com/jmap/api",
    "downloadUrl": "https://mail.example.com/download/{accountId}/{blobId}/{name}",
    "uploadUrl": "https://mail.example.com/upload/{accountId}",
    "state": "session-state",
}

ANSWERS: dict[str, Any] = {
    "Email/query": {"ids": ["e1", "e2"], "total": 2},
    "Email/get": {
        "list": [
            {
                "id": "e1",
                "threadId": "t1",
                "keywords": ["$seen", "$flagged"],
                "subject": "hello",
                "textBody": {"value": "hello body"},
            }
        ]
    },
    "Thread/get": {"list": [{"name": "hello", "emailIds": ["e1"], "totalEmails": 1}]},
    "Mailbox/get": {
        "state": "mbx-state-1",
        "list": [
            {
                "id": "m1",
                "name": "Inbox",
                "role": "inbox",
                "totalEmails": 2,
                "unreadEmails": 1,
            },
            {"id": "m2", "name": "Sent", "role": "sent", "totalEmails": 1, "unreadEmails": 0},
        ]
    },
    "Identity/get": {
        "list": [
            {"id": "i1", "email": "me@example.com", "name": "Me", "replyTo": [], "isDefault": True}
        ]
    },
    "Email/changes": {"newState": "state-2", "created": [], "updated": [], "destroyed": []},
    "Email/set": {"updated": {"e1": None}},
    "EmailSubmission/set": {"notCreated": {"send": {"type": "notFound"}}},
}


def make_handler(
    answers: dict[str, Any] | None = None,
    session: dict[str, Any] | None = None,
    envelope_state: str | None = "state-2",
) -> Callable[[httpx.Request], httpx.Response]:
    """A mock Stalwart: the session, the JMAP methods and the blob endpoints."""
    answers = answers if answers is not None else ANSWERS
    session = session if session is not None else SESSION_DOCUMENT

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET":
            if "/download" in path:
                return httpx.Response(404, text="no such blob")
            return httpx.Response(200, json=session)
        if request.method == "POST" and "/upload" in path:
            if request.headers.get("authorization") != f"Bearer {BARE}":
                return httpx.Response(401, text="unauthorized")
            return httpx.Response(200, json={"blobId": "blob-1"})
        body = json.loads(request.content)
        responses: list[list[Any]] = []
        for name, _args, call_id in body.get("methodCalls", []):
            answer = answers.get(name)
            if answer is None:
                responses.append(
                    [
                        "error",
                        {
                            "type": "urn:ietf:params:jmap:error:unknownMethod",
                            "code": "unknownMethod",
                            "description": f"The method {name} is not supported",
                        },
                        call_id,
                    ]
                )
            else:
                responses.append([name, answer, call_id])
        return httpx.Response(
            200,
            json={"methodResponses": responses, "sessionState": envelope_state},
        )

    return handler


def make_probe(
    answers: dict[str, Any] | None = None,
    session: dict[str, Any] | None = None,
    envelope_state: str | None = "state-2",
    **options: Any,
) -> Probe:
    """A probe that talks to the mock upstream."""
    transport = httpx.MockTransport(make_handler(answers, session, envelope_state))
    settings = build_settings("https://mail.example.com", "/jmap/session", 5)
    client = JmapClient(settings, transport=transport)
    probe = Probe(
        client=client,
        bearer=BARE,
        timeout=5,
        options=Options(transport=transport, **options),
    )
    probe.session_response = httpx.Response(200, json=session or SESSION_DOCUMENT)
    probe.raw_session = session or SESSION_DOCUMENT
    return probe


async def test_every_check_prints_a_row() -> None:
    """All 16 checks run and end with a verdict of the allowed set."""
    probe = make_probe()
    results = await run_checks(probe)

    assert len(CHECKS) == 16
    assert [result.number for result in results] == list(range(1, 17))
    for result in results:
        assert result.verdict in (OK, UNSUPPORTED, ERROR, SKIPPED)
        assert result.evidence
    assert not [result for result in results if result.verdict == ERROR]


async def test_the_bearer_never_reaches_the_output(capsys: pytest.CaptureFixture[str]) -> None:
    """Not one row of the report carries the key."""
    probe = make_probe()
    results = await run_checks(probe)

    print_table(results)
    print_summary(results)
    printed = capsys.readouterr().out

    assert BARE not in printed
    for result in results:
        assert BARE not in result.evidence


async def test_a_server_without_thread_get_is_unsupported() -> None:
    """A refusal of the server is a verdict, not a crash."""
    answers = {name: value for name, value in ANSWERS.items() if name != "Thread/get"}
    probe = make_probe(answers)
    results = await run_checks(probe)

    thread = results[1]
    assert thread.verdict == UNSUPPORTED
    assert "Thread/get" in thread.evidence


async def test_a_broken_session_fails_the_first_check_only() -> None:
    """Without a mail account the probe reports the error and skips the rest."""
    session = dict(SESSION_DOCUMENT)
    session["primaryAccounts"] = {}
    probe = make_probe(session=session)
    results = await run_checks(probe)

    assert results[0].verdict == ERROR
    assert all(result.verdict == SKIPPED for result in results[1:])


async def test_a_server_without_state_is_unsupported() -> None:
    """No state at all means the delta sync is not available."""
    session = dict(SESSION_DOCUMENT)
    session.pop("state")
    answers = {**ANSWERS}
    answers["Mailbox/get"] = {name: value for name, value in ANSWERS["Mailbox/get"].items() if name != "state"}
    probe = make_probe(answers, session, envelope_state=None)
    results = await run_checks(probe)

    assert results[11].verdict == UNSUPPORTED
    assert "no account state" in results[11].evidence


async def test_a_state_only_in_the_answers_is_enough() -> None:
    """The session may carry no state while the answers carry one."""
    session = dict(SESSION_DOCUMENT)
    session.pop("state")
    probe = make_probe(session=session)
    results = await run_checks(probe)

    assert results[11].verdict == OK
    assert "session.state=—" in results[11].evidence
    assert "mailbox.state=mbx-state-1" in results[11].evidence


async def test_check_10_sends_body_properties_as_a_list() -> None:
    """RFC 8621 §4.2: bodyProperties is String[], not an object."""
    bodies: list[dict[str, Any]] = []
    transport = httpx.MockTransport(make_handler())
    inner_handler = transport.handler

    def recording_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            bodies.append(json.loads(request.content))
        return inner_handler(request)

    transport = httpx.MockTransport(recording_handler)
    settings = build_settings("https://mail.example.com", "/jmap/session", 5)
    client = JmapClient(settings, transport=transport)
    probe = Probe(client=client, bearer=BARE, timeout=5, options=Options(transport=transport))
    probe.session_response = httpx.Response(200, json=SESSION_DOCUMENT)
    probe.raw_session = SESSION_DOCUMENT
    probe.session = await probe.client.session(BARE)

    verdict, evidence = await check_10_body(probe)

    assert verdict == OK
    requests = [
        args
        for body in bodies
        for name, args, _call_id in body.get("methodCalls", [])
        if name == "Email/get" and "bodyProperties" in args
    ]
    assert requests, "check 10 must send Email/get with bodyProperties"
    body_properties = requests[0]["bodyProperties"]
    assert isinstance(body_properties, list)
    assert all(isinstance(item, str) for item in body_properties)
    assert "textBody=" in evidence


async def test_check_12_feeds_email_changes_the_account_state() -> None:
    """Email/changes wants the account state of Mailbox/get, not the session state."""
    bodies: list[dict[str, Any]] = []
    transport = httpx.MockTransport(make_handler())
    inner_handler = transport.handler

    def recording_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            bodies.append(json.loads(request.content))
        return inner_handler(request)

    transport = httpx.MockTransport(recording_handler)
    settings = build_settings("https://mail.example.com", "/jmap/session", 5)
    client = JmapClient(settings, transport=transport)
    probe = Probe(client=client, bearer=BARE, timeout=5, options=Options(transport=transport))
    probe.session_response = httpx.Response(200, json=SESSION_DOCUMENT)
    probe.raw_session = SESSION_DOCUMENT
    probe.session = await probe.client.session(BARE)

    verdict, evidence = await check_12_state(probe)

    assert verdict == OK
    changes = [
        args
        for body in bodies
        for name, args, _call_id in body.get("methodCalls", [])
        if name == "Email/changes"
    ]
    assert len(changes) == 1
    assert changes[0]["sinceState"] == "mbx-state-1"
    assert changes[0]["sinceState"] != SESSION_DOCUMENT["state"]
    assert "session.state=session-state" in evidence
    assert "mailbox.state=mbx-state-1" in evidence
    assert "HTTP 200" in evidence


async def test_a_server_that_answers_500_is_an_error() -> None:
    """A broken session is an error of the first check, the rest is skipped."""
    probe = make_probe()
    probe.session_response = httpx.Response(500, text="internal error")
    results = await run_checks(probe)

    assert results[0].verdict == ERROR
    assert all(result.verdict == SKIPPED for result in results[1:])


def test_the_exit_code_follows_the_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One error is enough for a non zero exit code."""
    monkeypatch.setenv("STALWART_URL", "https://mail.example.com")
    monkeypatch.setenv("STALWART_API_KEY", BARE)

    async def fake_run_probe(**kwargs: Any) -> list[Any]:
        return [
            CheckResult(1, "first", OK, "fine"),
            CheckResult(2, "second", ERROR, "boom"),
        ]

    monkeypatch.setattr("tools.live_check.run_probe", fake_run_probe)

    code = main([])

    assert code == 1
    assert "error=1" in capsys.readouterr().out


def test_without_the_url_the_probe_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without STALWART_URL the probe says so and exits with a non zero code."""
    monkeypatch.delenv("STALWART_URL", raising=False)
    monkeypatch.delenv("STALWART_API_KEY", raising=False)
    monkeypatch.setattr(Path, "is_file", lambda self: False)

    code = main([])

    assert code != 0
    assert "STALWART_URL" in capsys.readouterr().err
