"""Google Drive API v3 adapter for metadata queries, folder listing, and media streaming."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from gdown2.application.ports import MediaStream
from gdown2.domain.errors import PermissionDeniedError
from gdown2.domain.models import DriveItem
from gdown2.domain.url_parser import ParsedTarget
from gdown2.infrastructure.auth.base import AuthProvider
from gdown2.infrastructure.drive.metadata import drive_item_from_api_json
from gdown2.infrastructure.http.client import HttpClient
from gdown2.infrastructure.http.retry import classify_http_error

DRIVE_API_BASE = "https://www.googleapis.com/drive/v3/files"
_FILE_FIELDS = (
    "id,name,mimeType,size,md5Checksum,parents,modifiedTime,"
    "headRevisionId,shortcutDetails,exportLinks,webContentLink"
)


class DriveApiSource:
    """Interacts with Google Drive via REST API v3."""

    def __init__(
        self,
        http: HttpClient,
        auth: AuthProvider,
        *,
        chunk_size: int = 64 * 1024,
        page_size: int = 200,
    ) -> None:
        self._http = http
        self._auth = auth
        self._chunk_size = chunk_size
        self._page_size = page_size

    async def fetch_metadata(self, target: ParsedTarget) -> DriveItem:
        headers: dict[str, str] = {}
        params: dict[str, Any] = {
            "fields": _FILE_FIELDS,
            "supportsAllDrives": "true",
        }
        if target.resource_key:
            headers["X-Goog-Drive-Resource-Keys"] = f"{target.id}/{target.resource_key}"

        await self._auth.apply(headers, params)
        response = await self._http.request(
            "GET",
            f"{DRIVE_API_BASE}/{target.id}",
            params=params,
            headers=headers,
            item_id=target.id,
        )
        return drive_item_from_api_json(response.json())

    async def list_folder_children(
        self,
        folder_id: str,
        *,
        resource_key: str | None = None,
    ) -> list[DriveItem]:
        items: list[DriveItem] = []
        page_token: str | None = None

        while True:
            headers: dict[str, str] = {}
            params: dict[str, Any] = {
                "q": f"'{folder_id}' in parents and trashed = false",
                "fields": f"nextPageToken,files({_FILE_FIELDS})",
                "pageSize": self._page_size,
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
                "orderBy": "folder,name",
            }
            if page_token:
                params["pageToken"] = page_token
            if resource_key:
                headers["X-Goog-Drive-Resource-Keys"] = f"{folder_id}/{resource_key}"

            await self._auth.apply(headers, params)
            response = await self._http.request(
                "GET",
                DRIVE_API_BASE,
                params=params,
                headers=headers,
                item_id=folder_id,
            )
            payload = response.json()
            for raw_file in payload.get("files", []):
                items.append(drive_item_from_api_json(raw_file))

            page_token = payload.get("nextPageToken")
            if not page_token:
                break

        return items

    @asynccontextmanager
    async def open_download_stream(
        self,
        item: DriveItem,
        *,
        start_byte: int = 0,
        end_byte: int | None = None,
    ) -> AsyncIterator[MediaStream]:
        headers: dict[str, str] = {}
        params: dict[str, Any] = {
            "alt": "media",
            "supportsAllDrives": "true",
            "acknowledgeAbuse": "true",
        }
        if start_byte > 0 or end_byte is not None:
            end_str = str(end_byte) if end_byte is not None else ""
            headers["Range"] = f"bytes={start_byte}-{end_str}"

        await self._auth.apply(headers, params)

        async with self._http.stream(
            "GET",
            f"{DRIVE_API_BASE}/{item.id}",
            params=params,
            headers=headers,
            item_id=item.id,
        ) as response:
            if response.status_code == 416:
                yield MediaStream(
                    chunks=_empty_stream(),
                    status_code=416,
                    is_range_satisfied=False,
                )
                return

            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", errors="replace")
                raise classify_http_error(
                    response.status_code,
                    body_snippet=body[:1024],
                    headers=response.headers,
                    item_id=item.id,
                )

            raw_len = response.headers.get("content-length")
            content_length = int(raw_len) if raw_len and raw_len.isdigit() else None
            etag = response.headers.get("etag")
            accept_ranges = response.headers.get("accept-ranges", "").lower() == "bytes"

            yield MediaStream(
                chunks=response.aiter_bytes(self._chunk_size),
                status_code=response.status_code,
                content_length=content_length,
                etag=etag,
                accept_ranges=accept_ranges,
                is_range_satisfied=True,
            )

    @asynccontextmanager
    async def open_export_stream(
        self,
        item: DriveItem,
        export_mime: str,
    ) -> AsyncIterator[MediaStream]:
        headers: dict[str, str] = {}
        params: dict[str, Any] = {"mimeType": export_mime}
        await self._auth.apply(headers, params)

        async with self._http.stream(
            "GET",
            f"{DRIVE_API_BASE}/{item.id}/export",
            params=params,
            headers=headers,
            item_id=item.id,
        ) as response:
            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", errors="replace")
                raise classify_http_error(
                    response.status_code,
                    body_snippet=body[:1024],
                    headers=response.headers,
                    item_id=item.id,
                )

            content_type = response.headers.get("content-type", "").lower()
            disposition = response.headers.get("content-disposition")
            if content_type.startswith("text/html") and not disposition:
                raise PermissionDeniedError(
                    "Google Drive returned an HTML page instead of an exported document; "
                    "the document may require sign-in or permissions.",
                    item_id=item.id,
                )

            yield MediaStream(
                chunks=response.aiter_bytes(self._chunk_size),
                status_code=response.status_code,
            )


async def _empty_stream() -> AsyncIterator[bytes]:
    if False:  # pragma: no cover
        yield b""

