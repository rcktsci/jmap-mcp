"""The dialect layer: the forms of Stalwart 1.0.0 next to the forms of the RFC.

The mock server of this file speaks the dialect of Stalwart 1.0.0: it takes
exactly one condition per ``Email/query``, does not know the ``header``
filter, wants the folders of a message as a map, wants the id of the email
inside the submission and answers ``notCreated``/``notUpdated`` when the form
is wrong. Every test of this file asserts that the proxy never causes a
single 400 against it.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone
from typing import Any, Callable

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from src.config import Settings
from src.mcp import tools
from src.mcp.jmap import JmapClient
from tests.conftest import jmap_calls

ACCOUNT = "j"

#: The session document of the mock - the shapes of the live Stalwart.
SESSION = {
    "capabilities": {
        "urn:ietf:params:jmap:core": {},
        "urn:ietf:params:jmap:mail": {},
        "urn:ietf:params:jmap:submission": {},
    },
    "accounts": {ACCOUNT: {"name": "me", "isPersonal": True}},
    "primaryAccounts": {"urn:ietf:params:jmap:mail": ACCOUNT},
    "username": "katren@tech.rocketscien.se",
    "apiUrl": "https://mail.example.com/jmap/",
    "downloadUrl": "https://mail.example.com/jmap/download/{accountId}/{blobId}/{name}?accept={type}",
    "uploadUrl": "https://mail.example.com/jmap/upload/{accountId}/",
    "state": "cbb4eb6c",
}

#: The account state of the mock - the token of Email/changes.
ACCOUNT_STATE = "sau"

FOLDERS = [
    {"id": "mb-inbox", "name": "Inbox", "role": "inbox"},
    {"id": "mb-drafts", "name": "Drafts", "role": "drafts"},
    {"id": "mb-sent", "name": "Sent", "role": "sent"},
    {"id": "mb-trash", "name": "Trash", "role": "trash"},
]

MESSAGES = [
    {
        "id": "e1",
        "threadId": "t1",
        "subject": "Report",
        "from": [{"email": "boss@example.com", "name": "Boss"}],
        "to": [{"email": "me@example.com", "name": None}],
        "receivedAt": "2026-10-20T10:00:00Z",
        "preview": "the numbers",
        "keywords": ["$seen"],
        "attachments": [
            {"blobId": "blob-1", "name": "report.pdf", "type": "application/pdf", "size": 8, "isInline": False}
        ],
        "mailboxIds": {"mb-inbox": True},
    },
    {
        "id": "e2",
        "threadId": "t2",
        "subject": "Hello",
        "from": [{"email": "alice@example.com", "name": "Alice"}],
        "to": [{"email": "me@example.com", "name": None}],
        "receivedAt": "2026-10-21T10:00:00Z",
        "preview": "hi there",
        "keywords": [],
        "attachments": [],
        "mailboxIds": {"mb-inbox": True},
    },
    {
        "id": "e4",
        "threadId": "t4",
        "subject": "Later",
        "from": [{"email": "alice@example.com", "name": "Alice"}],
        "to": [{"email": "me@example.com", "name": None}],
        "receivedAt": "2026-10-19T10:00:00Z",
        "preview": "later",
        "keywords": [],
        "attachments": [],
        "mailboxIds": {"mb-inbox": True},
    },
    {
        "id": "e3",
        "threadId": "t3",
        "subject": "Draft of the report",
        "from": [{"email": "me@example.com", "name": None}],
        "to": [{"email": "you@example.com", "name": "You"}],
        "receivedAt": "2026-10-22T10:00:00Z",
        "preview": "a draft",
        "keywords": ["$draft"],
        "attachments": [],
        "mailboxIds": {"mb-drafts": True},
    },
    {
        "id": "e5",
        "threadId": "t5",
        "subject": "Doomed draft",
        "from": [{"email": "me@example.com", "name": None}],
        "to": [{"email": "you@example.com", "name": "You"}],
        "receivedAt": "2026-10-23T10:00:00Z",
        "preview": "doomed",
        "keywords": ["$draft"],
        "attachments": [],
        "mailboxIds": {"mb-drafts": True},
    },
]

IDENTITIES = {
    "accountId": ACCOUNT,
    "list": [{"id": "id-1", "email": "me@example.com", "name": "Me", "replyTo": []}],
    "notFound": [],
}

#: The draft the mock refuses to send.
DOOMED = "e5"


class Refused(Exception):
    """The mock refuses the request the way Stalwart does - HTTP 400."""


def _stamp(value: str) -> datetime:
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _matches(message: dict, condition: dict) -> bool:
    """Does the message match the one condition of the query?"""
    for key, value in condition.items():
        if key == "inMailbox":
            if not isinstance(value, str):
                raise Refused("the filter inMailbox takes one id, not a list")
            if value not in message["mailboxIds"]:
                return False
        elif key == "notKeyword":
            if value in message["keywords"]:
                return False
        elif key == "hasKeyword":
            if value not in message["keywords"]:
                return False
        elif key == "hasAttachment":
            if bool(message["attachments"]) != bool(value):
                return False
        elif key == "text":
            haystack = f"{message['subject']} {message['preview']}".lower()
            if str(value).lower() not in haystack:
                return False
        elif key == "after":
            if _stamp(message["receivedAt"]) <= _stamp(str(value)):
                return False
        elif key == "before":
            if _stamp(message["receivedAt"]) >= _stamp(str(value)):
                return False
        else:
            raise Refused(f"the filter {key} is not supported")
    return True


def _query(args: dict) -> dict:
    """``Email/query`` of the mock - one condition, no operator."""
    filter_ = args.get("filter")
    if isinstance(filter_, dict):
        if "operator" in filter_:
            raise Refused("the query must have exactly one condition")
        if "header" in filter_:
            raise Refused("the filter on a header is not supported")
    conditions = [filter_] if isinstance(filter_, dict) and filter_ else []
    found = [message for message in MESSAGES if all(_matches(message, c) for c in conditions)]
    for entry in args.get("sort") or []:
        if entry.get("property") == "receivedAt":
            found.sort(key=lambda message: message["receivedAt"], reverse=not entry.get("isAscending"))
    position = int(args.get("position") or 0)
    limit = int(args.get("limit") or len(found))
    return {
        "accountId": ACCOUNT,
        "ids": [message["id"] for message in found[position:position + limit]],
        "total": len(found),
        "position": position,
    }


def _get(args: dict) -> dict:
    """``Email/get`` of the mock."""
    wanted = [str(item) for item in (args.get("ids") or [])]
    properties = args.get("properties")
    items = []
    for message in MESSAGES:
        if message["id"] not in wanted:
            continue
        if isinstance(properties, list):
            items.append({key: message[key] for key in properties if key in message})
        else:
            items.append(dict(message))
    return {
        "accountId": ACCOUNT,
        "list": items,
        "notFound": [item for item in wanted if item not in {m["id"] for m in MESSAGES}],
    }


def _set(args: dict) -> dict:
    """``Email/set`` of the mock - the folders are a map."""
    updated: dict[str, Any] = {}
    not_updated: dict[str, Any] = {}
    not_found: list[str] = []
    for email_id, patch in (args.get("update") or {}).items():
        if email_id not in {message["id"] for message in MESSAGES}:
            not_found.append(email_id)
            continue
        if not isinstance(patch, dict):
            not_updated[email_id] = {"type": "invalidProperties", "code": "invalidProperties"}
            continue
        folders = patch.get("mailboxIds")
        if isinstance(folders, list):
            raise Refused("the folders of a message are a map, not a list")
        if isinstance(folders, dict) and any("/" in str(key) for key in folders):
            raise Refused("the folders of a message are a map, not a list")
        updated[email_id] = None
    return {"accountId": ACCOUNT, "updated": updated, "notUpdated": not_updated, "notFound": not_found}


def _submission_set(args: dict) -> dict:
    """``EmailSubmission/set`` of the mock - the email id is inside the value."""
    created: dict[str, Any] = {}
    not_created: dict[str, Any] = {}
    not_updated: dict[str, Any] = {}
    for key, value in (args.get("create") or {}).items():
        if not isinstance(value, dict) or not value.get("emailId"):
            not_created[key] = {
                "type": "urn:ietf:params:jmap:error:invalidProperties",
                "code": "invalidProperties",
                "description": "the emailId of the submission is missing",
            }
            continue
        if value["emailId"] == DOOMED:
            not_created[key] = {
                "type": "urn:ietf:params:jmap:error:forbidden",
                "code": "forbidden",
                "description": "the account may not send this message",
            }
            continue
        if value["emailId"] not in {message["id"] for message in MESSAGES}:
            not_created[key] = {
                "type": "urn:ietf:params:jmap:error:invalidProperties",
                "code": "invalidProperties",
                "description": "the message does not exist",
            }
            continue
        created[key] = {
            "id": "sub-1",
            "sendAt": "2026-10-27T10:00:00Z",
            "undoStatus": "pending",
        }

    # The folders of the sent message: the map Stalwart wants.
    on_success = args.get("onSuccessUpdateEmail")
    if isinstance(on_success, dict) and on_success:
        update = on_success.get("update")
        if not isinstance(update, dict):
            raise Refused("the argument onSuccessUpdateEmail is not supported")
        for email_id, patch in update.items():
            folders = (patch or {}).get("mailboxIds")
            if isinstance(folders, list):
                not_updated[email_id] = {
                    "type": "invalidProperties",
                    "code": "invalidProperties",
                    "description": "the folders are a map, not a list",
                }
            elif not isinstance(folders, dict):
                raise Refused("the argument onSuccessUpdateEmail is not supported")
    return {"accountId": ACCOUNT, "created": created, "notCreated": not_created, "notUpdated": not_updated}


class StalwartMock:
    """The mock server of the dialect of Stalwart 1.0.0."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.statuses: list[int] = []
        self.probe_statuses: list[int] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self._answer(request)
        if self._is_probe(request):
            # The probe of the dialect is answered with a 400 on purpose.
            self.probe_statuses.append(response.status_code)
        else:
            self.statuses.append(response.status_code)
        return response

    @staticmethod
    def _is_probe(request: httpx.Request) -> bool:
        if request.method != "POST" or not request.content:
            return False
        body = json.loads(request.content)
        for name, args, _call_id in body.get("methodCalls", []):
            if name == "Email/query":
                filter_ = args.get("filter")
                if isinstance(filter_, dict) and "operator" in filter_:
                    return True
        return False

    def _answer(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            if "/download" in request.url.path:
                return httpx.Response(200, content=b"the file", headers={"content-type": "application/pdf"})
            return httpx.Response(200, json=SESSION)
        if request.method == "POST" and "/upload" in request.url.path:
            return httpx.Response(200, json={"accountId": ACCOUNT, "blobId": "blob-1", "type": "application/pdf"})

        body = json.loads(request.content)
        responses: list[list[Any]] = []
        try:
            for name, args, call_id in body.get("methodCalls", []):
                responses.append([name, self._method(name, args), call_id])
        except Refused as exc:
            return httpx.Response(
                400,
                json={"type": "urn:ietf:params:jmap:error:notRequest", "detail": str(exc)},
            )
        return httpx.Response(200, json={"methodResponses": responses, "sessionState": SESSION["state"]})

    @staticmethod
    def _method(name: str, args: dict) -> Any:
        if name == "Email/query":
            return _query(args)
        if name == "Email/get":
            return _get(args)
        if name == "Email/set":
            return _set(args)
        if name == "EmailSubmission/set":
            return _submission_set(args)
        if name == "Identity/get":
            return IDENTITIES
        if name == "Mailbox/get":
            return {"accountId": ACCOUNT, "list": FOLDERS, "notFound": [], "state": ACCOUNT_STATE}
        if name == "Email/changes":
            return {
                "accountId": ACCOUNT,
                "oldState": args.get("sinceState"),
                "newState": ACCOUNT_STATE,
                "hasMoreChanges": False,
                "created": [],
                "updated": [],
                "destroyed": [],
            }
        raise Refused(f"the method {name} is not supported")

    def probes(self) -> list[dict]:
        """The arguments of the dialect probes the proxy sent."""
        found = []
        for args in jmap_calls(self.requests, "Email/query"):
            filter_ = args.get("filter")
            if isinstance(filter_, dict) and "operator" in filter_:
                found.append(args)
        return found

    def queries(self) -> list[dict]:
        """The arguments of the queries of the tools (the probe is not one)."""
        found = []
        for args in jmap_calls(self.requests, "Email/query"):
            filter_ = args.get("filter")
            if isinstance(filter_, dict) and "operator" in filter_:
                continue
            found.append(args)
        return found

    def errors(self) -> list[int]:
        """The answers that were not HTTP 200 (the probe is not one of them)."""
        return [status for status in self.statuses if status >= 400]


@pytest.fixture
def stalwart_settings(settings: Settings) -> Settings:
    """The settings of the mock upstream - the dialect is asked, not told."""
    return dataclasses.replace(
        settings, stalwart=dataclasses.replace(settings.stalwart, dialect="auto")
    )


@pytest.fixture
def mock_stalwart(
    stalwart_settings: Settings,
    make_transport: Callable[[Any], Any],
    monkeypatch: Any,
) -> Callable[[Settings | None], StalwartMock]:
    """Install the mock of the dialect of Stalwart 1.0.0."""

    def build(client_settings: Settings | None = None) -> StalwartMock:
        mock = StalwartMock()
        transport, _seen = make_transport(mock.handler)
        # One client for the whole test: the dialect is asked once and then
        # remembered (in the server it is one client for the process).
        client = JmapClient(client_settings or stalwart_settings, transport=transport)
        monkeypatch.setattr(tools, "get_jmap_client", lambda: client)
        return mock

    return build


# ---------------------------------------------------------------------------
# B1 - the folder of a query is one id
# ---------------------------------------------------------------------------


async def test_the_folder_of_a_query_is_one_id(mock_stalwart: Any, bearer: Any) -> None:
    mock = mock_stalwart()

    result = await tools.list_emails(mailbox_role="inbox")

    queries = mock.queries()
    assert queries[0]["filter"] == {"inMailbox": "mb-inbox"}
    assert result["total"] == 3
    assert mock.errors() == []


# ---------------------------------------------------------------------------
# B2 - several conditions are asked one at a time and intersected here
# ---------------------------------------------------------------------------


async def test_several_conditions_are_asked_one_at_a_time(mock_stalwart: Any, bearer: Any) -> None:
    mock = mock_stalwart()

    result = await tools.list_emails(mailbox_role="inbox", unread=True)

    queries = mock.queries()
    assert [args["filter"] for args in queries] == [{"inMailbox": "mb-inbox"}, {"notKeyword": "$seen"}]
    # The intersection: e1 is seen, e2 and e4 are not - the order of the
    # first query is kept.
    assert result["total"] == 2
    assert [email["id"] for email in result["emails"]] == ["e2", "e4"]
    assert mock.errors() == []


async def test_the_page_is_a_slice_of_the_intersection(mock_stalwart: Any, bearer: Any) -> None:
    mock = mock_stalwart()

    result = await tools.list_emails(mailbox_role="inbox", unread=True, limit=1, position=1)

    assert result["total"] == 2
    assert [email["id"] for email in result["emails"]] == ["e4"]
    assert result["has_more"] is False
    assert result["position"] == 1
    assert mock.errors() == []


async def test_the_count_is_the_size_of_the_intersection(mock_stalwart: Any, bearer: Any) -> None:
    mock = mock_stalwart()

    result = await tools.count_emails(mailbox_role="inbox", unread=True)

    assert result == {"count": 2}
    assert mock.errors() == []


# ---------------------------------------------------------------------------
# B3 - the header filter is applied here
# ---------------------------------------------------------------------------


async def test_the_header_filter_is_applied_by_the_proxy(mock_stalwart: Any, bearer: Any) -> None:
    mock = mock_stalwart()

    result = await tools.search_emails(subject="Report")

    # "Report" and "Draft of the report" - the total is counted again.
    assert result["total"] == 2
    assert sorted(email["id"] for email in result["emails"]) == ["e1", "e3"]
    assert mock.errors() == []


async def test_the_header_filter_of_the_sender(mock_stalwart: Any, bearer: Any) -> None:
    mock = mock_stalwart()

    result = await tools.search_emails(from_address="boss")

    assert result["total"] == 1
    assert result["emails"][0]["id"] == "e1"
    assert mock.errors() == []


async def test_the_header_filter_together_with_a_folder(mock_stalwart: Any, bearer: Any) -> None:
    mock = mock_stalwart()

    result = await tools.search_emails(subject="Report", mailbox_role="inbox")

    # The folder is a condition of its own, the header is filtered here.
    assert result["total"] == 1
    assert result["emails"][0]["id"] == "e1"
    assert mock.errors() == []


# ---------------------------------------------------------------------------
# B4 - the folders of a message are a map
# ---------------------------------------------------------------------------


async def test_the_folders_of_a_message_are_a_map(mock_stalwart: Any, bearer: Any) -> None:
    mock = mock_stalwart()

    result = await tools.move_emails(ids=["e1"], mailbox_role="trash")

    update = jmap_calls(mock.requests, "Email/set")[0]["update"]
    assert update == {"e1": {"mailboxIds": {"mb-trash": True}}}
    assert result["moved"] == ["e1"]
    assert mock.errors() == []


# ---------------------------------------------------------------------------
# B5 - the submission carries the id of the email
# ---------------------------------------------------------------------------


async def test_the_submission_carries_the_id_of_the_email(mock_stalwart: Any, bearer: Any) -> None:
    mock = mock_stalwart()

    result = await tools.send_draft(draft_id="e3")

    args = jmap_calls(mock.requests, "EmailSubmission/set")[0]
    assert args["create"] == {"e3": {"emailId": "e3", "identityId": "id-1"}}
    assert args["onSuccessUpdateEmail"] == {"update": {"e3": {"mailboxIds": {"mb-sent": True}}}}
    assert result["sent_at"].isoformat() == "2026-10-27T10:00:00+00:00"
    assert mock.errors() == []


# ---------------------------------------------------------------------------
# B6 - a failed method is never a success
# ---------------------------------------------------------------------------


async def test_a_refused_submission_is_an_error(mock_stalwart: Any, bearer: Any) -> None:
    mock = mock_stalwart()

    with pytest.raises(ToolError) as failure:
        await tools.send_draft(draft_id=DOOMED)

    assert "refused to send the draft" in str(failure.value)


async def test_a_refused_update_is_an_error(mock_stalwart: Any, bearer: Any) -> None:
    mock = mock_stalwart()

    with pytest.raises(ToolError) as failure:
        await tools.move_emails(ids=["e-does-not-exist"], mailbox_role="trash")

    assert "do not exist" in str(failure.value)


# ---------------------------------------------------------------------------
# B7 - the state of whoami is the account state
# ---------------------------------------------------------------------------


async def test_whoami_returns_the_account_state(mock_stalwart: Any, bearer: Any) -> None:
    mock = mock_stalwart()

    result = await tools.whoami()

    assert result["state"] == ACCOUNT_STATE
    assert result["state"] != SESSION["state"]
    assert mock.errors() == []


async def test_the_changes_are_asked_with_the_account_state(mock_stalwart: Any, bearer: Any) -> None:
    mock = mock_stalwart()

    result = await tools.list_changes(since_state=ACCOUNT_STATE)

    args = jmap_calls(mock.requests, "Email/changes")[0]
    assert args["sinceState"] == ACCOUNT_STATE
    assert result["state"] == ACCOUNT_STATE
    assert mock.errors() == []


# ---------------------------------------------------------------------------
# B8 - the type of the file is part of the download url
# ---------------------------------------------------------------------------


async def test_the_download_url_carries_the_type_of_the_file(
    mock_stalwart: Any, bearer: Any, sandbox: Any
) -> None:
    mock = mock_stalwart()

    result = await tools.download_attachment(email_id="e1", blob_id="blob-1")

    assert result["content_type"] == "application/pdf"
    downloads = [request for request in mock.requests if "/download" in request.url.path]
    assert downloads, "the download url was never used"
    assert "accept=application%2Fpdf" in str(downloads[0].url)
    assert mock.errors() == []


# ---------------------------------------------------------------------------
# The dialect itself
# ---------------------------------------------------------------------------


async def test_the_dialect_is_asked_once(mock_stalwart: Any, bearer: Any) -> None:
    mock = mock_stalwart()

    await tools.list_emails(mailbox_role="inbox")
    await tools.list_emails(mailbox_role="inbox")

    assert len(mock.probes()) == 1
    assert mock.errors() == []


async def test_the_probe_asks_two_conditions(mock_stalwart: Any, bearer: Any) -> None:
    mock = mock_stalwart()

    await tools.list_emails(mailbox_role="inbox")

    probe = mock.probes()[0]
    assert probe["limit"] == 1
    assert probe["filter"]["operator"] == "and"
    assert len(probe["filter"]["conditions"]) == 2
    # The probe is answered with a 400 - that is how the dialect is found.
    assert mock.probe_statuses == [400]
    assert mock.errors() == []


async def test_the_auto_dialect_asks_the_rfc_server(mock_jmap: Any, bearer: Any, settings: Settings) -> None:
    auto = dataclasses.replace(
        settings, stalwart=dataclasses.replace(settings.stalwart, dialect="auto")
    )
    seen = mock_jmap(
        {
            "Mailbox/get": {"accountId": "account-1", "list": [{"id": "mb-inbox", "role": "inbox"}]},
            "Email/query": {"accountId": "account-1", "ids": ["e1"], "total": 1, "position": 0},
            "Email/get": {
                "accountId": "account-1",
                "list": [
                    {
                        "id": "e1",
                        "subject": "Report",
                        "from": [{"email": "boss@example.com", "name": None}],
                        "to": [{"email": "me@example.com", "name": None}],
                        "receivedAt": "2026-10-20T10:00:00Z",
                        "keywords": [],
                        "attachments": [],
                    }
                ],
                "notFound": [],
            },
        },
        client_settings=auto,
    )

    result = await tools.list_emails(mailbox_role="inbox")

    probes = [
        args
        for args in jmap_calls(seen, "Email/query")
        if "operator" in (args.get("filter") or {})
    ]
    assert len(probes) == 1  # the server answered the probe - the RFC dialect
    queries = jmap_calls(seen, "Email/query")
    assert queries[-1]["filter"] == {"inMailbox": "mb-inbox"}
    assert result["total"] == 1


async def test_whoami_takes_the_state_of_the_answer(mock_jmap: Any, bearer: Any) -> None:
    mock_jmap(
        {
            "Identity/get": {
                "accountId": "account-1",
                "list": [{"id": "id-1", "email": "me@example.com", "name": None, "replyTo": []}],
                "notFound": [],
            },
            "Mailbox/get": {"accountId": "account-1", "list": [{"id": "mb-1"}], "state": "account-state"},
        }
    )

    result = await tools.whoami()

    assert result["state"] == "account-state"


async def test_the_dialect_can_be_told(mock_stalwart: Any, bearer: Any, stalwart_settings: Settings) -> None:
    told = dataclasses.replace(
        stalwart_settings,
        stalwart=dataclasses.replace(stalwart_settings.stalwart, dialect="rfc"),
    )
    mock = mock_stalwart(told)

    await tools.list_emails(mailbox_role="inbox")

    assert mock.probes() == []


async def test_the_probe_works_without_a_folder(
    settings: Settings, make_transport: Any, bearer: Any
) -> None:
    """No folder to point at: the probe asks two conditions without an id."""
    auto = dataclasses.replace(
        settings, stalwart=dataclasses.replace(settings.stalwart, dialect="auto")
    )
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=SESSION)
        body = json.loads(request.content)
        name, _args, call_id = body["methodCalls"][0]
        if name == "Mailbox/get":
            return httpx.Response(
                200,
                json={
                    "methodResponses": [
                        [
                            "error",
                            {
                                "type": "urn:ietf:params:jmap:error:unknownMethod",
                                "code": "unknownMethod",
                            },
                            call_id,
                        ]
                    ]
                },
            )
        return httpx.Response(400, json={"type": "urn:ietf:params:jmap:error:notRequest"})

    transport, _seen = make_transport(handler)
    client = JmapClient(auto, transport=transport)
    session = await client.session(bearer)

    dialect = await client.dialect(bearer, session)

    assert dialect.name == "stalwart"
    queries = jmap_calls(seen, "Email/query")
    assert queries[0]["filter"]["conditions"] == [{"notKeyword": "$seen"}, {"hasAttachment": False}]


# ---------------------------------------------------------------------------
# The RFC side of the same tools
# ---------------------------------------------------------------------------


async def test_the_rfc_dialect_asks_the_server(mock_jmap: Any, bearer: Any) -> None:
    seen = mock_jmap(
        {
            "Mailbox/get": {"accountId": "account-1", "list": [{"id": "mb-inbox", "role": "inbox"}]},
            "Email/query": {"accountId": "account-1", "ids": ["e1"], "total": 1, "position": 0},
            "Email/get": {
                "accountId": "account-1",
                "list": [
                    {
                        "id": "e1",
                        "subject": "Report",
                        "from": [{"email": "boss@example.com", "name": None}],
                        "to": [{"email": "me@example.com", "name": None}],
                        "receivedAt": "2026-10-20T10:00:00Z",
                        "keywords": [],
                        "attachments": [],
                    }
                ],
                "notFound": [],
            },
        }
    )

    result = await tools.list_emails(mailbox_role="inbox")

    queries = jmap_calls(seen, "Email/query")
    assert queries[0]["filter"] == {"inMailbox": "mb-inbox"}
    assert result["total"] == 1


async def test_the_rfc_dialect_asks_the_server_for_several_conditions(
    mock_jmap: Any, bearer: Any
) -> None:
    seen = mock_jmap(
        {
            "Mailbox/get": {"accountId": "account-1", "list": [{"id": "mb-inbox", "role": "inbox"}]},
            "Email/query": {"accountId": "account-1", "ids": ["e1"], "total": 1, "position": 0},
            "Email/get": {"accountId": "account-1", "list": [], "notFound": []},
        }
    )

    await tools.list_emails(mailbox_role="inbox", unread=True)

    queries = jmap_calls(seen, "Email/query")
    assert len(queries) == 1
    assert queries[0]["filter"] == {
        "operator": "and",
        "conditions": [{"inMailbox": "mb-inbox"}, {"notKeyword": "$seen"}],
    }
