"""Download worker executing individual DownloadTasks with resume, retry, and verification."""

from __future__ import annotations

import asyncio
import hashlib

from gdown2.application.exporter import ExportService
from gdown2.application.ports import DriveSource, ResumeRepository
from gdown2.application.reporter import Reporter
from gdown2.application.verifier import Verifier
from gdown2.domain.errors import (
    ChecksumMismatchError,
    DownloadCancelledError,
    GdownError,
    RateLimitError,
    TransientNetworkError,
)
from gdown2.domain.models import DownloadTask, TaskState, TransferState
from gdown2.domain.policies import ConcurrencyPolicy, RangePolicy, RetryPolicy
from gdown2.infrastructure.storage.fs_sink import FileSystemSink


class DownloadWorker:
    """Executes a single :class:`DownloadTask` with retries, range resume, and verification."""

    def __init__(
        self,
        source: DriveSource,
        sink: FileSystemSink,
        verifier: Verifier,
        reporter: Reporter,
        *,
        retry_policy: RetryPolicy | None = None,
        range_policy: RangePolicy | None = None,
        concurrency_policy: ConcurrencyPolicy | None = None,
        resume_repo: ResumeRepository | None = None,
        pause_event: asyncio.Event | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> None:
        self._source = source
        self._sink = sink
        self._verifier = verifier
        self._reporter = reporter
        self._retry_policy = retry_policy or RetryPolicy()
        self._range_policy = range_policy or RangePolicy()
        self._concurrency_policy = concurrency_policy or ConcurrencyPolicy()
        self._resume_repo = resume_repo
        self._exporter = ExportService(source, sink)
        self._pause_event = pause_event
        self._cancel_event = cancel_event

    async def execute(self, task: DownloadTask) -> DownloadTask:
        """Run ``task`` to completion, handling retries and state updates."""
        if task.state == TaskState.SKIPPED:
            self._reporter.on_task_skipped(task)
            if self._resume_repo is not None:
                self._resume_repo.remove_task(self._task_key(task))
            return task

        if task.stale_part:
            self._sink.discard_part(task.part_path)
            self._sink.discard_part(task.mpart_path)
            task.stale_part = False
            task.resume_offset = 0
            task.bytes_done = 0
        elif task.mpart_path.exists():
            self._sink.discard_part(task.mpart_path)

        if task.bytes_total:
            remaining = max(0, task.bytes_total - task.resume_offset)
            self._sink.assert_disk_space(task.local_path.parent, remaining)

        task.state = TaskState.RUNNING
        self._reporter.on_task_started(task)

        while True:
            self._check_cancelled(task)
            task.attempts += 1
            try:
                if task.item.is_google_doc:
                    await self._run_export(task)
                elif (
                    self._concurrency_policy.chunks_per_file > 1
                    and task.resume_offset == 0
                    and task.item.size is not None
                    and task.item.size >= self._range_policy.min_multipart_size
                ):
                    await self._run_multipart_download(task)
                else:
                    await self._run_single_stream_download(task)

                task.state = TaskState.DONE
                self._concurrency_policy.on_success()
                if self._resume_repo is not None:
                    self._resume_repo.remove_task(self._task_key(task))
                self._reporter.on_task_done(task)
                return task

            except DownloadCancelledError:
                task.state = TaskState.CANCELLED
                self._persist_state(task)
                raise

            except GdownError as exc:
                if isinstance(exc, RateLimitError):
                    self._concurrency_policy.on_rate_limit()

                if isinstance(exc, ChecksumMismatchError):
                    # Discard corrupted .part/.mpart file so the retry starts clean from byte 0.
                    self._sink.discard_part(task.part_path)
                    self._sink.discard_part(task.mpart_path)
                    task.resume_offset = 0
                    task.bytes_done = 0

                if not exc.retryable or task.attempts >= self._retry_policy.max_attempts:
                    task.state = TaskState.FAILED
                    task.error_message = exc.message
                    self._persist_state(task)
                    self._reporter.on_task_failed(task, exc.message)
                    return task

                delay = self._retry_policy.compute_delay(
                    task.attempts,
                    retry_after=exc.retry_after,
                )
                self._persist_state(task)
                self._reporter.on_task_retry(task, delay, exc.message)
                await asyncio.sleep(delay)

            except Exception as exc:
                task.state = TaskState.FAILED
                task.error_message = str(exc)
                self._persist_state(task)
                self._reporter.on_task_failed(task, str(exc))
                return task

    async def _run_export(self, task: DownloadTask) -> None:
        task.bytes_done = 0
        task.resume_offset = 0

        def _on_chunk(n: int) -> None:
            self._check_cancelled(task)
            task.bytes_done += n
            self._reporter.on_task_progress(task, n)

        written = await self._exporter.export_task(task, on_bytes=_on_chunk)
        task.bytes_total = written
        self._verifier.verify_file(task.part_path, task.item, expected_size=written)
        self._sink.promote_part(task.part_path, task.local_path)

    async def _run_single_stream_download(self, task: DownloadTask) -> None:
        # Synchronize resume_offset with actual .part file on disk before requesting Range.
        if task.part_path.exists():
            task.resume_offset = task.part_path.stat().st_size
            task.bytes_done = task.resume_offset
        else:
            task.resume_offset = 0
            task.bytes_done = 0

        start_offset = task.resume_offset

        async with self._source.open_download_stream(
            task.item,
            start_byte=start_offset,
        ) as media:
            if media.status_code == 416 and task.part_path.exists():
                # Server reports range not satisfiable; check if .part is already complete.
                try:
                    self._verifier.verify_file(task.part_path, task.item)
                    self._sink.promote_part(task.part_path, task.local_path)
                    return
                except ChecksumMismatchError:
                    self._sink.discard_part(task.part_path)
                    task.resume_offset = 0
                    task.bytes_done = 0

            if media.etag:
                task.etag = media.etag

            # If we asked for a byte range (start_offset > 0) and the server replied 200 OK,
            # the server ignored Range and is sending the entire file from byte 0.
            append_mode = start_offset > 0 and media.status_code == 206
            if start_offset > 0 and not append_mode:
                start_offset = 0
                task.resume_offset = 0
                task.bytes_done = 0

            if media.content_length is not None:
                task.bytes_total = start_offset + media.content_length

            streaming_hasher = hashlib.md5() if start_offset == 0 else None

            with self._sink.open_part(task.part_path, append=append_mode) as handle:
                async for chunk in media.chunks:
                    if not chunk:
                        continue
                    await self._wait_if_paused(task)
                    self._check_cancelled(task)
                    handle.write(chunk)
                    if streaming_hasher is not None:
                        streaming_hasher.update(chunk)
                    n = len(chunk)
                    task.bytes_done += n
                    task.resume_offset = task.bytes_done
                    self._reporter.on_task_progress(task, n)

        computed_md5 = streaming_hasher.hexdigest() if streaming_hasher is not None else None
        self._verifier.verify_file(
            task.part_path,
            task.item,
            expected_size=task.bytes_total,
            computed_md5=computed_md5,
        )
        self._sink.promote_part(task.part_path, task.local_path)

    async def _run_multipart_download(self, task: DownloadTask) -> None:
        assert task.item.size is not None
        total_size = task.item.size
        ranges = self._range_policy.compute_ranges(
            total_size,
            self._concurrency_policy.chunks_per_file,
        )
        task.bytes_done = 0
        task.bytes_total = total_size
        file_lock = asyncio.Lock()

        try:
            with self._sink.open_random_access_part(task.mpart_path, total_size) as handle:

                async def _fetch_part(start: int, end: int) -> None:
                    expected = end - start + 1
                    offset = start
                    written = 0
                    async with self._source.open_download_stream(
                        task.item,
                        start_byte=start,
                        end_byte=end,
                    ) as media:
                        if media.status_code != 206:
                            raise TransientNetworkError(
                                f"Server ignored multipart Range request for {task.item.name!r} "
                                f"(HTTP {media.status_code}).",
                                item_id=task.item.id,
                            )
                        if (
                            media.content_length is not None
                            and media.content_length != expected
                        ):
                            raise TransientNetworkError(
                                f"Unexpected multipart Content-Length for {task.item.name!r}: "
                                f"expected {expected}, got {media.content_length}.",
                                item_id=task.item.id,
                            )
                        async for chunk in media.chunks:
                            if not chunk:
                                continue
                            await self._wait_if_paused(task)
                            self._check_cancelled(task)
                            async with file_lock:
                                handle.seek(offset)
                                handle.write(chunk)
                            n = len(chunk)
                            offset += n
                            written += n
                            task.bytes_done += n
                            self._reporter.on_task_progress(task, n)

                    if written != expected:
                        raise TransientNetworkError(
                            f"Incomplete multipart range for {task.item.name!r}: "
                            f"expected {expected} bytes, got {written}.",
                            item_id=task.item.id,
                        )

                part_tasks = [
                    asyncio.create_task(_fetch_part(start, end)) for start, end in ranges
                ]
                try:
                    await asyncio.gather(*part_tasks)
                finally:
                    for part_task in part_tasks:
                        if not part_task.done():
                            part_task.cancel()
                    await asyncio.gather(*part_tasks, return_exceptions=True)

            self._verifier.verify_file(
                task.mpart_path,
                task.item,
                expected_size=total_size,
            )
            self._sink.promote_part(task.mpart_path, task.local_path)
        except BaseException:
            task.bytes_done = 0
            task.resume_offset = 0
            self._sink.discard_part(task.mpart_path)
            raise

    async def _wait_if_paused(self, task: DownloadTask) -> None:
        if self._pause_event is not None and not self._pause_event.is_set():
            task.state = TaskState.PAUSED
            self._persist_state(task)
            await self._pause_event.wait()
            task.state = TaskState.RUNNING

    def _check_cancelled(self, task: DownloadTask) -> None:
        if self._cancel_event is not None and self._cancel_event.is_set():
            raise DownloadCancelledError(
                f"Download cancelled for {task.item.name!r}",
                item_id=task.item.id,
            )

    def _persist_state(self, task: DownloadTask) -> None:
        if self._resume_repo is not None:
            self._resume_repo.save_task(TransferState.from_task(task))

    @staticmethod
    def _task_key(task: DownloadTask) -> str:
        return f"{task.item.id}:{task.relative_path.as_posix()}"

