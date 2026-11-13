"""Public Google Drive web source (zero-auth scraping + virus-scan token bypass)."""

from __future__ import annotations

import html
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from gdown2.application.ports import MediaStream
from gdown2.domain.errors import (
    PermissionDeniedError,
    QuotaExceededError,
    ResourceNotFoundError,
    UntrustedRedirectError,
)
from gdown2.domain.models import (
    EXPORT_FORMAT_ALIASES,
    FOLDER_MIME_TYPE,
    DriveItem,
)
from gdown2.domain.url_parser import ParsedTarget, TargetKind
from gdown2.infrastructure.drive.metadata import (
    KIND_TO_MIME,
    parse_content_disposition_filename,
    parse_html_title,
)
from gdown2.infrastructure.http.client import HttpClient, is_trusted_redirect_url
from gdown2.infrastructure.http.retry import classify_http_error

USERCONTENT_DOWNLOAD_URL = "https://drive.usercontent.google.com/download"
DRIVE_UC_URL = "https://drive.google.com/uc"
DRIVE_FOLDER_URL = "https://drive.google.com/drive/folders"
EMBEDDED_FOLDER_URL = "https://drive.google.com/embeddedfolderview"

_CONTENT_RANGE_TOTAL_RE = re.compile(r"bytes\s+\d+-\d+/(\d+)", re.IGNORECASE)
_UC_NAME_SIZE_RE = re.compile(
    r'<span\s+class="uc-name-size">\s*<a[^>]*>([^<]+)</a>\s*\(([^)]+)\)\s*</span>',
    re.IGNORECASE,
)
_IVD_SCRIPT_RE = re.compile(r"window\['_DRIVE_ivd'\]\s*=\s*'((?:\\.|[^'])*)'")
_JS_HEX_ESCAPE_RE = re.compile(r"\\x([0-9a-fA-F]{2})|\\u([0-9a-fA-F]{4})")

_MIME_TO_SHORT_EXPORT: dict[str, str] = {
    mime: short for short, (mime, _) in EXPORT_FORMAT_ALIASES.items()
}


class DriveWebSource:
    """Downloads public Google Drive files and crawls public folders without an API key."""

    def __init__(self, http: HttpClient, *, chunk_size: int = 64 * 1024) -> None:
        self._http = http
        self._chunk_size = chunk_size

    async def fetch_metadata(self, target: ParsedTarget) -> DriveItem:
        if target.is_folder_hint:
            return await self._fetch_folder_metadata(target.id, target.resource_key)

        if target.is_google_doc_hint:
            return await self._fetch_doc_metadata(target)

        return await self._fetch_file_metadata(target)

    async def _fetch_folder_metadata(
        self,
        folder_id: str,
        resource_key: str | None = None,
    ) -> DriveItem:
        params: dict[str, str] = {"id": folder_id}
        if resource_key:
            params["resourcekey"] = resource_key
        response = await self._http.request(
            "GET",
            EMBEDDED_FOLDER_URL,
            params=params,
            item_id=folder_id,
        )
        name = parse_html_title(response.text) or folder_id
        return DriveItem(
            id=folder_id,
            name=name,
            mime_type=FOLDER_MIME_TYPE,
        )

    async def _fetch_doc_metadata(self, target: ParsedTarget) -> DriveItem:
        mime_type = KIND_TO_MIME.get(target.kind, "application/vnd.google-apps.document")
        section = {
            TargetKind.DOCUMENT: "document",
            TargetKind.SPREADSHEET: "spreadsheets",
            TargetKind.PRESENTATION: "presentation",
            TargetKind.DRAWING: "drawings",
        }.get(target.kind, "document")

        url = f"https://docs.google.com/{section}/d/{target.id}/edit"
        response = await self._http.request("GET", url, item_id=target.id)
        name = parse_html_title(response.text) or target.id
        return DriveItem(
            id=target.id,
            name=name,
            mime_type=mime_type,
        )

    async def _fetch_file_metadata(self, target: ParsedTarget) -> DriveItem:
        params: dict[str, str] = {"id": target.id, "export": "download"}
        if target.resource_key:
            params["resourcekey"] = target.resource_key

        # Open a minimal Range probe so we don't pull multi-gigabyte bodies just to read headers.
        async with self._http.stream(
            "GET",
            USERCONTENT_DOWNLOAD_URL,
            params=params,
            headers={"Range": "bytes=0-0"},
            item_id=target.id,
        ) as response:
            if response.status_code >= 400 and response.status_code != 416:
                body = (await response.aread()).decode("utf-8", errors="replace")
                raise classify_http_error(
                    response.status_code,
                    body_snippet=body[:1024],
                    headers=response.headers,
                    item_id=target.id,
                )

            content_type = response.headers.get("content-type", "").lower()
            disposition = response.headers.get("content-disposition")
            filename = parse_content_disposition_filename(disposition)

            if filename or not content_type.startswith("text/html"):
                size = _extract_total_size(response.headers)
                etag = response.headers.get("etag")
                mime = content_type.split(";")[0].strip() or "application/octet-stream"
                return DriveItem(
                    id=target.id,
                    name=filename or target.id,
                    mime_type=mime,
                    size=size,
                    etag=etag,
                    web_download_url=str(response.url),
                )

            html_body = (await response.aread()).decode("utf-8", errors="replace")

        self._check_html_error_page(html_body, item_id=target.id)
        parsed_form = _parse_interstitial_html(html_body, target.id)
        if parsed_form is not None:
            return parsed_form

        # If target was a bare ID, check whether it's actually a folder.
        if target.kind == TargetKind.UNKNOWN:
            try:
                return await self._fetch_folder_metadata(target.id, target.resource_key)
            except Exception:
                pass

        title = parse_html_title(html_body) or target.id
        return DriveItem(
            id=target.id,
            name=title,
            mime_type="application/octet-stream",
        )

    async def list_folder_children(
        self,
        folder_id: str,
        *,
        resource_key: str | None = None,
    ) -> list[DriveItem]:
        embed_params: dict[str, str] = {"id": folder_id}
        if resource_key:
            embed_params["resourcekey"] = resource_key

        # Primary: embeddedfolderview is ~40x smaller than the main Drive SPA page,
        # exposes clean filenames without tooltip suffixes, and links explicitly distinguish
        # files (/file/d/), native docs (docs.google.com/<kind>/d/), and folders (/folders/).
        embed_resp = await self._http.request(
            "GET",
            EMBEDDED_FOLDER_URL,
            params=embed_params,
            item_id=folder_id,
        )
        items = _parse_embedded_folder_html(embed_resp.text, parent_id=folder_id)
        if items or "flip-entries" in embed_resp.text or "flip-embedded" in embed_resp.text:
            return items

        # Fallback: full Drive folder page (_drive_ivd JSON or [data-id] DOM rows).
        params: dict[str, str] = {}
        if resource_key:
            params["resourcekey"] = resource_key
        response = await self._http.request(
            "GET",
            f"{DRIVE_FOLDER_URL}/{folder_id}",
            params=params or None,
            item_id=folder_id,
        )
        return _parse_folder_html(response.text, parent_id=folder_id)

    @asynccontextmanager
    async def open_download_stream(
        self,
        item: DriveItem,
        *,
        start_byte: int = 0,
        end_byte: int | None = None,
    ) -> AsyncIterator[MediaStream]:
        url = item.web_download_url or USERCONTENT_DOWNLOAD_URL
        if not is_trusted_redirect_url(url):
            raise UntrustedRedirectError(
                f"Refusing to download from untrusted URL: {url}",
                item_id=item.id,
            )
        params: dict[str, str] | None = (
            None if item.web_download_url else {"id": item.id, "export": "download"}
        )

        # Check if we already have a download_warning confirmation cookie.
        for cookie_name, cookie_val in self._http.cookies.items():
            if (
                cookie_name.startswith("download_warning")
                and cookie_val
                and params is not None
            ):
                params["confirm"] = cookie_val

        range_headers: dict[str, str] = {}
        if start_byte > 0 or end_byte is not None:
            end_str = str(end_byte) if end_byte is not None else ""
            range_headers["Range"] = f"bytes={start_byte}-{end_str}"

        async with self._http.stream(
            "GET",
            url,
            params=params,
            headers=range_headers or None,
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

            content_type = response.headers.get("content-type", "").lower()
            disposition = response.headers.get("content-disposition")

            # If Google directly serves the file stream (not an HTML interstitial), yield it.
            if disposition or not content_type.startswith("text/html"):
                yield _build_media_stream(response, self._chunk_size)
                return

            # Otherwise we hit the virus-scan warning page; parse confirmation URL and re-open.
            html_body = (await response.aread()).decode("utf-8", errors="replace")

        self._check_html_error_page(html_body, item_id=item.id)
        confirm_url = _extract_confirm_download_url(html_body, item.id)
        item.web_download_url = confirm_url

        async with self._http.stream(
            "GET",
            confirm_url,
            headers=range_headers or None,
            item_id=item.id,
        ) as confirmed_resp:
            if confirmed_resp.status_code == 416:
                yield MediaStream(
                    chunks=_empty_stream(),
                    status_code=416,
                    is_range_satisfied=False,
                )
                return

            if confirmed_resp.status_code >= 400:
                body = (await confirmed_resp.aread()).decode("utf-8", errors="replace")
                raise classify_http_error(
                    confirmed_resp.status_code,
                    body_snippet=body[:1024],
                    headers=confirmed_resp.headers,
                    item_id=item.id,
                )

            c_type = confirmed_resp.headers.get("content-type", "").lower()
            c_disp = confirmed_resp.headers.get("content-disposition")
            if not c_disp and c_type.startswith("text/html"):
                err_html = (await confirmed_resp.aread()).decode("utf-8", errors="replace")
                self._check_html_error_page(err_html, item_id=item.id)
                raise PermissionDeniedError(
                    "Google Drive returned an HTML page instead of file contents; "
                    "the file may require sign-in or permissions.",
                    item_id=item.id,
                )

            yield _build_media_stream(confirmed_resp, self._chunk_size)

    @asynccontextmanager
    async def open_export_stream(
        self,
        item: DriveItem,
        export_mime: str,
    ) -> AsyncIterator[MediaStream]:
        fmt = _MIME_TO_SHORT_EXPORT.get(export_mime, "pdf")
        section = _doc_section_from_mime(item.mime_type)
        export_url = f"https://docs.google.com/{section}/d/{item.id}/export"

        async with self._http.stream(
            "GET",
            export_url,
            params={"format": fmt},
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
                body = (await response.aread()).decode("utf-8", errors="replace")
                self._check_html_error_page(body, item_id=item.id)
                raise PermissionDeniedError(
                    "Google Drive returned an HTML page instead of an exported document; "
                    "the document may require sign-in or permissions.",
                    item_id=item.id,
                )

            yield _build_media_stream(response, self._chunk_size)

    @staticmethod
    def _check_html_error_page(html_text: str, *, item_id: str) -> None:
        lower = html_text.lower()
        if (
            "too many users have viewed or downloaded this file" in lower
            or "download quota exceeded" in lower
            or "you can't view or download this file at this time" in lower
        ):
            raise QuotaExceededError(
                "Google Drive download quota exceeded for this file. "
                "Try again later or configure an API key / OAuth credentials.",
                item_id=item_id,
            )
        needs_access = (
            "you need access" in lower or "request access" in lower or "sign in" in lower
        )
        if needs_access and ("<form" not in lower or 'id="download-form"' not in lower):
            raise PermissionDeniedError(
                "File is not publicly shared (sign-in or access request required).",
                item_id=item_id,
            )
        if "sorry, the file you have requested does not exist" in lower:
            raise ResourceNotFoundError(
                "The requested file does not exist on Google Drive.",
                item_id=item_id,
            )


def _build_media_stream(response: Any, chunk_size: int) -> MediaStream:
    raw_len = response.headers.get("content-length")
    content_length = int(raw_len) if raw_len and raw_len.isdigit() else None
    etag = response.headers.get("etag")
    accept_ranges = response.headers.get("accept-ranges", "").lower() == "bytes"
    return MediaStream(
        chunks=response.aiter_bytes(chunk_size),
        status_code=response.status_code,
        content_length=content_length,
        etag=etag,
        accept_ranges=accept_ranges,
        is_range_satisfied=True,
    )


def _extract_total_size(headers: Any) -> int | None:
    content_range = headers.get("content-range")
    if content_range:
        match = _CONTENT_RANGE_TOTAL_RE.search(content_range)
        if match:
            return int(match.group(1))
    content_length = headers.get("content-length")
    if content_length and content_length.isdigit():
        return int(content_length)
    return None


def _parse_interstitial_html(html_text: str, item_id: str) -> DriveItem | None:
    """Extract file name, approximate size, and confirmed download URL from the warning page."""
    confirm_url = _extract_confirm_download_url(html_text, item_id, strict=True)
    name_match = _UC_NAME_SIZE_RE.search(html_text)
    if not name_match and not confirm_url:
        return None

    name = (
        html.unescape(name_match.group(1)).strip()
        if name_match
        else (parse_html_title(html_text) or item_id)
    )

    # The warning page only exposes an approximate human-readable size, so leave
    # the exact size to the download response headers.
    return DriveItem(
        id=item_id,
        name=name,
        mime_type="application/octet-stream",
        size=None,
        web_download_url=confirm_url,
    )


def _extract_confirm_download_url(
    html_text: str,
    item_id: str,
    *,
    strict: bool = False,
) -> str:
    soup = BeautifulSoup(html_text, "html.parser")
    form = soup.find("form", id="download-form") or soup.find("form", action=True)
    if form is not None:
        action = str(form.get("action") or USERCONTENT_DOWNLOAD_URL)
        if action.startswith("/"):
            action = f"https://drive.google.com{action}"
        if not is_trusted_redirect_url(action):
            action = USERCONTENT_DOWNLOAD_URL
        params: dict[str, str] = {}
        for inp in form.find_all("input"):
            inp_name = inp.get("name")
            inp_val = inp.get("value")
            if inp_name and inp_val is not None:
                params[str(inp_name)] = str(inp_val)
        params.setdefault("id", item_id)
        params.setdefault("export", "download")
        params.setdefault("confirm", "t")
        sep = "&" if "?" in action else "?"
        return f"{action}{sep}{urlencode(params)}"

    # Legacy uc-download-link anchor fallback.
    link = soup.find("a", id="uc-download-link")
    if link is not None and link.get("href"):
        href = str(link["href"])
        if href.startswith("/"):
            return f"https://drive.google.com{href}"
        return href

    if strict:
        return ""

    default_params = {"id": item_id, "export": "download", "confirm": "t"}
    return f"{USERCONTENT_DOWNLOAD_URL}?{urlencode(default_params)}"


def _decode_js_string(raw: str) -> str:
    """Decode the JavaScript string escapes used in Drive's ``_DRIVE_ivd`` payload."""
    decoded = _JS_HEX_ESCAPE_RE.sub(
        lambda match: chr(int(match.group(1) or match.group(2), 16)),
        raw,
    )
    return (
        decoded.replace("\\/", "/")
        .replace("\\'", "'")
        .replace('\\"', '"')
        .replace("\\\\", "\\")
    )


def _parse_folder_html(html_text: str, *, parent_id: str) -> list[DriveItem]:
    """Extract folder children from Drive folder HTML (`_drive_ivd` or DOM nodes)."""
    items: list[DriveItem] = []

    ivd_match = _IVD_SCRIPT_RE.search(html_text)
    if ivd_match:
        raw_escaped = ivd_match.group(1)
        try:
            payload = json.loads(_decode_js_string(raw_escaped))
            entries = payload[0] if isinstance(payload, list) and payload else []
            if isinstance(entries, list):
                for entry in entries:
                    if not isinstance(entry, list) or len(entry) < 4:
                        continue
                    child_id = str(entry[0])
                    name = str(entry[2])
                    mime = str(entry[3])
                    size: int | None = None
                    if len(entry) > 13 and isinstance(entry[13], int | str):
                        try:
                            size = int(entry[13])
                        except ValueError:
                            size = None
                    items.append(
                        DriveItem(
                            id=child_id,
                            name=name,
                            mime_type=mime,
                            size=size,
                            parents=[parent_id],
                        )
                    )
                if items:
                    return items
        except Exception:
            pass

    soup = BeautifulSoup(html_text, "html.parser")
    seen: set[str] = set()
    for elem in soup.select("[data-id]"):
        child_id = str(elem.get("data-id") or "").strip()
        if not child_id or child_id == parent_id or child_id in seen or child_id.startswith("_"):
            continue
        seen.add(child_id)

        title_el = elem.select_one("[data-tooltip]") or elem
        raw_name = (
            elem.get("data-name")
            or title_el.get("data-tooltip")
            or elem.get_text(strip=True)
            or child_id
        )
        name = str(raw_name).strip()
        # Clean Google Drive tooltip suffixes like "Shared folder" or "PDF".
        if "\n" in name:
            name = name.splitlines()[0].strip()

        mime = str(elem.get("data-mime-type") or "").strip()
        if not mime:
            target_type = str(elem.get("data-target") or elem.get("data-type") or "").lower()
            if "folder" in target_type or name.endswith(" Shared folder"):
                mime = FOLDER_MIME_TYPE
                name = name.removesuffix(" Shared folder").strip()
            else:
                mime = "application/octet-stream"

        raw_size = elem.get("data-size")
        size = int(str(raw_size)) if raw_size and str(raw_size).isdigit() else None
        items.append(
            DriveItem(
                id=child_id,
                name=name,
                mime_type=mime,
                size=size,
                parents=[parent_id],
            )
        )

    return items


_EMBED_FILE_RE = re.compile(r"/file/d/([A-Za-z0-9_-]{10,128})")
_EMBED_FOLDER_RE = re.compile(r"/folders/([A-Za-z0-9_-]{10,128})")
_EMBED_DOCS_RE = re.compile(r"https?://docs\.google\.com/(\w+)/d/([A-Za-z0-9_-]{10,128})")


def _parse_embedded_folder_html(html_text: str, *, parent_id: str) -> list[DriveItem]:
    soup = BeautifulSoup(html_text, "html.parser")
    items: list[DriveItem] = []
    seen: set[str] = set()

    for a_tag in soup.find_all("a", href=True):
        href = str(a_tag.get("href") or "")
        name = a_tag.get_text(strip=True)

        file_match = _EMBED_FILE_RE.search(href)
        if file_match:
            child_id = file_match.group(1)
            if child_id not in seen:
                seen.add(child_id)
                items.append(
                    DriveItem(
                        id=child_id,
                        name=name or child_id,
                        mime_type="application/octet-stream",
                        parents=[parent_id],
                    )
                )
            continue

        docs_match = _EMBED_DOCS_RE.search(href)
        if docs_match:
            kind, child_id = docs_match.group(1), docs_match.group(2)
            if child_id not in seen:
                seen.add(child_id)
                singular = kind.removesuffix("s")
                items.append(
                    DriveItem(
                        id=child_id,
                        name=name or child_id,
                        mime_type=f"application/vnd.google-apps.{singular}",
                        parents=[parent_id],
                    )
                )
            continue

        folder_match = _EMBED_FOLDER_RE.search(href)
        if folder_match:
            child_id = folder_match.group(1)
            if child_id != parent_id and child_id not in seen:
                seen.add(child_id)
                items.append(
                    DriveItem(
                        id=child_id,
                        name=name or child_id,
                        mime_type=FOLDER_MIME_TYPE,
                        parents=[parent_id],
                    )
                )
            continue

    if items:
        return items

    for entry in soup.select(".flip-entry"):
        entry_id = str(entry.get("id") or "")
        if not entry_id.startswith("entry-"):
            continue
        child_id = entry_id.removeprefix("entry-")
        if child_id in seen:
            continue
        seen.add(child_id)
        title_el = entry.select_one(".flip-entry-title")
        name = title_el.get_text(strip=True) if title_el else child_id
        link_el = entry.find("a", href=True)
        href = str(link_el["href"]) if link_el else ""
        is_folder = "/folders/" in href or "folder" in str(entry.get("class") or "")
        mime = FOLDER_MIME_TYPE if is_folder else "application/octet-stream"
        items.append(
            DriveItem(
                id=child_id,
                name=name,
                mime_type=mime,
                parents=[parent_id],
            )
        )

    return items


def _doc_section_from_mime(mime_type: str) -> str:
    if "spreadsheet" in mime_type:
        return "spreadsheets"
    if "presentation" in mime_type:
        return "presentation"
    if "drawing" in mime_type:
        return "drawings"
    return "document"


async def _empty_stream() -> AsyncIterator[bytes]:
    if False:  # pragma: no cover
        yield b""

