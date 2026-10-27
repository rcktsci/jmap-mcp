"""Shared fixtures. Nothing in the test suite touches the network."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from src.config import Settings, StalwartSettings

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS = REPO_ROOT / "docs"

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
