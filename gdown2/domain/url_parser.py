"""URL and identifier parser for Google Drive and Google Workspace links.

Google Drive has accumulated half a dozen URL layouts over the years:
- ``https://drive.google.com/file/d/<id>/view?usp=sharing``
- ``https://drive.google.com/drive/u/0/folders/<id>?resourcekey=...``
- ``https://drive.google.com/open?id=<id>``
- ``https://drive.google.com/uc?export=download&id=<id>``
- ``https://drive.usercontent.google.com/download?id=<id>&confirm=t``
- ``https://docs.google.com/{document,spreadsheets,presentation,drawings}/d/<id>/edit``
- Raw resource IDs passed directly on the command line.

This module normalizes all of them into a :class:`ParsedTarget` without touching
the network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import parse_qs, urlparse

from gdown2.domain.errors import InvalidUrlError

ALLOWED_GOOGLE_HOSTS: frozenset[str] = frozenset(
    {
        "drive.google.com",
        "docs.google.com",
        "drive.usercontent.google.com",
        "www.googleapis.com",
        "content.googleapis.com",
    }
)

# Drive IDs are base64url-like tokens, typically 19 to 64 characters long,
# though older workspace IDs can occasionally be down to 10 characters.
_RAW_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,128}$")

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"/file/d/([A-Za-z0-9_-]{10,128})"), "file"),
    (re.compile(r"/folders/([A-Za-z0-9_-]{10,128})"), "folder"),
    (re.compile(r"/document/d/([A-Za-z0-9_-]{10,128})"), "document"),
    (re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]{10,128})"), "spreadsheet"),
    (re.compile(r"/presentation/d/([A-Za-z0-9_-]{10,128})"), "presentation"),
    (re.compile(r"/drawings/d/([A-Za-z0-9_-]{10,128})"), "drawing"),
]


class TargetKind(StrEnum):
    """Best-effort hint inferred from the URL structure."""

    UNKNOWN = "unknown"
    FILE = "file"
    FOLDER = "folder"
    DOCUMENT = "document"
    SPREADSHEET = "spreadsheet"
    PRESENTATION = "presentation"
    DRAWING = "drawing"


@dataclass(frozen=True, slots=True)
class ParsedTarget:
    """Normalized representation of a user-supplied URL or Drive ID."""

    id: str
    kind: TargetKind = TargetKind.UNKNOWN
    resource_key: str | None = None
    export_format: str | None = None
    original_input: str = ""

    @property
    def is_folder_hint(self) -> bool:
        return self.kind == TargetKind.FOLDER

    @property
    def is_google_doc_hint(self) -> bool:
        return self.kind in {
            TargetKind.DOCUMENT,
            TargetKind.SPREADSHEET,
            TargetKind.PRESENTATION,
            TargetKind.DRAWING,
        }


def parse_target(url_or_id: str) -> ParsedTarget:
    """Parse a Google Drive URL or bare ID into a :class:`ParsedTarget`.

    Raises:
        InvalidUrlError: If no valid Drive resource ID can be extracted.
    """
    raw = url_or_id.strip()
    if not raw:
        raise InvalidUrlError("Empty URL or file ID provided.")

    # Fast path: bare ID without scheme or slashes.
    if "://" not in raw and "/" not in raw and "?" not in raw and "." not in raw:
        if _RAW_ID_RE.match(raw):
            return ParsedTarget(id=raw, kind=TargetKind.UNKNOWN, original_input=raw)
        raise InvalidUrlError(f"Invalid Google Drive ID: {raw!r}")

    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    if parsed.scheme not in {"http", "https"}:
        raise InvalidUrlError(f"Unsupported URL scheme {parsed.scheme!r} in {raw!r}")

    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_GOOGLE_HOSTS and not host.endswith(".googleusercontent.com"):
        raise InvalidUrlError(f"Unsupported host {host!r}; expected a Google Drive domain.")

    query = parse_qs(parsed.query)
    resource_key = _first(query, "resourcekey")
    export_fmt = _first(query, "format")

    for pattern, kind_str in _PATTERNS:
        match = pattern.search(parsed.path)
        if match:
            item_id = match.group(1)
            return ParsedTarget(
                id=item_id,
                kind=TargetKind(kind_str),
                resource_key=resource_key,
                export_format=export_fmt,
                original_input=raw,
            )

    query_id = _first(query, "id")
    if query_id and _RAW_ID_RE.match(query_id):
        return ParsedTarget(
            id=query_id,
            kind=TargetKind.FILE,
            resource_key=resource_key,
            export_format=export_fmt,
            original_input=raw,
        )

    raise InvalidUrlError(f"Could not extract a Google Drive ID from {raw!r}")


def extract_id(url_or_id: str) -> str:
    """Convenience helper returning only the resource ID string."""
    return parse_target(url_or_id).id


def _first(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    if values and values[0]:
        return values[0]
    return None

