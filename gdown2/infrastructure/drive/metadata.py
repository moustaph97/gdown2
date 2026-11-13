"""Normalization utilities converting API JSON and web HTML into DriveItem objects."""

from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Any
from urllib.parse import unquote

from gdown2.domain.models import FOLDER_MIME_TYPE, DriveItem
from gdown2.domain.url_parser import TargetKind

_FILENAME_STAR_RE = re.compile(r"filename\*\s*=\s*([^']*)'[^']*'([^;\s]+)", re.IGNORECASE)
_FILENAME_QUOTED_RE = re.compile(r'filename\s*=\s*"([^"]+)"', re.IGNORECASE)
_FILENAME_BARE_RE = re.compile(r"filename\s*=\s*([^;\s]+)", re.IGNORECASE)
_TITLE_TAG_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_DRIVE_TITLE_SUFFIX_RE = re.compile(
    r"\s*-\s*Google\s+(?:Drive|Docs|Sheets|Slides|Drawings)\s*$",
    re.IGNORECASE,
)

KIND_TO_MIME: dict[TargetKind, str] = {
    TargetKind.FOLDER: FOLDER_MIME_TYPE,
    TargetKind.DOCUMENT: "application/vnd.google-apps.document",
    TargetKind.SPREADSHEET: "application/vnd.google-apps.spreadsheet",
    TargetKind.PRESENTATION: "application/vnd.google-apps.presentation",
    TargetKind.DRAWING: "application/vnd.google-apps.drawing",
}


def drive_item_from_api_json(payload: dict[str, Any]) -> DriveItem:
    """Convert a Google Drive API v3 ``File`` resource dict into a :class:`DriveItem`."""
    raw_size = payload.get("size")
    size = int(raw_size) if raw_size is not None else None

    mod_time: datetime | None = None
    raw_mtime = payload.get("modifiedTime")
    if isinstance(raw_mtime, str) and raw_mtime:
        try:
            mod_time = datetime.fromisoformat(raw_mtime.replace("Z", "+00:00"))
        except ValueError:
            mod_time = None

    shortcut = payload.get("shortcutDetails") or {}
    export_links = payload.get("exportLinks") or {}

    return DriveItem(
        id=str(payload["id"]),
        name=str(payload.get("name") or payload["id"]),
        mime_type=str(payload.get("mimeType") or "application/octet-stream"),
        size=size,
        md5=payload.get("md5Checksum"),
        parents=[str(p) for p in payload.get("parents", [])],
        modified_time=mod_time,
        etag=payload.get("etag") or payload.get("headRevisionId"),
        shortcut_target_id=shortcut.get("targetId"),
        shortcut_target_mime=shortcut.get("targetMimeType"),
        export_formats=list(export_links.keys()),
        web_download_url=payload.get("webContentLink"),
    )


def parse_content_disposition_filename(header_value: str | None) -> str | None:
    """Extract a filename from an HTTP ``Content-Disposition`` header."""
    if not header_value:
        return None

    star_match = _FILENAME_STAR_RE.search(header_value)
    if star_match:
        encoding = star_match.group(1) or "utf-8"
        encoded_name = star_match.group(2)
        try:
            return unquote(encoded_name, encoding=encoding, errors="replace")
        except LookupError:
            return unquote(encoded_name)

    quoted_match = _FILENAME_QUOTED_RE.search(header_value)
    if quoted_match:
        return quoted_match.group(1).strip()

    bare_match = _FILENAME_BARE_RE.search(header_value)
    if bare_match:
        return bare_match.group(1).strip().strip("'\"")

    return None


def parse_html_title(html_text: str) -> str | None:
    """Extract the resource name from a Google Drive HTML ``<title>`` element."""
    match = _TITLE_TAG_RE.search(html_text)
    if not match:
        return None
    raw_title = html.unescape(match.group(1)).strip()
    cleaned = _DRIVE_TITLE_SUFFIX_RE.sub("", raw_title).strip()
    if not cleaned or cleaned.lower() in {
        "google drive",
        "google drive - virus scan warning",
        "virus scan warning",
        "sign in - google accounts",
        "error 404 (not found)!!1",
    }:
        return None
    return cleaned

