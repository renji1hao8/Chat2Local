"""MCP surface for binary file transfer.

These six tools sit on top of the same ``DeviceRouter`` as the original tools,
so they inherit device selection, workspace policy and remote-Agent routing
without touching it.  Each returns a ``CallToolResult``: a JSON metadata block
first, then zero or more MCP content blocks (image / embedded resource) that a
client can render or hand to the model.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Annotated, Any

from mcp.server.mcpserver import Context  # noqa: F401  (kept for signature parity)
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, EmbeddedResource, ImageContent, TextContent, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from chat2local.dispatch.local import ToolExecutionError
from chat2local.device.registry import DeviceError
from chat2local.hub.router import DeviceRouter
from chat2local.mcp.tools import get_router
from chat2local.runtime.workspace import WorkspaceError

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer


DEVICE = "Target device ID. Omit for this MCP server's local device; use list_devices on a Hub."
WORKSPACE = (
    "Absolute workspace directory on the selected device. Omit to use its startup default; "
    "it must lie within that device's allowed roots and applies only to this call."
)


class ChatFile(BaseModel):
    """Exact OpenAI file parameter shape; optional properties stay strings."""
    model_config = ConfigDict(extra="forbid")
    download_url: str = Field(min_length=1, max_length=4096)
    file_id: str = Field(min_length=1, max_length=512)
    mime_type: str = Field(default="", max_length=255)
    file_name: str = Field(default="", max_length=1024)


IMPORT_DESCRIPTION = """Download a user-provided ChatGPT attachment onto the selected device.
Use this when a chat attachment (Word template, PDF, image, data file) must be available to
local MATLAB or document-editing programs. ChatGPT supplies file through its file parameter
adapter: download_url and file_id are required; mime_type and file_name are optional. Never
invent a URL, file ID or base64. A file ID or sandbox path alone cannot be downloaded; ask the
user to reattach/reselect the file if the client cannot supply a usable file reference.

Choose an explicit destination path inside the authorized workspace. The attachment name is
metadata only, never a filesystem path. The selected device fetches the temporary URL using
the existing source URL policy, size limits, digest verification and overwrite protection.
No OpenAI API key is needed. Expired URLs must be refreshed by the chat client; do not retry
the same expired URL. After import, use the returned local path to run MATLAB and fill a copy
of the template, keeping the original intact."""

UPLOAD_DESCRIPTION = """Write one file onto the selected device. Workspace policy, size limits and \
overwrite rules belong to the target device, not to the caller.

Choose exactly one source:
- content_base64: raw bytes as standard base64 (bounded by the device's inline limit, default 512 KiB);
- content_text: UTF-8 text, convenient for code, Markdown, CSV, SVG and JSON;
- source_url: an https URL the device downloads itself. For ChatGPT attachments, prefer
  import_chat_file, which declares the official file parameter adapter.

Files larger than the inline limit use a resumable session:
1. action=begin with path and size (and optionally sha256) returns session_id and next_offset;
2. action=chunk repeatedly with session_id, offset and data_base64 (default chunk 256 KiB);
3. action=commit verifies size and SHA-256, then renames the staged file into place atomically.
action=status reports progress after a dropped connection; action=abort discards it.
Re-sending the same chunk at the same offset is safe. A repeated begin/commit with the same
idempotency_key replays the original result instead of writing twice.

Existing files are never replaced unless overwrite=true and the device permits it. Errors are a
JSON object with a stable error.code such as outside_workspace, exists, sha256_mismatch or
session_expired."""

DOWNLOAD_DESCRIPTION = """Read a file from the selected device and deliver its bytes.

Small files arrive inline: text media as text, everything else as an embedded base64 resource, plus
a JSON metadata block with size, MIME type and SHA-256. Larger files return a short-lived
single-use download_url on the device (open it in a browser; it is not a permanent public URL) and
can additionally be read in ranges with offset/length for resumable transfer.

Use mode=url to skip inlining and only mint a link, or mode=inline to force bytes (bounded by the
device's inline limit). This tool is read-only and cannot write."""

PREVIEW_DESCRIPTION = """Show a file the way a chat client can display it.

PNG, JPEG, GIF, WebP and BMP are returned as image content when they fit the preview budget; SVG
and text-like files as text. PDF and Office files support selected page images and extracted text
using optional Poppler and LibreOffice converters. mode=both (default), pages or text; page is
1-based and page_count is bounded to 3. Scanned PDFs need OCR, which is not provided. Missing
converters or failed conversion return explicit metadata and an optional download link. Temporary
conversion files are cleaned up; source files are not modified."""

STATUS_DESCRIPTION = """Report file-transfer sessions on the selected device: state, bytes received, \
next_offset, remaining, chunk size, expiry and the committed result. Call it after a dropped \
connection to learn where to resume. With no session_id it lists all sessions."""

DIRECTORY_DESCRIPTION = """Batch and incremental file transfer for a directory.

action=manifest walks the directory and returns path/size/mtime/SHA-256 for every file (bounded by
max_files and max_bytes). Compare two manifests to compute what changed, which is how incremental
sync is driven.

action=download builds a ZIP of the selected files on the device and returns a short-lived download
link (inline too when small).

action=upload extracts a ZIP into the directory per-file-natomically. The archive may come from
zip_path (a file already on the device), content_base64 (small) or source_url. Entries that are
absolute, contain '..', carry a drive letter or are symlinks are rejected, and extraction is capped
in file count and total bytes."""


def _to_result(payload: dict[str, Any]) -> CallToolResult:
    result = payload.get("result") or {}
    blocks = payload.get("blocks") or []
    content: list[Any] = [
        TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2)),
    ]
    for block in blocks:
        kind = block.get("type")
        if kind == "text":
            content.append(TextContent.model_validate(block))
        elif kind == "image":
            content.append(ImageContent.model_validate(block))
        elif kind == "resource":
            content.append(EmbeddedResource.model_validate(block))
    return CallToolResult(content=content, structured_content=result)


async def _run(router: DeviceRouter | None, tool: str, arguments: dict[str, Any], device: str | None) -> CallToolResult:
    try:
        payload = await (router or get_router()).execute(tool, arguments, device)
    except (WorkspaceError, ToolExecutionError, DeviceError) as error:
        raise ToolError(str(error)) from error
    return _to_result(payload)


def register(server: "MCPServer", router: DeviceRouter | None = None) -> None:
    @server.tool(name="import_chat_file", title="Import ChatGPT Attachment to Device",
                 description=IMPORT_DESCRIPTION, meta={"openai/fileParams": ["file"]},
                 annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                             idempotentHint=False, openWorldHint=True),
                 structured_output=False)
    async def import_chat_file_tool(
        file: ChatFile,
        path: Annotated[str, Field(description="Explicit destination file path inside the selected workspace.")],
        sha256: Annotated[str | None, Field(description="Optional expected SHA-256 of the original attachment.")] = None,
        overwrite: Annotated[bool, Field(description="Replace an existing file only if device configuration allows it.")] = False,
        workspace: Annotated[str | None, Field(description=WORKSPACE)] = None,
        device: Annotated[str | None, Field(description=DEVICE)] = None,
    ) -> CallToolResult:
        # Route the existing primitive: Agents do not need a new internal tool.
        try:
            payload = await (router or get_router()).execute("upload_file", {
                "path": path, "source_url": file.download_url, "sha256": sha256,
                "overwrite": overwrite, "workspace": workspace,
            }, device)
        except (WorkspaceError, ToolExecutionError, DeviceError) as error:
            # A signed URL is a credential. Never echo it into the chat error.
            raise ToolError(str(error).replace(file.download_url, "[attachment URL redacted]")) from None
        result = dict(payload.get("result") or {})
        result.pop("source_url", None)
        result.update(imported_file_id=file.file_id, original_file_name=file.file_name,
                      declared_mime_type=file.mime_type)
        return _to_result({"result": result, "blocks": payload.get("blocks") or []})

    @server.tool(name="upload_file", title="Upload File to Device", description=UPLOAD_DESCRIPTION,
                 annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                             idempotentHint=False, openWorldHint=True),
                 structured_output=False)
    async def upload_file_tool(
        path: Annotated[str, Field(description="Destination file path, relative to the workspace or absolute inside it.")],
        action: Annotated[str, Field(description="auto (default), begin, chunk, commit, abort or status.")] = "auto",
        content_base64: Annotated[str | None, Field(description="Standard base64 payload for a small file.")] = None,
        content_text: Annotated[str | None, Field(description="UTF-8 text payload for a small text file.")] = None,
        source_url: Annotated[str | None, Field(description="https URL the device fetches, streams and verifies.")] = None,
        size: Annotated[int | None, Field(description="Total file size in bytes; required for begin.")] = None,
        sha256: Annotated[str | None, Field(description="Expected SHA-256 hex digest of the complete file.")] = None,
        session_id: Annotated[str | None, Field(description="Transfer session returned by begin.")] = None,
        offset: Annotated[int | None, Field(description="Byte offset of this chunk.")] = None,
        data_base64: Annotated[str | None, Field(description="Base64 bytes of this chunk.")] = None,
        chunk_sha256: Annotated[str | None, Field(description="Optional SHA-256 of this chunk.")] = None,
        chunk_size: Annotated[int | None, Field(description="Preferred chunk size in bytes for begin.")] = None,
        overwrite: Annotated[bool | None, Field(description="Replace an existing file; default false.")] = None,
        create_parents: Annotated[bool | None, Field(description="Create missing parent directories.")] = None,
        idempotency_key: Annotated[str | None, Field(description="Replay-safe key for write-once uploads.")] = None,
        workspace: Annotated[str | None, Field(description=WORKSPACE)] = None,
        device: Annotated[str | None, Field(description=DEVICE)] = None,
    ) -> CallToolResult:
        return await _run(router, "upload_file", {
            "path": path, "action": action, "content_base64": content_base64,
            "content_text": content_text, "source_url": source_url, "size": size,
            "sha256": sha256, "session_id": session_id, "offset": offset,
            "data_base64": data_base64, "chunk_sha256": chunk_sha256,
            "chunk_size": chunk_size, "overwrite": overwrite,
            "create_parents": create_parents, "idempotency_key": idempotency_key,
            "workspace": workspace,
        }, device)

    @server.tool(name="download_file", title="Download File from Device", description=DOWNLOAD_DESCRIPTION,
                 annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
                 structured_output=False)
    async def download_file_tool(
        path: Annotated[str, Field(description="File path, relative to the workspace or absolute inside it.")],
        offset: Annotated[int, Field(description="First byte to return; use next_offset to resume.")] = 0,
        length: Annotated[int | None, Field(description="Bytes to return; defaults to the inline budget.")] = None,
        mode: Annotated[str, Field(description="auto (default), inline or url.")] = "auto",
        sha256: Annotated[bool, Field(description="Compute the whole-file SHA-256.")] = False,
        workspace: Annotated[str | None, Field(description=WORKSPACE)] = None,
        device: Annotated[str | None, Field(description=DEVICE)] = None,
    ) -> CallToolResult:
        return await _run(router, "download_file", {
            "path": path, "offset": offset, "length": length, "mode": mode,
            "sha256": sha256, "workspace": workspace,
        }, device)

    @server.tool(name="preview_file", title="Preview File", description=PREVIEW_DESCRIPTION,
                 annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
                 structured_output=False)
    async def preview_file_tool(
        path: Annotated[str, Field(description="File path, relative to the workspace or absolute inside it.")],
        max_bytes: Annotated[int | None, Field(description="Inline preview budget for this call.")] = None,
        mode: Annotated[str, Field(description="both (default), pages or text for PDF/Office.")] = "both",
        page: Annotated[int, Field(description="First PDF/exported Office page, 1-based.", ge=1)] = 1,
        page_count: Annotated[int, Field(description="Number of pages to preview, at most 3.", ge=1, le=3)] = 1,
        workspace: Annotated[str | None, Field(description=WORKSPACE)] = None,
        device: Annotated[str | None, Field(description=DEVICE)] = None,
    ) -> CallToolResult:
        return await _run(router, "preview_file", {
            "path": path, "max_bytes": max_bytes, "mode": mode, "page": page,
            "page_count": page_count, "workspace": workspace,
        }, device)

    @server.tool(name="transfer_status", title="File Transfer Status", description=STATUS_DESCRIPTION,
                 annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
                 structured_output=False)
    async def transfer_status_tool(
        session_id: Annotated[str | None, Field(description="Session to inspect; omit to list all.")] = None,
        include_finished: Annotated[bool, Field(description="Include committed and aborted sessions.")] = True,
        device: Annotated[str | None, Field(description=DEVICE)] = None,
    ) -> CallToolResult:
        return await _run(router, "transfer_status", {
            "session_id": session_id, "include_finished": include_finished,
        }, device)

    @server.tool(name="transfer_directory", title="Transfer Directory", description=DIRECTORY_DESCRIPTION,
                 annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                             idempotentHint=False, openWorldHint=True),
                 structured_output=False)
    async def transfer_directory_tool(
        path: Annotated[str, Field(description="Directory path on the selected device.")],
        action: Annotated[str, Field(description="manifest (default), download or upload.")] = "manifest",
        include: Annotated[list[str] | None, Field(description="Glob patterns to include, e.g. ['**/*.png'].")] = None,
        exclude: Annotated[list[str] | None, Field(description="Glob patterns to exclude.")] = None,
        max_files: Annotated[int | None, Field(description="Cap on files considered.")] = None,
        max_bytes: Annotated[int | None, Field(description="Cap on total bytes considered.")] = None,
        sha256: Annotated[bool, Field(description="Include per-file SHA-256 in a manifest.")] = True,
        zip_path: Annotated[str | None, Field(description="Existing device ZIP to extract (action=upload).")] = None,
        content_base64: Annotated[str | None, Field(description="Inline ZIP archive (action=upload).")] = None,
        source_url: Annotated[str | None, Field(description="https URL of a ZIP (action=upload).")] = None,
        overwrite: Annotated[bool | None, Field(description="Replace existing files during extraction.")] = None,
        archive: Annotated[bool, Field(description="Build a ZIP for action=download (always true today).")] = True,
        workspace: Annotated[str | None, Field(description=WORKSPACE)] = None,
        device: Annotated[str | None, Field(description=DEVICE)] = None,
    ) -> CallToolResult:
        return await _run(router, "transfer_directory", {
            "path": path, "action": action, "include": include, "exclude": exclude,
            "max_files": max_files, "max_bytes": max_bytes, "sha256": sha256,
            "zip_path": zip_path, "content_base64": content_base64,
            "source_url": source_url, "overwrite": overwrite, "archive": archive,
            "workspace": workspace,
        }, device)
