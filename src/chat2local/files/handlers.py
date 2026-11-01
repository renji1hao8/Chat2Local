"""Bind the file-transfer tools into a device's ``LocalToolDispatcher``.

Every device — the Hub's local one and each remote Agent — registers these
handlers, so ``upload_file(device="laptop")`` executes on the laptop with the
laptop's own workspace policy, staging area and configuration.  Tool results
stay plain dictionaries so they survive the Hub↔Agent JSON transport unchanged;
``blocks`` carries optional MCP content blocks (image / embedded resource).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel

from chat2local.dispatch.local import LocalToolDispatcher, ToolExecutionError
from chat2local.files.errors import TransferError
from chat2local.files.service import (
    DirectoryArgs,
    DownloadArgs,
    FileService,
    PreviewArgs,
    StatusArgs,
    UploadArgs,
    get_file_service,
)


def _wrap(
    dispatcher: LocalToolDispatcher,
    model: type[BaseModel],
    call: Callable[[Any, Any], Awaitable[dict[str, Any]]],
    *,
    needs_workspace: bool = True,
) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            args = model.model_validate(arguments)
            if needs_workspace:
                workspace = dispatcher.workspace if args.workspace is None else dispatcher.workspace.select_workspace(args.workspace)
                return await call(args, workspace)
            return await call(args)
        except TransferError as error:
            # Structured errors must survive both the local call and the
            # Agent JSON transport, so they travel as a JSON message.
            raise ToolExecutionError(error.to_json()) from None

    return handler


def register_file_handlers(dispatcher: LocalToolDispatcher, service: FileService | None = None) -> None:
    service = service or get_file_service()
    dispatcher.register("upload_file", _wrap(dispatcher, UploadArgs, service.upload))
    dispatcher.register("download_file", _wrap(dispatcher, DownloadArgs, service.download))
    dispatcher.register("preview_file", _wrap(dispatcher, PreviewArgs, service.preview))
    dispatcher.register(
        "transfer_status", _wrap(dispatcher, StatusArgs, service.status, needs_workspace=False)
    )
    dispatcher.register("transfer_directory", _wrap(dispatcher, DirectoryArgs, service.directory))
