"""Portable regressions: python -m unittest discover -s tests -p test_regressions.py -v."""
import asyncio
import base64
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from fastapi import FastAPI
from types import SimpleNamespace
from starlette.requests import Request
from chat2local.files.config import FileTransferConfig
from chat2local.files.errors import TransferError
from chat2local.files.links import LINKS
from chat2local.files.http import router, download_link
from chat2local.files.service import FileService, DirectoryArgs, PreviewArgs, DownloadArgs
from chat2local.runtime.workspace import WorkspaceManager


async def http_request(app, path, method="GET", headers=None):
    messages = []
    scope = {"type": "http", "asgi": {"spec_version": "2.4"}, "method": method, "path": path, "raw_path": path.encode(),
             "root_path": "", "query_string": b"", "scheme": "http", "http_version": "1.1",
             "server": ("test", 80), "client": ("test", 1234),
             "headers": [(key.lower().encode(), value.encode()) for key, value in (headers or {}).items()]}
    async def send(message):
        messages.append(message)
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    await app(scope, receive, send)
    return SimpleNamespace(status_code=next(x["status"] for x in messages if x["type"] == "http.response.start"),
                           content=b"".join(x.get("body", b"") for x in messages))


class Regressions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="chat2local-regression-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.ws = self.base / "ws"
        self.ws.mkdir()
        self.manager = WorkspaceManager(self.ws)
        self.config = FileTransferConfig(state_dir=self.base / "state", deny_globs=["**/secret.txt"])
        self.service = FileService(self.config)
        await self.service.startup()

    def write(self, name, data=b"test"):
        path = self.ws / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    async def test_directory_policy_in_manifest_and_zip(self):
        self.write("secret.txt", b"secret")
        self.write("nested/secret.txt", b"secret")
        self.write("visible.txt")
        with self.assertRaises(TransferError):
            await self.service.download(DownloadArgs(path="secret.txt"), self.manager)
        result = await self.service.directory(DirectoryArgs(path="."), self.manager)
        self.assertEqual([x["path"] for x in result["result"]["entries"]], ["visible.txt"])
        await self.service.directory(DirectoryArgs(path=".", action="download"), self.manager)
        archive = next(self.service.store.archives_dir.glob("*.zip"))
        with zipfile.ZipFile(archive) as handle:
            self.assertEqual(handle.namelist(), ["visible.txt"])

    async def test_extract_policy_and_unsafe_members(self):
        archive = self.ws / "incoming.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("secret.txt", "denied")
            handle.writestr("../escape.txt", "denied")
            handle.writestr("nested/name:stream", "denied")
            handle.writestr("visible.txt", "allowed")
        result = await self.service.directory(DirectoryArgs(path="out", action="upload", zip_path="incoming.zip"), self.manager)
        self.assertEqual(result["result"]["written_count"], 1)
        self.assertEqual(result["result"]["failed_count"], 3)
        self.assertFalse((self.ws / "out/secret.txt").exists())
        self.assertFalse(list((self.ws / "out").glob(".c2l-extract-*")))

    async def test_server_limits_cannot_be_enlarged(self):
        self.write("a.txt", b"a" * 10)
        self.write("b.txt", b"b" * 10)
        self.config.max_preview_bytes = 4
        result = await self.service.preview(PreviewArgs(path="a.txt", max_bytes=1000), self.manager)
        self.assertEqual(result["result"]["shown_bytes"], 4)
        self.config.max_directory_files = 1
        result = await self.service.directory(DirectoryArgs(path=".", max_files=1000), self.manager)
        self.assertEqual(result["result"]["entry_count"], 1)
        self.config.max_directory_bytes = 4
        result = await self.service.directory(DirectoryArgs(path=".", max_bytes=1000), self.manager)
        self.assertEqual(result["result"]["entry_count"], 0)
        self.config.max_inline_bytes = 4
        result = await self.service.download(DownloadArgs(path="a.txt", length=10000), self.manager)
        self.assertEqual(result["result"]["length"], 4)

    async def test_extraction_respects_configured_limit(self):
        archive = self.ws / "incoming.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("large.txt", "too large")
        self.config.max_directory_bytes = 4
        result = await self.service.directory(DirectoryArgs(path="out", action="upload", zip_path="incoming.zip", max_bytes=10000), self.manager)
        self.assertEqual(result["result"]["written_count"], 0)

    async def test_image_wire_budget_and_no_fake_preview(self):
        self.write("large.png", b"\x89PNG\r\n\x1a\n" + b"a" * (600 * 1024))
        result = await self.service.preview(PreviewArgs(path="large.png", max_bytes=10**9), self.manager)
        self.assertFalse(result["blocks"])
        self.assertFalse(result["result"]["previewable"])
        self.config.download_url_enabled = False
        result = await self.service.preview(PreviewArgs(path="large.png"), self.manager)
        self.assertEqual(result["result"]["preview"], "metadata")

    async def test_public_download_base_and_validation(self):
        config = FileTransferConfig(state_dir=self.base / "state2", download_base_url="https://files.example.test/prefix/")
        service = FileService(config)
        url, _ = service._download_url(self.write("a.txt"))
        self.assertTrue(url.startswith("https://files.example.test/prefix/files/"))
        for value in ("file:///etc", "https://user:pass@host", "https://host/?token=x"):
            with self.assertRaises(ValueError):
                FileTransferConfig(download_base_url=value)

    async def test_head_range_prefetch_and_full_get(self):
        path = self.write("download.txt", b"payload")
        link = LINKS.create(path, ttl=60)
        app = FastAPI()
        app.include_router(router)
        url = f"/files/{link.token}"
        self.assertEqual((await http_request(app, url, "HEAD")).status_code, 200)
        self.assertEqual((await http_request(app, url, headers={"Range": "bytes=0-2"})).status_code, 416)
        self.assertEqual((await http_request(app, url, headers={"Sec-Purpose": "prefetch"})).status_code, 204)
        self.assertIsNotNone(LINKS.peek(link.token))
        response = await http_request(app, url)
        self.assertEqual(response.content, b"payload")
        self.assertEqual((await http_request(app, url)).status_code, 404)

    async def test_failed_send_releases_link_and_reservation_is_exclusive(self):
        path = self.write("download.txt", b"payload")
        link = LINKS.create(path, ttl=60)
        scope = {"type": "http", "asgi": {"spec_version": "2.4"}, "method": "GET", "path": "/", "headers": [], "extensions": {}}
        response = await download_link(link.token, Request(scope))
        self.assertIsNone(LINKS.acquire(link.token))
        async def send(message):
            if message["type"] == "http.response.body":
                raise OSError("disconnected")
        async def receive():
            return {"type": "http.disconnect"}
        with self.assertRaises(Exception):
            await response(scope, receive, send)
        self.assertIsNotNone(LINKS.acquire(link.token))
        LINKS.revoke(link.token)

    async def test_missing_converter_falls_back_explicitly(self):
        self.write("paper.pdf", b"%PDF-1.4\n")
        self.config.pdfinfo_path = "nonexistent-chat2local-pdfinfo"
        result = await self.service.preview(PreviewArgs(path="paper.pdf"), self.manager)
        self.assertFalse(result["result"]["previewable"])
        self.assertEqual(result["result"]["reason"], "preview_dependency_missing")

    @unittest.skipUnless(os.environ.get("C2L_PREVIEW_FIXTURES"), "optional real document fixtures not supplied")
    async def test_real_pdf_and_office_pages_and_text(self):
        directory = Path(os.environ["C2L_PREVIEW_FIXTURES"])
        for suffix in ("pdf", "docx", "xlsx", "pptx"):
            with self.subTest(suffix=suffix):
                path = self.write(f"sample.{suffix}", (directory / f"sample.{suffix}").read_bytes())
                result = await self.service.preview(PreviewArgs(path=path.name), self.manager)
                self.assertTrue(result["result"]["previewable"], result["result"])
                self.assertTrue(result["result"]["text_available"], result["result"])
                self.assertEqual(result["result"]["rendered_pages"], [1])
                self.assertIn("Preview", "".join(x.get("text", "") for x in result["blocks"]))
                self.assertIn("image", [x["type"] for x in result["blocks"]])
                self.assertLess(len(json.dumps(result).encode()), 1024 * 1024)
                image = next(x for x in result["blocks"] if x["type"] == "image")
                (directory / f"verified-{suffix}.jpg").write_bytes(base64.b64decode(image["data"]))
        result = await self.service.preview(PreviewArgs(path="sample.pdf", mode="text", page=2), self.manager)
        self.assertIn("Second", "".join(x.get("text", "") for x in result["blocks"]))
        self.assertNotIn("image", [x["type"] for x in result["blocks"]])


if __name__ == "__main__":
    unittest.main()
