"""Business logic of the tools.

The description of a tool is its docstring and the schema of its result comes
from the type annotations (``src/mcp/schemas.py``) - see ``docs/tools-schema.md``.
Expected failures are raised as ``ToolError``: the MCP SDK turns them into a
tool error with an actionable text instead of a successful answer carrying an
``error`` field.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from src.config import get_settings
from src.mcp.auth import current_bearer, fingerprint
from src.mcp.jmap import JmapClient
from src.mcp.schemas import EmailAddress, Identity, WhoamiResult

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_jmap_client() -> JmapClient:
    """The JMAP client of the process (the session cache lives inside it)."""
    return JmapClient(get_settings())


def reset_client_cache() -> None:
    """Drop the cached client (tests, reconfiguration)."""
    get_jmap_client.cache_clear()


def _parse_addresses(raw: Any) -> list[EmailAddress]:
    """JMAP address list -> the address figure of our schema."""
    if not isinstance(raw, list):
        return []
    addresses: list[EmailAddress] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        email = str(item.get("email") or "")
        if not email:
            continue
        name = item.get("name")
        addresses.append({"email": email, "name": name if isinstance(name, str) else None})
    return addresses


def _parse_identities(payload: Any) -> list[Identity]:
    """JMAP ``Identity/get`` payload -> the identity figure of our schema."""
    raw = payload.get("list") if isinstance(payload, dict) else None
    raw = raw if isinstance(raw, list) else []
    if not raw:
        raise ToolError(
            "the mail server returned no identity for this account. The bearer may have no mail rights - "
            "check the API key and the account of the server."
        )

    identities: list[Identity] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        email = str(item.get("email") or "")
        if not email:
            continue
        name = item.get("name")
        identities.append(
            {
                "id": str(item.get("id") or ""),
                "email": email,
                "name": name if isinstance(name, str) else None,
                "reply_to": _parse_addresses(item.get("replyTo")),
                "is_default": bool(item.get("isDefault")),
            }
        )

    if not identities:
        raise ToolError(
            "the mail server returned identities without an address for this account. "
            "The bearer may have no mail rights - check the API key."
        )
    if not any(identity["is_default"] for identity in identities):
        identities[0]["is_default"] = True
    return identities


async def whoami() -> WhoamiResult:
    """Who am I? Returns the mail account behind the API key, the sender identities of that account and the capabilities the server advertises.

    Call this first: it tells you the account id, which addresses you may send from, and the JMAP capabilities of the server. `identities` is the same list `list_identities` returns. `state` is the current account state token - keep it and pass it to `list_changes` later to learn what changed since this moment; it is null when the server gives no state, and then `list_changes` is unavailable. The API key itself is never returned.
    """
    bearer = current_bearer()
    if not bearer:
        raise ToolError(
            "the request carries no bearer credential, so there is no account to look at. "
            "Send the API key in the authorization header of the request."
        )

    client = get_jmap_client()
    try:
        session = await client.session(bearer)
        envelope = await client.call(
            bearer,
            session,
            [("Identity/get", {"accountId": session.account_id})],
        )
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001 - the tool must not crash the call
        correlation = fingerprint(bearer)
        logger.exception("whoami failed (correlation %s)", correlation)
        raise ToolError(
            f"the whoami call failed unexpectedly ({type(exc).__name__}). The server log has the details; "
            "retry, and check the mail server if it keeps failing."
        ) from None

    identities = _parse_identities(envelope.result_of("Identity/get"))
    default = next((identity for identity in identities if identity["is_default"]), identities[0])

    return {
        "account_id": session.account_id,
        "email": default["email"],
        "identities": identities,
        "capabilities": list(session.capabilities),
        "state": envelope.state,
    }
