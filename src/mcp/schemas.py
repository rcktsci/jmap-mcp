"""Types of the tool results.

The figures below are copied from the source of truth of the schemas
(``tools/tools_schema.py``), so the published JSON Schemas of the tools are
generated from the very same types that the implementation fills in. The full
catalogue of the 15 tools lives in ``docs/tools-schema.md``; phase 1 ships the
figures ``whoami`` needs, the rest arrives with phase 2.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Annotated, TypedDict

from pydantic import Field


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


class WhoamiResult(TypedDict):
    """Result of whoami."""

    account_id: Annotated[str, Field(description="JMAP account id of the mail account.")]
    email: Annotated[str, Field(description="Primary address of the account (the default identity).")]
    identities: Annotated[list[Identity], Field(description="Sender identities of the account.")]
    capabilities: Annotated[list[str], Field(description="JMAP capability URNs advertised by the server.")]
    state: Annotated[str | None, Field(description="Current account state token; pass it to list_changes as since_state.")]


# ---------------------------------------------------------------------------
# Input-schema contract that Pydantic cannot express
# ---------------------------------------------------------------------------

#: Groups of parameters that must not be given together. Filled in by phase 2
#: (mailbox_role vs mailbox_id).
EXCLUSIVE_GROUPS: dict[str, list[list[str]]] = {}

#: Groups of parameters of which exactly one must be given.
REQUIRED_ONE_OF: dict[str, list[list[str]]] = {}

#: Groups of parameters of which at least one must be given.
REQUIRED_ONE_FLAG: dict[str, list[str]] = {}


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
