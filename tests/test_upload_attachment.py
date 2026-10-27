"""upload_attachment: a file of the server goes to the blob store of the account."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from src.mcp import tools


async def test_it_uploads_a_file_of_the_allowed_roots(
    mock_jmap: Any, bearer: Any, sandbox: Any
) -> None:
    source = sandbox.allowed / "note.txt"
    source.write_text("hello world", encoding="utf-8")
    seen = mock_jmap({}, blobs={"upload": {"blobId": "blob-1"}}, client_settings=sandbox.settings)

    result = await tools.upload_attachment(path=str(source))

    assert result == {
        "blob_id": "blob-1",
        "name": "note.txt",
        "size_bytes": 11,
        "content_type": "text/plain",
    }
    uploads = [request for request in seen if request.url.path.startswith("/upload")]
    assert len(uploads) == 1
    assert b"note.txt" in uploads[0].content


async def test_it_refuses_a_file_outside_the_allowed_roots(
    mock_jmap: Any, bearer: Any, sandbox: Any
) -> None:
    outside = sandbox.root / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    mock_jmap({}, blobs={"upload": {"blobId": "blob-1"}}, client_settings=sandbox.settings)

    with pytest.raises(ToolError) as failure:
        await tools.upload_attachment(path=str(outside))

    assert "outside the allowed roots" in str(failure.value)


async def test_it_refuses_a_symlink_out_of_the_allowed_roots(
    mock_jmap: Any, bearer: Any, sandbox: Any
) -> None:
    outside = sandbox.root / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = sandbox.allowed / "link.txt"
    try:
        link.symlink_to(outside)
        path = str(link)
    except OSError:
        # No symlinks on this machine - the same escape with a '..' is refused too.
        path = str(sandbox.allowed / ".." / "outside.txt")
    mock_jmap({}, blobs={"upload": {"blobId": "blob-1"}}, client_settings=sandbox.settings)

    with pytest.raises(ToolError) as failure:
        await tools.upload_attachment(path=path)

    assert "outside the allowed roots" in str(failure.value)


async def test_it_refuses_a_missing_file(mock_jmap: Any, bearer: Any, sandbox: Any) -> None:
    mock_jmap({}, blobs={"upload": {"blobId": "blob-1"}}, client_settings=sandbox.settings)

    with pytest.raises(ToolError) as failure:
        await tools.upload_attachment(path=str(sandbox.allowed / "nothing.txt"))

    assert "there is no file" in str(failure.value)


async def test_it_refuses_a_directory(mock_jmap: Any, bearer: Any, sandbox: Any) -> None:
    mock_jmap({}, blobs={"upload": {"blobId": "blob-1"}}, client_settings=sandbox.settings)

    with pytest.raises(ToolError) as failure:
        await tools.upload_attachment(path=str(sandbox.allowed))

    assert "not a regular file" in str(failure.value)


async def test_it_refuses_a_file_bigger_than_the_limit(
    mock_jmap: Any, bearer: Any, sandbox: Any, monkeypatch: Any
) -> None:
    source = sandbox.allowed / "big.bin"
    source.write_bytes(b"x" * 32)
    tiny = dataclasses.replace(
        sandbox.settings,
        stalwart=dataclasses.replace(sandbox.settings.stalwart, max_attachment_mb=0),
    )
    monkeypatch.setattr(tools, "get_settings", lambda: tiny)
    mock_jmap({}, blobs={"upload": {"blobId": "blob-1"}}, client_settings=tiny)

    with pytest.raises(ToolError) as failure:
        await tools.upload_attachment(path=str(source))

    assert "bigger than the configured limit" in str(failure.value)


async def test_it_sanitizes_the_name(mock_jmap: Any, bearer: Any, sandbox: Any) -> None:
    source = sandbox.allowed / "note.txt"
    source.write_text("hello world", encoding="utf-8")
    mock_jmap({}, blobs={"upload": {"blobId": "blob-1"}}, client_settings=sandbox.settings)

    result = await tools.upload_attachment(path=str(source), name="../../evil name.txt")

    assert result["name"] == "evil_name.txt"
    assert result["content_type"] == "text/plain"


async def test_the_server_answers_without_a_blob_id(mock_jmap: Any, bearer: Any, sandbox: Any) -> None:
    source = sandbox.allowed / "note.txt"
    source.write_text("hello world", encoding="utf-8")
    mock_jmap({}, blobs={"upload": {}}, client_settings=sandbox.settings)

    with pytest.raises(ToolError) as failure:
        await tools.upload_attachment(path=str(source))

    assert "no blob id" in str(failure.value)


async def test_the_upload_uses_the_multipart_field_file(
    mock_jmap: Any, bearer: Any, sandbox: Any
) -> None:
    source = sandbox.allowed / "note.txt"
    source.write_text("hello world", encoding="utf-8")
    seen = mock_jmap({}, blobs={"upload": {"blobId": "blob-1"}}, client_settings=sandbox.settings)

    await tools.upload_attachment(path=str(source))

    uploads = [request for request in seen if request.url.path.startswith("/upload")]
    assert len(uploads) == 1
    assert b'name="file"' in uploads[0].content
    assert b"note.txt" in uploads[0].content
    assert not list(sandbox.downloads.iterdir())
