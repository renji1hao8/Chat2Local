"""Fetch a remote object into a local staging file.

This is how a real attachment reaches a device when the chat client can only
hand the tool a URL instead of bytes.  It is deliberately conservative:

* ``https`` only unless ``allow_insecure_source_url`` is set;
* credentials in the URL are refused;
* every hop of the redirect chain is re-validated;
* the host must resolve to public addresses unless private hosts are allowed
  (blocks ``localhost``, link-local metadata endpoints and RFC1918 targets);
* the byte count is capped *while streaming*, so a lying Content-Length cannot
  fill the disk.

DNS is validated before connecting, not pinned afterwards; a hostile resolver
that changes its answer between the two lookups is out of scope here (the same
is true of every URL-fetching tool without a custom socket factory).
"""

from __future__ import annotations

import hashlib
import ipaddress
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from chat2local.files.errors import TransferError

READ_BLOCK = 1024 * 1024
MAX_REDIRECTS = 3
USER_AGENT = "Chat2Local-FileTransfer/1"


def _check_host(url: str, *, allow_private: bool) -> None:
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname
    if not host:
        raise TransferError("source_url_denied", "URL has no host", url=url)
    if parts.username or parts.password:
        raise TransferError("source_url_denied", "URL must not contain credentials")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise TransferError("source_unreachable", "Could not resolve the source host", host=host) from None
    addresses = {info[4][0] for info in infos}
    if allow_private:
        return
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address.split("%")[0])
        except ValueError:
            raise TransferError("source_url_denied", "Unparseable source address", host=host) from None
        if not ip.is_global or ip.is_multicast:
            raise TransferError(
                "source_url_denied",
                "Source resolves to a non-public address",
                host=host,
                address=str(ip),
            )


def check_url(url: str, *, allow_insecure: bool, allow_private: bool) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("https", "http"):
        raise TransferError("source_url_denied", "Only http(s) URLs are supported", scheme=parts.scheme)
    if parts.scheme == "http" and not allow_insecure:
        raise TransferError("source_url_denied", "Plain http is disabled for source URLs")
    if len(url) > 4096:
        raise TransferError("source_url_denied", "URL is too long")
    _check_host(url, allow_private=allow_private)


class _ValidatingRedirects(urllib.request.HTTPRedirectHandler):
    max_redirections = MAX_REDIRECTS

    def __init__(self, *, allow_insecure: bool, allow_private: bool) -> None:
        self._allow_insecure = allow_insecure
        self._allow_private = allow_private

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        check_url(newurl, allow_insecure=self._allow_insecure, allow_private=self._allow_private)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_to_file(
    url: str,
    destination: Path,
    *,
    max_bytes: int,
    allow_insecure: bool = False,
    allow_private: bool = False,
    timeout: float = 60.0,
    progress: Callable[[int], None] | None = None,
) -> dict[str, object]:
    """Stream ``url`` into ``destination`` and return size/sha256 metadata."""

    check_url(url, allow_insecure=allow_insecure, allow_private=allow_private)
    opener = urllib.request.build_opener(
        _ValidatingRedirects(allow_insecure=allow_insecure, allow_private=allow_private)
    )
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    digest = hashlib.sha256()
    total = 0

    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        raise TransferError(
            "source_fetch_failed", "Source returned an HTTP error", status=error.code
        ) from None
    except (urllib.error.URLError, ssl.SSLError, TimeoutError, OSError) as error:
        reason = getattr(error, "reason", None) or str(error)
        raise TransferError("source_fetch_failed", "Could not fetch the source", reason=str(reason)) from None

    with response:
        declared = response.headers.get("Content-Length")
        if declared is not None:
            try:
                if int(declared) > max_bytes:
                    raise TransferError(
                        "file_too_large",
                        "Source is larger than the configured limit",
                        size=int(declared),
                        limit=max_bytes,
                    )
            except ValueError:
                pass
        try:
            with destination.open("wb") as handle:
                while True:
                    block = response.read(READ_BLOCK)
                    if not block:
                        break
                    total += len(block)
                    if total > max_bytes:
                        raise TransferError(
                            "file_too_large",
                            "Source exceeded the configured limit while streaming",
                            limit=max_bytes,
                        )
                    digest.update(block)
                    handle.write(block)
                    if progress is not None:
                        progress(total)
                handle.flush()
        except TransferError:
            destination.unlink(missing_ok=True)
            raise
        except OSError as error:
            destination.unlink(missing_ok=True)
            raise TransferError(
                "staging_write_failed", "Could not write the staging file", reason=str(error)
            ) from None

    return {
        "size": total,
        "sha256": digest.hexdigest(),
        "content_type": response.headers.get("Content-Type"),
        "final_url": response.geturl(),
    }
