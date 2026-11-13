"""gdown2 — Fast, resumable Google Drive file and folder downloader."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from gdown2.domain.models import DownloadTask, DriveItem, FolderNode, TaskState
from gdown2.sdk import download, download_sync, get_info, get_tree, resume_directory

try:
    __version__ = version("gdown2")
except PackageNotFoundError:  # pragma: no cover - source checkout without installation
    __version__ = "0.2.0"

__all__ = [
    "DownloadTask",
    "DriveItem",
    "FolderNode",
    "TaskState",
    "__version__",
    "download",
    "download_sync",
    "get_info",
    "get_tree",
    "resume_directory",
]

