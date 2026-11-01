"""Structured, machine-readable failures for the file-transfer tools.

Every failure travels back to the caller as a JSON object so that a model can
branch on ``error.code`` instead of parsing prose.  The dispatcher converts a
``TransferError`` into a ``ToolExecutionError`` whose message is this JSON.
"""

from __future__ import annotations

import json
from typing import Any


class TransferError(Exception):
    """A file-transfer failure with a stable code and optional details."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return {"ok": False, "error": payload}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"), default=str)


def error_payload(code: str, message: str, **details: Any) -> dict[str, Any]:
    return TransferError(code, message, **details).to_dict()
