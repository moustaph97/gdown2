"""Ports (interfaces) used by the application layer to talk to infrastructure."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol

from gdown2.domain.models import DriveItem, TransferState
from gdown2.domain.url_parser import ParsedTarget


@dataclass(slots=True)
class MediaStream:
    """Opened byte stream along with HTTP transfer headers."""

    chunks: AsyncIterator[bytes]
    status_code: int = 200
    content_length: int | None = None
    etag: str | None = None
    accept_ranges: bool = False
    is_range_satisfied: bool = True


class DriveSource(Protocol):
    """Port for querying Google Drive metadata and streaming file contents."""

    async def fetch_metadata(self, target: ParsedTarget) -> DriveItem:
        """Fetch metadata for a single file, folder, or native doc."""
        ...

    async def list_folder_children(
        self,
        folder_id: str,
        *,
        resource_key: str | None = None,
    ) -> list[DriveItem]:
        """List immediate children of a Drive folder (handling pagination internally)."""
        ...

    def open_download_stream(
        self,
        item: DriveItem,
        *,
        start_byte: int = 0,
        end_byte: int | None = None,
    ) -> AbstractAsyncContextManager[MediaStream]:
        """Open a binary download stream, optionally starting at ``start_byte``."""
        ...

    def open_export_stream(
        self,
        item: DriveItem,
        export_mime: str,
    ) -> AbstractAsyncContextManager[MediaStream]:
        """Open an export stream for a Google Workspace native document."""
        ...


class MetadataCache(Protocol):
    """Port for caching resolved DriveItem records."""

    def get(self, item_id: str) -> DriveItem | None:
        ...

    def put(self, item: DriveItem, *, persist: bool = True) -> None:
        ...

    def flush(self) -> None:
        ...


class ResumeRepository(Protocol):
    """Port for persisting and loading transfer state across runs."""

    def load_all(self) -> dict[str, TransferState]:
        ...

    def save_task(self, state: TransferState) -> None:
        ...

    def remove_task(self, task_key: str) -> None:
        ...

