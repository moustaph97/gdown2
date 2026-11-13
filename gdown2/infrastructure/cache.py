"""TTL metadata cache backed by memory and optional JSON disk persistence."""

from __future__ import annotations

import contextlib
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from gdown2.domain.models import DriveItem


class DiskMetadataCache:
    """Stores resolved :class:`DriveItem` entries with a time-to-live (TTL)."""

    def __init__(
        self,
        cache_file: Path | None = None,
        *,
        ttl_seconds: float = 3600.0,
    ) -> None:
        self._cache_file = cache_file
        self._ttl = ttl_seconds
        self._entries: dict[str, tuple[float, DriveItem]] = {}
        if self._cache_file is not None:
            self._load()

    def get(self, item_id: str) -> DriveItem | None:
        record = self._entries.get(item_id)
        if record is None:
            return None
        expires_at, item = record
        if time.time() > expires_at:
            del self._entries[item_id]
            return None
        return item

    def put(self, item: DriveItem, *, persist: bool = True) -> None:
        if self._ttl <= 0:
            return
        self._entries[item.id] = (time.time() + self._ttl, item)
        if persist and self._cache_file is not None:
            self._save()

    def flush(self) -> None:
        """Persist buffered entries to disk in a single write."""
        if self._cache_file is not None:
            self._save()

    def clear(self) -> None:
        self._entries.clear()
        if self._cache_file is not None and self._cache_file.exists():
            with contextlib.suppress(OSError):
                self._cache_file.unlink()

    def _load(self) -> None:
        assert self._cache_file is not None
        if not self._cache_file.exists():
            return
        try:
            raw = json.loads(self._cache_file.read_text(encoding="utf-8"))
            now = time.time()
            for item_id, entry in raw.items():
                expires_at = float(entry["expires_at"])
                if expires_at <= now:
                    continue
                item = _deserialize_item(entry["item"])
                self._entries[item_id] = (expires_at, item)
        except Exception:
            self._entries.clear()

    def _save(self) -> None:
        assert self._cache_file is not None
        try:
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)
            now = time.time()
            payload = {
                item_id: {
                    "expires_at": expires_at,
                    "item": _serialize_item(item),
                }
                for item_id, (expires_at, item) in self._entries.items()
                if expires_at > now
            }
            self._cache_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError:
            pass


def _serialize_item(item: DriveItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "name": item.name,
        "mime_type": item.mime_type,
        "size": item.size,
        "md5": item.md5,
        "parents": item.parents,
        "modified_time": item.modified_time.isoformat() if item.modified_time else None,
        "etag": item.etag,
        "shortcut_target_id": item.shortcut_target_id,
        "shortcut_target_mime": item.shortcut_target_mime,
        "export_formats": item.export_formats,
        "web_download_url": item.web_download_url,
        "resource_key": item.resource_key,
    }


def _deserialize_item(data: dict[str, Any]) -> DriveItem:
    mtime = (
        datetime.fromisoformat(data["modified_time"])
        if data.get("modified_time")
        else None
    )
    return DriveItem(
        id=str(data["id"]),
        name=str(data["name"]),
        mime_type=str(data.get("mime_type", "application/octet-stream")),
        size=int(data["size"]) if data.get("size") is not None else None,
        md5=data.get("md5"),
        parents=list(data.get("parents", [])),
        modified_time=mtime,
        etag=data.get("etag"),
        shortcut_target_id=data.get("shortcut_target_id"),
        shortcut_target_mime=data.get("shortcut_target_mime"),
        export_formats=list(data.get("export_formats", [])),
        web_download_url=data.get("web_download_url"),
        resource_key=data.get("resource_key"),
    )

