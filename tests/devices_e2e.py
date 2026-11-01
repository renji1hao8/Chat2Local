"""Device-routing test: one Hub, one remote Agent, no cross-device writes.

Run: .venv/Scripts/python.exe tests/devices_e2e.py
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
import urllib.request
import functools
import http.server
import threading
from pathlib import Path

from _util import reset_base, test_base, CLI

ROOT = Path(__file__).resolve().parents[1]
BASE = test_base("devices")
HUB_WS = BASE / "hub-ws"
AGENT_WS = BASE / "agent-ws"
PORT = 8800
TOKEN = "test-hub-token"


def passed(label: str, condition: bool, detail: str = "") -> None:
    print(("PASS " if condition else "FAIL ") + label + (f"  {detail}" if detail else ""))
    if not condition:
        raise SystemExit(1)


def png_bytes(size=(40, 30), seed=0) -> bytes:
    from PIL import Image
    image = Image.new("RGB", size)
    pixels = image.load()
    for y in range(size[1]):
        for x in range(size[0]):
            pixels[x, y] = ((x * 5 + seed) % 256, (y * 7) % 256, (seed * 13) % 256)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def error_code(result) -> str | None:
    if not result.is_error:
        return None
    text = "".join(block.text for block in result.content if getattr(block, "text", None))
    try:
        return json.loads(text[text.find("{"):])["error"]["code"]
    except Exception:
        return f"unparsed:{text[:120]}"


async def wait_health(url: str) -> None:
    for _ in range(100):
        try:
            with urllib.request.urlopen(url, timeout=1):
                return
        except Exception:
            await asyncio.sleep(0.3)
    raise SystemExit(f"server not healthy: {url}")


async def wait_device(client, device_id: str) -> None:
    for _ in range(60):
        listing = await client.call_tool("list_devices", {})
        data = json.loads(listing.content[0].text)
        for device in data["devices"]:
            if device["device_id"] == device_id:
                return
        await asyncio.sleep(0.5)
    raise SystemExit(f"agent never registered: {device_id}")


async def main() -> None:
    reset_base(BASE)
    for path in (HUB_WS, AGENT_WS, BASE / "home-hub", BASE / "home-agent",
                 BASE / "state-hub", BASE / "state-agent"):
        path.mkdir(parents=True, exist_ok=True)

    hub_config = BASE / "hub-files.yaml"
    agent_config = BASE / "agent-files.yaml"
    hub_config.write_text(f"state_dir: {BASE.as_posix()}/state-hub\nmax_inline_bytes: 131072\n", encoding="utf-8")
    agent_config.write_text(f"state_dir: {BASE.as_posix()}/state-agent\nmax_inline_bytes: 131072\n", encoding="utf-8")
    with agent_config.open("a", encoding="utf-8") as handle:
        handle.write("allow_private_source_hosts: true\nallow_insecure_source_url: true\n")
    source_dir = BASE / "attachment-source"
    source_dir.mkdir()
    attachment_bytes = b"ChatGPT attachment bytes for target Agent"
    (source_dir / "template.bin").write_bytes(attachment_bytes)
    source_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(source_dir)))
    threading.Thread(target=source_server.serve_forever, daemon=True).start()

    hub_env = dict(os.environ, CHAT2LOCAL_FILES_CONFIG=str(hub_config), USERPROFILE=str(BASE / "home-hub"), HOME=str(BASE / "home-hub"))
    agent_env = dict(os.environ, CHAT2LOCAL_FILES_CONFIG=str(agent_config), USERPROFILE=str(BASE / "home-agent"), HOME=str(BASE / "home-agent"))

    hub_log = (BASE / "hub.log").open("w", encoding="utf-8")
    agent_log = (BASE / "agent.log").open("w", encoding="utf-8")
    hub = subprocess.Popen(
        [*CLI, "hub", "--workspace", str(HUB_WS), "--host", "127.0.0.1", "--port", str(PORT),
         "--token", TOKEN, "--device-id", "hub-dev"],
        env=hub_env, stdout=hub_log, stderr=subprocess.STDOUT, text=True,
    )
    agent = None
    try:
        await wait_health(f"http://127.0.0.1:{PORT}/health")
        agent = subprocess.Popen(
            [*CLI, "agent", "--workspace", str(AGENT_WS),
             "--hub-url", f"ws://127.0.0.1:{PORT}/device/ws", "--token", TOKEN,
             "--device-id", "agent-dev"],
            env=agent_env, stdout=agent_log, stderr=subprocess.STDOUT, text=True,
        )
        from mcp import Client

        async with Client(f"http://127.0.0.1:{PORT}/mcp/", read_timeout_seconds=90) as client:
            await wait_device(client, "agent-dev")
            listing = json.loads((await client.call_tool("list_devices", {})).content[0].text)
            ids = sorted(device["device_id"] for device in listing["devices"])
            passed("both devices visible", ids == ["agent-dev", "hub-dev"], str(ids))
            agent_tools = next(d for d in listing["devices"] if d["device_id"] == "agent-dev")["tools"]
            passed("agent advertises file tools",
                   {"upload_file", "download_file", "preview_file", "transfer_directory"} <= set(agent_tools),
                   str(sorted(agent_tools)))

            # 1. write only to the agent
            agent_png = png_bytes(seed=3)
            result = await client.call_tool("upload_file", {
                "path": "from-chat/agent.png",
                "content_base64": base64.b64encode(agent_png).decode(),
                "device": "agent-dev",
            })
            passed("upload to agent succeeds", not result.is_error, error_code(result) or "")
            passed("file lands in the agent workspace",
                   (AGENT_WS / "from-chat/agent.png").is_file())
            passed("hub workspace untouched", not (HUB_WS / "from-chat/agent.png").exists())
            meta = json.loads(result.content[0].text)
            passed("result reports the agent path",
                   Path(meta["absolute_path"]).resolve() == (AGENT_WS / "from-chat/agent.png").resolve(),
                   meta["absolute_path"])

            # 2. write only to the hub
            hub_png = png_bytes(seed=9)
            hub_result = await client.call_tool("upload_file", {
                "path": "from-chat/hub.png",
                "content_base64": base64.b64encode(hub_png).decode(),
                "device": "hub-dev",
            })
            passed("upload to hub succeeds", not hub_result.is_error)
            passed("file lands in the hub workspace", (HUB_WS / "from-chat/hub.png").is_file())
            passed("agent workspace untouched", not (AGENT_WS / "from-chat/hub.png").exists())

            # 3. read back from the agent through the hub
            download = await client.call_tool("download_file", {
                "path": "from-chat/agent.png", "device": "agent-dev"})
            blob = next(b for b in download.content if b.type == "resource").resource.blob
            passed("agent file downloads through the hub",
                   base64.b64decode(blob) == agent_png)

            # 4. resumable chunked upload routed to the agent
            payload = os.urandom(90_000)
            sha = hashlib.sha256(payload).hexdigest()
            begin = await client.call_tool("upload_file", {
                "path": "big/agent.bin", "action": "begin", "size": len(payload), "sha256": sha,
                "device": "agent-dev"})
            session = json.loads(begin.content[0].text)["session_id"]
            offset = 0
            while offset < len(payload):
                piece = payload[offset:offset + 30_000]
                await client.call_tool("upload_file", {
                    "path": "big/agent.bin", "action": "chunk", "session_id": session,
                    "offset": offset, "data_base64": base64.b64encode(piece).decode(),
                    "device": "agent-dev"})
                offset += len(piece)
            commit = await client.call_tool("upload_file", {
                "path": "big/agent.bin", "action": "commit", "session_id": session,
                "device": "agent-dev"})
            passed("chunked upload to agent commits",
                   json.loads(commit.content[0].text)["sha256"] == sha)
            passed("agent chunked file intact", (AGENT_WS / "big/agent.bin").read_bytes() == payload)
            passed("hub has no copy", not (HUB_WS / "big/agent.bin").exists())

            # 5. the agent's own workspace policy is enforced on its filesystem
            escape = await client.call_tool("upload_file", {
                "path": "../../escape.png", "content_text": "x", "device": "agent-dev"})
            passed("agent refuses paths outside its workspace",
                   error_code(escape) == "outside_workspace", str(error_code(escape)))
            passed("nothing escaped to the hub", not (HUB_WS / "escape.png").exists())

            imported = await client.call_tool("import_chat_file", {
                "file": {"download_url": f"http://127.0.0.1:{source_server.server_port}/template.bin",
                         "file_id": "file-agent-attachment", "file_name": "../../untrusted-name.bin"},
                "path": "templates/imported.bin", "device": "agent-dev"})
            passed("chat attachment imports onto selected Agent", not imported.is_error and (AGENT_WS / "templates/imported.bin").read_bytes() == attachment_bytes)
            passed("chat attachment never lands on Hub", not (HUB_WS / "templates/imported.bin").exists())

            # Preview responses must fit the real Agent -> Hub websocket frame.
            (AGENT_WS / "large.png").write_bytes(b"\x89PNG\r\n\x1a\n" + os.urandom(800 * 1024))
            preview = await client.call_tool("preview_file", {"path": "large.png", "device": "agent-dev", "max_bytes": 10**9})
            metadata = json.loads(preview.content[0].text)
            passed("oversized remote image falls back without disconnecting", not preview.is_error and not metadata["previewable"])
            if os.environ.get("C2L_PREVIEW_FIXTURES"):
                for suffix in ("pdf", "docx", "xlsx", "pptx"):
                    source = Path(os.environ["C2L_PREVIEW_FIXTURES"]) / f"sample.{suffix}"
                    shutil.copyfile(source, AGENT_WS / source.name)
                    preview = await client.call_tool("preview_file", {"path": source.name, "device": "agent-dev", "mode": "both"})
                    metadata = json.loads(preview.content[0].text)
                    passed(f"remote {suffix} returns page image and text", not preview.is_error and metadata.get("text_available") and metadata.get("rendered_pages") == [1], str(metadata.get("reason", "")))
    finally:
        for process in (agent, hub):
            if process is None:
                continue
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        hub_log.close()
        agent_log.close()
        source_server.shutdown()
        source_server.server_close()

    print("\nDEVICE ROUTING PASSED")


if __name__ == "__main__":
    asyncio.run(main())
