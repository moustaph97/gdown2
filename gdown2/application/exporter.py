"""Export service for Google Workspace documents (Docs, Sheets, Slides, Drawings)."""

from __future__ import annotations

from collections.abc import Callable

from gdown2.application.ports import DriveSource
from gdown2.domain.models import DownloadTask
from gdown2.infrastructure.storage.fs_sink import FileSystemSink


class ExportService:
    """Streams Google Workspace documents in their target export MIME format."""

    def __init__(self, source: DriveSource, sink: FileSystemSink) -> None:
        self._source = source
        self._sink = sink

    async def export_task(
        self,
        task: DownloadTask,
        *,
        on_bytes: Callable[[int], None] | None = None,
    ) -> int:
        """Stream the exported document into ``task.part_path`` and return total bytes written."""
        export_mime, _ = task.item.resolve_export(task.export_format)
        written = 0

        async with self._source.open_export_stream(task.item, export_mime) as media:
            with self._sink.open_part(task.part_path, append=False) as handle:
                async for chunk in media.chunks:
                    if not chunk:
                        continue
                    handle.write(chunk)
                    written += len(chunk)
                    if on_bytes is not None:
                        on_bytes(len(chunk))

        return written

