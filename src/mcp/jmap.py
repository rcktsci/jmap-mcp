"""JMAP client: session, batched method calls, dialect, blob up/download.

The client is a pure proxy. The only credential it uses is the bearer of the
request that is being served; it is sent upstream as-is, never stored and never
logged. Everything the client raises is a ``ToolError``: the MCP SDK turns it
into a tool error (``isError: true``) with an actionable message for the model.

The client speaks two dialects of the JMAP wire format:

* ``rfc`` - RFC 8620 and RFC 8621 as they are written down;
* ``stalwart`` - the dialect of Stalwart 1.0.0, which differs from the RFC in
  a few places (one condition per query, no ``header`` filter, its own form of
  ``mailboxIds`` and of the submission - see ``docs/tools-schema.md`` §1.6).

Which dialect the upstream speaks is asked once (a read-only probe) and then
remembered for the process. The translation of the forms lives here and nowhere
else: the tools speak the logical form, the client speaks the dialect.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any, Sequence
from urllib.parse import quote, urlsplit

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from src.config import Settings, get_settings
from src.config.settings import DIALECT_AUTO, DIALECT_RFC, DIALECT_STALWART
from src.mcp.auth import fingerprint

logger = logging.getLogger(__name__)

CORE_URN = "urn:ietf:params:jmap:core"
MAIL_URN = "urn:ietf:params:jmap:mail"
SUBMISSION_URN = "urn:ietf:params:jmap:submission"

#: Capabilities this proxy is able to speak. ``using`` is the intersection of
#: this list with what the server advertises.
KNOWN_CAPABILITIES = (CORE_URN, MAIL_URN, SUBMISSION_URN)

WELL_KNOWN_SESSION_PATH = "/.well-known/jmap"

DEFAULT_PORTS = {"http": 80, "https": 443}

#: How many ids one query of a client side intersection fetches at once.
INTERSECTION_LIMIT = 2000

#: How many pages one client side intersection may fetch before the proxy
#: gives up and says so.
INTERSECTION_MAX_PAGES = 5

#: How many ids one request of the client side header filter fetches.
HEADER_BATCH = 200

#: The content type used when the caller does not know the type of a file.
DEFAULT_CONTENT_TYPE = "application/octet-stream"


@dataclass(frozen=True)
class JmapSession:
    """The parts of the JMAP session document the proxy works with."""

    api_url: str
    download_url: str
    upload_url: str
    account_id: str
    capabilities: tuple[str, ...]
    state: str | None = None


@dataclass(frozen=True)
class JmapMethodResult:
    """One successful method call out of a batch."""

    name: str
    result: Any


@dataclass(frozen=True)
class JmapEnvelope:
    """The envelope of a JMAP response: the results plus the account state."""

    results: tuple[JmapMethodResult, ...]
    state: str | None = None

    def result_of(self, name: str) -> Any:
        """The payload of the named method, None when it is not in the batch."""
        for item in self.results:
            if item.name == name:
                return item.result
        return None


class JmapMethodFailure(ToolError):
    """A JMAP method that failed - the answer of the server is kept.

    The failures of a ``/set`` method arrive inside HTTP 200 (``notCreated``,
    ``notUpdated``, ``notFound``). They are never a success: the client turns
    them into this error, which carries the payload of the answer so that a
    tool can look at the details (see ``set_flags`` and the ``$answered``
    keyword).
    """

    def __init__(self, message: str, method: str, payload: Any) -> None:
        super().__init__(message)
        self.method = method
        self.payload = payload


@dataclass(frozen=True)
class Dialect:
    """The JMAP dialect of one upstream: the forms its methods understand."""

    name: str = DIALECT_RFC

    @property
    def is_stalwart(self) -> bool:
        """Is this the dialect of Stalwart 1.0.0?"""
        return self.name == DIALECT_STALWART

    # -- Email/query ------------------------------------------------------

    @staticmethod
    def conditions_of(filter_: Any) -> list[dict]:
        """The single conditions of a filter (RFC 8621 §4.4.1).

        A filter is either the operator form of RFC 8620 (``operator`` plus
        ``conditions``) or a flat ``FilterCondition``; a flat condition with
        several properties is the AND of the conditions it carries.
        """
        if not isinstance(filter_, dict) or not filter_:
            return []
        raw = filter_.get("conditions")
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, dict) and item]
        return [{key: value} for key, value in filter_.items() if key != "operator"]

    @staticmethod
    def is_header(condition: dict) -> bool:
        """Is this the header condition - the one Stalwart does not know?"""
        return "header" in condition

    # -- Email/set --------------------------------------------------------

    @staticmethod
    def mailbox_patch(patch: dict, dialect: str) -> dict:
        """The folders of a message in the dialect of the server.

        The logical form is a map ``{folder: wanted}``. Stalwart 1.0.0 wants
        the map of the folders the message is in (``{folder: true}``); RFC
        8621 §4.6 wants the patch syntax of RFC 8620 §5.3 - the folders are
        added (``mailboxIds/<id>``: true) and removed (``mailboxIds/<id>``:
        null).
        """
        if dialect == DIALECT_STALWART:
            return {"mailboxIds": {key: True for key, value in patch.items() if value}}
        translated: dict[str, Any] = {}
        for key, value in patch.items():
            translated[f"mailboxIds/{key}"] = True if value else None
        return translated

    # -- EmailSubmission/set ----------------------------------------------

    @staticmethod
    def submission_create(create: dict) -> dict:
        """The create map of a submission.

        RFC 8621 §7.1 makes ``emailId`` a required property of an
        ``EmailSubmission``, and Stalwart 1.0.0 wants it inside the value as
        well; the key of the map is the id of the submission - the id of the
        email is unique enough.
        """
        return {
            key: {"emailId": value.get("emailId"), "identityId": value.get("identityId")}
            for key, value in create.items()
            if isinstance(value, dict)
        }

    @staticmethod
    def on_success_update_email(args: dict, dialect: str) -> dict:
        """The ``onSuccessUpdateEmail`` argument in the dialect of the server.

        The logical form is ``{"update": {emailId: {"mailboxIds": {...}}}}``.
        Stalwart 1.0.0 wants it as it is (without the account id); RFC 8621
        §7.5 wants a map of the id of the submission (``"#"`` plus the
        creation id) to the patch of the message.
        """
        raw = args.get("onSuccessUpdateEmail")
        if not isinstance(raw, dict) or not raw:
            return {}
        update = raw.get("update")
        if not isinstance(update, dict) or not update:
            return raw
        if dialect == DIALECT_STALWART:
            return {
                "update": {
                    email_id: {"mailboxIds": {key: True for key, value in (patch.get("mailboxIds") or {}).items() if value}}
                    for email_id, patch in update.items()
                    if isinstance(patch, dict)
                }
            }
        create = args.get("create") if isinstance(args.get("create"), dict) else {}
        creation_id = next(iter(create), "")
        translated: dict[str, Any] = {}
        for patch in update.values():
            if not isinstance(patch, dict):
                continue
            entries: dict[str, Any] = {}
            for mailbox_id, wanted in (patch.get("mailboxIds") or {}).items():
                entries[f"mailboxIds/{mailbox_id}"] = True if wanted else None
            translated[f"#{creation_id}"] = entries
        return translated


def _origin(url: str) -> tuple[str, str, int]:
    """Scheme, host and port of a URL, with the default port filled in."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    return scheme, (parts.hostname or "").lower(), parts.port or DEFAULT_PORTS.get(scheme, 0)


def _positive_int(value: Any, default: int = 0) -> int:
    """An integer out of an argument, the default when there is none."""
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def _intersect(lists: Sequence[Sequence[str]]) -> list[str]:
    """The ids that are in every list - the order of the first one is kept."""
    if not lists:
        return []
    if len(lists) == 1:
        return list(lists[0])
    others = [set(item) for item in lists[1:]]
    return [item for item in lists[0] if all(item in group for group in others)]


def _chunks(items: Sequence[str], size: int) -> list[list[str]]:
    """The items in pieces of ``size``."""
    return [list(items[index:index + size]) for index in range(0, len(items), size)]


def _header_parts(value: Any) -> tuple[str, str]:
    """The name of a header and the text to look for in it.

    RFC 8621 §4.4.1: the header condition is an array of the name of the
    header field and (optional) the text to look for. The form of a draft
    (``{"name": ..., "values": [...]}``) is understood as well.
    """
    if isinstance(value, (list, tuple)):
        parts = [str(item) for item in value if item is not None]
        name = parts[0] if parts else ""
        needle = parts[1] if len(parts) > 1 else ""
        return name, needle
    if isinstance(value, dict):
        name = str(value.get("name") or "")
        values = value.get("values")
        if isinstance(values, (list, tuple)):
            needle = str(values[0]) if values else ""
        else:
            needle = str(value.get("value") or "")
        return name, needle
    return str(value or ""), ""


def _address_text(item: Any) -> str:
    """The text of one address of a message - the address and the name."""
    if not isinstance(item, dict):
        return str(item or "")
    parts = [str(item.get("email") or "")]
    name = item.get("name")
    if isinstance(name, str) and name:
        parts.append(name)
    return " ".join(part for part in parts if part)


class JmapClient:
    """Async JMAP client bound to one upstream (Stalwart).

    The session document is cached in memory for ``session_cache_ttl_sec``
    seconds. The cache key is the sha256 of the bearer - the bearer itself is
    never put into the cache. The dialect of the upstream is asked once and
    remembered for the process as well.
    """

    def __init__(self, settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._settings = settings or get_settings()
        self._transport = transport
        self._cache: dict[str, tuple[float, JmapSession]] = {}
        self._dialects: dict[str, Dialect] = {}

    # -- session ----------------------------------------------------------

    async def session(self, bearer: str) -> JmapSession:
        """The JMAP session of the account behind the bearer (cached)."""
        ttl = self._settings.stalwart.session_cache_ttl_sec
        now = time.monotonic()
        correlation = fingerprint(bearer)
        if ttl > 0:
            cached = self._cache.get(self._cache_key(bearer))
            if cached is not None and cached[0] > now:
                logger.info("jmap session taken from the cache (correlation %s)", correlation)
                return cached[1]

        session = await self._fetch_session(bearer)
        if ttl > 0:
            self._cache[self._cache_key(bearer)] = (now + ttl, session)
        logger.info("jmap session fetched (correlation %s)", correlation)
        return session

    def clear_cache(self) -> None:
        """Forget every cached session."""
        self._cache.clear()

    def _cache_key(self, bearer: str) -> str:
        return hashlib.sha256(bearer.encode("utf-8")).hexdigest()

    async def _fetch_session(self, bearer: str) -> JmapSession:
        base = self._settings.stalwart.url
        path = self._settings.stalwart.session_path or WELL_KNOWN_SESSION_PATH
        response = await self._request(bearer, "GET", f"{base}{path}")
        if response.status_code == 404 and path != WELL_KNOWN_SESSION_PATH:
            response = await self._request(bearer, "GET", f"{base}{WELL_KNOWN_SESSION_PATH}")
        if response.status_code >= 400:
            raise ToolError(self._status_text(response, "read the JMAP session"))

        try:
            document = response.json()
        except ValueError:
            raise ToolError("the mail server answered the session request with something that is not JSON") from None
        if not isinstance(document, dict):
            raise ToolError("the mail server answered the session request with an unexpected document")

        session = self._parse_session(document)
        self._check_upstream(session)
        return session

    @staticmethod
    def _parse_session(document: dict) -> JmapSession:
        api_url = str(document.get("apiUrl") or "")
        if not api_url:
            raise ToolError("the JMAP session document has no apiUrl - the server cannot be called")

        primary = document.get("primaryAccounts") or {}
        account_id = str(primary.get(MAIL_URN) or "") if isinstance(primary, dict) else ""
        if not account_id:
            raise ToolError(
                "the JMAP session document has no mail account (primaryAccounts[urn:ietf:params:jmap:mail] is missing) "
                "- the bearer may have no mail rights"
            )

        raw_capabilities = document.get("capabilities") or {}
        if isinstance(raw_capabilities, dict):
            capabilities = tuple(str(name) for name in raw_capabilities)
        elif isinstance(raw_capabilities, (list, tuple)):
            capabilities = tuple(str(name) for name in raw_capabilities)
        else:
            capabilities = ()

        state = document.get("state")
        return JmapSession(
            api_url=api_url,
            download_url=str(document.get("downloadUrl") or ""),
            upload_url=str(document.get("uploadUrl") or ""),
            account_id=account_id,
            capabilities=capabilities,
            state=state if isinstance(state, str) else None,
        )

    def _check_upstream(self, session: JmapSession) -> None:
        """The URLs of the session must point to the configured upstream.

        The session document is data, not trust: if it points somewhere else the
        proxy refuses to call it instead of leaking the bearer to another host.
        """
        expected = _origin(self._settings.stalwart.url)
        for label, url in (
            ("apiUrl", session.api_url),
            ("downloadUrl", session.download_url),
            ("uploadUrl", session.upload_url),
        ):
            if not url:
                continue
            found = _origin(url)
            if found != expected:
                raise ToolError(
                    f"the JMAP session points {label} to host '{found[1]}' while the configured upstream is "
                    f"'{expected[1]}' - the call was not made; check stalwart.url"
                )

    def using(self, session: JmapSession) -> tuple[str, ...]:
        """The ``using`` list of a request: what the server has and we speak."""
        advertised = set(session.capabilities)
        common = tuple(name for name in KNOWN_CAPABILITIES if name in advertised)
        return common or tuple(session.capabilities)

    # -- the dialect ------------------------------------------------------

    async def dialect(self, bearer: str, session: JmapSession) -> Dialect:
        """The dialect of the upstream - asked once and then remembered."""
        configured = str(self._settings.stalwart.dialect or DIALECT_AUTO).strip().lower()
        if configured == DIALECT_RFC:
            return Dialect(DIALECT_RFC)
        if configured == DIALECT_STALWART:
            return Dialect(DIALECT_STALWART)

        cached = self._dialects.get(session.api_url)
        if cached is not None:
            return cached
        detected = await self._detect_dialect(bearer, session)
        dialect = Dialect(detected)
        self._dialects[session.api_url] = dialect
        logger.info("the upstream speaks the %s dialect of JMAP", dialect.name)
        return dialect

    async def _detect_dialect(self, bearer: str, session: JmapSession) -> str:
        """Ask the server which dialect it speaks - a read-only probe.

        Stalwart 1.0.0 accepts exactly one condition per ``Email/query`` and
        answers HTTP 400 (``notRequest``) to a query with two of them; RFC
        8620 servers answer 200. The probe changes nothing.
        """
        mailbox_id = await self._first_mailbox_id(bearer, session)
        if mailbox_id:
            conditions: list[dict] = [{"notKeyword": "$seen"}, {"inMailbox": mailbox_id}]
        else:
            # No folder to point at - two conditions without an id do the same.
            logger.info("the dialect probe found no folder - the probe asks two conditions without one")
            conditions = [{"notKeyword": "$seen"}, {"hasAttachment": False}]

        probe = {
            "accountId": session.account_id,
            "filter": {"operator": "and", "conditions": conditions},
            "limit": 1,
            "position": 0,
        }
        payload = {
            "using": list(self.using(session)),
            "methodCalls": [["Email/query", probe, "c0"]],
        }
        response = await self._request(bearer, "POST", session.api_url, json=payload)
        if response.status_code == 400:
            logger.info("the upstream refuses a query with two conditions - it is the Stalwart dialect")
            return DIALECT_STALWART
        if response.status_code >= 400:
            logger.info("the dialect probe answered HTTP %s - the RFC dialect is assumed", response.status_code)
            return DIALECT_RFC
        return DIALECT_RFC

    async def _first_mailbox_id(self, bearer: str, session: JmapSession) -> str:
        """The id of the first folder of the account - "" when there is none."""
        try:
            payload = await self._call_method(
                bearer,
                session,
                "Mailbox/get",
                {"accountId": session.account_id, "properties": ["id", "role"]},
            )
        except ToolError as exc:
            logger.info("the dialect probe could not read the folders (%s)", exc)
            return ""
        items = payload.get("list") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return ""
        for item in items:
            if isinstance(item, dict) and item.get("id"):
                return str(item["id"])
        return ""

    async def account_state(self, bearer: str, session: JmapSession) -> str | None:
        """The account state of the session - the token of ``Email/changes``.

        It is the ``state`` of the answer of a method (RFC 8620 §5.1), not the
        ``state`` of the session document: the session state is not accepted by
        ``Email/changes``.
        """
        try:
            payload = await self._call_method(
                bearer, session, "Mailbox/get", {"accountId": session.account_id, "properties": ["id"]}
            )
        except ToolError:
            return None
        state = payload.get("state") if isinstance(payload, dict) else None
        return state if isinstance(state, str) and state else None

    # -- method calls -----------------------------------------------------

    async def call(
        self,
        bearer: str,
        session: JmapSession,
        methods: Sequence[tuple[str, dict]],
        action: str | None = None,
    ) -> JmapEnvelope:
        """Call one or more JMAP methods in a single request.

        The arguments are translated into the dialect of the upstream first.
        Method level errors arrive inside HTTP 200 - they are turned into a
        ``ToolError`` carrying the message of the JMAP error.
        """
        if not methods:
            raise ValueError("no method calls given")

        dialect = await self.dialect(bearer, session)
        results: list[JmapMethodResult] = []
        batch: list[tuple[str, dict]] = []
        state: str | None = None

        for name, args in methods:
            if name == "Email/query" and dialect.is_stalwart:
                # Stalwart takes one condition per query: the query of several
                # conditions is done here, in several requests.
                results.append(JmapMethodResult(name, await self._query_stalwart(bearer, session, args)))
                continue
            batch.append((name, self._translate(name, args, dialect)))

        if batch:
            envelope = await self._send(bearer, session, batch, action)
            results.extend(envelope.results)
            state = envelope.state

        return JmapEnvelope(results=tuple(results), state=state)

    def _translate(self, name: str, args: dict, dialect: Dialect) -> dict:
        """The arguments of a method in the dialect of the upstream."""
        if name == "Email/set":
            return self._translate_email_set(args, dialect)
        if name == "EmailSubmission/set":
            return self._translate_submission_set(args, dialect)
        return args

    @staticmethod
    def _translate_email_set(args: dict, dialect: Dialect) -> dict:
        """``Email/set``: the folders of a message in the dialect."""
        update = args.get("update")
        if not isinstance(update, dict):
            return args
        translated = dict(args)
        new_update: dict[str, Any] = {}
        for email_id, patch in update.items():
            if not isinstance(patch, dict):
                new_update[email_id] = patch
                continue
            folders = patch.get("mailboxIds")
            if not isinstance(folders, dict):
                new_update[email_id] = patch
                continue
            new_patch = {key: value for key, value in patch.items() if key != "mailboxIds"}
            new_patch.update(Dialect.mailbox_patch(folders, dialect.name))
            new_update[email_id] = new_patch
        translated["update"] = new_update
        return translated

    @staticmethod
    def _translate_submission_set(args: dict, dialect: Dialect) -> dict:
        """``EmailSubmission/set``: the submission in the dialect."""
        translated = dict(args)
        create = args.get("create")
        if isinstance(create, dict) and create:
            translated["create"] = Dialect.submission_create(create)
        if "onSuccessUpdateEmail" in args:
            translated["onSuccessUpdateEmail"] = Dialect.on_success_update_email(args, dialect.name)
        return translated

    async def _send(
        self,
        bearer: str,
        session: JmapSession,
        methods: Sequence[tuple[str, dict]],
        action: str | None = None,
    ) -> JmapEnvelope:
        """One batched JMAP request - the answers of the methods."""
        call_ids = {f"c{index}": name for index, (name, _args) in enumerate(methods)}
        payload = {
            "using": list(self.using(session)),
            "methodCalls": [
                [name, args, f"c{index}"] for index, (name, args) in enumerate(methods)
            ],
        }

        response = await self._request(bearer, "POST", session.api_url, json=payload)
        if response.status_code >= 400:
            raise ToolError(self._status_text(response, "call the JMAP server"))

        try:
            body = response.json()
        except ValueError:
            raise ToolError("the mail server answered the method call with something that is not JSON") from None
        if not isinstance(body, dict):
            raise ToolError("the mail server answered the method call with an unexpected document")

        responses = body.get("methodResponses")
        if not isinstance(responses, list):
            # Some servers answer with the short key of the draft documents.
            responses = body.get("responses")
        if not isinstance(responses, list):
            raise ToolError("the mail server answered the method call without a responses array")

        results: list[JmapMethodResult] = []
        for item in responses:
            if not isinstance(item, (list, tuple)) or len(item) < 3:
                raise ToolError("the mail server answered the method call with a malformed responses array")
            kind, payload_part, call_id = item[0], item[1], item[2]
            if kind == "error":
                raise ToolError(self._error_text(call_ids.get(call_id), payload_part))
            self._check_failure(str(kind), payload_part, action)
            results.append(JmapMethodResult(name=str(kind), result=payload_part))

        state = body.get("state")
        if not isinstance(state, str):
            state = body.get("sessionState")
        return JmapEnvelope(results=tuple(results), state=state if isinstance(state, str) else None)

    async def _call_method(self, bearer: str, session: JmapSession, method: str, args: dict) -> Any:
        """One method call - the payload of the answer, errors included."""
        envelope = await self._send(bearer, session, [(method, args)])
        return envelope.result_of(method)

    @staticmethod
    def _check_failure(method: str, payload: Any, action: str | None = None) -> None:
        """A method that failed is never a success.

        The answer of a ``/set`` method carries the failures in ``notCreated``,
        ``notUpdated`` and ``notFound``. They arrive inside HTTP 200 and have
        to become an error of the tool.
        """
        if not isinstance(payload, dict) or not method.endswith("/set"):
            return

        problems: list[str] = []
        not_created = payload.get("notCreated")
        if isinstance(not_created, dict) and not_created:
            key, error = next(iter(not_created.items()))
            problems.append(
                f"{len(not_created)} object(s) were not created ({key}): {JmapClient._set_error_text(error)}"
            )
        not_updated = payload.get("notUpdated")
        if isinstance(not_updated, dict) and not_updated:
            key, error = next(iter(not_updated.items()))
            problems.append(
                f"{len(not_updated)} object(s) were not updated ({key}): {JmapClient._set_error_text(error)}"
            )
        not_found = payload.get("notFound")
        if isinstance(not_found, list) and not_found:
            listed = ", ".join(str(item) for item in list(not_found)[:5])
            problems.append(f"{len(not_found)} object(s) do not exist in this account ({listed})")
        if not problems:
            return

        where = f"'{method}'"
        if action:
            where = f"{where} ({action})"
        raise JmapMethodFailure(
            f"the mail server refused the JMAP call {where}: " + "; ".join(problems),
            method,
            payload,
        )

    @staticmethod
    def _set_error_text(error: Any) -> str:
        """The text of a per-id error of the answer of a ``/set`` method."""
        if isinstance(error, dict):
            kind = str(error.get("type") or "jmapError")
            code = str(error.get("code") or "")
            description = str(error.get("description") or "")
            text = f"{kind}: {code}" if code else kind
            return f"{text} - {description}" if description else text
        return str(error)

    @staticmethod
    def _error_text(method: str | None, error: Any) -> str:
        """An actionable text for a JMAP method error."""
        if isinstance(error, dict):
            kind = str(error.get("type") or "jmapError")
            code = str(error.get("code") or "")
            description = str(error.get("description") or "")
            text = f"{kind}: {code}" if code else kind
            if description:
                text = f"{text} - {description}"
        else:
            text = str(error)
        where = f"'{method}'" if method else "the method"
        return (
            f"the mail server refused the JMAP call {where}: {text}. "
            "Check the capabilities reported by whoami - the account may not support this method."
        )

    # -- the query of the Stalwart dialect ---------------------------------

    async def _query_stalwart(self, bearer: str, session: JmapSession, args: dict) -> dict:
        """``Email/query`` in the dialect of Stalwart 1.0.0.

        Stalwart accepts exactly one condition per query, so a filter with
        several conditions is asked one condition at a time and the ids are
        intersected here; the order is the order of the first query. The
        ``header`` condition is not supported by Stalwart at all - it is
        applied to the messages that are loaded anyway.
        """
        conditions = Dialect.conditions_of(args.get("filter"))
        headers = [item for item in conditions if Dialect.is_header(item)]
        server = [item for item in conditions if not Dialect.is_header(item)]

        limit = _positive_int(args.get("limit"), 0)
        position = _positive_int(args.get("position"), 0)
        base = {key: value for key, value in args.items() if key not in ("filter", "limit", "position")}

        if not headers and len(server) <= 1:
            # One condition (or none) - the server does the paging itself.
            plain = dict(args)
            if server:
                plain["filter"] = server[0]
            payload = await self._call_method(bearer, session, "Email/query", plain)
            return payload if isinstance(payload, dict) else {}

        if server:
            found: list[list[str]] = []
            for condition in server:
                found.append(await self._fetch_ids(bearer, session, {**base, "filter": condition}))
            ids = _intersect(found)
        else:
            ids = await self._fetch_ids(bearer, session, dict(base))

        if headers:
            ids = await self._filter_by_headers(bearer, session, ids, headers)

        page = ids[position:position + limit] if limit else ids[position:]
        return {
            "accountId": session.account_id,
            "ids": page,
            "total": len(ids),
            "position": position,
            "limit": limit,
        }

    async def _fetch_ids(self, bearer: str, session: JmapSession, args: dict) -> list[str]:
        """All the ids of a query - paged, but bounded."""
        ids: list[str] = []
        position = 0
        total: int | None = None
        for _page in range(INTERSECTION_MAX_PAGES):
            payload = await self._call_method(
                bearer,
                session,
                "Email/query",
                {**args, "position": position, "limit": INTERSECTION_LIMIT},
            )
            if not isinstance(payload, dict):
                return ids
            page = [str(item) for item in (payload.get("ids") or []) if isinstance(item, (str, int))]
            ids.extend(page)
            if total is None and isinstance(payload.get("total"), int) and not isinstance(payload.get("total"), bool):
                total = payload["total"]
            if not page or (total is not None and len(ids) >= total):
                return ids
            position += len(page)
        raise ToolError(
            "the mail server accepts only one condition per query, so the proxy has to intersect the results "
            f"itself - and this filter matches more than {INTERSECTION_LIMIT * INTERSECTION_MAX_PAGES} messages. "
            "Narrow the filter (one folder, one flag) and try again."
        )

    async def _filter_by_headers(
        self,
        bearer: str,
        session: JmapSession,
        ids: Sequence[str],
        headers: Sequence[dict],
    ) -> list[str]:
        """The header condition of Stalwart - applied to the messages here."""
        wanted: list[tuple[str, str]] = []
        for condition in headers:
            name, needle = _header_parts(condition.get("header"))
            if name.strip().lower() not in ("subject", "from", "to"):
                raise ToolError(
                    f"the mail server does not support the filter on the header '{name}' - the proxy can only "
                    "filter by 'subject', 'from' and 'to' itself. Search for another header with the text search."
                )
            wanted.append((name.strip().lower(), needle.strip().lower()))

        items: list[dict] = []
        for chunk in _chunks(list(ids), HEADER_BATCH):
            payload = await self._call_method(
                bearer,
                session,
                "Email/get",
                {
                    "accountId": session.account_id,
                    "ids": chunk,
                    "properties": ["id", "subject", "from", "to"],
                },
            )
            raw = payload.get("list") if isinstance(payload, dict) else None
            if isinstance(raw, list):
                items.extend(item for item in raw if isinstance(item, dict))

        kept: list[str] = []
        for item in items:
            email_id = str(item.get("id") or "")
            if not email_id:
                continue
            if all(_header_matches(item, name, needle) for name, needle in wanted):
                kept.append(email_id)
        return kept

    # -- blobs ------------------------------------------------------------

    async def download_blob(
        self,
        bearer: str,
        session: JmapSession,
        blob_id: str,
        name: str | None = None,
        content_type: str = DEFAULT_CONTENT_TYPE,
    ) -> tuple[bytes, str]:
        """Download a blob of the account. Returns the bytes and the mime type."""
        if not session.download_url:
            raise ToolError("the JMAP session document has no downloadUrl - attachments cannot be downloaded")
        url = self._blob_url(session.download_url, session.account_id, blob_id, name, content_type)
        response = await self._request(bearer, "GET", url)
        if response.status_code >= 400:
            raise ToolError(self._status_text(response, "download the attachment"))
        limit = self._settings.stalwart.max_attachment_mb * 1024 * 1024
        content = response.content
        if len(content) > limit:
            raise ToolError(
                f"the attachment is bigger than the configured limit of {self._settings.stalwart.max_attachment_mb} MB"
            )
        return content, response.headers.get("content-type", DEFAULT_CONTENT_TYPE)

    async def upload_blob(
        self,
        bearer: str,
        session: JmapSession,
        filename: str,
        content: bytes,
        content_type: str = DEFAULT_CONTENT_TYPE,
    ) -> str:
        """Upload a file to the blob store of the account. Returns the blob id."""
        if not session.upload_url:
            raise ToolError("the JMAP session document has no uploadUrl - files cannot be uploaded")
        limit = self._settings.stalwart.max_attachment_mb * 1024 * 1024
        if len(content) > limit:
            raise ToolError(
                f"the file is bigger than the configured limit of {self._settings.stalwart.max_attachment_mb} MB"
            )
        url = session.upload_url.replace("{accountId}", quote(session.account_id, safe=""))
        response = await self._request(
            bearer,
            "POST",
            url,
            files={"file": (filename, content, content_type)},
        )
        if response.status_code >= 400:
            raise ToolError(self._status_text(response, "upload the file"))
        try:
            body = response.json()
        except ValueError:
            raise ToolError("the mail server answered the upload with something that is not JSON") from None
        blob_id = str((body or {}).get("blobId") or "")
        if not blob_id:
            raise ToolError("the mail server accepted the upload but returned no blob id")
        return blob_id

    @staticmethod
    def _blob_url(
        template: str,
        account_id: str,
        blob_id: str,
        name: str | None,
        content_type: str = DEFAULT_CONTENT_TYPE,
    ) -> str:
        url = template.replace("{accountId}", quote(account_id, safe=""))
        url = url.replace("{blobId}", quote(blob_id, safe=""))
        if "{name}" in url:
            url = url.replace("{name}", quote(name or "file", safe=""))
        elif not url.rstrip("/").endswith(quote(blob_id, safe="")):
            url = f"{url.rstrip('/')}/{quote(blob_id, safe='')}"
        # The type of the file is part of the URL of Stalwart 1.0.0.
        quoted = quote(content_type or DEFAULT_CONTENT_TYPE, safe="")
        url = url.replace("{type}", quoted).replace("{accept}", quoted)
        return url

    # -- transport --------------------------------------------------------

    async def _request(self, bearer: str, method: str, url: str, **kwargs: Any) -> httpx.Response:
        headers = {"authorization": f"Bearer {bearer}", "accept": "application/json"}
        timeout = self._settings.stalwart.request_timeout_sec
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=timeout,
                follow_redirects=False,
            ) as client:
                return await client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            host = _origin(url)[1]
            raise ToolError(
                f"could not reach the mail server at '{host}': {type(exc).__name__}. Check stalwart.url and the network."
            ) from None

    @staticmethod
    def _status_text(response: httpx.Response, action: str) -> str:
        code = response.status_code
        text = f"the mail server answered HTTP {code} while the proxy tried to {action}"
        if code in (401, 403):
            text += ". The bearer was rejected by the server - check the API key"
        elif code == 404:
            text += ". The path does not exist on the server"
        return text


def _header_matches(item: dict, name: str, needle: str) -> bool:
    """Does the message match the header condition?"""
    if name == "subject":
        haystack = [str(item.get("subject") or "")]
    elif name == "from":
        raw = item.get("from")
        haystack = [_address_text(address) for address in raw] if isinstance(raw, list) else []
    elif name == "to":
        raw = item.get("to")
        haystack = [_address_text(address) for address in raw] if isinstance(raw, list) else []
    else:  # pragma: no cover - the names are checked before the fetch
        return False
    if not needle:
        return bool(any(text.strip() for text in haystack))
    return any(needle in text.lower() for text in haystack)
