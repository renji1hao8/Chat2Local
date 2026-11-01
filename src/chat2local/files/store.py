"""On-disk state for resumable uploads.

Layout under ``state_dir`` (default ``~/.chat2local/transfers``)::

    sessions/<session-id>.json   one metadata record per transfer
    archives/<name>.zip          staged ZIPs produced by transfer_directory
    idempotency.json             replay index for write-once requests

Payload bytes never live here: a partial upload is staged next to its final
destination as ``.c2l-part-<session-id>`` so that the commit is a same-volume
``os.replace`` (atomic on NTFS) rather than a cross-volume copy.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from chat2local.files.config import FileTransferConfig
from chat2local.files.errors import TransferError

STAGING_PREFIX = ".c2l-part-"


@dataclass
class UploadSession:
    id: str
    device_id: str
    target: str
    workspace_root: str
    total_size: int
    chunk_size: int
    received: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    state: str = "open"
    expected_sha256: str | None = None
    overwrite: bool = False
    idempotency_key: str | None = None
    source: str = "chunked"
    result: dict[str, Any] | None = None

    @property
    def staging_path(self) -> Path:
        return Path(self.target).parent / f"{STAGING_PREFIX}{self.id}"

    def public(self, ttl: float = 0.0) -> dict[str, Any]:
        return {
            "session_id": self.id,
            "state": self.state,
            "path": self.target,
            "size": self.total_size,
            "chunk_size": self.chunk_size,
            "received": self.received,
            "next_offset": self.received,
            "remaining": max(0, self.total_size - self.received),
            "complete": self.received >= self.total_size,
            "sha256_expected": self.expected_sha256,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.updated_at + ttl,
            "source": self.source,
        }


class SessionStore:
    def __init__(self, config: FileTransferConfig) -> None:
        self.config = config
        self.root = config.state_path()
        self.sessions_dir = self.root / "sessions"
        self.archives_dir = self.root / "archives"
        self._lock = asyncio.Lock()
        self._sessions: dict[str, UploadSession] = {}
        self._idempotency: dict[str, dict[str, Any]] | None = None

    # -- lifecycle ---------------------------------------------------------
    def ensure_dirs(self) -> None:
        for directory in (self.root, self.sessions_dir, self.archives_dir):
            directory.mkdir(parents=True, exist_ok=True)

    def load_all(self) -> None:
        self.ensure_dirs()
        for record in self.sessions_dir.glob("*.json"):
            try:
                data = json.loads(record.read_text(encoding="utf-8"))
                session = UploadSession(**data)
            except (OSError, ValueError, TypeError):
                continue
            self._sessions[session.id] = session

    def quota_used(self) -> int:
        return sum(s.total_size for s in self._sessions.values() if s.state == "open")

    def active_count(self) -> int:
        return sum(1 for s in self._sessions.values() if s.state == "open")

    # -- persistence -------------------------------------------------------
    async def save(self, session: UploadSession) -> None:
        self._sessions[session.id] = session
        await asyncio.to_thread(self._write_record, session)

    def _write_record(self, session: UploadSession) -> None:
        self.ensure_dirs()
        path = self.sessions_dir / f"{session.id}.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(asdict(session), ensure_ascii=False), encoding="utf-8"
        )
        os.replace(temporary, path)

    async def get(self, session_id: str) -> UploadSession:
        session = self._sessions.get(session_id)
        if session is None:
            path = self.sessions_dir / f"{session_id}.json"
            if path.exists():
                try:
                    session = UploadSession(**json.loads(path.read_text(encoding="utf-8")))
                except (OSError, ValueError, TypeError):
                    session = None
                if session is not None:
                    self._sessions[session.id] = session
        if session is None:
            raise TransferError("session_not_found", "Unknown transfer session", session_id=session_id)
        if session.state == "open" and self._expired(session):
            await self.discard(session, state="expired")
            raise TransferError(
                "session_expired", "Transfer session expired; start a new upload", session_id=session_id
            )
        return session

    def _expired(self, session: UploadSession) -> bool:
        return time.time() - session.updated_at > self.config.session_ttl

    async def discard(self, session: UploadSession, *, state: str = "aborted") -> None:
        session.state = state
        session.updated_at = time.time()
        await self.save(session)
        await asyncio.to_thread(_unlink_quietly, session.staging_path)

    async def create(
        self,
        *,
        device_id: str,
        target: Path,
        workspace_root: Path,
        total_size: int,
        chunk_size: int,
        expected_sha256: str | None,
        overwrite: bool,
        idempotency_key: str | None,
        source: str,
    ) -> UploadSession:
        if total_size > self.config.max_file_bytes:
            raise TransferError(
                "file_too_large",
                "File exceeds the configured maximum",
                size=total_size,
                limit=self.config.max_file_bytes,
            )
        if self.active_count() >= self.config.max_sessions:
            raise TransferError("too_many_sessions", "Too many concurrent transfer sessions")
        if self.quota_used() + total_size > self.config.max_staging_bytes:
            raise TransferError(
                "quota_exceeded",
                "Staging quota would be exceeded",
                limit=self.config.max_staging_bytes,
            )
        session = UploadSession(
            id=uuid4().hex,
            device_id=device_id,
            target=str(target),
            workspace_root=str(workspace_root),
            total_size=total_size,
            chunk_size=chunk_size,
            expected_sha256=expected_sha256,
            overwrite=overwrite,
            idempotency_key=idempotency_key,
            source=source,
        )
        await self.save(session)
        return session

    # -- idempotency -------------------------------------------------------
    async def _load_idempotency(self) -> dict[str, dict[str, Any]]:
        if self._idempotency is None:
            path = self.root / "idempotency.json"
            data: dict[str, dict[str, Any]] = {}
            if path.exists():
                try:
                    loaded = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        data = loaded
                except (OSError, ValueError):
                    data = {}
            self._idempotency = data
        return self._idempotency

    async def lookup_idempotent(self, key: str, fingerprint: str) -> dict[str, Any] | None:
        async with self._lock:
            index = await self._load_idempotency()
            entry = index.get(key)
            if entry is None:
                return None
            if entry.get("fingerprint") != fingerprint:
                raise TransferError(
                    "idempotency_conflict",
                    "idempotency_key was already used with different parameters",
                    idempotency_key=key,
                )
            return entry.get("result")

    async def record_idempotent(self, key: str, fingerprint: str, result: dict[str, Any]) -> None:
        async with self._lock:
            index = await self._load_idempotency()
            index[key] = {"fingerprint": fingerprint, "result": result, "at": time.time()}
            path = self.root / "idempotency.json"
            payload = json.dumps(index, ensure_ascii=False)
            await asyncio.to_thread(_atomic_write, path, payload)

    # -- maintenance -------------------------------------------------------
    async def sweep(self) -> dict[str, int]:
        """Delete expired sessions, orphan staging files and stale archives."""

        removed = 0
        for session in list(self._sessions.values()):
            if session.state == "open" and self._expired(session):
                await self.discard(session, state="expired")
                removed += 1
        # Staging files whose session record vanished (crash between save steps).
        known = {s.id for s in self._sessions.values() if s.state == "open"}
        for session in list(self._sessions.values()):
            staging = session.staging_path
            if session.state != "open" and staging.exists():
                await asyncio.to_thread(_unlink_quietly, staging)
        if self.root.exists():
            deadline = time.time() - self.config.session_ttl
            for archive in self.archives_dir.glob("*.zip"):
                try:
                    if archive.stat().st_mtime < deadline:
                        await asyncio.to_thread(_unlink_quietly, archive)
                except OSError:
                    pass
        self._drop_stale = known  # kept for introspection in tests
        return {"expired_sessions": removed}

    async def clear_staging_for(self, target_dir: Path, older_than: float) -> int:
        """Remove abandoned ``.c2l-part-*`` files in a directory."""

        removed = 0
        if not target_dir.is_dir():
            return 0
        for entry in target_dir.glob(f"{STAGING_PREFIX}*"):
            try:
                if entry.is_file() and entry.stat().st_mtime < older_than:
                    await asyncio.to_thread(_unlink_quietly, entry)
                    removed += 1
            except OSError:
                pass
        return removed


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    os.replace(temporary, path)


def remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
