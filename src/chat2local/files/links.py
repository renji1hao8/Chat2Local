"""Short-lived, unguessable download links.

A link is an opaque 256-bit token held in memory, bound to one file's identity
(path, size, mtime).  Nothing about the filesystem path appears in the URL, and
links expire (and are single-use by default), so a URL copied out of a chat
history stops working.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class DownloadLink:
    token: str
    path: Path
    size: int
    mtime_ns: int
    expires_at: float
    single_use: bool
    filename: str
    consumed: bool = False
    in_use: bool = False

    def expired(self, now: float | None = None) -> bool:
        return (now or time.time()) >= self.expires_at


class LinkStore:
    def __init__(self) -> None:
        self._links: dict[str, DownloadLink] = {}
        self._lock = threading.Lock()

    def create(
        self,
        path: Path,
        *,
        ttl: float,
        single_use: bool = True,
        filename: str | None = None,
    ) -> DownloadLink:
        stat = path.stat()
        token = secrets.token_urlsafe(32)
        link = DownloadLink(
            token=token,
            path=path,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            expires_at=time.time() + ttl,
            single_use=single_use,
            filename=filename or path.name,
        )
        with self._lock:
            self._prune_locked()
            self._links[token] = link
        return link

    def peek(self, token: str) -> DownloadLink | None:
        """Validate a token without consuming it."""

        with self._lock:
            link = self._links.get(token)
            if link is None:
                return None
            if link.expired() or (link.single_use and link.consumed):
                self._links.pop(token, None)
                return None
            return link

    def consume(self, token: str) -> DownloadLink | None:
        with self._lock:
            link = self._links.get(token)
            if link is None:
                return None
            if link.expired() or (link.single_use and link.consumed):
                self._links.pop(token, None)
                return None
            if link.single_use:
                link.consumed = True
                self._links.pop(token, None)
            return link

    def revoke(self, token: str) -> None:
        with self._lock:
            self._links.pop(token, None)

    def acquire(self, token: str) -> DownloadLink | None:
        """Reserve a single-use link until its response finishes or fails."""
        with self._lock:
            link = self._links.get(token)
            if link is None or link.expired() or link.consumed or link.in_use:
                return None
            if link.single_use:
                link.in_use = True
            return link

    def finish(self, token: str, *, completed: bool) -> None:
        with self._lock:
            link = self._links.get(token)
            if link is None:
                return
            link.in_use = False
            if completed and link.single_use:
                link.consumed = True
                self._links.pop(token, None)

    def _prune_locked(self) -> None:
        now = time.time()
        for token in [t for t, link in self._links.items() if link.expired(now)]:
            self._links.pop(token, None)


LINKS = LinkStore()
