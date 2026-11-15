"""Execution planner turning resolved Drive items or folder trees into DownloadTasks."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from gdown2.domain.models import (
    DownloadTask,
    DriveItem,
    FolderNode,
    TaskState,
    TransferState,
)
from gdown2.domain.path_policy import PathPolicy
from gdown2.domain.policies import ExistingFilePolicy
from gdown2.domain.tree import walk_files, walk_folders_bfs


@dataclass(slots=True)
class DownloadPlan:
    """Ordered directories to create and download tasks to execute."""

    root_dir: Path
    directories: list[Path] = field(default_factory=list)
    tasks: list[DownloadTask] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(t.bytes_total or 0 for t in self.tasks)


class Planner:
    """Produces a deterministic :class:`DownloadPlan` from domain objects."""

    def __init__(
        self,
        path_policy: PathPolicy | None = None,
        existing_policy: ExistingFilePolicy = ExistingFilePolicy.RESUME,
        flat: bool = False,
        export_format: str | None = None,
    ) -> None:
        self._path_policy = path_policy or PathPolicy()
        self._existing_policy = existing_policy
        self._flat = flat
        self._export_format = export_format

    def plan_single(
        self,
        item: DriveItem,
        output: Path,
        *,
        saved_states: dict[str, TransferState] | None = None,
    ) -> DownloadPlan:
        """Create a plan for a single file or Google Workspace document."""
        forced_ext: str | None = None
        if item.is_google_doc:
            _, forced_ext = item.resolve_export(self._export_format)

        # If output is an existing directory (or ends with a separator), place the file inside it.
        is_directory_output = output.is_dir() or str(output).endswith(("/", "\\"))
        if is_directory_output:
            root_dir = output.resolve()
            rel = self._path_policy.allocate_child_path(
                PurePosixPath("."),
                item.name,
                forced_extension=forced_ext,
            )
            local_path = self._path_policy.resolve_local_path(root_dir, rel)
        else:
            root_dir = output.parent.resolve()
            rel = PurePosixPath(self._path_policy.sanitize_segment(output.name, fallback=item.name))
            if forced_ext and not rel.name.lower().endswith(forced_ext.lower()):
                rel = PurePosixPath(f"{rel.name}{forced_ext}")
            local_path = self._path_policy.resolve_local_path(root_dir, rel)

        task = self._build_task(item, local_path, rel, saved_states=saved_states)
        return DownloadPlan(
            root_dir=root_dir,
            directories=[root_dir],
            tasks=[task],
        )

    def plan_tree(
        self,
        tree: FolderNode,
        output_dir: Path,
        *,
        saved_states: dict[str, TransferState] | None = None,
    ) -> DownloadPlan:
        """Create a plan for an entire :class:`FolderNode` hierarchy."""
        root_dir = output_dir.resolve()
        directories: list[Path] = [root_dir]
        tasks: list[DownloadTask] = []

        if not self._flat:
            for folder in walk_folders_bfs(tree):
                folder_path = self._path_policy.resolve_local_path(root_dir, folder.relative_path)
                if folder_path not in directories:
                    directories.append(folder_path)

        flat_policy = PathPolicy()
        for entry in walk_files(tree):
            if self._flat:
                forced_ext: str | None = None
                if entry.item.is_google_doc:
                    _, forced_ext = entry.item.resolve_export(self._export_format)
                rel = flat_policy.allocate_child_path(
                    PurePosixPath("."),
                    entry.item.name,
                    forced_extension=forced_ext,
                )
            else:
                rel = entry.relative_path

            local_path = self._path_policy.resolve_local_path(root_dir, rel)
            task = self._build_task(entry.item, local_path, rel, saved_states=saved_states)
            tasks.append(task)

        return DownloadPlan(
            root_dir=root_dir,
            directories=directories,
            tasks=tasks,
        )

    def _build_task(
        self,
        item: DriveItem,
        local_path: Path,
        relative_path: PurePosixPath,
        *,
        saved_states: dict[str, TransferState] | None = None,
    ) -> DownloadTask:
        task = DownloadTask(
            item=item,
            local_path=local_path,
            relative_path=relative_path,
            bytes_total=item.size,
            etag=item.etag,
            export_format=self._export_format if item.is_google_doc else None,
        )

        # Check if the final file already exists on disk.
        if local_path.exists() and local_path.is_file():
            existing_size = local_path.stat().st_size
            if self._existing_policy == ExistingFilePolicy.SKIP:
                task.state = TaskState.SKIPPED
                task.bytes_done = existing_size
                return task
            if self._existing_policy == ExistingFilePolicy.RESUME and self._is_complete(
                item, local_path, existing_size
            ):
                task.state = TaskState.SKIPPED
                task.bytes_done = existing_size
                return task

        # Overwriting must never resume stale bytes from a previous transfer.
        if self._existing_policy == ExistingFilePolicy.OVERWRITE and (
            task.part_path.exists() or task.mpart_path.exists()
        ):
            task.stale_part = True
            task.resume_offset = 0
            task.bytes_done = 0
            return task

        # Interrupted multipart files contain sparse holes and cannot be linearly resumed.
        if task.mpart_path.exists():
            task.stale_part = True

        # Inspect .part file and saved TransferState for mid-stream resume.
        if self._existing_policy == ExistingFilePolicy.RESUME and task.part_path.exists():
            part_size = task.part_path.stat().st_size
            saved = (saved_states or {}).get(task.task_id)
            if saved and saved.etag and item.etag and saved.etag != item.etag:
                # Remote file changed since last session; discard stale partial data.
                task.stale_part = True
                task.resume_offset = 0
                task.bytes_done = 0
            else:
                task.stale_part = False
                task.resume_offset = part_size
                task.bytes_done = part_size
                if saved and saved.etag and not task.etag:
                    task.etag = saved.etag

        return task

    @staticmethod
    def _is_complete(item: DriveItem, local_path: Path, existing_size: int) -> bool:
        if item.size is None or existing_size != item.size:
            return False
        if not item.md5:
            return True
        hasher = hashlib.md5()
        with local_path.open("rb") as handle:
            while chunk := handle.read(256 * 1024):
                hasher.update(chunk)
        return hasher.hexdigest().lower() == item.md5.lower()

