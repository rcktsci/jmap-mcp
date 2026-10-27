"""Shared fixtures. Nothing in the test suite touches the network."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import httpx
import pytest

from src.config import Settings, StalwartSettings
from src.mcp import auth, tools
from src.mcp.jmap import JmapClient

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS = REPO_ROOT / "docs"

BARE = "starlight-api-key"

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
    "state": "session-state-token",
}


@pytest.fixture
def settings() -> Settings:
    """Settings pointing at a fake upstream that never leaves the process."""
    return Settings(
        stalwart=StalwartSettings(
            url="https://mail.example.com",
            session_path="/jmap/session",
            request_timeout_sec=5,
            max_attachment_mb=25,
            max_page_size=50,
            max_body_chars=20000,
            allow_roots=(".",),
            download_dir=None,
            session_cache_ttl_sec=60,
            dialect="rfc",
        ),
        listen="127.0.0.1",
        port=8000,
        log_level="info",
    )


@pytest.fixture
def make_transport() -> Callable[[Callable[[httpx.Request], httpx.Response]], tuple[httpx.MockTransport, list[httpx.Request]]]:
    """Build a mock transport that also records the requests it saw."""

    def factory(handler: Callable[[httpx.Request], httpx.Response]) -> tuple[httpx.MockTransport, list[httpx.Request]]:
        seen: list[httpx.Request] = []

        def wrapper(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        return httpx.MockTransport(wrapper), seen

    return factory


def jmap_response(method_responses: list[list[Any]], state: str | None = None) -> httpx.Response:
    """A JMAP response envelope with the given method responses."""
    body: dict[str, Any] = {"methodResponses": method_responses, "sessionState": state}
    return httpx.Response(200, json=body)


def load_published_schema(tool: str, kind: str) -> dict[str, Any]:
    """Read a JSON schema block out of docs/tools-schema.md."""
    text = (DOCS / "tools-schema.md").read_text(encoding="utf-8")
    start_marker = f"<!-- schema:{tool}:{kind} -->"
    end_marker = f"<!-- /schema:{tool}:{kind} -->"
    start = text.index(start_marker) + len(start_marker)
    end = text.index(end_marker, start)
    block = text[start:end].strip()
    if block.startswith("```json"):
        block = block[len("```json") :]
    if block.endswith("```"):
        block = block[: -len("```")]
    return json.loads(block.strip())


def jmap_error(error_type: str, code: str = "", description: str = "") -> dict[str, Any]:
    """An error answer of a JMAP method (the error arrives inside HTTP 200)."""
    return {
        "__error__": {
            "type": f"urn:ietf:params:jmap:error:{error_type}",
            "code": code or error_type,
            "description": description,
        }
    }


def jmap_calls(seen: list[Any], method: str) -> list[dict]:
    """The arguments of every call of the method the mock transport saw."""
    found: list[dict] = []
    for request in seen:
        if request.method != "POST" or not request.content:
            continue
        body = json.loads(request.content)
        for name, args, _call_id in body.get("methodCalls", []):
            if name == method:
                found.append(args)
    return found


def is_dialect_probe(args: dict) -> bool:
    """Is this the read-only probe the client uses to ask the dialect?

    The probe is one ``Email/query`` with two conditions (``operator`` in the
    filter) and ``limit: 1``; it never asks for the total.
    """
    filter_ = args.get("filter")
    return (
        args.get("limit") == 1
        and "calculateTotal" not in args
        and isinstance(filter_, dict)
        and "operator" in filter_
    )


def is_dialect_probe_mailbox(args: dict) -> bool:
    """Is this the call the client uses to find a folder for the probe?"""
    return args.get("properties") == ["id", "role"]


@pytest.fixture
def bearer() -> Any:
    """The bearer of the request being served."""
    marker = auth.set_bearer(BARE)
    yield BARE
    auth.reset_bearer(marker)


@pytest.fixture
def mock_jmap(
    settings: Settings,
    make_transport: Callable[[Callable[[httpx.Request], httpx.Response]], tuple[httpx.MockTransport, list[httpx.Request]]],
    monkeypatch: Any,
) -> Callable[..., list[httpx.Request]]:
    """A mock JMAP upstream.

    ``mock_jmap({"Email/query": {...}})`` installs it and returns the list of
    the requests it saw. The value of a method is its answer; a list of values
    is a queue of answers (one per call), which is how the fallbacks are
    tested. A method without an answer gets an ``unknownMethod`` error.

    ``blobs`` answers the blob endpoints of the session document: ``upload``
    (the JSON body of the upload answer) and ``download`` (the bytes of the
    file, or a ``(bytes, content-type)`` pair).
    """

    def build(
        answers: dict[str, Any],
        state: str | None = None,
        blobs: dict[str, Any] | None = None,
        client_settings: Settings | None = None,
    ) -> list[httpx.Request]:
        queues = {name: list(value) for name, value in answers.items() if isinstance(value, list)}
        blobs = blobs or {}

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if request.method == "GET":
                if "/download" in path:
                    answer = blobs.get("download", b"")
                    if isinstance(answer, tuple):
                        content, content_type = answer
                        return httpx.Response(
                            200, content=content, headers={"content-type": content_type}
                        )
                    return httpx.Response(200, content=answer)
                return httpx.Response(200, json=SESSION_DOCUMENT)
            if request.method == "POST" and "/upload" in path:
                return httpx.Response(200, json=blobs.get("upload", {"blobId": "blob-1"}))
            body = json.loads(request.content)
            responses: list[list[Any]] = []
            for name, _args, call_id in body.get("methodCalls", []):
                if name == "Email/query" and is_dialect_probe(_args):
                    # The probe of the client: the RFC dialect answers it.
                    responses.append(
                        ["Email/query", {"accountId": "account-1", "ids": [], "total": 0}, call_id]
                    )
                    continue
                if name == "Mailbox/get" and is_dialect_probe_mailbox(_args):
                    responses.append(
                        [
                            "Mailbox/get",
                            {"accountId": "account-1", "list": [{"id": "mb-probe", "role": "inbox"}]},
                            call_id,
                        ]
                    )
                    continue
                queue = queues.get(name)
                if queue is not None:
                    answer = queue.pop(0) if queue else None
                else:
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
                elif isinstance(answer, dict) and "__error__" in answer:
                    responses.append(["error", answer["__error__"], call_id])
                else:
                    responses.append([name, answer, call_id])
            return httpx.Response(200, json={"methodResponses": responses, "sessionState": state})

        transport, seen = make_transport(handler)
        monkeypatch.setattr(
            tools,
            "get_jmap_client",
            lambda: JmapClient(client_settings or settings, transport=transport),
        )
        return seen

    return build


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: Any) -> Any:
    """A file sandbox: one allowed root to read from, one directory to write into."""
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    settings = Settings(
        stalwart=StalwartSettings(
            url="https://mail.example.com",
            session_path="/jmap/session",
            request_timeout_sec=5,
            max_attachment_mb=25,
            max_page_size=50,
            max_body_chars=20000,
            allow_roots=(str(allowed),),
            download_dir=str(downloads),
            session_cache_ttl_sec=60,
            dialect="rfc",
        ),
        listen="127.0.0.1",
        port=8000,
        log_level="info",
    )
    monkeypatch.setattr(tools, "get_settings", lambda: settings)
    return SimpleNamespace(settings=settings, allowed=allowed, downloads=downloads, root=tmp_path)

