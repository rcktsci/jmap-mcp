"""JMAP client: session, batched method calls, blob up/download.

The client is a pure proxy. The only credential it uses is the bearer of the
request that is being served; it is sent upstream as-is, never stored and never
logged. Everything the client raises is a ``ToolError``: the MCP SDK turns it
into a tool error (``isError: true``) with an actionable message for the model.
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


def _origin(url: str) -> tuple[str, str, int]:
    """Scheme, host and port of a URL, with the default port filled in."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    return scheme, (parts.hostname or "").lower(), parts.port or DEFAULT_PORTS.get(scheme, 0)


class JmapClient:
    """Async JMAP client bound to one upstream (Stalwart).

    The session document is cached in memory for ``session_cache_ttl_sec``
    seconds. The cache key is the sha256 of the bearer - the bearer itself is
    never put into the cache.
    """

    def __init__(self, settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._settings = settings or get_settings()
        self._transport = transport
        self._cache: dict[str, tuple[float, JmapSession]] = {}

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

    # -- method calls -----------------------------------------------------

    async def call(
        self,
        bearer: str,
        session: JmapSession,
        methods: Sequence[tuple[str, dict]],
    ) -> JmapEnvelope:
        """Call one or more JMAP methods in a single request.

        Method level errors arrive inside HTTP 200 - they are turned into a
        ``ToolError`` carrying the message of the JMAP error.
        """
        if not methods:
            raise ValueError("no method calls given")

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
            results.append(JmapMethodResult(name=str(kind), result=payload_part))

        state = body.get("state")
        if not isinstance(state, str):
            state = body.get("sessionState")
        return JmapEnvelope(results=tuple(results), state=state if isinstance(state, str) else None)

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

    # -- blobs ------------------------------------------------------------

    async def download_blob(
        self,
        bearer: str,
        session: JmapSession,
        blob_id: str,
        name: str | None = None,
    ) -> tuple[bytes, str]:
        """Download a blob of the account. Returns the bytes and the mime type."""
        if not session.download_url:
            raise ToolError("the JMAP session document has no downloadUrl - attachments cannot be downloaded")
        url = self._blob_url(session.download_url, session.account_id, blob_id, name)
        response = await self._request(bearer, "GET", url)
        if response.status_code >= 400:
            raise ToolError(self._status_text(response, "download the attachment"))
        limit = self._settings.stalwart.max_attachment_mb * 1024 * 1024
        content = response.content
        if len(content) > limit:
            raise ToolError(
                f"the attachment is bigger than the configured limit of {self._settings.stalwart.max_attachment_mb} MB"
            )
        return content, response.headers.get("content-type", "application/octet-stream")

    async def upload_blob(
        self,
        bearer: str,
        session: JmapSession,
        filename: str,
        content: bytes,
        content_type: str = "application/octet-stream",
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
    def _blob_url(template: str, account_id: str, blob_id: str, name: str | None) -> str:
        url = template.replace("{accountId}", quote(account_id, safe=""))
        url = url.replace("{blobId}", quote(blob_id, safe=""))
        if "{name}" in url:
            url = url.replace("{name}", quote(name or "file", safe=""))
        elif not url.rstrip("/").endswith(quote(blob_id, safe="")):
            url = f"{url.rstrip('/')}/{quote(blob_id, safe='')}"
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
