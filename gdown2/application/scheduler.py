"""Task scheduler with bounded queue backpressure, adaptive concurrency, and cancellation."""

from __future__ import annotations

import asyncio
import time

from gdown2.application.planner import DownloadPlan
from gdown2.application.ports import DriveSource, ResumeRepository
from gdown2.application.reporter import Reporter
from gdown2.application.verifier import Verifier
from gdown2.application.worker import DownloadWorker
from gdown2.domain.errors import DownloadCancelledError
from gdown2.domain.models import DownloadTask, TaskState, TransferState
from gdown2.domain.policies import ConcurrencyPolicy, RangePolicy, RetryPolicy
from gdown2.infrastructure.storage.fs_sink import FileSystemSink


class Scheduler:
    """Dispatches :class:`DownloadTask` objects across an async worker pool."""

    def __init__(
        self,
        source: DriveSource,
        sink: FileSystemSink,
        verifier: Verifier,
        reporter: Reporter,
        *,
        concurrency_policy: ConcurrencyPolicy | None = None,
        retry_policy: RetryPolicy | None = None,
        range_policy: RangePolicy | None = None,
        resume_repo: ResumeRepository | None = None,
    ) -> None:
        self._source = source
        self._sink = sink
        self._verifier = verifier
        self._reporter = reporter
        self._concurrency = concurrency_policy or ConcurrencyPolicy()
        self._retry = retry_policy or RetryPolicy()
        self._range = range_policy or RangePolicy()
        self._resume_repo = resume_repo

        # unpaused by default (set = running, cleared = paused).
        self._pause_event = asyncio.Event()
        self._pause_event.set()
        self._cancel_event = asyncio.Event()

    @property
    def is_paused(self) -> bool:
        return not self._pause_event.is_set()

    def pause(self) -> None:
        """Pause all active workers at their next chunk boundary."""
        self._pause_event.clear()

    def resume(self) -> None:
        """Resume paused workers."""
        self._pause_event.set()

    def cancel(self) -> None:
        """Signal all workers to abort and flush their resume state."""
        self._cancel_event.set()
        self._pause_event.set()

    async def run(self, plan: DownloadPlan) -> list[DownloadTask]:
        """Execute all tasks in ``plan`` and return the updated task list."""
        start_time = time.monotonic()
        self._sink.ensure_directories(plan.directories)

        if self._resume_repo is not None:
            for task in plan.tasks:
                if task.state != TaskState.SKIPPED:
                    self._resume_repo.save_task(TransferState.from_task(task))

        self._reporter.on_plan_ready(plan.tasks)

        queue: asyncio.Queue[tuple[int, DownloadTask] | None] = asyncio.Queue(
            maxsize=max(1, self._concurrency.queue_capacity)
        )
        active_count = 0
        cond = asyncio.Condition()

        worker = DownloadWorker(
            self._source,
            self._sink,
            self._verifier,
            self._reporter,
            retry_policy=self._retry,
            range_policy=self._range,
            concurrency_policy=self._concurrency,
            resume_repo=self._resume_repo,
            pause_event=self._pause_event,
            cancel_event=self._cancel_event,
        )

        async def _producer() -> None:
            for idx, task in enumerate(plan.tasks):
                if self._cancel_event.is_set():
                    break
                await queue.put((idx, task))
            for _ in range(self._concurrency.max_files):
                await queue.put(None)

        async def _consumer() -> None:
            nonlocal active_count
            while True:
                item = await queue.get()
                if item is None:
                    queue.task_done()
                    break

                _, task = item
                if self._cancel_event.is_set():
                    task.state = TaskState.CANCELLED
                    queue.task_done()
                    continue

                # Respect AIMD adaptive concurrency limit dynamically.
                async with cond:
                    while active_count >= self._concurrency.current_limit:
                        await cond.wait()
                    active_count += 1

                try:
                    await worker.execute(task)
                except DownloadCancelledError:
                    pass
                finally:
                    async with cond:
                        active_count -= 1
                        cond.notify_all()
                    queue.task_done()

        consumers = [
            asyncio.create_task(_consumer())
            for _ in range(self._concurrency.max_files)
        ]
        producer = asyncio.create_task(_producer())

        await asyncio.gather(producer, *consumers)

        if self._cancel_event.is_set():
            for task in plan.tasks:
                if task.state == TaskState.PENDING:
                    task.state = TaskState.CANCELLED

        elapsed = time.monotonic() - start_time
        self._reporter.on_summary(plan.tasks, elapsed)
        return plan.tasks

