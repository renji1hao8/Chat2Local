"""Path policy and content sniffing for file transfers.

All paths handed to the transfer tools pass through :func:`resolve_target`,
which is stricter than the upstream ``WorkspaceManager.resolve_path``:

* Windows device paths (``\\\\?\\``, ``\\\\.\\``) are refused outright;
* the *resolved* (symlink-free) path must stay inside the workspace root,
  which also defeats junctions and directory symlinks pointing outside;
* writing through a symlink whose final component is itself a link is refused;
* an optional deny-glob list blocks subtrees such as ``**/.git/**``.

The containment check is case-insensitive on Windows, where ``Path.is_relative_to``
is not.
"""

from __future__ import annotations

import fnmatch
import mimetypes
import os
from pathlib import Path

from chat2local.files.errors import TransferError
from chat2local.runtime.workspace import WorkspaceError, WorkspaceManager


TEXT_EXTENSIONS = frozenset({
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json", ".jsonl",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".xml", ".html", ".htm",
    ".svg", ".css", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".py", ".pyi",
    ".m", ".r", ".jl", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".java", ".kt",
    ".go", ".rs", ".rb", ".php", ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd",
    ".sql", ".tex", ".bib", ".m4a.txt", ".env", ".gitignore", ".dockerfile",
})

IMAGE_MIMES = frozenset({
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp",
})

PREVIEWABLE_IMAGE_MIMES = IMAGE_MIMES | {"image/svg+xml"}

_DEVICE_PREFIXES = ("\\\\?\\", "\\\\.\\", "\\??\\")


def _norm(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def is_within(path: Path, root: Path) -> bool:
    """Case-correct containment test (Windows-safe)."""

    child, parent = _norm(path), _norm(root)
    if child == parent:
        return True
    return child.startswith(parent if parent.endswith(os.sep) else parent + os.sep)


def check_raw_string(path: str) -> None:
    if not path or "\x00" in path:
        raise TransferError("invalid_path", "Path is empty or contains a NUL byte")
    if path.startswith(_DEVICE_PREFIXES) or "\\\\.\\" in path:
        raise TransferError("invalid_path", "Device paths are not allowed", path=path)


def _denied(relative: str, deny_globs: list[str]) -> str | None:
    for pattern in deny_globs:
        variants = {pattern, pattern.replace("**/", "")}
        if any(fnmatch.fnmatch(relative.lower(), item.lower()) for item in variants):
            return pattern
    return None


def resolve_target(
    workspace: WorkspaceManager,
    path: str,
    *,
    write: bool,
    deny_globs: list[str] | None = None,
) -> Path:
    """Resolve ``path`` inside ``workspace`` or raise a structured TransferError."""

    check_raw_string(path)
    raw = Path(path).expanduser()
    if not raw.is_absolute():
        raw = workspace.root / raw

    resolved_root = workspace.root.resolve()
    resolved = raw.resolve()

    if not is_within(resolved, resolved_root):
        raise TransferError(
            "outside_workspace",
            "Path escapes the selected workspace",
            path=path,
            workspace=str(resolved_root),
        )

    if write and raw.is_symlink():
        raise TransferError(
            "symlink_target_refused",
            "Refusing to write through a symbolic link",
            path=path,
        )

    try:
        relative = resolved.relative_to(resolved_root).as_posix()
    except ValueError:  # pragma: no cover - is_within already proved containment
        relative = resolved.as_posix()

    matched = _denied(relative, deny_globs or [])
    if matched is not None:
        raise TransferError(
            "path_denied",
            "Path matches a deny pattern",
            path=relative,
            pattern=matched,
        )
    return resolved


def relative_name(path: Path, workspace: WorkspaceManager) -> str:
    try:
        return path.relative_to(workspace.root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def ensure_parent(path: Path, *, create: bool) -> None:
    parent = path.parent
    if parent.is_dir():
        return
    if not create:
        raise TransferError(
            "parent_missing", "Parent directory does not exist", parent=str(parent)
        )
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise TransferError(
            "parent_create_failed",
            "Could not create the parent directory",
            parent=str(parent),
            reason=error.strerror or str(error),
        ) from None


def sniff_mime(path: Path, head: bytes = b"") -> str:
    """Detect a MIME type from magic bytes first, extension second."""

    magic = _sniff_magic(head)
    if magic is not None:
        if magic == "application/zip":
            by_ext = mimetypes.guess_type(path.name)[0]
            if by_ext in {
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                "application/epub+zip",
            }:
                return by_ext
            return "application/zip"
        return magic

    guessed, _ = mimetypes.guess_type(path.name)
    if guessed:
        return guessed
    if path.suffix.lower() in TEXT_EXTENSIONS:
        return "text/plain"
    return "application/octet-stream"


def _sniff_magic(head: bytes) -> str | None:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"BM"):
        return "image/bmp"
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06"):
        return "application/zip"
    if head.startswith(b"\x1f\x8b"):
        return "application/gzip"
    if head.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "application/x-7z-compressed"
    if head.startswith(b"Rar!\x1a\x07"):
        return "application/vnd.rar"
    if head.startswith(b"\x7fELF"):
        return "application/x-elf"
    if head.startswith(b"MZ"):
        return "application/x-msdownload"
    return None


def looks_like_text(path: Path, head: bytes) -> bool:
    if path.suffix.lower() in TEXT_EXTENSIONS:
        return True
    if not head:
        return False
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def read_head(path: Path, length: int = 512) -> bytes:
    try:
        with path.open("rb") as handle:
            return handle.read(length)
    except OSError:
        return b""
