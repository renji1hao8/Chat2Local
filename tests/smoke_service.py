"""Direct FileService exercise (no MCP transport).  Run: python tests/smoke_service.py"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import os
import shutil
import sys
from pathlib import Path
from _util import test_base

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from chat2local.files.config import FileTransferConfig
from chat2local.files.errors import TransferError
from chat2local.files.service import (
    DirectoryArgs, DownloadArgs, FileService, PreviewArgs, StatusArgs, UploadArgs,
)
from chat2local.runtime.workspace import WorkspaceManager

BASE = test_base("smoke")
WS = BASE / "ws"
STATE = BASE / "state"


def png_bytes(size=(64, 48), color=(20, 120, 220)) -> bytes:
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def check(label: str, condition: bool, detail: str = "") -> None:
    print(("PASS " if condition else "FAIL ") + label + (f"  {detail}" if detail else ""))
    if not condition:
        raise SystemExit(1)


async def expect_error(label: str, coro) -> str:
    try:
        await coro
    except TransferError as error:
        print(f"PASS {label}  [{error.code}]")
        return error.code
    print(f"FAIL {label} (no error)")
    raise SystemExit(1)


async def main() -> None:
    if BASE.exists():
        shutil.rmtree(BASE)
    WS.mkdir(parents=True)
    STATE.mkdir(parents=True)

    config = FileTransferConfig(
        state_dir=STATE,
        chunk_size=8192,
        max_chunk_bytes=65536,
        max_inline_bytes=65536,
    )
    service = FileService(config, device_id="testdev", base_url="http://127.0.0.1:8799")
    await service.startup()
    workspace = WorkspaceManager(WS, allowed_roots=[WS])

    # 1. inline upload of a real PNG
    data = png_bytes()
    digest = hashlib.sha256(data).hexdigest()
    out = await service.upload(UploadArgs(
        path="img/from-chat.png",
        content_base64=base64.b64encode(data).decode(),
        sha256=digest,
    ), workspace)
    result = out["result"]
    check("inline upload commits", result["ok"] and result["size"] == len(data), result["path"])
    on_disk = WS / "img" / "from-chat.png"
    check("file exists on disk", on_disk.is_file())
    check("sha256 matches", hashlib.sha256(on_disk.read_bytes()).hexdigest() == digest)
    from PIL import Image
    with Image.open(on_disk) as image:
        check("PIL can decode uploaded PNG", image.size == (64, 48), str(image.size))

    # 2. text upload
    await service.upload(UploadArgs(path="notes/readme.txt", content_text="hello 世界\n"), workspace)
    check("text upload", (WS / "notes/readme.txt").read_text(encoding="utf-8") == "hello 世界\n")

    # 3. default no-overwrite + explicit overwrite denied by policy
    await expect_error("second upload without overwrite", service.upload(
        UploadArgs(path="notes/readme.txt", content_text="x"), workspace))
    await expect_error("overwrite=true denied by config", service.upload(
        UploadArgs(path="notes/readme.txt", content_text="x", overwrite=True), workspace))

    # 4. chunked upload with a simulated drop and resume
    big = os.urandom(300_000)
    big_sha = hashlib.sha256(big).hexdigest()
    begin = await service.upload(UploadArgs(path="bin/blob.dat", action="begin", size=len(big),
                                            sha256=big_sha), workspace)
    session_id = begin["result"]["session_id"]
    offset = 0
    chunk = 8192
    sent = 0
    while sent < 3:  # simulate a client that dies after three chunks
        piece = big[offset:offset + chunk]
        await service.upload(UploadArgs(path="bin/blob.dat", action="chunk", session_id=session_id,
                                        offset=offset, data_base64=base64.b64encode(piece).decode()), workspace)
        offset += len(piece)
        sent += 1
    state = await service.status(StatusArgs(session_id=session_id))
    check("resume offset after drop", state["result"]["received"] == offset,
          f"received={state['result']['received']} expected={offset}")

    # duplicate chunk at an earlier offset must be accepted and must not corrupt
    first = big[0:chunk]
    await service.upload(UploadArgs(path="bin/blob.dat", action="chunk", session_id=session_id,
                                    offset=0, data_base64=base64.b64encode(first).decode()), workspace)
    check("duplicate chunk tolerated", (await service.status(StatusArgs(session_id=session_id)))["result"]["received"] == offset)
    await expect_error("conflicting duplicate chunk", service.upload(
        UploadArgs(path="bin/blob.dat", action="chunk", session_id=session_id, offset=0,
                   data_base64=base64.b64encode(b"\x00" * chunk).decode()), workspace))

    while offset < len(big):
        piece = big[offset:offset + chunk]
        await service.upload(UploadArgs(path="bin/blob.dat", action="chunk", session_id=session_id,
                                        offset=offset, data_base64=base64.b64encode(piece).decode()), workspace)
        offset += len(piece)
    committed = await service.upload(UploadArgs(path="bin/blob.dat", action="commit", session_id=session_id), workspace)
    check("chunked upload commits", committed["result"]["sha256"] == big_sha)
    check("resumed file identical", (WS / "bin/blob.dat").read_bytes() == big)
    replay = await service.upload(UploadArgs(path="bin/blob.dat", action="commit", session_id=session_id), workspace)
    check("commit is idempotent", replay["result"].get("idempotent_replay") is True)

    # 5. download round trip
    down = await service.download(DownloadArgs(path="bin/blob.dat", length=4096, sha256=True), workspace)
    check("download returns inline bytes", len(base64.b64decode(down["blocks"][0]["resource"]["blob"])) == 4096)
    check("download reports whole-file sha", down["result"]["sha256"] == big_sha)
    check("download has a link", bool(down["result"]["download_url"]))

    text = await service.download(DownloadArgs(path="notes/readme.txt"), workspace)
    check("text download is text content", text["blocks"][0]["type"] == "text")

    # 6. preview
    prev = await service.preview(PreviewArgs(path="img/from-chat.png"), workspace)
    check("preview returns image content", prev["blocks"][0]["type"] == "image" and prev["result"]["previewable"])

    # 7. security
    await expect_error("parent traversal", service.upload(
        UploadArgs(path="../../escape.txt", content_text="x"), workspace))
    await expect_error("absolute escape", service.upload(
        UploadArgs(path="C:/Windows/Temp/escape.txt", content_text="x"), workspace))
    if not (WS / "link-out").exists():
        target = BASE / "outside"
        target.mkdir(exist_ok=True)
        try:
            os.symlink(target, WS / "link-out", target_is_directory=True)
        except OSError:
            import subprocess
            subprocess.run(["cmd", "/c", "mklink", "/J", str(WS / "link-out"), str(target)],
                           capture_output=True)
    check("link-out created for escape test", (WS / "link-out").exists())
    if (WS / "link-out").exists():
        await expect_error("junction/symlink escape", service.upload(
            UploadArgs(path="link-out/evil.txt", content_text="x"), workspace))
        await expect_error("junction/symlink download escape", service.download(
            DownloadArgs(path="link-out"), workspace))
    await expect_error("sha256 mismatch", service.upload(
        UploadArgs(path="bad.bin", content_text="x", sha256="0" * 64), workspace))
    await expect_error("missing zip", service.directory(
        DirectoryArgs(path="stuff", action="upload", zip_path="nope.zip"), workspace))

    # 8. idempotency
    key = "k-1"
    one = await service.upload(UploadArgs(path="idem/one.txt", content_text="v1", idempotency_key=key), workspace)
    two = await service.upload(UploadArgs(path="idem/one.txt", content_text="v1", idempotency_key=key), workspace)
    check("idempotent replay", two["result"].get("idempotent_replay") is True and one["result"]["size"] == 2)
    await expect_error("idempotency conflict", service.upload(
        UploadArgs(path="idem/one.txt", content_text="v2", idempotency_key=key), workspace))

    # 9. directory: manifest -> zip -> extract
    (WS / "tree/sub").mkdir(parents=True)
    (WS / "tree/a.txt").write_text("A")
    (WS / "tree/sub/b.bin").write_bytes(os.urandom(1024))
    manifest = await service.directory(DirectoryArgs(path="tree", action="manifest"), workspace)
    check("manifest lists files", manifest["result"]["entry_count"] == 2, str(manifest["result"]["entry_count"]))
    zipped = await service.directory(DirectoryArgs(path="tree", action="download"), workspace)
    check("zip produced", zipped["result"]["archive_size"] > 0)
    # copy the produced archive into the workspace so zip_path can see it
    produced = sorted(service.store.archives_dir.glob("*.zip"), key=lambda p: p.stat().st_mtime)[-1]
    shutil.copy(produced, WS / "tree.zip")
    pushed = await service.directory(DirectoryArgs(path="tree2", action="upload", zip_path="tree.zip"), workspace)
    check("zip extracted", pushed["result"]["written_count"] == 2, str(pushed["result"]))
    check("extracted bytes match", (WS / "tree2/sub/b.bin").read_bytes() == (WS / "tree/sub/b.bin").read_bytes())
    again = await service.directory(DirectoryArgs(path="tree2", action="upload", zip_path="tree.zip"), workspace)
    check("re-extract reports exists instead of overwriting",
          again["result"]["ok"] is False and again["result"]["failed_count"] == 2,
          str(again["result"]["failed"]))

    print("\nALL SERVICE CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
