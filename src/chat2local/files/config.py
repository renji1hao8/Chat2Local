"""Runtime configuration for the file-transfer tools.

Kept in its own YAML file so the upstream ``AppConfig`` schema (which forbids
unknown keys) stays untouched and an older Chat2Local still starts.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from urllib.parse import urlsplit


DEFAULT_CONFIG_PATH = Path.home() / ".chat2local" / "files.yaml"
DEFAULT_STATE_DIR = Path.home() / ".chat2local" / "transfers"
CONFIG_ENV = "CHAT2LOCAL_FILES_CONFIG"


class FileConfigError(ValueError):
    pass


class FileTransferConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True

    # --- size limits -------------------------------------------------------
    max_file_bytes: int = Field(default=4 * 1024**3, ge=1)
    max_inline_bytes: int = Field(default=512 * 1024, ge=1)
    chunk_size: int = Field(default=256 * 1024, ge=1024)
    max_chunk_bytes: int = Field(default=1024 * 1024, ge=1024)
    max_source_bytes: int = Field(default=2 * 1024**3, ge=1)
    max_preview_bytes: int = Field(default=4 * 1024 * 1024, ge=1)
    document_preview_enabled: bool = True
    max_document_bytes: int = Field(default=32 * 1024 * 1024, ge=1)
    max_preview_pages: int = Field(default=3, ge=1, le=3)
    preview_timeout: float = Field(default=20, gt=0, le=20)
    # Optional absolute converter paths; defaults are discovered on PATH.
    soffice_path: str | None = None
    pdftoppm_path: str | None = None
    pdftotext_path: str | None = None
    pdfinfo_path: str | None = None
    max_directory_files: int = Field(default=5000, ge=1)
    max_directory_bytes: int = Field(default=2 * 1024**3, ge=1)

    # --- behaviour ---------------------------------------------------------
    allow_overwrite: bool = False
    create_parents: bool = True
    allow_text_upload: bool = True
    allow_base64_upload: bool = True
    allow_source_url: bool = True
    allow_insecure_source_url: bool = False
    allow_private_source_hosts: bool = False

    # --- sessions ----------------------------------------------------------
    session_ttl: float = Field(default=3600.0, gt=0)
    max_sessions: int = Field(default=64, ge=1)
    max_concurrent_transfers: int = Field(default=4, ge=1)
    max_staging_bytes: int = Field(default=2 * 1024**3, ge=1)
    state_dir: Path = DEFAULT_STATE_DIR

    # --- download links ----------------------------------------------------
    download_url_enabled: bool = True
    download_token_ttl: float = Field(default=900.0, gt=0)
    download_token_single_use: bool = True
    # Externally reachable base URL including any reverse-proxy path prefix.
    # Without it, links use the device's configured local HTTP address.
    download_base_url: str | None = None

    @field_validator("download_base_url")
    @classmethod
    def validate_download_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = urlsplit(value)
        if (parts.scheme not in {"http", "https"} or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.query or parts.fragment or any(c.isspace() for c in value)):
            raise ValueError("download_base_url must be an http(s) URL without credentials, query or fragment")
        _ = parts.port
        return value.rstrip("/")

    # --- path policy -------------------------------------------------------
    deny_globs: list[str] = Field(default_factory=list)

    def state_path(self) -> Path:
        return Path(self.state_dir).expanduser()


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        raise FileConfigError(f"Could not read file-transfer config: {path}") from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise FileConfigError(f"Invalid YAML in {path}{where}") from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise FileConfigError(f"File-transfer config must be a mapping: {path}")
    return data


def load_file_config(path: Path | str | None = None) -> FileTransferConfig:
    """Load ``~/.chat2local/files.yaml`` when present, else defaults.

    An explicitly named file must exist; an explicit ``CHAT2LOCAL_FILES_CONFIG``
    counts as explicitly named.
    """

    explicit = path or os.environ.get(CONFIG_ENV)
    config_path = Path(explicit).expanduser() if explicit else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        if explicit:
            raise FileConfigError(f"File-transfer config does not exist: {config_path}")
        return FileTransferConfig()
    data = _read_yaml(config_path)
    try:
        return FileTransferConfig.model_validate(data)
    except ValidationError as error:
        message = "; ".join(
            f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
            for item in error.errors(include_input=False, include_context=False, include_url=False)
        )
        raise FileConfigError(f"Invalid file-transfer config: {message}") from None


@lru_cache(maxsize=1)
def get_file_config() -> FileTransferConfig:
    """Process-wide configuration, loaded once on first use."""

    return load_file_config()
