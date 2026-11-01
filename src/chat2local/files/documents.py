"""Bounded document previews using optional, locally installed converters.

No shell is involved. Each conversion uses a private temporary directory and
LibreOffice profile; original files are never passed to Office for editing.
These converters run as the current OS user, not in an OS sandbox.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from chat2local.files.errors import TransferError

OFFICE_SUFFIXES = frozenset({".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt", ".odt", ".ods", ".odp"})
DOCUMENT_SUFFIXES = OFFICE_SUFFIXES | {".pdf"}
MAX_INPUT_BYTES = 32 * 1024 * 1024
MAX_IMAGE_BYTES = 384 * 1024
MAX_TEXT_BYTES = 16 * 1024


def _tool(name: str, configured: str | None = None) -> str:
    resolved = shutil.which(configured or name)
    if resolved:
        return resolved
    raise TransferError("preview_dependency_missing", f"Document preview requires {name}", dependency=name)


def _run(command: list[str], directory: Path, timeout: float) -> None:
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with (directory / "converter.log").open("wb") as log:
        process = subprocess.Popen(command, cwd=directory, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=log, creationflags=flags,
                                   start_new_session=os.name != "nt")
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=flags, timeout=10)
            else:
                import signal
                os.killpg(process.pid, signal.SIGKILL)
            process.kill()
            process.wait()
            raise TransferError("preview_timeout", "Document conversion timed out") from None
    if code:
        raise TransferError("preview_conversion_failed", "Document converter failed", exit_code=code)


def render_document(path: Path, *, page: int, page_count: int, mode: str,
                    image_budget: int, text_budget: int, timeout: float = 60,
                    soffice: str | None = None, pdftoppm: str | None = None,
                    pdftotext: str | None = None, pdfinfo: str | None = None,
                    max_input_bytes: int = MAX_INPUT_BYTES) -> dict[str, Any]:
    """Return JPEG page bytes and UTF-8 text for a bounded PDF page range.

    Office page numbers refer to the exported PDF (printed sheets/slides).
    Text is extracted from that same page range; scanned PDFs need OCR, which
    is intentionally not claimed here.
    """
    if path.stat().st_size > min(max_input_bytes, MAX_INPUT_BYTES):
        raise TransferError("preview_input_too_large", "Document exceeds the preview input limit")
    deadline = time.monotonic() + timeout

    def remaining_time() -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TransferError("preview_timeout", "Document conversion timed out")
        return remaining

    def run(command: list[str], work: Path) -> None:
        _run(command, work, remaining_time())
    with tempfile.TemporaryDirectory(prefix="chat2local-preview-") as temporary:
        work = Path(temporary)
        source = work / ("source" + path.suffix.lower())
        shutil.copyfile(path, source)
        pdf = source
        if path.suffix.lower() in OFFICE_SUFFIXES:
            executable = _tool("soffice", soffice)
            profile = work / "profile"
            profile.mkdir()
            # Highest macro security; do not refresh external links on load.
            (profile / "registrymodifications.xcu").write_text(
                '<?xml version="1.0"?><oor:items xmlns:oor="http://openoffice.org/2001/registry">'
                '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
                '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item>'
                '<item oor:path="/org.openoffice.Office.Common/Load">'
                '<prop oor:name="UpdateDocMode" oor:op="fuse"><value>0</value></prop></item>'
                '</oor:items>', encoding="utf-8")
            run([executable, f"-env:UserInstallation={profile.as_uri()}", "--headless",
                  "--nologo", "--nodefault", "--norestore", "--convert-to", "pdf",
                  "--outdir", str(work), str(source)], work)
            pdf = work / "source.pdf"
            if not pdf.is_file():
                raise TransferError("preview_conversion_failed", "Office conversion produced no PDF")

        info_exe = _tool("pdfinfo", pdfinfo)
        # Write metadata to a file instead of buffering unbounded subprocess output.
        info_path = work / "info.txt"
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            with info_path.open("wb") as handle:
                info = subprocess.run([info_exe, str(pdf)], stdout=handle, stderr=subprocess.DEVNULL,
                                      timeout=remaining_time(), creationflags=flags)
        except subprocess.TimeoutExpired:
            raise TransferError("preview_timeout", "PDF metadata inspection timed out") from None
        with info_path.open("rb") as handle:
            metadata = handle.read(65536).decode("utf-8", "replace")
        match = re.search(r"^Pages:\s+(\d+)", metadata, re.MULTILINE)
        if info.returncode or match is None:
            raise TransferError("preview_conversion_failed", "PDF is invalid, encrypted or unreadable")
        total_pages = int(match.group(1))
        if page > total_pages:
            raise TransferError("preview_page_out_of_range", "Page is beyond the document", total_pages=total_pages)
        last = min(total_pages, page + page_count - 1)
        result: dict[str, Any] = {"total_pages": total_pages, "page": page, "last_page": last,
                                  "pages": [], "text": "", "text_truncated": False,
                                  "image_truncated": False}
        if mode in {"text", "both"}:
            text_exe = _tool("pdftotext", pdftotext)
            output = work / "text.txt"
            run([text_exe, "-f", str(page), "-l", str(last), "-enc", "UTF-8",
                  "-layout", str(pdf), str(output)], work)
            limit = min(text_budget, MAX_TEXT_BYTES)
            with output.open("rb") as handle:
                raw = handle.read(limit + 1)
            result["text_truncated"] = len(raw) > limit
            result["text"] = raw[:limit].decode("utf-8", "ignore")
        if mode in {"pages", "both"}:
            ppm_exe = _tool("pdftoppm", pdftoppm)
            remaining = min(image_budget, MAX_IMAGE_BYTES)
            for number in range(page, last + 1):
                prefix = work / f"page-{number}"
                output = prefix.with_suffix(".jpg")
                per_page = remaining // (last - number + 1)
                data = None
                for scale in (1200, 900, 600):
                    run([ppm_exe, "-f", str(number), "-l", str(number), "-singlefile",
                          "-scale-to", str(scale), "-jpeg", "-jpegopt", "quality=65",
                          str(pdf), str(prefix)], work)
                    if output.stat().st_size <= per_page:
                        data = output.read_bytes()
                        break
                if data is None:
                    result["image_truncated"] = True
                    break
                result["pages"].append({"page": number, "data": data, "mime_type": "image/jpeg"})
                remaining -= len(data)
        return result
