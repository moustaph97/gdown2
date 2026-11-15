"""Core domain entities for gdown2.

All classes in this module are pure data structures with zero I/O dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
SHORTCUT_MIME_TYPE = "application/vnd.google-apps.shortcut"
GOOGLE_APPS_PREFIX = "application/vnd.google-apps."

# Default export format and extension for Google Workspace native documents.
DEFAULT_EXPORT_FORMATS: dict[str, tuple[str, str]] = {
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docx",
    ),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pptx",
    ),
    "application/vnd.google-apps.drawing": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.script": ("application/vnd.google-apps.script+json", ".json"),
}

# Short format aliases accepted on the CLI (e.g. --export-format pdf).
EXPORT_FORMAT_ALIASES: dict[str, tuple[str, str]] = {
    "docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docx",
    ),
    "xlsx": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    "pptx": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pptx",
    ),
    "pdf": ("application/pdf", ".pdf"),
    "txt": ("text/plain", ".txt"),
    "csv": ("text/csv", ".csv"),
    "tsv": ("text/tab-separated-values", ".tsv"),
    "html": ("text/html", ".html"),
    "odt": ("application/vnd.oasis.opendocument.text", ".odt"),
    "ods": ("application/vnd.oasis.opendocument.spreadsheet", ".ods"),
    "odp": ("application/vnd.oasis.opendocument.presentation", ".odp"),
    "epub": ("application/epub+zip", ".epub"),
}


@dataclass(frozen=True, slots=True)
class Checksum:
    """Hash digest associated with a remote or local file."""

    algorithm: str
    hex_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "algorithm", self.algorithm.lower().strip())
        object.__setattr__(self, "hex_digest", self.hex_digest.lower().strip())


@dataclass(slots=True)
class DriveItem:
    """Metadata for a single Google Drive resource (file, folder, or native doc)."""

    id: str
    name: str
    mime_type: str = "application/octet-stream"
    size: int | None = None
    md5: str | None = None
    parents: list[str] = field(default_factory=list)
    modified_time: datetime | None = None
    etag: str | None = None
    shortcut_target_id: str | None = None
    shortcut_target_mime: str | None = None
    export_formats: list[str] = field(default_factory=list)
    web_download_url: str | None = None
    resource_key: str | None = None

    @property
    def is_folder(self) -> bool:
        return self.mime_type == FOLDER_MIME_TYPE

    @property
    def is_shortcut(self) -> bool:
        return self.mime_type == SHORTCUT_MIME_TYPE

    @property
    def is_google_doc(self) -> bool:
        return (
            self.mime_type.startswith(GOOGLE_APPS_PREFIX)
            and not self.is_folder
            and not self.is_shortcut
        )

    @property
    def checksum(self) -> Checksum | None:
        if self.md5:
            return Checksum(algorithm="md5", hex_digest=self.md5)
        return None

    def resolve_export(self, preferred_format: str | None = None) -> tuple[str, str]:
        """Return ``(export_mime_type, file_extension)`` for a Google Workspace doc."""
        if preferred_format:
            key = preferred_format.lower().lstrip(".")
            if key in EXPORT_FORMAT_ALIASES:
                return EXPORT_FORMAT_ALIASES[key]
        if self.mime_type in DEFAULT_EXPORT_FORMATS:
            return DEFAULT_EXPORT_FORMATS[self.mime_type]
        return ("application/pdf", ".pdf")


@dataclass(slots=True)
class FileEntry:
    """A leaf file positioned inside a resolved folder tree."""

    item: DriveItem
    relative_path: PurePosixPath
    depth: int = 0


@dataclass(slots=True)
class FolderNode:
    """Recursive representation of a Drive folder hierarchy."""

    item: DriveItem
    relative_path: PurePosixPath = field(default_factory=lambda: PurePosixPath("."))
    depth: int = 0
    files: list[FileEntry] = field(default_factory=list)
    subfolders: list[FolderNode] = field(default_factory=list)

    @property
    def children(self) -> list[FolderNode | FileEntry]:
        """Combined list of subfolders followed by files for tree traversal."""
        return [*self.subfolders, *self.files]


class TaskState(StrEnum):
    """Lifecycle states of a download task."""

    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class DownloadTask:
    """Concrete unit of work executed by a download worker."""

    item: DriveItem
    local_path: Path
    relative_path: PurePosixPath = field(default_factory=lambda: PurePosixPath("."))
    state: TaskState = TaskState.PENDING
    bytes_done: int = 0
    bytes_total: int | None = None
    attempts: int = 0
    etag: str | None = None
    resume_offset: int = 0
    export_format: str | None = None
    error_message: str | None = None
    stale_part: bool = False

    @property
    def task_id(self) -> str:
        return f"{self.item.id}:{self.relative_path.as_posix()}"

    @property
    def part_path(self) -> Path:
        return self.local_path.with_name(f"{self.local_path.name}.part")

    @property
    def mpart_path(self) -> Path:
        return self.local_path.with_name(f"{self.local_path.name}.mpart")


@dataclass(slots=True)
class TransferState:
    """Serializable snapshot of an in-progress or completed transfer for session resume."""

    task_key: str
    item_id: str
    name: str
    mime_type: str
    local_path: str
    relative_path: str
    bytes_total: int | None = None
    resume_offset: int = 0
    etag: str | None = None
    md5: str | None = None
    state: str = TaskState.PENDING.value
    export_format: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_key": self.task_key,
            "item_id": self.item_id,
            "name": self.name,
            "mime_type": self.mime_type,
            "local_path": self.local_path,
            "relative_path": self.relative_path,
            "bytes_total": self.bytes_total,
            "resume_offset": self.resume_offset,
            "etag": self.etag,
            "md5": self.md5,
            "state": self.state,
            "export_format": self.export_format,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TransferState:
        return cls(
            task_key=str(data["task_key"]),
            item_id=str(data["item_id"]),
            name=str(data["name"]),
            mime_type=str(data.get("mime_type", "application/octet-stream")),
            local_path=str(data["local_path"]),
            relative_path=str(data.get("relative_path", data["name"])),
            bytes_total=int(data["bytes_total"]) if data.get("bytes_total") is not None else None,
            resume_offset=int(data.get("resume_offset", 0)),
            etag=data.get("etag"),
            md5=data.get("md5"),
            state=str(data.get("state", TaskState.PENDING.value)),
            export_format=data.get("export_format"),
        )

    @classmethod
    def from_task(cls, task: DownloadTask) -> TransferState:
        return cls(
            task_key=task.task_id,
            item_id=task.item.id,
            name=task.item.name,
            mime_type=task.item.mime_type,
            local_path=str(task.local_path),
            relative_path=task.relative_path.as_posix(),
            bytes_total=task.bytes_total,
            resume_offset=task.bytes_done,
            etag=task.etag or task.item.etag,
            md5=task.item.md5,
            state=task.state.value,
            export_format=task.export_format,
        )

