"""Recursive folder crawler with shortcut resolution and cycle prevention."""

from __future__ import annotations

import asyncio
from pathlib import PurePosixPath

from gdown2.application.ports import DriveSource, MetadataCache
from gdown2.domain.errors import PermissionDeniedError, ResourceNotFoundError
from gdown2.domain.models import DriveItem, FileEntry, FolderNode
from gdown2.domain.path_policy import PathPolicy
from gdown2.domain.url_parser import ParsedTarget, TargetKind


class FolderCrawler:
    """Builds a complete :class:`FolderNode` tree from a root Drive folder."""

    def __init__(
        self,
        source: DriveSource,
        path_policy: PathPolicy | None = None,
        cache: MetadataCache | None = None,
        max_depth: int = 32,
        follow_shortcuts: bool = True,
        preferred_export_format: str | None = None,
        concurrency: int = 6,
    ) -> None:
        self._source = source
        self._path_policy = path_policy or PathPolicy()
        self._cache = cache
        self._max_depth = max_depth
        self._follow_shortcuts = follow_shortcuts
        self._preferred_export_format = preferred_export_format
        self._concurrency = max(1, concurrency)

    async def crawl(self, root_item: DriveItem) -> FolderNode:
        """Traverse ``root_item`` in breadth-first order and return its :class:`FolderNode`."""
        root_name = self._path_policy.sanitize_segment(root_item.name, fallback=root_item.id)
        root_node = FolderNode(
            item=root_item,
            relative_path=PurePosixPath(root_name),
            depth=0,
        )

        visited_folders: set[str] = {root_item.id}
        current_level: list[FolderNode] = [root_node]
        sem = asyncio.Semaphore(self._concurrency)

        async def _fetch_children(node: FolderNode) -> tuple[FolderNode, list[DriveItem]]:
            async with sem:
                try:
                    children = await self._source.list_folder_children(
                        node.item.id,
                        resource_key=node.item.resource_key,
                    )
                    return node, children
                except (ResourceNotFoundError, PermissionDeniedError):
                    if node.depth == 0:
                        raise
                    return node, []

        while current_level:
            results = await asyncio.gather(*(_fetch_children(node) for node in current_level))
            next_level: list[FolderNode] = []

            for current, children in results:
                for child in children:
                    resolved = await self._resolve_if_shortcut(child)
                    if resolved is None:
                        continue

                    if self._cache is not None:
                        self._cache.put(resolved, persist=False)

                    if resolved.is_folder:
                        if resolved.id in visited_folders:
                            # Skip shortcut cycles pointing back to an ancestor or queued folder.
                            continue
                        visited_folders.add(resolved.id)
                        folder_rel = self._path_policy.allocate_child_path(
                            current.relative_path,
                            resolved.name,
                        )
                        sub_node = FolderNode(
                            item=resolved,
                            relative_path=folder_rel,
                            depth=current.depth + 1,
                        )
                        current.subfolders.append(sub_node)
                        if sub_node.depth < self._max_depth:
                            next_level.append(sub_node)
                    else:
                        forced_ext: str | None = None
                        if resolved.is_google_doc:
                            _, forced_ext = resolved.resolve_export(self._preferred_export_format)
                        file_rel = self._path_policy.allocate_child_path(
                            current.relative_path,
                            resolved.name,
                            forced_extension=forced_ext,
                        )
                        current.files.append(
                            FileEntry(
                                item=resolved,
                                relative_path=file_rel,
                                depth=current.depth + 1,
                            )
                        )

            if self._cache is not None:
                self._cache.flush()
            current_level = next_level

        return root_node

    async def _resolve_if_shortcut(self, item: DriveItem) -> DriveItem | None:
        if not item.is_shortcut:
            return item
        if not self._follow_shortcuts or not item.shortcut_target_id:
            return None
        try:
            target = await self._source.fetch_metadata(
                ParsedTarget(id=item.shortcut_target_id, kind=TargetKind.UNKNOWN)
            )
        except (ResourceNotFoundError, PermissionDeniedError):
            return None
        if item.name:
            target.name = item.name
        return target
