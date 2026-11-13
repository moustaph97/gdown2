"""Filesystem storage sink with .part temporary files and atomic promotion."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import BinaryIO, cast

from gdown2.domain.errors import InsufficientDiskSpaceError, StorageError


class FileSystemSink:
    """Manages directory creation, partial file writes, and atomic renames."""

    def __init__(
        self,
        check_disk_space: bool = True,
        safety_margin_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        self._check_disk_space = check_disk_space
        self._safety_margin = safety_margin_bytes

    def ensure_directories(self, directories: list[Path]) -> None:
        """Create all target directories on disk before workers start."""
        for directory in directories:
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise StorageError(f"Failed to create directory {directory}: {exc}") from exc

    def assert_disk_space(self, directory: Path, required_bytes: int) -> None:
        """Ensure ``directory`` has at least ``required_bytes + safety_margin`` free."""
        if not self._check_disk_space or required_bytes <= 0:
            return
        probe = directory
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        try:
            usage = shutil.disk_usage(probe)
        except OSError:
            return
        needed = required_bytes + self._safety_margin
        if usage.free < needed:
            raise InsufficientDiskSpaceError(
                f"Insufficient disk space in {probe}: need {needed} bytes, "
                f"only {usage.free} bytes free."
            )

    def open_part(self, part_path: Path, *, append: bool = False) -> BinaryIO:
        """Open a ``.part`` temporary file for writing or appending."""
        try:
            part_path.parent.mkdir(parents=True, exist_ok=True)
            mode = "ab" if append and part_path.exists() else "wb"
            return cast(BinaryIO, part_path.open(mode))
        except OSError as exc:
            raise StorageError(f"Cannot open partial file {part_path}: {exc}") from exc

    def open_random_access_part(self, part_path: Path, total_size: int) -> BinaryIO:
        """Pre-allocate a ``.part`` file for multi-Range random-access writes."""
        try:
            part_path.parent.mkdir(parents=True, exist_ok=True)
            with part_path.open("wb") as fh:
                if total_size > 0:
                    fh.truncate(total_size)
            return part_path.open("r+b")
        except OSError as exc:
            raise StorageError(f"Cannot preallocate partial file {part_path}: {exc}") from exc

    def promote_part(self, part_path: Path, final_path: Path) -> None:
        """Atomically rename ``part_path`` to ``final_path`` via ``os.replace``."""
        try:
            final_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(part_path, final_path)
        except OSError as exc:
            raise StorageError(
                f"Atomic rename failed ({part_path} -> {final_path}): {exc}"
            ) from exc

    def discard_part(self, part_path: Path) -> None:
        """Remove a stale or corrupted ``.part`` file if it exists."""
        try:
            if part_path.exists():
                part_path.unlink()
        except OSError as exc:
            raise StorageError(f"Failed to remove partial file {part_path}: {exc}") from exc

