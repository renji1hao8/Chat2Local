"""File-transfer engine shared by the MCP tool layer and the device dispatcher.

Everything that touches the filesystem runs in a worker thread; the public
methods are coroutines so the MCP event loop is never blocked by a large read
or a slow source URL.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import mimetypes
import os
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from chat2local.files.config import FileTransferConfig, get_file_config
from chat2local.files.errors import TransferError
from chat2local.files.fetch import fetch_to_file
from chat2local.files.documents import DOCUMENT_SUFFIXES, render_document
from chat2local.files.links import LINKS, LinkStore
from chat2local.files.policy import (
    PREVIEWABLE_IMAGE_MIMES,
    ensure_parent,
    is_within,
    looks_like_text,
    read_head,
    relative_name,
    resolve_target,
    sniff_mime,
)
from chat2local.files.store import SessionStore, UploadSession, remove_tree
from chat2local.runtime.workspace import WorkspaceError, WorkspaceManager

HASH_BLOCK = 1024 * 1024
DEFAULT_CHUNK = 256 * 1024
# Leave room for JSON envelopes and worst-case escaped text on 1 MiB links.
WIRE_BINARY_BYTES = 512 * 1024
WIRE_TEXT_BYTES = 96 * 1024


# --------------------------------------------------------------------------
# argument models (one per tool; validated before any filesystem call)
# --------------------------------------------------------------------------
class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace: str | None = None


class UploadArgs(_Args):
    path: str
    action: Literal["auto", "begin", "chunk", "commit", "abort", "status"] = "auto"
    content_base64: str | None = None
    content_text: str | None = None
    source_url: str | None = None
    size: int | None = Field(default=None, ge=0)
    sha256: str | None = None
    chunk_size: int | None = Field(default=None, ge=1024)
    session_id: str | None = None
    offset: int | None = Field(default=None, ge=0)
    data_base64: str | None = None
    chunk_sha256: str | None = None
    overwrite: bool | None = None
    create_parents: bool | None = None
    idempotency_key: str | None = Field(default=None, max_length=128)


class DownloadArgs(_Args):
    path: str
    offset: int = Field(default=0, ge=0)
    length: int | None = Field(default=None, ge=1)
    mode: Literal["auto", "inline", "url"] = "auto"
    sha256: bool = False


class PreviewArgs(_Args):
    path: str
    max_bytes: int | None = Field(default=None, ge=1)
    mode: Literal["both", "pages", "text"] = "both"
    page: int = Field(default=1, ge=1)
    page_count: int = Field(default=1, ge=1, le=3)


class StatusArgs(_Args):
    session_id: str | None = None
    include_finished: bool = True


class DirectoryArgs(_Args):
    path: str
    action: Literal["manifest", "download", "upload"] = "manifest"
    include: list[str] | None = None
    exclude: list[str] | None = None
    max_files: int | None = Field(default=None, ge=1)
    max_bytes: int | None = Field(default=None, ge=1)
    sha256: bool = True
    zip_path: str | None = None
    content_base64: str | None = None
    source_url: str | None = None
    overwrite: bool | None = None
    archive: bool = True


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _b64decode(value: str, *, code: str = "invalid_base64") -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise TransferError(code, "Content is not valid base64") from None


def _blob_block(path: Path, data: bytes, mime: str, *, device_id: str) -> dict[str, Any]:
    uri = "chat2local://file/{}/{}".format(device_id, quote(path.as_posix(), safe=""))
    return {
        "type": "resource",
        "resource": {"uri": uri, "mimeType": mime, "blob": base64.b64encode(data).decode("ascii")},
    }


def _image_block(data: bytes, mime: str) -> dict[str, Any]:
    return {"type": "image", "data": base64.b64encode(data).decode("ascii"), "mimeType": mime}


def _text_block(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(HASH_BLOCK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _fsync(path: Path) -> None:
    with path.open("rb+") as handle:
        handle.flush()
        os.fsync(handle.fileno())


@dataclass
class _HashCache:
    key: tuple[int, int]
    value: str


class FileService:
    def __init__(
        self,
        config: FileTransferConfig | None = None,
        *,
        device_id: str = "local",
        base_url: str = "http://127.0.0.1:8765",
        links: LinkStore | None = None,
    ) -> None:
        self.config = config or get_file_config()
        self.device_id = device_id
        self.base_url = (self.config.download_base_url or base_url).rstrip("/")
        self.store = SessionStore(self.config)
        self.links = links or LINKS
        self._hash_cache: dict[Path, _HashCache] = {}
        self._semaphore = asyncio.Semaphore(self.config.max_concurrent_transfers)
        self._started = False

    # -- lifecycle ---------------------------------------------------------
    async def startup(self) -> None:
        if self._started:
            return
        await asyncio.to_thread(self.store.ensure_dirs)
        await asyncio.to_thread(self.store.load_all)
        await self.sweep()
        self._started = True

    async def sweep(self) -> dict[str, int]:
        result = await self.store.sweep()
        await asyncio.to_thread(self._clean_orphan_staging)
        return result

    def _clean_orphan_staging(self) -> None:
        deadline = time.time() - self.config.session_ttl
        for session in list(self.store._sessions.values()):
            if session.state == "open":
                continue
            try:
                if session.staging_path.exists():
                    session.staging_path.unlink(missing_ok=True)
            except OSError:
                pass
        del deadline

    # -- shared helpers ----------------------------------------------------
    def _limits(self) -> dict[str, Any]:
        return {
            "max_file_bytes": self.config.max_file_bytes,
            "max_inline_bytes": self.config.max_inline_bytes,
            "chunk_size": self.config.chunk_size,
            "max_chunk_bytes": self.config.max_chunk_bytes,
            "overwrite_allowed": self.config.allow_overwrite,
        }

    def _resolve(self, workspace: WorkspaceManager, path: str, *, write: bool) -> Path:
        try:
            return resolve_target(workspace, path, write=write, deny_globs=self.config.deny_globs)
        except WorkspaceError as error:
            raise TransferError("outside_workspace", str(error), path=path) from None

    def _relative(self, path: Path, workspace: WorkspaceManager) -> str:
        return relative_name(path, workspace)

    def _download_url(self, path: Path) -> tuple[str | None, float | None]:
        if not self.config.download_url_enabled:
            return None, None
        link = self.links.create(
            path,
            ttl=self.config.download_token_ttl,
            single_use=self.config.download_token_single_use,
        )
        return f"{self.base_url}/files/{link.token}", link.expires_at

    def _hash_of(self, path: Path, *, force: bool = False) -> str:
        stat = path.stat()
        cached = self._hash_cache.get(path)
        if not force and cached is not None and cached.key == (stat.st_size, stat.st_mtime_ns):
            return cached.value
        value = _sha256_file(path)
        self._hash_cache[path] = _HashCache((stat.st_size, stat.st_mtime_ns), value)
        return value

    # ======================================================================
    # upload
    # ======================================================================
    async def upload(self, args: UploadArgs, workspace: WorkspaceManager) -> dict[str, Any]:
        if not self.config.enabled:
            raise TransferError("disabled", "File transfer is disabled in the device configuration")
        await self._ensure_started()

        if args.action == "status":
            return await self.status(StatusArgs(workspace=args.workspace, session_id=args.session_id))
        if args.action == "chunk":
            return await self._upload_chunk(args)
        if args.action == "commit":
            return await self._upload_commit(args, workspace)
        if args.action == "abort":
            return await self._upload_abort(args)
        if args.action == "begin":
            return await self._upload_begin(args, workspace)

        # action == "auto"
        if args.session_id is not None:
            raise TransferError(
                "invalid_argument",
                "session_id requires an explicit action (chunk, commit or abort)",
            )
        return await self._upload_auto(args, workspace)

    async def _ensure_started(self) -> None:
        if not self._started:
            await self.startup()

    async def _upload_auto(self, args: UploadArgs, workspace: WorkspaceManager) -> dict[str, Any]:
        sources = [name for name in ("content_base64", "content_text", "source_url")
                   if getattr(args, name) is not None]
        if len(sources) != 1:
            raise TransferError(
                "invalid_argument",
                "Provide exactly one of content_base64, content_text or source_url",
                provided=sources,
            )
        if args.source_url is not None:
            return await self._upload_from_url(args, workspace)

        if not self.config.allow_text_upload and args.content_text is not None:
            raise TransferError("disabled", "Text uploads are disabled")
        if not self.config.allow_base64_upload and args.content_base64 is not None:
            raise TransferError("disabled", "Base64 uploads are disabled")

        if args.content_text is not None:
            data = args.content_text.encode("utf-8")
        else:
            data = _b64decode(args.content_base64 or "")
        if len(data) > self.config.max_inline_bytes:
            raise TransferError(
                "payload_too_large",
                "Inline payload exceeds the single-request limit; use begin/chunk or source_url",
                size=len(data),
                limit=self.config.max_inline_bytes,
            )
        return await self._store_inline(args, workspace, data)

    async def _store_inline(
        self, args: UploadArgs, workspace: WorkspaceManager, data: bytes
    ) -> dict[str, Any]:
        expected = (args.sha256 or "").lower() or None
        fingerprint = self._fingerprint(args, hashlib.sha256(data).hexdigest())
        if args.idempotency_key:
            replay = await self.store.lookup_idempotent(args.idempotency_key, fingerprint)
            if replay is not None:
                return {"result": {**replay, "idempotent_replay": True}, "blocks": []}

        target = self._resolve(workspace, args.path, write=True)
        self._check_writable(target, args, workspace, size=len(data))
        if expected is not None and expected != hashlib.sha256(data).hexdigest():
            raise TransferError("sha256_mismatch", "Payload does not match the expected digest",
                                expected=expected, actual=hashlib.sha256(data).hexdigest())
        if len(data) > self.config.max_file_bytes:
            raise TransferError("file_too_large", "File exceeds the configured maximum",
                                size=len(data), limit=self.config.max_file_bytes)

        session = await self.store.create(
            device_id=self.device_id,
            target=target,
            workspace_root=workspace.root,
            total_size=len(data),
            chunk_size=max(len(data), 1),
            expected_sha256=expected,
            overwrite=self._overwrite(args),
            idempotency_key=args.idempotency_key,
            source="inline",
        )
        await asyncio.to_thread(self._write_bytes, session.staging_path, data, 0)
        session.received = len(data)
        await self.store.save(session)
        result = await self._commit_session(session, workspace, computed_sha=hashlib.sha256(data).hexdigest())
        if args.idempotency_key:
            await self.store.record_idempotent(args.idempotency_key, fingerprint, result)
        return {"result": result, "blocks": []}

    async def _upload_from_url(self, args: UploadArgs, workspace: WorkspaceManager) -> dict[str, Any]:
        if not self.config.allow_source_url:
            raise TransferError("disabled", "source_url uploads are disabled")
        url = args.source_url or ""
        target = self._resolve(workspace, args.path, write=True)
        self._check_writable(target, args, workspace, size=args.size)
        fingerprint = self._fingerprint(args, url)
        if args.idempotency_key:
            replay = await self.store.lookup_idempotent(args.idempotency_key, fingerprint)
            if replay is not None:
                return {"result": {**replay, "idempotent_replay": True}, "blocks": []}

        declared = args.size if args.size is not None else self.config.max_source_bytes
        session = await self.store.create(
            device_id=self.device_id,
            target=target,
            workspace_root=workspace.root,
            total_size=declared,
            chunk_size=DEFAULT_CHUNK,
            expected_sha256=(args.sha256 or "").lower() or None,
            overwrite=self._overwrite(args),
            idempotency_key=args.idempotency_key,
            source="source_url",
        )
        async with self._semaphore:
            try:
                meta = await asyncio.to_thread(
                    fetch_to_file,
                    url,
                    session.staging_path,
                    max_bytes=self.config.max_source_bytes,
                    allow_insecure=self.config.allow_insecure_source_url,
                    allow_private=self.config.allow_private_source_hosts,
                )
            except TransferError:
                await self.store.discard(session)
                raise
            except Exception:
                await self.store.discard(session)
                raise
        session.total_size = int(meta["size"])
        session.received = int(meta["size"])
        if session.total_size > self.config.max_file_bytes:
            await self.store.discard(session)
            raise TransferError("file_too_large", "Source exceeds the configured maximum",
                                size=session.total_size, limit=self.config.max_file_bytes)
        await self.store.save(session)
        result = await self._commit_session(session, workspace, computed_sha=str(meta["sha256"]))
        result["source_url"] = meta.get("final_url") or url
        result["source_content_type"] = meta.get("content_type")
        if args.idempotency_key:
            await self.store.record_idempotent(args.idempotency_key, fingerprint, result)
        return {"result": result, "blocks": []}

    async def _upload_begin(self, args: UploadArgs, workspace: WorkspaceManager) -> dict[str, Any]:
        if args.size is None:
            raise TransferError("invalid_argument", "size is required for action=begin")
        target = self._resolve(workspace, args.path, write=True)
        self._check_writable(target, args, workspace, size=args.size)
        chunk_size = min(args.chunk_size or self.config.chunk_size, self.config.max_chunk_bytes)
        session = await self.store.create(
            device_id=self.device_id,
            target=target,
            workspace_root=workspace.root,
            total_size=args.size,
            chunk_size=chunk_size,
            expected_sha256=(args.sha256 or "").lower() or None,
            overwrite=self._overwrite(args),
            idempotency_key=args.idempotency_key,
            source="chunked",
        )
        await asyncio.to_thread(session.staging_path.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(session.staging_path.touch, exist_ok=True)
        return {
            "result": {
                **session.public(self.config.session_ttl),
                "path": self._relative(target, workspace),
                "absolute_path": str(target),
                "limits": self._limits(),
            },
            "blocks": [],
        }

    async def _upload_chunk(self, args: UploadArgs) -> dict[str, Any]:
        if not args.session_id:
            raise TransferError("invalid_argument", "session_id is required for action=chunk")
        if args.offset is None or args.data_base64 is None:
            raise TransferError("invalid_argument", "offset and data_base64 are required for action=chunk")
        session = await self.store.get(args.session_id)
        if session.state != "open":
            raise TransferError("session_closed", "Transfer session is no longer open",
                                session_id=session.id, state=session.state)
        data = _b64decode(args.data_base64)
        if args.chunk_sha256 and hashlib.sha256(data).hexdigest() != args.chunk_sha256.lower():
            raise TransferError("sha256_mismatch", "Chunk digest mismatch")
        if len(data) > self.config.max_chunk_bytes:
            raise TransferError("chunk_too_large", "Chunk exceeds the configured maximum",
                                size=len(data), limit=self.config.max_chunk_bytes)
        if args.offset + len(data) > session.total_size:
            raise TransferError("offset_mismatch", "Chunk runs past the declared file size",
                                offset=args.offset, length=len(data), size=session.total_size)
        async with self._semaphore:
            await asyncio.to_thread(self._write_chunk, session, data, args.offset)
        expected_offset = max(session.received, args.offset + len(data))
        session.received = expected_offset
        session.updated_at = time.time()
        await self.store.save(session)
        return {
            "result": {
                **session.public(self.config.session_ttl),
                "next_offset": session.received,
            },
            "blocks": [],
        }

    async def _upload_commit(self, args: UploadArgs, workspace: WorkspaceManager) -> dict[str, Any]:
        if not args.session_id:
            raise TransferError("invalid_argument", "session_id is required for action=commit")
        session = await self.store.get(args.session_id)
        if session.state == "committed" and session.result is not None:
            return {"result": {**session.result, "idempotent_replay": True}, "blocks": []}
        if session.state != "open":
            raise TransferError("session_closed", "Transfer session cannot be committed",
                                session_id=session.id, state=session.state)
        result = await self._commit_session(session, workspace)
        return {"result": result, "blocks": []}

    async def _upload_abort(self, args: UploadArgs) -> dict[str, Any]:
        if not args.session_id:
            raise TransferError("invalid_argument", "session_id is required for action=abort")
        session = await self.store.get(args.session_id)
        if session.state == "open":
            await self.store.discard(session, state="aborted")
        return {"result": {"session_id": session.id, "state": session.state}, "blocks": []}

    async def _commit_session(
        self, session: UploadSession, workspace: WorkspaceManager, *, computed_sha: str | None = None
    ) -> dict[str, Any]:
        staging = session.staging_path
        target = Path(session.target)

        def _prepare() -> tuple[int, str]:
            if not staging.exists():
                raise TransferError("staging_missing", "Staged data is missing", session_id=session.id)
            size = staging.stat().st_size
            if size != session.total_size:
                raise TransferError(
                    "size_mismatch",
                    "Staged data does not match the declared size",
                    expected=session.total_size,
                    actual=size,
                )
            digest = computed_sha or _sha256_file(staging)
            if session.expected_sha256 and digest != session.expected_sha256:
                raise TransferError(
                    "sha256_mismatch",
                    "Staged data does not match the expected digest",
                    expected=session.expected_sha256,
                    actual=digest,
                )
            _fsync(staging)
            if target.exists() and not session.overwrite:
                raise TransferError(
                    "exists", "Destination already exists and overwrite is false", path=str(target)
                )
            ensure_parent(target, create=self.config.create_parents)
            try:
                os.replace(staging, target)
            except OSError as error:
                raise TransferError(
                    "commit_failed", "Could not move the staged file into place",
                    path=str(target), reason=str(error),
                ) from None
            return size, digest

        async with self._semaphore:
            size, digest = await asyncio.to_thread(_prepare)

        session.state = "committed"
        session.received = size
        session.updated_at = time.time()
        result = {
            "ok": True,
            "session_id": session.id,
            "path": self._relative(target, workspace),
            "absolute_path": str(target),
            "size": size,
            "sha256": digest,
            "committed_at": session.updated_at,
        }
        session.result = result
        await self.store.save(session)
        self._hash_cache[target] = _HashCache((size, target.stat().st_mtime_ns), digest)
        return result

    # -- fs primitives -----------------------------------------------------
    def _write_bytes(self, path: Path, data: bytes, offset: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("r+b" if path.exists() else "wb") as handle:
            handle.seek(offset)
            handle.write(data)

    def _write_chunk(self, session: UploadSession, data: bytes, offset: int) -> None:
        path = session.staging_path
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.stat().st_size if path.exists() else 0
        if offset <= existing:
            overlap = min(len(data), existing - offset)
            if overlap:
                with path.open("rb") as handle:
                    handle.seek(offset)
                    if handle.read(overlap) != data[:overlap]:
                        raise TransferError(
                            "offset_mismatch",
                            "Retried chunk does not match the bytes already stored",
                            offset=offset,
                        )
                data = data[overlap:]
                offset += overlap
        if offset > existing and existing != 0:
            raise TransferError(
                "offset_mismatch", "Chunk does not continue the staged data",
                offset=offset, received=existing,
            )
        with path.open("r+b" if path.exists() else "wb") as handle:
            handle.seek(offset)
            handle.write(data)
            handle.flush()

    def _overwrite(self, args: UploadArgs) -> bool:
        if args.overwrite is None:
            return self.config.allow_overwrite
        if args.overwrite and not self.config.allow_overwrite:
            raise TransferError("overwrite_denied", "Overwriting is disabled by configuration")
        return args.overwrite

    def _check_writable(
        self, target: Path, args: UploadArgs, workspace: WorkspaceManager, *, size: int | None
    ) -> None:
        if target.exists() and target.is_dir():
            raise TransferError("is_directory", "Destination is a directory", path=str(target))
        overwrite = self._overwrite(args)
        if target.exists() and not overwrite:
            raise TransferError("exists", "Destination already exists and overwrite is false",
                                path=self._relative(target, workspace))
        if size is not None and size > self.config.max_file_bytes:
            raise TransferError("file_too_large", "File exceeds the configured maximum",
                                size=size, limit=self.config.max_file_bytes)
        parent = target.parent
        if not parent.is_dir():
            create = self.config.create_parents if args.create_parents is None else args.create_parents
            ensure_parent(target, create=create)

    def _fingerprint(self, args: UploadArgs, digest: str) -> str:
        material = json.dumps(
            {
                "path": args.path, "size": args.size, "sha256": (args.sha256 or "").lower(),
                "digest": digest, "workspace": args.workspace,
            },
            sort_keys=True,
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    # ======================================================================
    # download
    # ======================================================================
    async def download(self, args: DownloadArgs, workspace: WorkspaceManager) -> dict[str, Any]:
        if not self.config.enabled:
            raise TransferError("disabled", "File transfer is disabled in the device configuration")
        await self._ensure_started()
        path = self._resolve(workspace, args.path, write=False)
        if not path.exists():
            raise TransferError("not_found", "File does not exist", path=args.path)
        if path.is_dir():
            raise TransferError("is_directory", "Path is a directory; use transfer_directory", path=args.path)
        if not path.is_file():
            raise TransferError("not_regular_file", "Path is not a regular file", path=args.path)

        size = path.stat().st_size
        if args.offset > size:
            raise TransferError("range_not_satisfiable", "offset is past the end of the file",
                                offset=args.offset, size=size)
        remaining = size - args.offset
        head = await asyncio.to_thread(read_head, path)
        wire_budget = WIRE_TEXT_BYTES if looks_like_text(path, head) else WIRE_BINARY_BYTES
        inline_budget = min(self.config.max_inline_bytes, wire_budget)

        if args.length is not None:
            length = min(args.length, remaining, inline_budget)
        elif args.mode == "auto" and size <= inline_budget:
            length = remaining
        else:
            length = min(inline_budget, remaining)

        blocks: list[dict[str, Any]] = []
        data = b""
        if args.mode != "url" and length > 0:
            data = await asyncio.to_thread(_read_range, path, args.offset, length)
            mime = sniff_mime(path, data[:512])
            if looks_like_text(path, data[:4096]) and _decodes_utf8(data):
                blocks.append(_text_block(data.decode("utf-8")))
            else:
                blocks.append(_blob_block(path, data, mime, device_id=self.device_id))

        want_sha = args.sha256 or size <= self.config.max_inline_bytes
        digest = None
        if want_sha:
            try:
                digest = await asyncio.to_thread(self._hash_of, path)
            except OSError:
                digest = None

        url, expires = (None, None)
        if args.mode in ("auto", "url"):
            url, expires = self._download_url(path)

        mime_type = sniff_mime(path, await asyncio.to_thread(read_head, path))
        result = {
            "ok": True,
            "path": self._relative(path, workspace),
            "absolute_path": str(path),
            "size": size,
            "mime_type": mime_type,
            "sha256": digest,
            "offset": args.offset,
            "length": len(data),
            "next_offset": args.offset + len(data),
            "complete": args.offset + len(data) >= size,
            "download_url": url,
            "download_url_expires_at": expires,
            "delivery": "inline" if blocks else "url",
        }
        if args.mode == "url" or (not blocks and url is None):
            if url is None:
                raise TransferError("download_unavailable", "No delivery channel available")
        return {"result": result, "blocks": blocks}

    # ======================================================================
    # preview
    # ======================================================================
    async def preview(self, args: PreviewArgs, workspace: WorkspaceManager) -> dict[str, Any]:
        if not self.config.enabled:
            raise TransferError("disabled", "File transfer is disabled in the device configuration")
        await self._ensure_started()
        path = self._resolve(workspace, args.path, write=False)
        if not path.exists() or not path.is_file():
            raise TransferError("not_found", "File does not exist", path=args.path)

        size = path.stat().st_size
        head = await asyncio.to_thread(read_head, path)
        mime = sniff_mime(path, head)
        wire_budget = WIRE_BINARY_BYTES if mime in PREVIEWABLE_IMAGE_MIMES and mime != "image/svg+xml" else WIRE_TEXT_BYTES
        budget = min(args.max_bytes or self.config.max_preview_bytes, self.config.max_preview_bytes, wire_budget)

        result: dict[str, Any] = {
            "ok": True,
            "path": self._relative(path, workspace),
            "size": size,
            "mime_type": mime,
            "preview": "metadata",
            "previewable": False,
        }
        blocks: list[dict[str, Any]] = []

        if mime == "application/pdf" or path.suffix.lower() in DOCUMENT_SUFFIXES:
            return await self._preview_document(path, args, result)

        if mime in PREVIEWABLE_IMAGE_MIMES:
            if size > budget:
                url, expires = self._download_url(path)
                result.update({
                    "preview": "link" if url else "metadata",
                    "previewable": False,
                    "reason": "image larger than the inline preview budget",
                    "download_url": url,
                    "download_url_expires_at": expires,
                })
                return {"result": result, "blocks": []}
            data = await asyncio.to_thread(path.read_bytes)
            if mime == "image/svg+xml":
                blocks.append(_text_block(data.decode("utf-8", "replace")))
                result.update({"preview": "inline", "previewable": True})
            else:
                blocks.append(_image_block(data, mime))
                result.update({"preview": "inline", "previewable": True})
            return {"result": result, "blocks": blocks}

        if looks_like_text(path, head):
            text = await asyncio.to_thread(_read_text_head, path, budget)
            blocks.append(_text_block(text))
            result.update({
                "preview": "inline",
                "previewable": True,
                "truncated": size > len(text.encode("utf-8")),
                "shown_bytes": len(text.encode("utf-8")),
            })
            return {"result": result, "blocks": blocks}

        url, expires = self._download_url(path)
        result.update({
            "preview": "link" if url else "metadata",
            "note": "No inline preview for this media type; use download_file or the URL",
            "download_url": url,
            "download_url_expires_at": expires,
        })
        return {"result": result, "blocks": []}

    async def _preview_document(self, path: Path, args: PreviewArgs, result: dict[str, Any]) -> dict[str, Any]:
        url, expires = self._download_url(path)
        result.update(download_url=url, download_url_expires_at=expires)
        if not self.config.document_preview_enabled:
            result.update(preview="link" if url else "metadata", reason="document_preview_disabled")
            return {"result": result, "blocks": []}
        budget = min(args.max_bytes or self.config.max_preview_bytes, self.config.max_preview_bytes)
        # Reserve at most 16 KiB for text, keeping escaped JSON + images below 1 MiB.
        text_budget = min(budget // 4 if args.mode == "both" else budget, 16 * 1024)
        image_budget = budget - text_budget if args.mode == "both" else budget
        try:
            async with self._semaphore:
                rendered = await asyncio.to_thread(
                    render_document, path, page=args.page,
                    page_count=min(args.page_count, self.config.max_preview_pages), mode=args.mode,
                    image_budget=image_budget, text_budget=text_budget,
                    max_input_bytes=self.config.max_document_bytes, timeout=self.config.preview_timeout,
                    soffice=self.config.soffice_path, pdftoppm=self.config.pdftoppm_path,
                    pdftotext=self.config.pdftotext_path, pdfinfo=self.config.pdfinfo_path,
                )
        except TransferError as error:
            result.update(preview="link" if url else "metadata", reason=error.code,
                          preview_error=error.to_dict()["error"])
            return {"result": result, "blocks": []}
        except OSError:
            result.update(preview="link" if url else "metadata", reason="preview_converter_unavailable")
            return {"result": result, "blocks": []}
        blocks = []
        pages = rendered.pop("pages")
        text = rendered.pop("text")
        if text:
            blocks.append(_text_block(text))
        for item in pages:
            blocks.append(_text_block(f"Page {item['page']}"))
            blocks.append(_image_block(item["data"], item["mime_type"]))
        result.update(rendered, preview="inline" if blocks else "metadata", previewable=bool(blocks),
                      rendered_pages=[item["page"] for item in pages], text_available=bool(text),
                      note="Text covers the selected PDF pages; scanned pages need OCR. Office pagination follows LibreOffice PDF export.")
        return {"result": result, "blocks": blocks}

    # ======================================================================
    # status
    # ======================================================================
    async def status(self, args: StatusArgs) -> dict[str, Any]:
        await self._ensure_started()
        if args.session_id:
            session = await self.store.get(args.session_id)
            return {"result": {**session.public(self.config.session_ttl)}, "blocks": []}
        sessions = [
            s for s in self.store._sessions.values()
            if args.include_finished or s.state == "open"
        ]
        sessions.sort(key=lambda s: s.updated_at, reverse=True)
        return {
            "result": {
                "sessions": [s.public(self.config.session_ttl) for s in sessions],
                "active": self.store.active_count(),
                "staging_bytes": self.store.quota_used(),
                "limits": self._limits(),
            },
            "blocks": [],
        }

    # ======================================================================
    # directory transfer
    # ======================================================================
    async def directory(self, args: DirectoryArgs, workspace: WorkspaceManager) -> dict[str, Any]:
        if not self.config.enabled:
            raise TransferError("disabled", "File transfer is disabled in the device configuration")
        await self._ensure_started()
        root = self._resolve(workspace, args.path, write=args.action == "upload")
        if args.action == "manifest":
            return await self._directory_manifest(args, workspace, root)
        if args.action == "download":
            return await self._directory_download(args, workspace, root)
        return await self._directory_upload(args, workspace, root)

    async def _directory_manifest(
        self, args: DirectoryArgs, workspace: WorkspaceManager, root: Path
    ) -> dict[str, Any]:
        if not root.is_dir():
            raise TransferError("not_found", "Directory does not exist", path=args.path)
        entries, truncated, skipped = await asyncio.to_thread(
            self._walk, root, workspace, args,
        )
        return {
            "result": {
                "ok": True,
                "path": self._relative(root, workspace),
                "kind": "manifest",
                "entries": entries,
                "entry_count": len(entries),
                "total_bytes": sum(entry["size"] for entry in entries),
                "truncated": truncated,
                "skipped": skipped,
            },
            "blocks": [],
        }

    def _walk(
        self, root: Path, workspace: WorkspaceManager, args: DirectoryArgs
    ) -> tuple[list[dict[str, Any]], bool, dict[str, int]]:
        max_files = min(args.max_files or self.config.max_directory_files, self.config.max_directory_files)
        max_bytes = min(args.max_bytes or self.config.max_directory_bytes, self.config.max_directory_bytes)
        entries: list[dict[str, Any]] = []
        total = 0
        skipped = {"symlink": 0, "excluded": 0, "unreadable": 0, "denied": 0}
        manifest_bytes = 0
        truncated = False
        include = args.include or ["**"]
        exclude = args.exclude or []

        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            current = Path(dirpath)
            kept_dirs = []
            for name in sorted(dirnames):
                candidate = current / name
                if candidate.is_symlink() or candidate.is_junction():
                    skipped["symlink"] += 1
                    continue
                try:
                    self._resolve(workspace, str(candidate), write=False)
                except TransferError:
                    skipped["denied"] += 1
                    continue
                kept_dirs.append(name)
            dirnames[:] = kept_dirs
            for name in sorted(filenames):
                candidate = current / name
                if candidate.is_symlink():
                    skipped["symlink"] += 1
                    continue
                try:
                    relative = candidate.relative_to(root).as_posix()
                except ValueError:
                    continue
                if not any(_glob(relative, pattern) for pattern in include):
                    skipped["excluded"] += 1
                    continue
                if any(_glob(relative, pattern) for pattern in exclude):
                    skipped["excluded"] += 1
                    continue
                try:
                    candidate = self._resolve(workspace, str(candidate), write=False)
                    stat = candidate.stat()
                except TransferError:
                    skipped["denied"] += 1
                    continue
                except OSError:
                    skipped["unreadable"] += 1
                    continue
                if len(entries) >= max_files or total + stat.st_size > max_bytes:
                    truncated = True
                    break
                entry: dict[str, Any] = {
                    "path": relative,
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                }
                if args.sha256:
                    try:
                        entry["sha256"] = _sha256_file(candidate)
                    except OSError:
                        entry["sha256"] = None
                        skipped["unreadable"] += 1
                entry_bytes = len(json.dumps(entry, ensure_ascii=True).encode("utf-8")) + 2
                if manifest_bytes + entry_bytes > 512 * 1024:
                    truncated = True
                    break
                manifest_bytes += entry_bytes
                entries.append(entry)
                total += stat.st_size
            if truncated:
                break
        return entries, truncated, skipped

    async def _directory_download(
        self, args: DirectoryArgs, workspace: WorkspaceManager, root: Path
    ) -> dict[str, Any]:
        if not root.is_dir():
            raise TransferError("not_found", "Directory does not exist", path=args.path)
        entries, truncated, skipped = await asyncio.to_thread(self._walk, root, workspace, args)
        archive_path = self.store.archives_dir / f"{os.urandom(8).hex()}.zip"
        try:
            size = await asyncio.to_thread(self._write_zip, root, entries, archive_path, workspace)
        except Exception:
            archive_path.unlink(missing_ok=True)
            raise
        url, expires = self._download_url(archive_path)
        blocks: list[dict[str, Any]] = []
        if size <= min(self.config.max_inline_bytes, WIRE_BINARY_BYTES):
            data = await asyncio.to_thread(archive_path.read_bytes)
            blocks.append(_blob_block(archive_path, data, "application/zip", device_id=self.device_id))
        return {
            "result": {
                "ok": True,
                "kind": "zip",
                "path": self._relative(root, workspace),
                "archive_size": size,
                "entry_count": len(entries),
                "truncated": truncated,
                "skipped": skipped,
                "download_url": url,
                "download_url_expires_at": expires,
                "delivery": "inline" if blocks else "url",
            },
            "blocks": blocks,
        }

    def _write_zip(self, root: Path, entries: list[dict[str, Any]], destination: Path, workspace: WorkspaceManager) -> int:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for entry in entries:
                source = self._resolve(workspace, str(root / entry["path"]), write=False)
                if not is_within(source, root.resolve()):
                    raise TransferError("outside_workspace", "Archive member escaped the selected directory")
                copied = 0
                with source.open("rb") as handle, archive.open(entry["path"], "w", force_zip64=True) as target:
                    while True:
                        block = handle.read(HASH_BLOCK)
                        if not block:
                            break
                        copied += len(block)
                        if copied > entry["size"]:
                            raise TransferError("file_changed", "Archive member grew during packing", path=entry["path"])
                        target.write(block)
                if copied != entry["size"]:
                    raise TransferError("file_changed", "Archive member changed during packing", path=entry["path"])
        return destination.stat().st_size

    async def _directory_upload(
        self, args: DirectoryArgs, workspace: WorkspaceManager, root: Path
    ) -> dict[str, Any]:
        sources = [name for name in ("zip_path", "content_base64", "source_url")
                   if getattr(args, name) is not None]
        if len(sources) != 1:
            raise TransferError("invalid_argument",
                                "Provide exactly one of zip_path, content_base64 or source_url")
        if root.exists() and not root.is_dir():
            raise TransferError("not_a_directory", "Destination is not a directory", path=args.path)

        work_dir = self.store.archives_dir / f"extract-{os.urandom(8).hex()}"
        work_dir.mkdir(parents=True, exist_ok=True)
        archive_file = work_dir / "incoming.zip"
        try:
            if args.zip_path is not None:
                source = self._resolve(workspace, args.zip_path, write=False)
                if not source.is_file():
                    raise TransferError("not_found", "zip_path does not exist", path=args.zip_path)
                await asyncio.to_thread(_copy_file, source, archive_file)
            elif args.content_base64 is not None:
                data = _b64decode(args.content_base64)
                if len(data) > self.config.max_inline_bytes:
                    raise TransferError("payload_too_large", "Archive exceeds the inline limit",
                                        size=len(data), limit=self.config.max_inline_bytes)
                await asyncio.to_thread(archive_file.write_bytes, data)
            else:
                if not self.config.allow_source_url:
                    raise TransferError("disabled", "source_url uploads are disabled")
                await asyncio.to_thread(
                    fetch_to_file,
                    args.source_url or "",
                    archive_file,
                    max_bytes=self.config.max_source_bytes,
                    allow_insecure=self.config.allow_insecure_source_url,
                    allow_private=self.config.allow_private_source_hosts,
                )
            result = await asyncio.to_thread(
                self._extract_zip, archive_file, root, workspace, args,
            )
        finally:
            remove_tree(work_dir)
        return {"result": result, "blocks": []}

    def _extract_zip(
        self, archive_file: Path, root: Path, workspace: WorkspaceManager, args: DirectoryArgs
    ) -> dict[str, Any]:
        max_bytes = min(args.max_bytes or self.config.max_directory_bytes, self.config.max_directory_bytes)
        max_files = min(args.max_files or self.config.max_directory_files, self.config.max_directory_files)
        overwrite = self.config.allow_overwrite if args.overwrite is None else args.overwrite
        if args.overwrite and not self.config.allow_overwrite:
            raise TransferError("overwrite_denied", "Overwriting is disabled by configuration")

        written: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        total = 0
        try:
            archive = zipfile.ZipFile(archive_file)
        except zipfile.BadZipFile:
            raise TransferError("archive_invalid", "Uploaded data is not a valid ZIP archive") from None

        with archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                name = info.filename.replace("\\", "/")
                if not _safe_member(name):
                    failed.append({"path": name, "error": "unsafe_path"})
                    continue
                mode = (info.external_attr >> 16) & 0o170000
                if mode == 0o120000:
                    failed.append({"path": name, "error": "symlink_entry"})
                    continue
                total += info.file_size
                if len(written) >= max_files or total > max_bytes:
                    failed.append({"path": name, "error": "limits_exceeded"})
                    continue
                destination = root / PurePosixPath(name)
                staging = None
                try:
                    destination = self._resolve(workspace, str(destination), write=True)
                    resolved = destination.resolve()
                    if not is_within(resolved, root.resolve()):
                        failed.append({"path": name, "error": "outside_workspace"})
                        continue
                    if destination.exists() and not overwrite:
                        failed.append({"path": name, "error": "exists"})
                        continue
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    staging = destination.parent / f".c2l-extract-{os.urandom(6).hex()}"
                    actual_size = 0
                    with archive.open(info) as source, staging.open("xb") as target:
                        while True:
                            block = source.read(HASH_BLOCK)
                            if not block:
                                break
                            actual_size += len(block)
                            if actual_size > min(info.file_size, self.config.max_file_bytes) or total - info.file_size + actual_size > max_bytes:
                                raise TransferError("limits_exceeded", "Extracted member exceeds configured limits")
                            target.write(block)
                    destination = self._resolve(workspace, str(destination), write=True)
                    os.replace(staging, destination)
                    written.append({"path": name, "size": info.file_size})
                except TransferError as error:
                    failed.append({"path": name, "error": error.code})
                except (OSError, zipfile.BadZipFile, RuntimeError) as error:
                    failed.append({"path": name, "error": str(error)})
                finally:
                    if staging is not None:
                        staging.unlink(missing_ok=True)
        return {
            "ok": not failed,
            "kind": "directory_upload",
            "path": self._relative(root, workspace),
            "written": written,
            "written_count": len(written),
            "failed": failed,
            "failed_count": len(failed),
        }


# --------------------------------------------------------------------------
# module-level helpers
# --------------------------------------------------------------------------
def _read_range(path: Path, offset: int, length: int) -> bytes:
    with path.open("rb") as handle:
        handle.seek(offset)
        return handle.read(length)


def _read_text_head(path: Path, budget: int) -> str:
    with path.open("rb") as handle:
        data = handle.read(budget)
    text = data.decode("utf-8", "replace")
    return text.encode("utf-8")[:budget].decode("utf-8", "ignore")


def _decodes_utf8(data: bytes) -> bool:
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as reader, destination.open("wb") as writer:
        while True:
            block = reader.read(HASH_BLOCK)
            if not block:
                break
            writer.write(block)


def _glob(relative: str, pattern: str) -> bool:
    from fnmatch import fnmatchcase
    if fnmatchcase(relative, pattern):
        return True
    if "**" in pattern and fnmatchcase(relative, pattern.replace("**/", "")):
        return True
    return False


def _safe_member(name: str) -> bool:
    if not name or name.startswith("/") or name.startswith("\\"):
        return False
    if "\x00" in name:
        return False
    parts = PurePosixPath(name).parts
    if any(part == ".." for part in parts):
        return False
    if any(":" in part for part in parts):
        return False
    return True


# --------------------------------------------------------------------------
# process-wide service
# --------------------------------------------------------------------------
_service: FileService | None = None


def configure_file_service(
    *,
    device_id: str | None = None,
    base_url: str | None = None,
    config: FileTransferConfig | None = None,
) -> FileService:
    global _service
    _service = FileService(
        config or get_file_config(),
        device_id=device_id or "local",
        base_url=base_url or "http://127.0.0.1:8765",
    )
    return _service


def get_file_service() -> FileService:
    global _service
    if _service is None:
        _service = FileService(get_file_config())
    return _service
