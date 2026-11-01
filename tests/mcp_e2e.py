"""End-to-end MCP test: real server process, real MCP client, real bytes.

Run: .venv/Scripts/python.exe tests/mcp_e2e.py
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from _util import reset_base, test_base, write_config, CLI

ROOT = Path(__file__).resolve().parents[1]
BASE = test_base("mcp")
WS = BASE / "ws"
STATE = BASE / "state"
CONFIG = BASE / "files.yaml"
PORT = 8799
URL = f"http://127.0.0.1:{PORT}/mcp/"


def png_bytes(size=(80, 60), color=(200, 30, 90)) -> bytes:
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def passed(label: str, condition: bool, detail: str = "") -> None:
    print(("PASS " if condition else "FAIL ") + label + (f"  {detail}" if detail else ""))
    if not condition:
        raise SystemExit(1)


def error_code(result) -> str | None:
    if not result.is_error:
        return None
    text = "".join(block.text for block in result.content if getattr(block, "text", None))
    start = text.find("{")
    try:
        return json.loads(text[start:])["error"]["code"]
    except Exception:
        return f"unparsed:{text[:120]}"


async def wait_health(deadline: float = 40.0) -> None:
    end = time.time() + deadline
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=1) as response:
                if json.loads(response.read())["status"] == "ok":
                    return
        except Exception:
            await asyncio.sleep(0.3)
    raise SystemExit("server did not become healthy")


async def main() -> None:
    reset_base(BASE)
    WS.mkdir(parents=True, exist_ok=True)
    STATE.mkdir(parents=True)
    write_config(BASE)

    env = dict(
        os.environ,
        CHAT2LOCAL_FILES_CONFIG=str(CONFIG),
        # The production instance holds ~/.chat2local/runtime.lock, so the test
        # server gets its own home directory and therefore its own lock.
        USERPROFILE=str(BASE / "home"), HOME=str(BASE / "home"),
    )
    (BASE / "home").mkdir(parents=True, exist_ok=True)
    log_path = BASE / "server.log"
    log_handle = log_path.open("w", encoding="utf-8")
    server = subprocess.Popen(
        [*CLI,
         "--workspace", str(WS), "--host", "127.0.0.1", "--port", str(PORT)],
        env=env, stdout=log_handle, stderr=subprocess.STDOUT, text=True,
    )
    try:
        await wait_health()
        from mcp import Client

        async with Client(URL, read_timeout_seconds=60) as client:
            tools = await client.list_tools()
            names = sorted(tool.name for tool in tools.tools)
            print("tools:", " ".join(names))
            for required in ("import_chat_file", "upload_file", "download_file", "preview_file",
                             "transfer_status", "transfer_directory"):
                passed(f"tool registered: {required}", required in names)
            passed("original tools intact", {"read", "search", "apply_patch", "exec_command",
                                             "list_devices", "handoff_save"} <= set(names))
            descriptor = next(tool for tool in tools.tools if tool.name == "import_chat_file").model_dump(by_alias=True)
            passed("official attachment adapter metadata", descriptor["_meta"]["openai/fileParams"] == ["file"])
            schema = descriptor["inputSchema"]
            reference = schema["properties"]["file"]["$ref"].rsplit("/", 1)[-1]
            file_schema = schema["$defs"][reference]
            passed("file schema requires exactly URL and ID", set(file_schema["required"]) == {"download_url", "file_id"})
            passed("all four official file properties are strings", all(file_schema["properties"][key]["type"] == "string" for key in ("download_url", "file_id", "mime_type", "file_name")))
            incomplete = await client.call_tool("import_chat_file", {"path": "missing.docx", "file": {"file_id": "file-test"}})
            passed("incomplete attachment rejected without writing", incomplete.is_error and not (WS / "missing.docx").exists())

            # --- 1. inline PNG upload, as a chat client would send it ------
            png = png_bytes()
            digest = hashlib.sha256(png).hexdigest()
            result = await client.call_tool("upload_file", {
                "path": "shots/from-chat.png",
                "content_base64": base64.b64encode(png).decode(),
                "sha256": digest,
            })
            passed("upload_file succeeds", not result.is_error, error_code(result) or "")
            meta = json.loads(result.content[0].text)
            passed("uploaded sha matches", meta["sha256"] == digest, meta["path"])
            passed("file on disk", (WS / "shots/from-chat.png").read_bytes() == png)

            # --- 2. preview returns an image content block -----------------
            preview = await client.call_tool("preview_file", {"path": "shots/from-chat.png"})
            kinds = [block.type for block in preview.content]
            passed("preview has image block", "image" in kinds, str(kinds))
            image = next(block for block in preview.content if block.type == "image")
            passed("preview image decodes",
                   base64.b64decode(image.data) == png, image.mime_type)

            # --- 3. download returns resource + working one-time link -----
            download = await client.call_tool("download_file", {"path": "shots/from-chat.png"})
            inline = [block for block in download.content if block.type == "resource"]
            passed("download has embedded resource", len(inline) == 1)
            payload = base64.b64decode(inline[0].resource.blob)
            passed("downloaded bytes identical", payload == png)
            link = json.loads(download.content[0].text)["download_url"]
            with urllib.request.urlopen(link, timeout=10) as response:
                fetched = response.read()
            passed("download_url serves the file", fetched == png)
            try:
                urllib.request.urlopen(link, timeout=5)
                passed("download_url is single-use", False)
            except Exception:
                passed("download_url is single-use", True)

            # --- 4. chunked upload with an interruption and resume --------
            blob = os.urandom(200_000)
            blob_sha = hashlib.sha256(blob).hexdigest()
            begin = await client.call_tool("upload_file", {
                "path": "big/payload.bin", "action": "begin",
                "size": len(blob), "sha256": blob_sha,
            })
            session = json.loads(begin.content[0].text)
            offset = 0
            chunk = 20_000
            while offset < 3 * chunk:  # client "disconnects" after 3 chunks
                piece = blob[offset:offset + chunk]
                await client.call_tool("upload_file", {
                    "path": "big/payload.bin", "action": "chunk", "session_id": session["session_id"],
                    "offset": offset, "data_base64": base64.b64encode(piece).decode(),
                })
                offset += len(piece)
            status = await client.call_tool("transfer_status", {"session_id": session["session_id"]})
            info = json.loads(status.content[0].text)
            passed("status reports resume point", info["next_offset"] == offset,
                   f"next_offset={info['next_offset']}")
            while offset < len(blob):
                piece = blob[offset:offset + chunk]
                await client.call_tool("upload_file", {
                    "path": "big/payload.bin", "action": "chunk", "session_id": session["session_id"],
                    "offset": offset, "data_base64": base64.b64encode(piece).decode(),
                })
                offset += len(piece)
            commit = await client.call_tool("upload_file", {
                "path": "big/payload.bin", "action": "commit", "session_id": session["session_id"],
            })
            final = json.loads(commit.content[0].text)
            passed("resumed upload commits", final["sha256"] == blob_sha)
            passed("resumed bytes identical", (WS / "big/payload.bin").read_bytes() == blob)
            passed("interrupted file not visible before commit",
                   not (WS / "big/.c2l-part-uncommitted").exists())

            # --- 5. DOCX / PDF / ZIP integrity ----------------------------
            for name, data in (
                ("docs/report.docx", b"PK\x03\x04" + os.urandom(4096)),
                ("docs/paper.pdf", b"%PDF-1.7\n" + os.urandom(4096)),
                ("docs/bundle.zip", b"PK\x03\x04" + os.urandom(4096)),
            ):
                sha = hashlib.sha256(data).hexdigest()
                up = await client.call_tool("upload_file", {
                    "path": name, "content_base64": base64.b64encode(data).decode(), "sha256": sha,
                })
                passed(f"{name} upload ok", not up.is_error, error_code(up) or "")
                down = await client.call_tool("download_file", {"path": name, "mode": "url"})
                link = json.loads(down.content[0].text)["download_url"]
                with urllib.request.urlopen(link, timeout=10) as response:
                    fetched = response.read()
                passed(f"{name} sha round-trip", hashlib.sha256(fetched).hexdigest() == sha)

            # --- 6. path policy ------------------------------------------
            outside = await client.call_tool("upload_file", {
                "path": "../../escape.txt", "content_text": "nope"})
            passed("traversal refused", error_code(outside) == "outside_workspace", str(error_code(outside)))
            absolute = await client.call_tool("download_file", {"path": "C:/Windows/win.ini"})
            passed("absolute outside refused", error_code(absolute) == "outside_workspace",
                   str(error_code(absolute)))
            again = await client.call_tool("upload_file", {
                "path": "shots/from-chat.png", "content_text": "overwrite me"})
            passed("no silent overwrite", error_code(again) == "exists", str(error_code(again)))

            # --- 7. directory manifest ------------------------------------
            manifest = await client.call_tool("transfer_directory", {"path": "docs", "action": "manifest"})
            listing = json.loads(manifest.content[0].text)
            passed("manifest lists three files", listing["entry_count"] == 3, str(listing["entry_count"]))
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        log_handle.close()
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-2500:]
        if "Traceback" in tail or "ERROR" in tail:
            print("--- server log tail ---")
            print(tail)

    print("\nALL MCP E2E CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
