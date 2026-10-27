"""set_flags: the flags of one or more messages."""

from __future__ import annotations

from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from src.mcp import tools
from tests.conftest import jmap_calls, jmap_error

EMAIL_SET_OK = {"updated": {"m1": None, "m2": None}, "notFound": [], "notUpdated": {}}


async def test_it_changes_the_flags(mock_jmap: Any, bearer: Any) -> None:
    seen = mock_jmap({"Email/set": EMAIL_SET_OK})

    result = await tools.set_flags(ids=["m1", "m2"], seen=True, flagged=False)

    assert result["updated"] == ["m1", "m2"]
    assert result["not_found"] == []
    update = jmap_calls(seen, "Email/set")[0]["update"]
    assert update == {
        "m1": {"keywords": {"$seen": True, "$flagged": False}},
        "m2": {"keywords": {"$seen": True, "$flagged": False}},
    }


async def test_a_message_that_does_not_exist_is_a_tool_error(mock_jmap: Any, bearer: Any) -> None:
    mock_jmap({"Email/set": {"updated": {"m1": None, "m2": None}, "notFound": ["m3"], "notUpdated": {}}})

    with pytest.raises(ToolError) as failure:
        await tools.set_flags(ids=["m1", "m2", "m3"], seen=True)

    message = str(failure.value)
    assert "m3" in message
    assert "do not exist" in message


async def test_it_leaves_the_flags_it_was_not_given(mock_jmap: Any, bearer: Any) -> None:
    seen = mock_jmap({"Email/set": EMAIL_SET_OK})

    await tools.set_flags(ids=["m1"], answered=True)

    update = jmap_calls(seen, "Email/set")[0]["update"]
    assert update == {"m1": {"keywords": {"$answered": True}}}


async def test_it_refuses_a_call_without_a_flag(mock_jmap: Any, bearer: Any) -> None:
    mock_jmap({"Email/set": EMAIL_SET_OK})

    with pytest.raises(ToolError) as failure:
        await tools.set_flags(ids=["m1"])

    assert "at least one flag" in str(failure.value)


async def test_it_refuses_a_call_without_ids(mock_jmap: Any, bearer: Any) -> None:
    mock_jmap({"Email/set": EMAIL_SET_OK})

    with pytest.raises(ToolError) as failure:
        await tools.set_flags(ids=[], seen=True)

    assert "ids is required" in str(failure.value)


async def test_a_method_error_becomes_a_tool_error(mock_jmap: Any, bearer: Any) -> None:
    mock_jmap({"Email/set": jmap_error("invalidArguments", "invalidArguments", "no such keyword")})

    with pytest.raises(ToolError) as failure:
        await tools.set_flags(ids=["m1"], seen=True)

    assert "invalidArguments" in str(failure.value)


async def test_a_refused_message_becomes_a_tool_error(mock_jmap: Any, bearer: Any) -> None:
    mock_jmap(
        {
            "Email/set": {
                "updated": {},
                "notFound": [],
                "notUpdated": {"m1": {"type": "forbidden", "code": "notAllowed"}},
            }
        }
    )

    with pytest.raises(ToolError) as failure:
        await tools.set_flags(ids=["m1"], seen=True)

    assert "m1" in str(failure.value)


async def test_an_unsupported_answered_flag_applies_the_other_flags(mock_jmap: Any, bearer: Any) -> None:
    seen = mock_jmap(
        {
            "Email/set": [
                {
                    "updated": {},
                    "notFound": [],
                    "notUpdated": {
                        "m1": {
                            "type": "invalidProperties",
                            "code": "invalidProperties",
                            "description": "the keyword $answered is not supported",
                        }
                    },
                },
                {"updated": {"m1": None}, "notFound": [], "notUpdated": {}},
            ]
        }
    )

    with pytest.raises(ToolError) as failure:
        await tools.set_flags(ids=["m1"], seen=True, answered=True)

    assert "$answered" in str(failure.value)
    calls = jmap_calls(seen, "Email/set")
    assert len(calls) == 2
    assert calls[1]["update"] == {"m1": {"keywords": {"$seen": True}}}

