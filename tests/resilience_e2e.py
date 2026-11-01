"""Resilience: source_url fetch, SSRF refusal, session survival across a crash, TTL expiry.

Run: .venv/Scripts/python.exe tests/resilience_e2e.py
"""

from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import threading
import urllib.request
from pathlib import Path

from _util import reset_base, test_base, CLI

ROOT = Path(__file__).resolve().parents[1]
BASE = test_base("resilience")
WS = BASE / "ws"
STATE = BASE / "state"
HOME = BASE / "home"
PORT = 8801
SOURCE_PORT = 8899


def passed(label: str, condition: bool, detail: str = "") -> None:
    print(("PASS " if condition else "FAIL ") + label + (f"  {detail}" if detail else ""))
    if not condition:
        raise SystemExit(1)


def error_code(result) -> str | None:
    if not result.is_error:
        return None
    text = "".join(block.text for block in result.content if getattr(block, "text", None))
    try:
        return json.loads(text[text.find("{"):])["error"]["code"]
    except Exception:
        return f"unparsed:{text[:160]}"


async def wait_health(port: int) -> None:
    for _ in range(100):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1):
                return
        except Exception:
            await asyncio.sleep(0.3)
    raise SystemExit(f"server not healthy on {port}")


def start_server(port: int, config: Path) -> subprocess.Popen:
    env = dict(os.environ, CHAT2LOCAL_FILES_CONFIG=str(config), USERPROFILE=str(HOME), HOME=str(HOME))
    log = (BASE / f"server-{port}.log").open("w", encoding="utf-8")
    process = subprocess.Popen(
        [*CLI, "--workspace", str(WS), "--host", "127.0.0.1", "--port", str(port)],
        env=env, stdout=log, stderr=subprocess.STDOUT, text=True,
    )
    process._log = log  # type: ignore[attr-defined]
    return process


def stop_server(process: subprocess.Popen, *, hard: bool = False) -> None:
    if hard:
        process.kill()
    else:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
    process._log.close()  # type: ignore[attr-defined]


async def main() -> None:
    reset_base(BASE)
    (WS / "srv").mkdir(parents=True, exist_ok=True)
    STATE.mkdir(parents=True, exist_ok=True)
    HOME.mkdir(parents=True, exist_ok=True)

    served = os.urandom(1_500_000)
    served_sha = hashlib.sha256(served).hexdigest()
    (WS / "srv" / "remote.bin").write_bytes(served)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(WS / "srv"))
    source = http.server.ThreadingHTTPServer(("127.0.0.1", SOURCE_PORT), handler)
    threading.Thread(target=source.serve_forever, daemon=True).start()

    config = BASE / "files.yaml"
    config.write_text(
        f"state_dir: {BASE.as_posix()}/state\n"
        "allow_private_source_hosts: true\n"
        "allow_insecure_source_url: true\n"
        "max_inline_bytes: 131072\n"
        "chunk_size: 16384\n"
        "max_chunk_bytes: 65536\n",
        encoding="utf-8",
    )

    server = None
    try:
        server = start_server(PORT, config)
        await wait_health(PORT)
        from mcp import Client

        async with Client(f"http://127.0.0.1:{PORT}/mcp/", read_timeout_seconds=120) as client:
            # A. direct URL and the official attachment adapter share delivery.
            url = f"http://127.0.0.1:{SOURCE_PORT}/remote.bin"
            result = await client.call_tool("upload_file", {
                "path": "from-url/remote.bin", "source_url": url, "sha256": served_sha})
            passed("source_url upload succeeds", not result.is_error, error_code(result) or "")
            meta = json.loads(result.content[0].text)
            passed("source_url size and hash verified",
                   meta["sha256"] == served_sha and meta["size"] == len(served))
            passed("source_url bytes on disk",
                   hashlib.sha256((WS / "from-url/remote.bin").read_bytes()).hexdigest() == served_sha)
            attachment = {"download_url": url + "?signature=test-private-value", "file_id": "file-test-attachment"}
            imported = await client.call_tool("import_chat_file", {
                "file": attachment, "path": "templates/attachment.bin", "sha256": served_sha})
            passed("attachment adapter downloads original bytes", not imported.is_error and (WS / "templates/attachment.bin").read_bytes() == served)
            meta = json.loads(imported.content[0].text)
            passed("attachment result reports provenance and hash", meta["imported_file_id"] == attachment["file_id"] and meta["sha256"] == served_sha)
            passed("attachment signed URL is not echoed", "test-private-value" not in str(imported))
            duplicate = await client.call_tool("import_chat_file", {"file": attachment, "path": "templates/attachment.bin"})
            passed("attachment import refuses silent overwrite", error_code(duplicate) == "exists")
            escaped = await client.call_tool("import_chat_file", {"file": attachment, "path": "../escape.bin"})
            passed("attachment import enforces workspace", error_code(escaped) == "outside_workspace")
            missing = await client.call_tool("import_chat_file", {"file": {"download_url": url + "/missing?signature=private", "file_id": "file-expired"}, "path": "templates/missing.bin"})
            passed("unavailable attachment returns error without destination", missing.is_error and not (WS / "templates/missing.bin").exists())
    except SystemExit:
        raise
    finally:
        if server is not None:
            stop_server(server)

    # B. the same URL must be refused when private hosts are not allowed
    strict_config = BASE / "strict.yaml"
    strict_config.write_text(
        f"state_dir: {BASE.as_posix()}/state\n"
        "allow_insecure_source_url: true\n"
        "max_inline_bytes: 131072\n",
        encoding="utf-8",
    )
    strict_port = 8802
    server = start_server(strict_port, strict_config)
    try:
        await wait_health(strict_port)
        from mcp import Client
        async with Client(f"http://127.0.0.1:{strict_port}/mcp/", read_timeout_seconds=60) as client:
            blocked = await client.call_tool("upload_file", {
                "path": "from-url/blocked.bin",
                "source_url": f"http://127.0.0.1:{SOURCE_PORT}/remote.bin"})
            passed("SSRF guard refuses loopback source",
                   error_code(blocked) == "source_url_denied", str(error_code(blocked)))
            blocked_attachment = await client.call_tool("import_chat_file", {
                "file": {"download_url": f"http://127.0.0.1:{SOURCE_PORT}/remote.bin", "file_id": "file-ssrf"},
                "path": "templates/blocked.bin"})
            passed("attachment adapter preserves SSRF protection", error_code(blocked_attachment) == "source_url_denied")
            https = await client.call_tool("upload_file", {
                "path": "from-url/plain.bin", "source_url": "ftp://example.com/x"})
            passed("non-http scheme refused", error_code(https) == "source_url_denied",
                   str(error_code(https)))
    finally:
        stop_server(server)

    # C. session state must survive a hard process kill
    server = start_server(PORT, config)
    try:
        await wait_health(PORT)
        from mcp import Client
        async with Client(f"http://127.0.0.1:{PORT}/mcp/", read_timeout_seconds=120) as client:
            payload = os.urandom(120_000)
            sha = hashlib.sha256(payload).hexdigest()
            begin = await client.call_tool("upload_file", {
                "path": "resume/after-crash.bin", "action": "begin",
                "size": len(payload), "sha256": sha})
            session = json.loads(begin.content[0].text)["session_id"]
            offset = 0
            for _ in range(2):
                piece = payload[offset:offset + 16_000]
                await client.call_tool("upload_file", {
                    "action": "chunk", "path": "resume/after-crash.bin", "session_id": session,
                    "offset": offset, "data_base64": base64.b64encode(piece).decode()})
                offset += len(piece)
    finally:
        stop_server(server, hard=True)

    server = start_server(PORT, config)
    try:
        await wait_health(PORT)
        from mcp import Client
        async with Client(f"http://127.0.0.1:{PORT}/mcp/", read_timeout_seconds=120) as client:
            status = await client.call_tool("transfer_status", {"session_id": session})
            info = json.loads(status.content[0].text)
            passed("session survives a hard kill", info["next_offset"] == offset,
                   f"next_offset={info['next_offset']} expected={offset}")
            while offset < len(payload):
                piece = payload[offset:offset + 16_000]
                await client.call_tool("upload_file", {
                    "action": "chunk", "path": "resume/after-crash.bin", "session_id": session,
                    "offset": offset, "data_base64": base64.b64encode(piece).decode()})
                offset += len(piece)
            commit = await client.call_tool("upload_file", {
                "action": "commit", "path": "resume/after-crash.bin", "session_id": session})
            passed("resumed upload after restart commits",
                   json.loads(commit.content[0].text)["sha256"] == sha)
            passed("resumed bytes intact",
                   (WS / "resume/after-crash.bin").read_bytes() == payload)
    finally:
        stop_server(server)

    # D. stale sessions expire and their staging files are cleaned up
    expiry_config = BASE / "expiry.yaml"
    expiry_config.write_text(
        f"state_dir: {BASE.as_posix()}/state\nsession_ttl: 1\nmax_inline_bytes: 131072\n",
        encoding="utf-8",
    )
    expiry_port = 8803
    server = start_server(expiry_port, expiry_config)
    try:
        await wait_health(expiry_port)
        from mcp import Client
        async with Client(f"http://127.0.0.1:{expiry_port}/mcp/", read_timeout_seconds=60) as client:
            begin = await client.call_tool("upload_file", {
                "path": "expire/stale.bin", "action": "begin", "size": 1000})
            stale = json.loads(begin.content[0].text)["session_id"]
            staging = WS / "expire" / f".c2l-part-{stale}"
            passed("staging file created", staging.exists(), str(staging))
            await asyncio.sleep(2.5)
            expired = await client.call_tool("transfer_status", {"session_id": stale})
            passed("expired session reported", error_code(expired) == "session_expired",
                   str(error_code(expired)))
            passed("expired staging file removed", not staging.exists())
    finally:
        stop_server(server)
        source.shutdown()

    print("\nRESILIENCE PASSED")


if __name__ == "__main__":
    asyncio.run(main())
