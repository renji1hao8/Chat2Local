"""Shared test helpers."""

from __future__ import annotations

import shutil
import time
import atexit
import tempfile
import sys
from pathlib import Path

CLI = [sys.executable, "-m", "chat2local.main"]
_TEST_BASES: set[Path] = set()


def test_base(name: str) -> Path:
    base = Path(tempfile.mkdtemp(prefix=f"chat2local-{name}-"))
    _TEST_BASES.add(base.resolve())
    atexit.register(shutil.rmtree, base, ignore_errors=True)
    return base


def write_config(base: Path) -> Path:
    path = base / "files.yaml"
    path.write_text(f"state_dir: {base.as_posix()}/state\nmax_inline_bytes: 131072\n"
                    "max_chunk_bytes: 65536\nchunk_size: 16384\ndownload_token_ttl: 120\n",
                    encoding="utf-8")
    return path


def reset_base(base: Path) -> None:
    """Delete a test base directory even when a straggler briefly holds a handle."""

    if base.resolve() not in _TEST_BASES:
        raise ValueError("Only a temporary directory allocated by test_base may be reset")

    for _ in range(6):
        shutil.rmtree(base, ignore_errors=True)
        if not base.exists():
            break
        time.sleep(0.5)
    # Anything still locked is a log file from a previous run; drop the dirs the
    # tests care about so a stale file can never turn into a false "exists" result.
    for child in ("ws", "hub-ws", "agent-ws", "state", "state-hub", "state-agent", "home"):
        shutil.rmtree(base / child, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
