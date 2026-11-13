"""Operations on remote Drive folder trees.

Provides traversal helpers, summary statistics, and plain-text tree formatting
without any network or disk access.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass

from gdown2.domain.models import FileEntry, FolderNode


@dataclass(frozen=True, slots=True)
class TreeStats:
    """Aggregate counts and byte size for a :class:`FolderNode`."""

    folder_count: int
    file_count: int
    google_doc_count: int
    total_bytes: int


def walk_folders_bfs(root: FolderNode) -> Iterator[FolderNode]:
    """Yield all folders in breadth-first order starting at ``root``."""
    queue: deque[FolderNode] = deque([root])
    while queue:
        node = queue.popleft()
        yield node
        queue.extend(node.subfolders)


def walk_files(root: FolderNode) -> Iterator[FileEntry]:
    """Yield every :class:`FileEntry` in deterministic depth-first order."""
    yield from sorted(root.files, key=lambda f: f.relative_path.name.lower())
    for subfolder in sorted(root.subfolders, key=lambda d: d.relative_path.name.lower()):
        yield from walk_files(subfolder)


def compute_tree_stats(root: FolderNode) -> TreeStats:
    """Calculate total folders, files, Google Docs, and known byte size."""
    folders = 0
    files = 0
    docs = 0
    total_bytes = 0

    for folder in walk_folders_bfs(root):
        if folder is not root:
            folders += 1
        for entry in folder.files:
            files += 1
            if entry.item.is_google_doc:
                docs += 1
            if entry.item.size is not None:
                total_bytes += entry.item.size

    return TreeStats(
        folder_count=folders,
        file_count=files,
        google_doc_count=docs,
        total_bytes=total_bytes,
    )


def format_size(num_bytes: int | None) -> str:
    """Format a byte count into a compact human-readable string (e.g. ``12.3 MB``)."""
    if num_bytes is None:
        return "unknown"
    value = float(num_bytes)
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    for index, unit in enumerate(units):
        if value < 1024.0 or index == len(units) - 1:
            if index == 0:
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} PB"


def render_ascii_tree(root: FolderNode) -> str:
    """Render a :class:`FolderNode` as a Unicode box-drawing tree string."""
    lines: list[str] = [f"{root.item.name}/"]

    def _visit(node: FolderNode, prefix: str) -> None:
        subfolders = sorted(node.subfolders, key=lambda d: d.item.name.lower())
        files = sorted(node.files, key=lambda f: f.item.name.lower())
        entries: list[tuple[bool, FolderNode | FileEntry]] = [
            *((True, sf) for sf in subfolders),
            *((False, fe) for fe in files),
        ]
        total = len(entries)
        for idx, (is_dir, child) in enumerate(entries):
            is_last = idx == total - 1
            connector = "└── " if is_last else "├── "
            next_prefix = prefix + ("    " if is_last else "│   ")
            if is_dir:
                assert isinstance(child, FolderNode)
                lines.append(f"{prefix}{connector}{child.item.name}/")
                _visit(child, next_prefix)
            else:
                assert isinstance(child, FileEntry)
                tag = (
                    "[export]"
                    if child.item.is_google_doc
                    else f"[{format_size(child.item.size)}]"
                )
                lines.append(f"{prefix}{connector}{child.item.name}  {tag}")

    _visit(root, "")
    return "\n".join(lines)

