"""download_attachment: the file lands in the download directory of the server."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from src.mcp import tools

EMAIL_WITH_ATTACHMENT = {
    "accountId": "account-1",
    "list": [
        {
            "id": "m1",
            "attachments": [
                {
                    "blobId": "blob-1",
                    "name": "report.pdf",
                    "type": "application/pdf",
                    "size": 11,
                    "isInline": False,
                }
            ],
        }
    ],
    "notFound": [],
}


async def test_it_writes_the_file_into_the_download_directory(
    mock_jmap: Any, bearer: Any, sandbox: Any
) -> None:
    mock_jmap(
        {"Email/get": EMAIL_WITH_ATTACHMENT},
        blobs={"download": b"hello world"},
        client_settings=sandbox.settings,
    )

    result = await tools.download_attachment(email_id="m1", blob_id="blob-1")

    path = Path(result["path"])
    assert path.parent == sandbox.downloads.resolve()
    assert path.name == "report.pdf"
    assert path.read_bytes() == b"hello world"
    assert result["size_bytes"] == 11
    assert result["content_type"] == "application/pdf"


async def test_it_never_writes_outside_the_download_directory(
    mock_jmap: Any, bearer: Any, sandbox: Any
) -> None:
    mock_jmap(
        {"Email/get": EMAIL_WITH_ATTACHMENT},
        blobs={"download": b"hello world"},
        client_settings=sandbox.settings,
    )

    result = await tools.download_attachment(
        email_id="m1", blob_id="blob-1", name="../../escape.txt"
    )

    path = Path(result["path"])
    assert path.parent == sandbox.downloads.resolve()
    assert path.name == "escape.txt"
    assert not (sandbox.root / "escape.txt").exists()


async def test_it_does_not_overwrite_an_existing_file(
    mock_jmap: Any, bearer: Any, sandbox: Any
) -> None:
    mock_jmap(
        {"Email/get": EMAIL_WITH_ATTACHMENT},
        blobs={"download": b"hello world"},
        client_settings=sandbox.settings,
    )

    first = await tools.download_attachment(email_id="m1", blob_id="blob-1")
    second = await tools.download_attachment(email_id="m1", blob_id="blob-1")

    assert first["path"] != second["path"]
    assert Path(first["path"]).read_bytes() == b"hello world"
    assert Path(second["path"]).read_bytes() == b"hello world"


async def test_it_refuses_an_attachment_bigger_than_the_limit(
    mock_jmap: Any, bearer: Any, sandbox: Any
) -> None:
    mock_jmap(
        {
            "Email/get": {
                "accountId": "account-1",
                "list": [
                    {
                        "id": "m1",
                        "attachments": [
                            {
                                "blobId": "blob-1",
                                "name": "big.bin",
                                "type": "application/octet-stream",
                                "size": 100 * 1024 * 1024,
                                "isInline": False,
                            }
                        ],
                    }
                ],
                "notFound": [],
            }
        },
        blobs={"download": b"hello world"},
        client_settings=sandbox.settings,
    )

    with pytest.raises(ToolError) as failure:
        await tools.download_attachment(email_id="m1", blob_id="blob-1")

    assert "bigger than the configured limit" in str(failure.value)
    assert list(sandbox.downloads.iterdir()) == []


async def test_it_refuses_an_unknown_blob(mock_jmap: Any, bearer: Any, sandbox: Any) -> None:
    mock_jmap(
        {"Email/get": EMAIL_WITH_ATTACHMENT},
        blobs={"download": b"hello world"},
        client_settings=sandbox.settings,
    )

    with pytest.raises(ToolError) as failure:
        await tools.download_attachment(email_id="m1", blob_id="blob-9")

    assert "no attachment with the blob id" in str(failure.value)
    assert list(sandbox.downloads.iterdir()) == []


async def test_it_refuses_an_unknown_message(mock_jmap: Any, bearer: Any, sandbox: Any) -> None:
    mock_jmap(
        {"Email/get": {"accountId": "account-1", "list": [], "notFound": ["m9"]}},
        blobs={"download": b"hello world"},
        client_settings=sandbox.settings,
    )

    with pytest.raises(ToolError) as failure:
        await tools.download_attachment(email_id="m9", blob_id="blob-1")

    assert "no message with the id" in str(failure.value)


async def test_a_method_error_becomes_a_tool_error(
    mock_jmap: Any, bearer: Any, sandbox: Any
) -> None:
    mock_jmap(
        {"Email/get": {"__error__": {"type": "unknownMethod", "code": "unknownMethod"}}},
        blobs={"download": b"hello world"},
        client_settings=sandbox.settings,
    )

    with pytest.raises(ToolError) as failure:
        await tools.download_attachment(email_id="m1", blob_id="blob-1")

    assert "unknownMethod" in str(failure.value)
