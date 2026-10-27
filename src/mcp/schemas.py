"""Types of the tool results.

The figures below are copied from the source of truth of the schemas
(``tools/tools_schema.py``), so the published JSON Schemas of the tools are
generated from the very same types that the implementation fills in. The full
catalogue of the 15 tools lives in ``docs/tools-schema.md``: phase 2a shipped
the figures of the eight read-only tools (and the ones ``whoami`` needs),
phase 2b adds the figures of the six write and blob tools.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Annotated, Literal, TypedDict

from pydantic import Field

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

MailboxRole = Literal["inbox", "drafts", "sent", "trash", "junk", "archive", "other"]
EmailSort = Literal["received_desc", "received_asc"]


# ---------------------------------------------------------------------------
# Shared output figures ($defs)
# ---------------------------------------------------------------------------


class EmailAddress(TypedDict):
    """A mail address with an optional display name."""

    email: Annotated[str, Field(description="The address itself, e.g. 'user@example.com'.")]
    name: Annotated[
        str | None,
        Field(description="Display name shown for the address; null when the address carries none."),
    ]


class Identity(TypedDict):
    """A sender identity (an alias the account may send from)."""

    id: Annotated[str, Field(description="JMAP identity id; pass it as from_identity_id in create_draft.")]
    email: Annotated[str, Field(description="The address of this identity.")]
    name: Annotated[str | None, Field(description="Display name of the identity; null when not set.")]
    reply_to: Annotated[
        list[EmailAddress],
        Field(description="Reply-to addresses of this identity; empty when the identity has none."),
    ]
    is_default: Annotated[
        bool,
        Field(description="True for the identity used when from_identity_id is not given."),
    ]


class Mailbox(TypedDict):
    """A mail folder."""

    id: Annotated[str, Field(description="JMAP mailbox id; stable only inside one session.")]
    name: Annotated[str, Field(description="Folder name as shown by the server.")]
    role: Annotated[
        MailboxRole | None,
        Field(description="Well-known role of the folder; null when the folder has no role."),
    ]
    parent_id: Annotated[str | None, Field(description="Id of the parent folder; null for top-level folders.")]
    total_emails: Annotated[int, Field(description="Number of messages in the folder, per the server counter.")]
    unread_emails: Annotated[int, Field(description="Number of unread messages in the folder.")]
    sort_order: Annotated[int, Field(description="Server sort order of the folder inside its parent.")]


class Attachment(TypedDict):
    """Metadata of one attachment of a message."""

    blob_id: Annotated[str, Field(description="Blob id of the file; pass it to download_attachment.")]
    name: Annotated[str | None, Field(description="File name as declared by the sender; null when absent.")]
    type: Annotated[str, Field(description="MIME type of the file, e.g. 'application/pdf'.")]
    size: Annotated[int, Field(description="Size of the file in bytes.")]
    is_inline: Annotated[
        bool,
        Field(description="True for files shown inside the body (inline images) rather than as attachments."),
    ]


# 'from' is a Python keyword, so the two message figures are built with the functional
# TypedDict syntax; the JSON field names stay the JMAP ones (from/to/cc/bcc).
EmailSummary = TypedDict(
    "EmailSummary",
    {
        "id": Annotated[str, Field(description="JMAP email id; pass it to get_email.")],
        "thread_id": Annotated[str | None, Field(description="Id of the conversation; null when the message is not in a thread.")],
        "subject": Annotated[str | None, Field(description="Subject of the message; null when it has none.")],
        "from": Annotated[list[EmailAddress], Field(description="Sender addresses.")],
        "to": Annotated[list[EmailAddress], Field(description="To recipients.")],
        "date": Annotated[datetime | None, Field(description="Date and time the message was received (ISO 8601, UTC).")],
        "preview": Annotated[str | None, Field(description="Short plain-text excerpt of the body; null when the server has none.")],
        "keywords": Annotated[list[str], Field(description="JMAP keywords of the message, e.g. '$seen', '$flagged', '$draft', '$answered'.")],
        "has_attachment": Annotated[bool, Field(description="True when the message carries at least one attachment.")],
    },
)

EmailDetail = TypedDict(
    "EmailDetail",
    {
        "id": Annotated[str, Field(description="JMAP email id.")],
        "thread_id": Annotated[str | None, Field(description="Id of the conversation; null when the message is not in a thread.")],
        "subject": Annotated[str | None, Field(description="Subject of the message; null when it has none.")],
        "from": Annotated[list[EmailAddress], Field(description="Sender addresses.")],
        "to": Annotated[list[EmailAddress], Field(description="To recipients.")],
        "cc": Annotated[list[EmailAddress], Field(description="Cc recipients; empty when there are none.")],
        "bcc": Annotated[list[EmailAddress], Field(description="Bcc recipients; usually empty for received mail.")],
        "reply_to": Annotated[list[EmailAddress], Field(description="Reply-to addresses; empty when the message has none.")],
        "date": Annotated[datetime | None, Field(description="Date and time the message was received (ISO 8601, UTC).")],
        "message_id": Annotated[str | None, Field(description="RFC 5322 Message-ID header; null when absent.")],
        "in_reply_to": Annotated[str | None, Field(description="Message-ID of the message this one replies to; null when it is not a reply.")],
        "preview": Annotated[str | None, Field(description="Short plain-text excerpt of the body; null when the server has none.")],
        "text_body": Annotated[str | None, Field(description="Body as plain text (HTML converted); null when no body was requested or the message has none.")],
        "truncated": Annotated[bool, Field(description="True when the body was cut at max_body_chars.")],
        "keywords": Annotated[list[str], Field(description="JMAP keywords of the message, e.g. '$seen', '$flagged', '$draft', '$answered'.")],
        "attachments": Annotated[list[Attachment], Field(description="Attachments of the message; empty when there are none.")],
    },
)

# ---------------------------------------------------------------------------
# Per-tool output figures
# ---------------------------------------------------------------------------


class WhoamiResult(TypedDict):
    """Result of whoami."""

    account_id: Annotated[str, Field(description="JMAP account id of the mail account.")]
    email: Annotated[str, Field(description="Primary address of the account (the default identity).")]
    identities: Annotated[list[Identity], Field(description="Sender identities of the account.")]
    capabilities: Annotated[list[str], Field(description="JMAP capability URNs advertised by the server.")]
    state: Annotated[str | None, Field(description="Current account state token; pass it to list_changes as since_state.")]


class IdentitiesResult(TypedDict):
    """Result of list_identities."""

    identities: Annotated[list[Identity], Field(description="Sender identities of the account.")]


class MailboxesResult(TypedDict):
    """Result of list_mailboxes."""

    mailboxes: Annotated[list[Mailbox], Field(description="Folders of the account.")]


class EmailPage(TypedDict):
    """A page of message summaries."""

    emails: Annotated[list[EmailSummary], Field(description="Messages of this page, newest first.")]
    total: Annotated[int, Field(description="Total number of messages matching the filter, not just this page.")]
    has_more: Annotated[bool, Field(description="True when more messages exist after this page.")]
    position: Annotated[int, Field(description="Offset of this page in the result set; pass it back to continue.")]


class EmailCount(TypedDict):
    """Result of count_emails."""

    count: Annotated[int, Field(description="Number of messages matching the filter.")]


class ThreadResult(TypedDict):
    """Result of get_thread."""

    thread_id: Annotated[str, Field(description="Id of the conversation.")]
    subject: Annotated[str | None, Field(description="Subject of the conversation (subject of its first message).")]
    message_count: Annotated[int, Field(description="Total number of messages in the conversation.")]
    emails: Annotated[list[EmailDetail], Field(description="Messages of the conversation, oldest first.")]


class FlagUpdateResult(TypedDict):
    """Result of set_flags."""

    updated: Annotated[list[str], Field(description="Ids of the messages whose flags were changed.")]
    not_found: Annotated[list[str], Field(description="Ids that do not exist in this account.")]


class MoveResult(TypedDict):
    """Result of move_emails."""

    moved: Annotated[list[str], Field(description="Ids of the messages that were moved.")]
    not_found: Annotated[list[str], Field(description="Ids that do not exist in this account.")]


class DraftResult(TypedDict):
    """Result of create_draft."""

    draft_id: Annotated[str, Field(description="Id of the created draft; pass it to send_draft.")]
    thread_id: Annotated[str | None, Field(description="Id of the conversation the draft belongs to; null when the server reports none.")]


class SendResult(TypedDict):
    """Result of send_draft."""

    message_id: Annotated[str | None, Field(description="RFC 5322 Message-ID of the sent message; null when the server reports none.")]
    sent_at: Annotated[datetime, Field(description="Moment the message was handed over for delivery (ISO 8601, UTC).")]
    recipients: Annotated[list[EmailAddress], Field(description="All recipients of the sent message: To, then Cc, then Bcc.")]


class ChangesResult(TypedDict):
    """Result of list_changes."""

    created: Annotated[list[str], Field(description="Ids of messages created since the given state.")]
    updated: Annotated[list[str], Field(description="Ids of messages updated since the given state.")]
    destroyed: Annotated[list[str], Field(description="Ids of messages deleted since the given state.")]
    state: Annotated[str, Field(description="New current account state token; keep it for the next call.")]
    has_more_changes: Annotated[bool, Field(description="True when more changes exist after this batch.")]


class DownloadResult(TypedDict):
    """Result of download_attachment."""

    path: Annotated[str, Field(description="Absolute path of the written file on the server.")]
    name: Annotated[str, Field(description="File name as written on the server.")]
    size_bytes: Annotated[int, Field(description="Size of the written file in bytes.")]
    content_type: Annotated[str, Field(description="MIME type of the file.")]


class UploadResult(TypedDict):
    """Result of upload_attachment."""

    blob_id: Annotated[str, Field(description="Blob id of the uploaded file; pass it to create_draft.")]
    name: Annotated[str, Field(description="File name sent to the server.")]
    size_bytes: Annotated[int, Field(description="Size of the uploaded file in bytes.")]
    content_type: Annotated[str, Field(description="MIME type of the uploaded file.")]


class AttachmentRef(TypedDict):
    """Reference to an already uploaded attachment."""

    blob_id: Annotated[str, Field(description="Blob id returned by upload_attachment.")]


EmailSummary.__doc__ = "Short summary of a message: headers, preview, keywords - no body."
EmailDetail.__doc__ = "One message: headers, plain-text body, keywords and attachment metadata."


# ---------------------------------------------------------------------------
# Reusable parameter annotations
# ---------------------------------------------------------------------------

MailboxRoleParam = Annotated[
    MailboxRole | None,
    Field(
        description=(
            "Folder role to read from; mutually exclusive with mailbox_id. "
            "Preferred over ids: roles are stable between sessions."
        )
    ),
]
MailboxIdParam = Annotated[
    str | None,
    Field(description="Id of the folder to read from; mutually exclusive with mailbox_role."),
]
UnreadParam = Annotated[
    bool,
    Field(description="True selects only unread messages; False (default) does not filter by read state."),
]
FlaggedParam = Annotated[
    bool,
    Field(description="True selects only flagged (starred) messages; False (default) does not filter by the flag."),
]
LimitParam = Annotated[
    int,
    Field(description="Maximum number of messages to return; the server may lower it further.", ge=1, le=100),
]
PositionParam = Annotated[
    int,
    Field(description="Offset in the result set; increase it by limit to read the next page.", ge=0),
]
SortParam = Annotated[
    EmailSort,
    Field(description="Order of the page: received_desc (newest first, default) or received_asc (oldest first)."),
]
MaxBodyCharsParam = Annotated[
    int,
    Field(description="Maximum number of characters of the body to return; the server may lower it further.", ge=200, le=100000),
]


# ---------------------------------------------------------------------------
# Input-schema contract that Pydantic cannot express
# ---------------------------------------------------------------------------

#: Groups of parameters that must not be given together.
EXCLUSIVE_GROUPS: dict[str, list[list[str]]] = {
    "list_emails": [["mailbox_role", "mailbox_id"]],
    "search_emails": [["mailbox_role", "mailbox_id"]],
    "count_emails": [["mailbox_role", "mailbox_id"]],
    "move_emails": [["mailbox_role", "mailbox_id"]],
}

#: Groups of parameters of which exactly one must be given.
REQUIRED_ONE_OF: dict[str, list[list[str]]] = {
    "move_emails": [["mailbox_role", "mailbox_id"]],
}

#: Groups of parameters of which at least one must be given.
REQUIRED_ONE_FLAG: dict[str, list[str]] = {"set_flags": ["seen", "flagged", "answered"]}


def apply_input_contract(schema: dict, tool: str) -> dict:
    """Add the rules Pydantic's generated argument model cannot express.

    * ``additionalProperties: false`` - the tool reads only the declared fields.
    * mutually exclusive groups (e.g. mailbox_role vs mailbox_id).
    * at least one of a group (e.g. the move target, the flags).
    """
    schema = deepcopy(schema)
    schema["additionalProperties"] = False

    for group in EXCLUSIVE_GROUPS.get(tool, []):
        schema.setdefault("allOf", []).append({"not": {"required": group}})

    any_of: list[dict] = []
    for group in REQUIRED_ONE_OF.get(tool, []):
        any_of.extend(
            {"required": [name], "properties": {name: {"type": "string"}}} for name in group
        )

    flags = REQUIRED_ONE_FLAG.get(tool)
    if flags:
        any_of.extend(
            {"required": [name], "properties": {name: {"type": "boolean"}}} for name in flags
        )

    if any_of:
        schema["anyOf"] = any_of

    return schema
