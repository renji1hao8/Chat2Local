"""HTTP surface for short-lived download links (``GET /files/<token>``).

The link is opaque and expires; the filesystem path never appears in the URL.
The token is a bearer credential, independent of MCP OAuth. Anyone holding it
can download the bound file until expiry. The configured base URL must route
to this device's /files endpoint, not merely to the Hub's MCP endpoint.
"""

from __future__ import annotations

import mimetypes
from email.utils import formatdate
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from chat2local.files.links import LINKS
from chat2local.files.service import get_file_service
from chat2local.files.store import STAGING_PREFIX

router = APIRouter()


class DownloadResponse(FileResponse):
    """Consume single-use links only after the full ASGI response is sent.

    This confirms server-side delivery, not receipt or saving by the client.
    Failed sends release the reservation while the token is still valid.
    """

    def __init__(self, *args, token: str, **kwargs):
        super().__init__(*args, **kwargs)
        self.token = token

    async def __call__(self, scope, receive, send):
        completed = False

        async def tracked_send(message):
            nonlocal completed
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                completed = True

        try:
            # Avoid pathsend shortcuts so completion is observed after body bytes.
            scope = dict(scope, extensions={})
            await super().__call__(scope, receive, tracked_send)
        finally:
            LINKS.finish(self.token, completed=completed)


def _not_found(reason: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error": {"code": "link_invalid", "message": reason}},
                        status_code=404)


@router.get("/files/{token}")
async def download_link(token: str, request: Request) -> Response:
    link = LINKS.peek(token)
    if link is None:
        return _not_found("Download link is invalid, expired or already used")
    path = link.path
    try:
        stat = path.stat()
    except OSError:
        return _not_found("File is no longer available")
    if stat.st_size != link.size or stat.st_mtime_ns != link.mtime_ns:
        return _not_found("File changed since the link was issued")
    if path.name.startswith(STAGING_PREFIX):
        return _not_found("File is not ready")
    # Single-use links deliberately support full GET only. A rejected Range
    # request does not burn the token; use download_file offsets for resuming.
    if link.single_use and "range" in request.headers:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{link.size}", "Cache-Control": "no-store"})
    if request.headers.get("purpose") == "prefetch" or "prefetch" in request.headers.get("sec-purpose", ""):
        return Response(status_code=204, headers={"Cache-Control": "no-store"})
    if LINKS.acquire(token) is None:
        return _not_found("Download link is expired, already used or currently downloading")

    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(path.name)}",
        "Cache-Control": "no-store",
        "X-Robots-Tag": "noindex",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }
    return DownloadResponse(path, media_type=media_type, headers=headers, token=token)


@router.head("/files/{token}")
async def download_link_head(token: str) -> Response:
    link = LINKS.peek(token)
    if link is None:
        return Response(status_code=404)
    return Response(
        status_code=200,
        headers={
            "Content-Length": str(link.size),
            "Content-Type": mimetypes.guess_type(link.filename)[0] or "application/octet-stream",
            "Last-Modified": formatdate(link.mtime_ns / 1e9, usegmt=True),
            "Cache-Control": "no-store",
        },
    )


@router.get("/files")
async def files_root() -> Response:
    service = get_file_service()
    return JSONResponse({"ok": True, "enabled": service.config.enabled})


__all__ = ["router"]
