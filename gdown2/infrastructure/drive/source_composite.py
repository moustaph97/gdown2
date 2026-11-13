"""Composite DriveSource that routes to Drive API v3 when authenticated or web fallback."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from gdown2.application.ports import MediaStream
from gdown2.domain.models import DriveItem
from gdown2.domain.url_parser import ParsedTarget
from gdown2.infrastructure.auth.base import AuthProvider
from gdown2.infrastructure.drive.source_api import DriveApiSource
from gdown2.infrastructure.drive.source_web import DriveWebSource


class CompositeDriveSource:
    """Delegates to :class:`DriveApiSource` when authenticated, else :class:`DriveWebSource`."""

    def __init__(
        self,
        api_source: DriveApiSource,
        web_source: DriveWebSource,
        auth: AuthProvider,
    ) -> None:
        self._api = api_source
        self._web = web_source
        self._auth = auth

    @property
    def active_source(self) -> DriveApiSource | DriveWebSource:
        return self._api if self._auth.is_authenticated else self._web

    async def fetch_metadata(self, target: ParsedTarget) -> DriveItem:
        return await self.active_source.fetch_metadata(target)

    async def list_folder_children(
        self,
        folder_id: str,
        *,
        resource_key: str | None = None,
    ) -> list[DriveItem]:
        return await self.active_source.list_folder_children(
            folder_id,
            resource_key=resource_key,
        )

    @asynccontextmanager
    async def open_download_stream(
        self,
        item: DriveItem,
        *,
        start_byte: int = 0,
        end_byte: int | None = None,
    ) -> AsyncIterator[MediaStream]:
        async with self.active_source.open_download_stream(
            item,
            start_byte=start_byte,
            end_byte=end_byte,
        ) as stream:
            yield stream

    @asynccontextmanager
    async def open_export_stream(
        self,
        item: DriveItem,
        export_mime: str,
    ) -> AsyncIterator[MediaStream]:
        async with self.active_source.open_export_stream(item, export_mime) as stream:
            yield stream

