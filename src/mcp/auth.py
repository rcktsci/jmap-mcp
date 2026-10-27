"""Bearer pass-through for the MCP port.

The client sends its own Stalwart API key in the ``authorization`` header of
every MCP request. The middleware does not look that key up anywhere: it only
checks the shape of the header, rejects everything else with 401 *before* the
MCP application is entered, and then hands the value to the tools through a
ContextVar.

The key is never written to disk, never logged and lives exactly as long as the
request that carried it. For log correlation only a short, non-reversible
fingerprint (``sha256`` prefix) is used.
"""

from __future__ import annotations

import hashlib
import json
import logging
from contextvars import ContextVar, Token
from typing import Any

logger = logging.getLogger(__name__)

#: The bearer of the request currently being served.
_bearer: ContextVar[str | None] = ContextVar("jmap_bearer", default=None)

AUTHORIZATION_HEADER = b"authorization"
BEARER_SCHEME = "bearer"


def set_bearer(value: str) -> Token:
    """Put the bearer of the current request into the context."""
    return _bearer.set(value)


def reset_bearer(token: Token) -> None:
    """Restore the previous bearer (end of the request)."""
    _bearer.reset(token)


def current_bearer() -> str | None:
    """The bearer of the current request, or None when there is none."""
    return _bearer.get()


def fingerprint(value: str) -> str:
    """Short non-reversible id of a bearer, safe to put into a log line."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


class BearerAuthMiddleware:
    """ASGI middleware: only http scopes are guarded, everything else passes.

    The middleware wraps the whole ASGI application, so a request without a
    well-formed bearer is answered with 401 without ever reaching the MCP
    handler. The validity of the key is *not* checked here - Stalwart decides
    that (design doc, security model).
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        bearer = self._parse_bearer(scope)
        if bearer is None:
            await self._send_json(send, 401, {"detail": "unauthorized"})
            return

        marker = set_bearer(bearer)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_bearer(marker)

    @staticmethod
    def _parse_bearer(scope: Any) -> str | None:
        """Read the bearer out of the request headers, None when it is not one."""
        for name, value in scope.get("headers") or []:
            if name.lower() != AUTHORIZATION_HEADER:
                continue
            parts = value.decode("latin-1").strip().split(None, 1)
            if len(parts) == 2 and parts[0].lower() == BEARER_SCHEME:
                bearer = parts[1].strip()
                return bearer or None
            return None
        return None

    @staticmethod
    async def _send_json(send: Any, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
