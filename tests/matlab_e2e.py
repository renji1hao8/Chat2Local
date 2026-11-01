"""Acceptance: PNG uploaded over MCP -> MATLAB imread -> PNG written by MATLAB -> downloaded over MCP.

Run: .venv/Scripts/python.exe tests/matlab_e2e.py
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
BASE = test_base("matlab")
WS = BASE / "ws"
PORT = 8799
MATLAB = Path(os.environ.get("MATLAB_EXE") or shutil.which("matlab") or "matlab")


def passed(label: str, condition: bool, detail: str = "") -> None:
    print(("PASS " if condition else "FAIL ") + label + (f"  {detail}" if detail else ""))
    if not condition:
        raise SystemExit(1)


def make_png(size=(96, 64)) -> bytes:
    from PIL import Image
    image = Image.new("RGB", size)
    pixels = image.load()
    for y in range(size[1]):
        for x in range(size[0]):
            pixels[x, y] = (x * 2 % 256, y * 4 % 256, (x + y) % 256)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


async def wait_health() -> None:
    for _ in range(80):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=1) as response:
                if json.loads(response.read())["status"] == "ok":
                    return
        except Exception:
            await asyncio.sleep(0.3)
    raise SystemExit("server did not become healthy")


async def main() -> None:
    reset_base(BASE)
    (WS / "shots").mkdir(parents=True, exist_ok=True)
    (WS / "matlab").mkdir(parents=True)
    shutil.copy(ROOT / "tests" / "matlab_roundtrip.m", WS / "matlab_roundtrip.m")
    (BASE / "state").mkdir(parents=True)
    (BASE / "home").mkdir(parents=True)

    env = dict(os.environ, CHAT2LOCAL_FILES_CONFIG=str(write_config(BASE)),
               USERPROFILE=str(BASE / "home"), HOME=str(BASE / "home"))
    log_handle = (BASE / "server.log").open("w", encoding="utf-8")
    server = subprocess.Popen(
        [*CLI,
         "--workspace", str(WS), "--host", "127.0.0.1", "--port", str(PORT)],
        env=env, stdout=log_handle, stderr=subprocess.STDOUT, text=True,
    )
    try:
        await wait_health()
        from mcp import Client

        async with Client(f"http://127.0.0.1:{PORT}/mcp/", read_timeout_seconds=60) as client:
            png = make_png()
            sent_sha = hashlib.sha256(png).hexdigest()
            upload = await client.call_tool("upload_file", {
                "path": "shots/chat-upload.png",
                "content_base64": base64.b64encode(png).decode(),
                "sha256": sent_sha,
            })
            passed("PNG uploaded over MCP", not upload.is_error)
            local_png = WS / "shots" / "chat-upload.png"
            passed("bytes identical after upload",
                   hashlib.sha256(local_png.read_bytes()).hexdigest() == sent_sha)

            # MATLAB reads the uploaded file and writes a new one.  MATLAB's
            # -batch can linger on exit under license contention, so completion
            # is signalled by a result file, not by process exit.
            result_file = WS / "matlab" / "result.txt"
            result_file.unlink(missing_ok=True)
            command = (
                f"cd('{WS.as_posix()}'); "
                "matlab_roundtrip('shots/chat-upload.png', 'matlab/matlab-out.png');"
            )
            process = subprocess.Popen([str(MATLAB), "-batch", command],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            outcome = "timeout"
            for _ in range(600):  # 300 s
                if result_file.exists() and result_file.stat().st_size > 0:
                    outcome = await asyncio.to_thread(result_file.read_text, encoding="utf-8")
                    break
                if process.poll() is not None and not result_file.exists():
                    await asyncio.sleep(1)
                    if result_file.exists():
                        outcome = await asyncio.to_thread(result_file.read_text, encoding="utf-8")
                        break
                await asyncio.sleep(0.5)
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
            print("matlab:", outcome.replace("\n", " | ")[:200])
            passed("MATLAB produced a result file", outcome != "timeout")
            passed("MATLAB imread() read the uploaded PNG",
                   "READ_OK 64 96 3" in outcome.replace("\n", " "), outcome.replace("\n", " | ")[:120])
            passed("MATLAB reported no error", "ERROR" not in outcome)
            out_png = WS / "matlab" / "matlab-out.png"
            passed("MATLAB wrote a PNG", out_png.is_file() and out_png.stat().st_size > 0,
                   str(out_png.stat().st_size if out_png.exists() else 0))
            matlab_sha = hashlib.sha256(out_png.read_bytes()).hexdigest()

            # Download the MATLAB output and preview it like a chat client would.
            preview = await client.call_tool("preview_file", {"path": "matlab/matlab-out.png"})
            kinds = [block.type for block in preview.content]
            passed("preview of MATLAB PNG returns image", "image" in kinds, str(kinds))
            image = next(block for block in preview.content if block.type == "image")
            returned = base64.b64decode(image.data)
            passed("previewed bytes equal the file on disk",
                   hashlib.sha256(returned).hexdigest() == matlab_sha)
            from PIL import Image as PILImage
            with PILImage.open(io.BytesIO(returned)) as decoded:
                downloaded = decoded.convert("RGB")
                passed("downloaded MATLAB image is a valid 96x64 PNG",
                       decoded.size == (96, 64) and decoded.format == "PNG", str(decoded.size))
            with PILImage.open(local_png) as original:
                expected = original.convert("RGB").transpose(PILImage.Transpose.FLIP_TOP_BOTTOM)
            passed("MATLAB image is the exact vertical flip of the upload",
                   downloaded.tobytes() == expected.tobytes())

            download = await client.call_tool("download_file", {"path": "matlab/matlab-out.png",
                                                               "sha256": True})
            meta = json.loads(download.content[0].text)
            passed("download_file reports the same sha", meta["sha256"] == matlab_sha)
            link = meta["download_url"]
            with urllib.request.urlopen(link, timeout=15) as response:
                fetched = response.read()
            passed("download_url returns identical bytes",
                   hashlib.sha256(fetched).hexdigest() == matlab_sha)
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        log_handle.close()

    print("\nMATLAB ROUND TRIP PASSED")


if __name__ == "__main__":
    asyncio.run(main())
