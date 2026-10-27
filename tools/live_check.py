"""Live probe of a Stalwart server: the 16 points of docs/tools-schema.md §1.7.

The 15 tools are covered by mock tests, but the JMAP dialect of Stalwart
differs from the RFC in details that break the implementation quietly. This
probe asks the live server every question of §1.7 and prints, for each of
them, whether the server supports it (``ok``), does not support it
(``unsupported``), failed to answer (``error``) or could not be asked
(``skipped``).

The API key comes from the environment and is only ever sent upstream as a
bearer - exactly the way the proxy does it. It is never printed, never
logged and never written to disk: the output carries its sha256 prefix
(``fingerprint``) instead. Nothing is written to disk at all unless
``--json <path>`` is given.

Run:
    STALWART_URL=https://mail.example.com STALWART_API_KEY=API_... python tools/live_check.py

Options:
    --json <path>      write the results as JSON into PATH
    --allow-write      let the probe write a flag on a real message (and restore it)
    --allow-send       let the probe create a draft and send it to the account itself
    --session-path     path of the session document (default: STALWART_SESSION_PATH, else /jmap/session)
    --timeout          timeout of one request in seconds (default: from the config)

Exit code: 0 when no check ended with ``error``, 1 when at least one did,
2 when the probe could not start (no URL, no key).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.config.settings import (  # noqa: E402
    DEFAULTS,
    Settings,
    StalwartSettings,
    load_settings,
    read_dotenv,
)
from src.mcp.jmap import JmapClient, JmapEnvelope, JmapSession, ToolError  # noqa: E402

CORE_URN = "urn:ietf:params:jmap:core"
MAIL_URN = "urn:ietf:params:jmap:mail"
SUBMISSION_URN = "urn:ietf:params:jmap:submission"

#: The verdicts of a check.
OK = "ok"
UNSUPPORTED = "unsupported"
ERROR = "error"
SKIPPED = "skipped"

#: The roles the proxy knows (§1.7 #6).
KNOWN_ROLES = ("inbox", "drafts", "sent", "trash", "junk", "archive", "other")

#: The keywords of the JMAP mail spec (§1.7 #12).
STANDARD_KEYWORDS = ("$seen", "$flagged", "$draft", "$answered")

#: How long a piece of evidence may be.
EVIDENCE_LIMIT = 200

#: Markers of a refusal that means "the server does not support this".
UNSUPPORTED_MARKERS = (
    "unknownmethod",
    "notsupported",
    "unsupported",
    "notimplemented",
    "unknownfilter",
    "invalidfilter",
    "unknownpropert",
    "invalidpropert",
    "invalidargument",
    "unknownargument",
    "unknowncapability",
    "invalidcapability",
    "invalidcapabilities",
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def excerpt(text: Any, limit: int = EVIDENCE_LIMIT) -> str:
    """A short, single line excerpt of a value."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def short_urn(urn: str) -> str:
    """``urn:ietf:params:jmap:mail`` -> ``mail``."""
    return urn.rsplit(":", 1)[-1] if urn else urn


def list_of(payload: Any) -> list[dict]:
    """The ``list`` of a JMAP answer, always a list of dictionaries."""
    if not isinstance(payload, dict):
        return []
    raw = payload.get("list")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def refused(message: str, extra: Sequence[str] = ()) -> tuple[str, str]:
    """The verdict of a call the server refused.

    A JMAP level refusal (``unknownMethod``, ``invalidFilter``,
    ``invalidProperties``...) means "this server does not do that"; anything
    else (network, ``serverFail``, ``forbidden``) is an error of the probe.
    """
    low = message.lower()
    markers = UNSUPPORTED_MARKERS + tuple(marker.lower() for marker in extra)
    verdict = UNSUPPORTED if any(marker in low for marker in markers) else ERROR
    return verdict, excerpt(message)


def ids_of(value: Any) -> list[str]:
    """A list of ids out of an answer."""
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, (str, int)) and str(item)]


# ---------------------------------------------------------------------------
# The probe
# ---------------------------------------------------------------------------


@dataclass
class Options:
    """What the probe is allowed to do."""

    allow_write: bool = False
    allow_send: bool = False
    transport: httpx.AsyncBaseTransport | None = None


@dataclass
class Sample:
    """A few messages of the account, used by several checks."""

    ids: list[str] = field(default_factory=list)
    threads: list[str] = field(default_factory=list)


@dataclass
class CheckResult:
    """The verdict of one check."""

    number: int
    title: str
    verdict: str
    evidence: str
    elapsed_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        """The row of the report as a plain dictionary."""
        return {
            "number": self.number,
            "check": self.title,
            "verdict": self.verdict,
            "evidence": self.evidence,
            "elapsed_ms": round(self.elapsed_ms, 1),
        }


class Probe:
    """Everything the checks need: the client, the bearer and the session."""

    def __init__(
        self,
        *,
        client: JmapClient,
        bearer: str,
        timeout: float,
        options: Options,
    ) -> None:
        self.client = client
        self.bearer = bearer
        self.timeout = timeout
        self.options = options
        self.session: JmapSession | None = None
        self.raw_session: dict[str, Any] = {}
        self.session_response: httpx.Response | None = None
        self.session_error: str = ""

    # -- plumbing ---------------------------------------------------------

    @property
    def account_id(self) -> str:
        """The mail account of the session."""
        return self.session.account_id if self.session else ""

    def scrub(self, text: str) -> str:
        """The bearer must never reach the output - not even by accident."""
        if self.bearer and self.bearer in text:
            return text.replace(self.bearer, "***")
        return text

    async def raw(self, method: str, url: str, *, authenticated: bool = True, **kwargs: Any) -> httpx.Response:
        """A raw HTTP call to the server - for the endpoints outside JMAP."""
        headers: dict[str, str] = {"accept": "application/json"}
        if authenticated:
            headers["authorization"] = f"Bearer {self.bearer}"
        try:
            async with httpx.AsyncClient(
                transport=self.options.transport,
                timeout=self.timeout,
                follow_redirects=False,
            ) as client:
                return await client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise ToolError(f"could not reach the mail server: {type(exc).__name__}") from None

    async def call_envelope(self, method: str, args: dict[str, Any]) -> JmapEnvelope:
        """One JMAP method call - the answer of the server, errors included."""
        return await self.client.call(self.bearer, self.session, [(method, args)])

    async def call(self, method: str, args: dict[str, Any]) -> Any:
        """The payload of one JMAP method call."""
        return (await self.call_envelope(method, args)).result_of(method)

    # -- data of the account ----------------------------------------------

    async def sample(self, count: int = 5) -> Sample:
        """A few messages of the account, with their conversation ids."""
        payload = await self.call(
            "Email/query",
            {"accountId": self.account_id, "limit": count, "position": 0},
        )
        ids = ids_of(payload.get("ids") if isinstance(payload, dict) else None)
        if not ids:
            return Sample()
        emails = list_of(
            await self.call(
                "Email/get",
                {"accountId": self.account_id, "ids": ids, "properties": ["id", "threadId"]},
            )
        )
        threads = [
            str(item.get("threadId"))
            for item in emails
            if isinstance(item.get("threadId"), str) and item.get("threadId")
        ]
        return Sample(ids=ids, threads=threads)

    async def first_identity(self) -> dict[str, Any]:
        """The first sender identity of the account."""
        items = list_of(await self.call("Identity/get", {"accountId": self.account_id}))
        return items[0] if items else {}

    async def mailbox_with_role(self, role: str) -> str:
        """The id of the folder with the given role, "" when there is none."""
        payload = await self.call(
            "Mailbox/get",
            {"accountId": self.account_id, "filter": {"role": role}, "properties": ["id", "role"]},
        )
        for item in list_of(payload):
            if str(item.get("role") or "").lower() == role:
                return str(item.get("id") or "")
        return ""


# ---------------------------------------------------------------------------
# The 16 checks
# ---------------------------------------------------------------------------


async def check_01_session(probe: Probe) -> tuple[str, str]:
    """The session document: apiUrl, the mail account, the capabilities."""
    response = probe.session_response
    if response is None:
        return ERROR, f"the session request failed: {probe.session_error or 'no answer'}"
    if response.status_code >= 400:
        return ERROR, f"HTTP {response.status_code} on the session request: {excerpt(response.text)}"

    document = probe.raw_session
    api_url = str(document.get("apiUrl") or "")
    primary = document.get("primaryAccounts")
    account_id = str((primary or {}).get(MAIL_URN) or "") if isinstance(primary, dict) else ""
    raw_caps = document.get("capabilities")
    if isinstance(raw_caps, dict):
        caps = [str(name) for name in raw_caps]
    elif isinstance(raw_caps, list):
        caps = [str(name) for name in raw_caps]
    else:
        caps = []
    download_url = str(document.get("downloadUrl") or "")
    upload_url = str(document.get("uploadUrl") or "")
    session_state = document.get("state")

    missing = [
        name
        for name, value in (
            ("apiUrl", api_url),
            ("primaryAccounts[mail]", account_id),
            ("downloadUrl", download_url),
            ("uploadUrl", upload_url),
        )
        if not value
    ]
    caps_missing = [
        short_urn(urn)
        for urn in (CORE_URN, MAIL_URN, SUBMISSION_URN)
        if urn not in caps
    ]

    evidence = (
        f"apiUrl={api_url or '—'}; account={account_id or '—'}; "
        f"caps={','.join(short_urn(c) for c in caps) or '—'}; "
        f"blob urls={'both' if download_url and upload_url else 'partial'}"
    )
    if caps_missing:
        evidence += f"; no {','.join(caps_missing)}"
    if session_state:
        evidence += f"; session.state={excerpt(session_state, 24)}"
    if missing:
        return ERROR, f"missing in the session: {', '.join(missing)}; {evidence}"

    # The path the proxy takes itself must work as well.
    try:
        probe.session = await probe.client.session(probe.bearer)
    except ToolError as exc:
        return ERROR, f"the proxy cannot use the session: {excerpt(str(exc))}; {evidence}"
    return OK, evidence


async def check_02_thread(probe: Probe) -> tuple[str, str]:
    """Thread/get: is it supported and which fields does it answer?"""
    sample = await probe.sample()
    if not sample.threads:
        return SKIPPED, "the account has no message with a conversation id"

    thread_id = sample.threads[0]
    args = {
        "accountId": probe.account_id,
        "ids": [thread_id],
        "properties": ["name", "emailIds", "totalEmails"],
    }
    try:
        payload = await probe.call("Thread/get", args)
    except ToolError as exc:
        message = str(exc)
        if "invalidpropert" in message.lower() or "unknownpropert" in message.lower():
            # The server knows the method but not every property: ask for less.
            try:
                payload = await probe.call(
                    "Thread/get",
                    {"accountId": probe.account_id, "ids": [thread_id], "properties": ["name", "emailIds"]},
                )
            except ToolError as retry_exc:
                return refused(str(retry_exc))
            items = list_of(payload)
            fields = sorted(items[0].keys()) if items else []
            return OK, (
                f"Thread/get works; totalEmails is refused ({excerpt(message, 120)}); "
                f"the fields that came: {','.join(fields) or '—'}"
            )
        return refused(message)

    items = list_of(payload)
    if not items:
        return OK, f"Thread/get answered, but the conversation {thread_id} is empty"
    thread = items[0]
    fields = [name for name in ("name", "emailIds", "totalEmails") if name in thread]
    return OK, (
        f"fields: {','.join(fields) or '—'}; "
        f"totalEmails={thread.get('totalEmails', '—')}; "
        f"emailIds={len(ids_of(thread.get('emailIds')))}"
    )


async def check_03_calculate_total(probe: Probe) -> tuple[str, str]:
    """Email/query with calculateTotal: does it count, and what does it cost?"""
    started = time.perf_counter()
    try:
        payload = await probe.call(
            "Email/query",
            {"accountId": probe.account_id, "calculateTotal": True, "limit": 1, "position": 0},
        )
    except ToolError as exc:
        return refused(str(exc))
    elapsed = (time.perf_counter() - started) * 1000
    total = payload.get("total") if isinstance(payload, dict) else None
    ids = ids_of(payload.get("ids")) if isinstance(payload, dict) else []
    if not isinstance(total, int) or isinstance(total, bool):
        return UNSUPPORTED, f"the server does not count (no total in the answer) after {elapsed:.0f} ms"
    return OK, f"total={total}; ids={len(ids)}; {elapsed:.0f} ms"


async def check_04_full_text(probe: Probe) -> tuple[str, str]:
    """The full text operator: does ``text`` work without an index?"""
    started = time.perf_counter()
    try:
        payload = await probe.call(
            "Email/query",
            {"accountId": probe.account_id, "filter": {"text": "the"}, "limit": 1, "position": 0},
        )
    except ToolError as exc:
        return refused(
            str(exc),
            extra=("not enabled", "no index", "not indexed", "full text", "fulltext", "index"),
        )
    elapsed = (time.perf_counter() - started) * 1000
    ids = ids_of(payload.get("ids")) if isinstance(payload, dict) else []
    return OK, f"the filter text is accepted; ids={len(ids)}; {elapsed:.0f} ms"


async def check_05_filters(probe: Probe) -> tuple[str, str]:
    """The filter operators: header, before, after, hasKeyword, notKeyword."""
    filters: tuple[tuple[str, dict[str, Any]], ...] = (
        ("header", {"header": ["subject", "a"]}),
        ("before", {"before": "2030-01-01T00:00:00+00:00"}),
        ("after", {"after": "2020-01-01T00:00:00+00:00"}),
        ("hasKeyword", {"hasKeyword": "$seen"}),
        ("notKeyword", {"notKeyword": "$seen"}),
    )
    accepted: list[str] = []
    rejected: list[str] = []
    broken: list[str] = []
    for name, condition in filters:
        try:
            await probe.call(
                "Email/query",
                {"accountId": probe.account_id, "filter": condition, "limit": 1, "position": 0},
            )
        except ToolError as exc:
            verdict, text = refused(str(exc))
            if verdict == UNSUPPORTED:
                rejected.append(f"{name} ({excerpt(text, 90)})")
            else:
                broken.append(f"{name} ({excerpt(text, 90)})")
        else:
            accepted.append(name)

    evidence = f"accepted: {','.join(accepted) or '—'}"
    if rejected:
        evidence += f"; refused: {'; '.join(rejected)}"
    if broken:
        return ERROR, f"{evidence}; failed: {'; '.join(broken)}"
    if rejected:
        return UNSUPPORTED, evidence
    return OK, evidence


async def check_06_roles(probe: Probe) -> tuple[str, str]:
    """Mailbox/get: the exact strings of the roles."""
    try:
        payload = await probe.call(
            "Mailbox/get",
            {"accountId": probe.account_id, "properties": ["id", "name", "role"]},
        )
    except ToolError as exc:
        return refused(str(exc))
    items = list_of(payload)
    if not items:
        return SKIPPED, "the account has no folders"

    pairs = [(str(item.get("name") or ""), str(item.get("role") or "")) for item in items]
    roles = [role for _name, role in pairs if role]
    if not roles:
        return UNSUPPORTED, "the folders carry no role at all"

    unknown = [role for role in roles if role not in KNOWN_ROLES]
    known = [role for role in KNOWN_ROLES if role in roles]
    evidence = f"roles: {','.join(roles)}"
    if known:
        evidence += f"; standard: {','.join(known)}"
    if unknown:
        evidence += f"; non standard: {','.join(unknown)}"
    missing = [role for role in KNOWN_ROLES if role not in roles]
    if missing:
        evidence += f"; no folder with the role: {','.join(missing)}"
    return OK, evidence


async def check_07_blob_urls(probe: Probe) -> tuple[str, str]:
    """The blob URLs: the template, the placeholders and the authorization."""
    download = probe.session.download_url if probe.session else ""
    upload = probe.session.upload_url if probe.session else ""
    if not download and not upload:
        return ERROR, "the session has neither downloadUrl nor uploadUrl"

    parts: list[str] = []
    for label, template in (("download", download), ("upload", upload)):
        if not template:
            parts.append(f"{label}: no template")
            continue
        placeholders = [
            name
            for name in ("accountId", "blobId", "name")
            if "{" + name + "}" in template
        ]
        parts.append(f"{label}: {template} ({{{','.join(placeholders)}}})")

    # Does the download endpoint want the same bearer? Ask it for a blob that
    # does not exist - with and without the credential.
    if download:
        url = blob_url(download, probe.account_id, "live-check-no-such-blob", "probe")
        with_bearer = await probe.raw("GET", url)
        without_bearer = await probe.raw("GET", url, authenticated=False)
        if without_bearer.status_code in (401, 403) and with_bearer.status_code != without_bearer.status_code:
            parts.append(f"download wants the bearer ({without_bearer.status_code} without it)")
        else:
            parts.append(
                f"download: HTTP {with_bearer.status_code} with the bearer, "
                f"{without_bearer.status_code} without it"
            )
    return OK, "; ".join(parts)


async def check_08_submission(probe: Probe) -> tuple[str, str]:
    """EmailSubmission/set and onSuccessUpdateEmail."""
    identity = await probe.first_identity()
    identity_id = str(identity.get("id") or "")
    if not identity_id:
        return SKIPPED, "the account has no sender identity"

    bogus = "live-check-no-such-email"
    base = {
        "accountId": probe.account_id,
        "create": {"send": {"identityId": identity_id, "emailId": bogus}},
    }
    with_argument = {
        **base,
        "onSuccessUpdateEmail": {"update": {}},
    }

    refused_argument = ""
    accepted = False
    try:
        await probe.call("EmailSubmission/set", with_argument)
        accepted = True
    except ToolError as exc:
        message = str(exc)
        if "onsuccessupdateemail" in message.lower():
            refused_argument = excerpt(message)
        else:
            # The server answered about the mail itself: ask the same question
            # without the argument - a difference points at the argument.
            try:
                await probe.call("EmailSubmission/set", base)
            except ToolError as base_exc:
                if "unknownmethod" in str(base_exc).lower():
                    return UNSUPPORTED, f"EmailSubmission/set is not supported: {excerpt(str(base_exc))}"
                return OK, (
                    "EmailSubmission/set answers about the mail itself, the argument was not objected to; "
                    f"{excerpt(message)}"
                )
            # The call without the argument worked: the argument is the problem.
            refused_argument = excerpt(message)

    if refused_argument:
        return UNSUPPORTED, f"the server refuses onSuccessUpdateEmail: {refused_argument}"
    if not accepted:
        return UNSUPPORTED, "EmailSubmission/set does not accept onSuccessUpdateEmail"

    evidence = "EmailSubmission/set and onSuccessUpdateEmail are accepted"
    if probe.options.allow_send:
        evidence += "; " + await _send_probe(probe, identity)
    else:
        evidence += "; the real send is skipped (--allow-send)"
    return OK, evidence


async def _send_probe(probe: Probe, identity: dict[str, Any]) -> str:
    """The destructive part: a draft to the account itself, sent and watched."""
    drafts = await probe.mailbox_with_role("drafts")
    sent = await probe.mailbox_with_role("sent")
    address = str(identity.get("email") or "")
    if not drafts or not address:
        return "the real send is impossible (no drafts folder or no address)"

    created = await probe.call(
        "Email/set",
        {
            "accountId": probe.account_id,
            "create": {
                "draft": {
                    "mailboxIds": {drafts: True},
                    "keywords": {"$draft": True},
                    "from": [{"email": address}],
                    "to": [{"email": address}],
                    "subject": "jmap-mcp live check",
                    "textBody": {"value": "jmap-mcp live check probe"},
                }
            },
        },
    )
    draft = (created.get("created") or {}).get("draft") if isinstance(created, dict) else None
    draft_id = str(draft.get("id") or "") if isinstance(draft, dict) else ""
    if not draft_id:
        return "the draft was not created (the server refused it)"

    try:
        await probe.call(
            "EmailSubmission/set",
            {
                "accountId": probe.account_id,
                "create": {"send": {"identityId": str(identity.get("id") or ""), "emailId": draft_id}},
                "onSuccessUpdateEmail": {"update": {draft_id: {"mailboxIds": {sent: True}}}},
            },
        )
    except ToolError as exc:
        return f"the send was refused: {excerpt(str(exc))}"

    after = list_of(
        await probe.call(
            "Email/get",
            {"accountId": probe.account_id, "ids": [draft_id], "properties": ["id", "mailboxIds"]},
        )
    )
    folders = ids_of(after[0].get("mailboxIds")) if after else []
    moved_by_server = bool(sent) and sent in folders
    return (
        f"a mail to itself was sent; the draft is now in: {','.join(folders) or '—'}; "
        f"the server moved it to Sent itself: {'yes' if moved_by_server else 'no'}"
    )


async def check_09_identities(probe: Probe) -> tuple[str, str]:
    """Identity/get: are replyTo and isDefault there?"""
    try:
        payload = await probe.call("Identity/get", {"accountId": probe.account_id})
    except ToolError as exc:
        return refused(str(exc))
    items = list_of(payload)
    if not items:
        return SKIPPED, "the account has no identity"
    identity = items[0]
    has_reply = "replyTo" in identity
    has_default = "isDefault" in identity
    missing = [name for name, there in (("replyTo", has_reply), ("isDefault", has_default)) if not there]
    evidence = (
        f"identities={len(items)}; replyTo={'yes' if has_reply else 'no'}; "
        f"isDefault={'yes' if has_default else 'no'}"
    )
    if missing:
        return UNSUPPORTED, f"missing: {', '.join(missing)}; {evidence}"
    return OK, evidence


async def check_10_body(probe: Probe) -> tuple[str, str]:
    """Email/get: what comes when the body is asked for - textBody or bodyValues?"""
    sample = await probe.sample()
    if not sample.ids:
        return SKIPPED, "the account has no message to read"

    email_id = sample.ids[0]
    args = {
        "accountId": probe.account_id,
        "ids": [email_id],
        "properties": ["id", "subject", "textBody", "htmlBody", "bodyValues"],
        # RFC 8621 §4.2: bodyProperties is String[], not an object.
        "bodyProperties": ["partId", "blobId", "size", "type", "charset"],
    }
    try:
        payload = await probe.call("Email/get", args)
    except ToolError as exc:
        message = str(exc)
        if "bodyproperties" in message.lower():
            try:
                payload = await probe.call(
                    "Email/get",
                    {"accountId": probe.account_id, "ids": [email_id], "properties": ["id", "textBody"]},
                )
            except ToolError as retry_exc:
                return refused(str(retry_exc))
            return UNSUPPORTED, f"the server refuses bodyProperties: {excerpt(message)}"
        return refused(message)

    items = list_of(payload)
    if not items:
        return SKIPPED, "the message could not be read"
    item = items[0]
    text_part = item.get("textBody") if isinstance(item.get("textBody"), dict) else None
    html_part = item.get("htmlBody") if isinstance(item.get("htmlBody"), dict) else None
    body_values = item.get("bodyValues") if isinstance(item.get("bodyValues"), dict) else None

    def size_of(part: Any) -> int:
        if isinstance(part, dict):
            size = part.get("size")
            if isinstance(size, int) and not isinstance(size, bool):
                return size
            if isinstance(part.get("value"), str):
                return len(part["value"])
        return 0

    evidence = (
        f"textBody={size_of(text_part)} chars; htmlBody={size_of(html_part)} chars; "
        f"bodyValues={'yes' if body_values else 'no'}"
    )
    if body_values:
        evidence += f" ({len(body_values)} parts)"
    if not text_part and not html_part and not body_values:
        return UNSUPPORTED, f"no body came at all; {evidence}"
    return OK, evidence


async def check_11_keywords(probe: Probe) -> tuple[str, str]:
    """The keywords of the messages: which ones does the server use?"""
    sample = await probe.sample()
    if not sample.ids:
        return SKIPPED, "the account has no message"

    try:
        payload = await probe.call(
            "Email/get",
            {"accountId": probe.account_id, "ids": sample.ids, "properties": ["id", "keywords"]},
        )
    except ToolError as exc:
        return refused(str(exc))

    seen: list[str] = []
    for item in list_of(payload):
        for keyword in ids_of(item.get("keywords")):
            if keyword not in seen:
                seen.append(keyword)
    if not seen:
        return UNSUPPORTED, "the messages carry no keywords"

    standard = [keyword for keyword in STANDARD_KEYWORDS if keyword in seen]
    extra = [keyword for keyword in seen if keyword not in STANDARD_KEYWORDS]
    evidence = f"keywords: {','.join(seen)}"
    if standard:
        evidence += f"; standard: {','.join(standard)}"
    if extra:
        evidence += f"; own: {','.join(extra)}"
    missing = [keyword for keyword in STANDARD_KEYWORDS if keyword not in seen]
    if missing:
        evidence += f"; not seen: {','.join(missing)}"
    return OK, evidence


async def check_12_state(probe: Probe) -> tuple[str, str]:
    """The state: is it in the session and in the answers?"""
    session_state = probe.raw_session.get("state")
    try:
        mailboxes = await probe.call(
            "Mailbox/get",
            {"accountId": probe.account_id, "properties": ["id"]},
        )
    except ToolError as exc:
        return refused(str(exc))
    account_state = mailboxes.get("state") if isinstance(mailboxes, dict) else None
    evidence = (
        f"session.state={excerpt(session_state, 24) if session_state else '—'}; "
        f"mailbox.state={excerpt(account_state, 24) if account_state else '—'}"
    )

    # Email/changes wants the account state - the one Mailbox/get and
    # Email/query answer with - not the state of the session document.
    if not isinstance(account_state, str) or not account_state:
        return UNSUPPORTED, f"no account state in the answers; {evidence}"
    try:
        changes = await probe.call(
            "Email/changes",
            {"accountId": probe.account_id, "sinceState": account_state},
        )
    except ToolError as exc:
        return refused(str(exc))
    new_state = changes.get("newState") if isinstance(changes, dict) else None
    return OK, f"{evidence}; Email/changes HTTP 200, newState={excerpt(new_state, 24) if new_state else '—'}"


async def check_13_answered(probe: Probe) -> tuple[str, str]:
    """Email/set: is the keyword $answered accepted?"""
    bogus = "live-check-no-such-email"

    def update(keyword: str) -> dict[str, Any]:
        return {bogus: {"keywords": {keyword: True}}}

    # Control: a keyword that cannot exist. If the server complains about the
    # keyword, it validates the keywords before it looks at the id - and the
    # answer for $answered means something.
    control = ""
    try:
        payload = await probe.call(
            "Email/set",
            {"accountId": probe.account_id, "update": update("$live-check-bogus-keyword")},
        )
    except ToolError as exc:
        control = str(exc)
    else:
        control = _not_updated_text(payload)

    try:
        payload = await probe.call(
            "Email/set",
            {"accountId": probe.account_id, "update": update("$answered")},
        )
    except ToolError as exc:
        text = str(exc)
        if "answered" in text.lower():
            return UNSUPPORTED, f"the server refuses the keyword $answered: {excerpt(text)}"
        if "live-check-bogus-keyword" in control.lower():
            return UNSUPPORTED, f"the server refuses the keyword $answered: {excerpt(text)}"
        return SKIPPED, (
            "the server answers about the id before the keyword; the keyword was not checked "
            f"({excerpt(text, 120)}); --allow-write checks it for real"
        )

    text = _not_updated_text(payload)
    if "answered" in text.lower():
        return UNSUPPORTED, f"the server refuses the keyword $answered: {excerpt(text)}"
    if "live-check-bogus-keyword" in control.lower():
        return UNSUPPORTED, "the server refuses the keyword $answered (the control keyword is refused as well)"
    if not probe.options.allow_write:
        return SKIPPED, (
            "the server answers about the id before the keyword, so $answered was not checked; "
            "--allow-write checks it for real"
        )

    return await _write_probe(probe)


def _not_updated_text(payload: Any) -> str:
    """The text of the per id errors of an Email/set answer."""
    if not isinstance(payload, dict):
        return ""
    not_updated = payload.get("notUpdated")
    if not isinstance(not_updated, dict):
        return ""
    parts: list[str] = []
    for error in not_updated.values():
        if isinstance(error, dict):
            parts.append(f"{error.get('type', '')} {error.get('description', '')}")
        else:
            parts.append(str(error))
    return " ".join(parts)


async def _write_probe(probe: Probe) -> tuple[str, str]:
    """The destructive part: set $answered on a real message, then restore it."""
    sample = await probe.sample()
    if not sample.ids:
        return SKIPPED, "the account has no message to write on"
    email_id = sample.ids[0]

    before = list_of(
        await probe.call(
            "Email/get",
            {"accountId": probe.account_id, "ids": [email_id], "properties": ["id", "keywords"]},
        )
    )
    original = ids_of(before[0].get("keywords")) if before else []

    payload = await probe.call(
        "Email/set",
        {"accountId": probe.account_id, "update": {email_id: {"keywords": {"$answered": True}}}},
    )
    text = _not_updated_text(payload)
    if "answered" in text.lower():
        return UNSUPPORTED, f"the server refuses the keyword $answered: {excerpt(text)}"

    # Restore what was there before.
    restore = {keyword: keyword in original for keyword in original}
    restore.setdefault("$answered", False)
    try:
        await probe.call(
            "Email/set",
            {"accountId": probe.account_id, "update": {email_id: {"keywords": restore}}},
        )
    except ToolError as exc:
        return OK, f"$answered is accepted, but the flags were not restored: {excerpt(str(exc))}"
    return OK, f"$answered is accepted on a message (and the flags were restored: {','.join(original) or '—'})"


async def check_14_mailbox_counters(probe: Probe) -> tuple[str, str]:
    """Mailbox/get: do the counters come without an explicit request?"""
    try:
        plain = await probe.call("Mailbox/get", {"accountId": probe.account_id})
    except ToolError as exc:
        return refused(str(exc))
    plain_items = list_of(plain)
    plain_has = "totalEmails" in plain_items[0] if plain_items else False

    try:
        asked = await probe.call(
            "Mailbox/get",
            {
                "accountId": probe.account_id,
                "properties": ["id", "name", "role", "totalEmails", "unreadEmails"],
            },
        )
    except ToolError as exc:
        message = str(exc)
        if "invalidpropert" in message.lower() or "unknownpropert" in message.lower():
            return UNSUPPORTED, f"the server refuses the counters: {excerpt(message)}"
        return refused(message)

    asked_items = list_of(asked)
    asked_has = "totalEmails" in asked_items[0] if asked_items else False
    unread_has = "unreadEmails" in asked_items[0] if asked_items else False
    evidence = (
        f"without properties: totalEmails={'yes' if plain_has else 'no'}; "
        f"with properties: totalEmails={'yes' if asked_has else 'no'}, "
        f"unreadEmails={'yes' if unread_has else 'no'}"
    )
    if not asked_has:
        return UNSUPPORTED, f"the server does not give the counters; {evidence}"
    return OK, evidence


async def check_15_limit_zero(probe: Probe) -> tuple[str, str]:
    """Email/query: is limit 0 accepted?"""
    try:
        payload = await probe.call(
            "Email/query",
            {"accountId": probe.account_id, "calculateTotal": True, "limit": 0, "position": 0},
        )
    except ToolError as exc:
        return refused(str(exc))
    ids = ids_of(payload.get("ids")) if isinstance(payload, dict) else []
    total = payload.get("total") if isinstance(payload, dict) else None
    return OK, f"limit 0 is accepted; ids={len(ids)}; total={total if total is not None else '—'}"


async def check_16_upload(probe: Probe) -> tuple[str, str]:
    """uploadUrl: does a multipart POST with the bearer work?"""
    upload = probe.session.upload_url if probe.session else ""
    if not upload:
        return ERROR, "the session has no uploadUrl"

    url = upload.replace("{accountId}", probe.account_id)
    content = b"jmap-mcp live check probe\n"
    # The name of the multipart field is up to the server - ask with the one
    # the proxy uses first, then with the usual candidates.
    last_error = ""
    for field_name in ("file", "blob", "upload", "attachment"):
        response = await probe.raw(
            "POST",
            url,
            files={field_name: ("live-check.txt", content, "text/plain")},
        )
        if response.status_code < 400:
            body: Any
            try:
                body = response.json()
            except ValueError:
                body = {}
            blob_id = str((body or {}).get("blobId") or "") if isinstance(body, dict) else ""
            return OK, (
                f"the field '{field_name}' is accepted; HTTP {response.status_code}; "
                f"blobId={'yes' if blob_id else 'no'}; the answer: {excerpt(response.text, 120)}"
            )
        last_error = f"HTTP {response.status_code}: {excerpt(response.text, 120)}"

    without = await probe.raw(
        "POST",
        url,
        files={"file": ("live-check.txt", content, "text/plain")},
        authenticated=False,
    )
    hint = ""
    if without.status_code in (401, 403):
        hint = "; without the bearer: " + str(without.status_code)
    return UNSUPPORTED, f"no multipart field was accepted; {last_error}{hint}"


#: The 16 checks of §1.7, in the order of the table.
CHECKS: tuple[tuple[str, Callable[[Probe], Any]], ...] = (
    ("session: apiUrl/аккаунт/capabilities", check_01_session),
    ("Thread/get: поддержка и поля", check_02_thread),
    ("Email/query: calculateTotal", check_03_calculate_total),
    ("полнотекстовый оператор text", check_04_full_text),
    ("фильтры header/before/after/hasKeyword/notKeyword", check_05_filters),
    ("Mailbox/get: точные строки role", check_06_roles),
    ("downloadUrl/uploadUrl: шаблон и авторизация", check_07_blob_urls),
    ("EmailSubmission/set + onSuccessUpdateEmail", check_08_submission),
    ("Identity/get: replyTo/isDefault", check_09_identities),
    ("Email/get: textBody против bodyValues", check_10_body),
    ("keywords у сообщений", check_11_keywords),
    ("state в session и в ответах", check_12_state),
    ("Email/set: ключевое слово $answered", check_13_answered),
    ("Mailbox/get: totalEmails/unreadEmails без properties", check_14_mailbox_counters),
    ("Email/query: limit 0", check_15_limit_zero),
    ("uploadUrl: POST multipart с Bearer", check_16_upload),
)


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


def blob_url(template: str, account_id: str, blob_id: str, name: str) -> str:
    """The download URL out of the template of the session."""
    url = template.replace("{accountId}", account_id)
    url = url.replace("{blobId}", blob_id)
    return url.replace("{name}", name)


async def run_checks(probe: Probe) -> list[CheckResult]:
    """Run every check; one broken check never stops the others."""
    results: list[CheckResult] = []
    for number, (title, function) in enumerate(CHECKS, start=1):
        started = time.perf_counter()
        if number > 1 and probe.session is None:
            # Without a session there is nothing to ask the server about.
            results.append(
                CheckResult(
                    number=number,
                    title=title,
                    verdict=SKIPPED,
                    evidence="no session document - the check cannot be asked",
                    elapsed_ms=(time.perf_counter() - started) * 1000,
                )
            )
            continue
        try:
            verdict, evidence = await function(probe)
        except ToolError as exc:
            verdict, evidence = refused(str(exc))
        except httpx.HTTPError as exc:
            verdict, evidence = ERROR, f"{type(exc).__name__}: {excerpt(str(exc))}"
        except Exception as exc:  # noqa: BLE001 - the probe must not die
            verdict, evidence = ERROR, f"{type(exc).__name__}: {excerpt(str(exc))}"
        elapsed = (time.perf_counter() - started) * 1000
        results.append(
            CheckResult(
                number=number,
                title=title,
                verdict=verdict,
                evidence=probe.scrub(evidence),
                elapsed_ms=elapsed,
            )
        )
    return results


def build_settings(url: str, session_path: str, timeout: float | None) -> Settings:
    """The settings of the probe: the URL from the environment, the rest from the config."""
    try:
        base = load_settings()
    except Exception:  # noqa: BLE001 - a broken config must not stop the probe
        stalwart = DEFAULTS["stalwart"]
        base = Settings(
            stalwart=StalwartSettings(
                url=str(stalwart["url"]).rstrip("/"),
                session_path=str(stalwart["session_path"]),
                request_timeout_sec=float(stalwart["request_timeout_sec"]),
                max_attachment_mb=int(stalwart["max_attachment_mb"]),
                max_page_size=int(stalwart["max_page_size"]),
                max_body_chars=int(stalwart["max_body_chars"]),
                allow_roots=tuple(stalwart["allow_roots"]),
                download_dir=stalwart["download_dir"],
                session_cache_ttl_sec=int(stalwart["session_cache_ttl_sec"]),
            ),
            listen=str(DEFAULTS["listen"]),
            port=int(DEFAULTS["port"]),
            log_level=str(DEFAULTS["log_level"]),
        )
    return replace(
        base,
        stalwart=replace(
            base.stalwart,
            url=url.rstrip("/"),
            session_path=session_path,
            request_timeout_sec=float(timeout if timeout else base.stalwart.request_timeout_sec),
            session_cache_ttl_sec=0,
            # The probe asks the server itself: the dialect layer of the proxy
            # would answer the questions of the server instead.
            dialect="rfc",
        ),
    )


async def run_probe(
    *,
    url: str,
    session_path: str,
    bearer: str,
    timeout: float | None,
    options: Options,
) -> list[CheckResult]:
    """Ask the live server every question of §1.7."""
    settings = build_settings(url, session_path, timeout)
    client = JmapClient(settings, transport=options.transport)
    probe = Probe(
        client=client,
        bearer=bearer,
        timeout=settings.stalwart.request_timeout_sec,
        options=options,
    )
    try:
        probe.session_response = await probe.raw("GET", f"{settings.stalwart.url}{session_path}")
        if probe.session_response.status_code < 400:
            try:
                document = probe.session_response.json()
            except ValueError:
                document = {}
            probe.raw_session = document if isinstance(document, dict) else {}
    except ToolError as exc:
        probe.session_error = str(exc)
    return await run_checks(probe)


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def print_table(results: Sequence[CheckResult]) -> None:
    """The table of the report."""
    print(f"{'№':>2}  {'проверка':<52}  {'вердикт':<11}  доказательство")
    print("-" * 118)
    for result in results:
        print(
            f"{result.number:>2}  {result.title:<52}  {result.verdict:<11}  {result.evidence}"
        )


def print_summary(results: Sequence[CheckResult]) -> dict[str, int]:
    """The counts of the verdicts."""
    counts = {OK: 0, UNSUPPORTED: 0, ERROR: 0, SKIPPED: 0}
    for result in results:
        counts[result.verdict] = counts.get(result.verdict, 0) + 1
    print()
    print(
        f"Итог: ok={counts[OK]} unsupported={counts[UNSUPPORTED]} "
        f"error={counts[ERROR]} skipped={counts[SKIPPED]} (всего {len(results)})"
    )
    return counts


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """The command line of the probe."""
    parser = argparse.ArgumentParser(
        prog="live_check.py",
        description="Probe of a live Stalwart server: the 16 points of docs/tools-schema.md §1.7.",
    )
    parser.add_argument("--json", metavar="PATH", help="write the results as JSON into PATH")
    parser.add_argument(
        "--allow-write",
        action="store_true",
        help="let the probe write a flag on a real message (and restore it)",
    )
    parser.add_argument(
        "--allow-send",
        action="store_true",
        help="let the probe create a draft and send it to the account itself",
    )
    parser.add_argument(
        "--session-path",
        default=None,
        help="path of the session document (default: STALWART_SESSION_PATH, else /jmap/session)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="timeout of one request in seconds (default: from the config)",
    )
    return parser.parse_args(argv)


def read_environment(args: argparse.Namespace) -> tuple[str, str, str]:
    """The URL, the path of the session and the key - from the environment."""
    dotenv = read_dotenv()
    url = (os.environ.get("STALWART_URL") or dotenv.get("STALWART_URL") or "").strip()
    key = (os.environ.get("STALWART_API_KEY") or dotenv.get("STALWART_API_KEY") or "").strip()
    session_path = (
        args.session_path
        or (os.environ.get("STALWART_SESSION_PATH") or dotenv.get("STALWART_SESSION_PATH") or "").strip()
        or "/jmap/session"
    )
    return url, session_path, key


def fingerprint(value: str) -> str:
    """The short, non reversible id of the key - the key itself is never shown."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def write_json(path: str, payload: dict[str, Any]) -> None:
    """Write the report as JSON - the only file the probe ever writes."""
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def use_utf8() -> None:
    """The report is in Russian - a console that cannot encode it must not crash."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover - exotic streams
            pass


def main(argv: Sequence[str] | None = None) -> int:
    """The entry point of the probe."""
    args = parse_args(argv)
    use_utf8()
    url, session_path, key = read_environment(args)

    if not url:
        print(
            "нужен STALWART_URL - базовый URL живого Stalwart, "
            "например STALWART_URL=https://mail.example.com",
            file=sys.stderr,
        )
        return 2
    if not key:
        print(
            "нужен STALWART_API_KEY - API key аккаунта (API_...), он нигде не печатается",
            file=sys.stderr,
        )
        return 2

    settings = build_settings(url, session_path, args.timeout)
    print("JMAP live check - Stalwart")
    print(f"URL:      {settings.stalwart.url}")
    print(f"Session:  {session_path}")
    print(f"Key:      sha256:{fingerprint(key)} (the key itself is never shown)")
    print(f"Timeout:  {settings.stalwart.request_timeout_sec:g} s")
    print(f"Started:  {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print()

    options = Options(allow_write=args.allow_write, allow_send=args.allow_send)
    results = asyncio.run(
        run_probe(
            url=url,
            session_path=session_path,
            bearer=key,
            timeout=args.timeout,
            options=options,
        )
    )

    print_table(results)
    counts = print_summary(results)

    if args.json:
        write_json(
            args.json,
            {
                "url": settings.stalwart.url,
                "session_path": session_path,
                "key_fingerprint": fingerprint(key),
                "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "allow_write": args.allow_write,
                "allow_send": args.allow_send,
                "summary": counts,
                "results": [result.as_dict() for result in results],
            },
        )
        print(f"JSON:     {args.json}")

    return 1 if counts.get(ERROR) else 0


if __name__ == "__main__":
    raise SystemExit(main())
