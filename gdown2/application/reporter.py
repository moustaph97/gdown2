"""Progress aggregation, EMA throughput smoothing, and observer notifications."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from gdown2.domain.models import DownloadTask, TaskState


class EventType(StrEnum):
    """Types of events emitted by the download engine."""

    PLAN_READY = "plan_ready"
    TASK_STARTED = "task_started"
    TASK_PROGRESS = "task_progress"
    TASK_RETRY = "task_retry"
    TASK_DONE = "task_done"
    TASK_SKIPPED = "task_skipped"
    TASK_FAILED = "task_failed"
    SUMMARY = "summary"


@dataclass(slots=True)
class TransferEvent:
    """Immutable event payload delivered to CLI, TUI, and JSON observers."""

    event: EventType
    timestamp: float = field(default_factory=time.time)
    task_id: str | None = None
    name: str | None = None
    relative_path: str | None = None
    state: TaskState | None = None
    bytes_done: int = 0
    bytes_total: int | None = None
    speed_bps: float = 0.0
    eta_seconds: float | None = None
    attempt: int = 0
    message: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "event": self.event.value,
            "timestamp": round(self.timestamp, 3),
        }
        if self.task_id is not None:
            payload["task_id"] = self.task_id
        if self.name is not None:
            payload["name"] = self.name
        if self.relative_path is not None:
            payload["relative_path"] = self.relative_path
        if self.state is not None:
            payload["state"] = self.state.value
        if self.bytes_done or self.event in {EventType.TASK_PROGRESS, EventType.TASK_DONE}:
            payload["bytes_done"] = self.bytes_done
        if self.bytes_total is not None:
            payload["bytes_total"] = self.bytes_total
        if self.speed_bps > 0:
            payload["speed_bps"] = round(self.speed_bps, 1)
        if self.eta_seconds is not None:
            payload["eta_seconds"] = round(self.eta_seconds, 1)
        if self.attempt > 0:
            payload["attempt"] = self.attempt
        if self.message is not None:
            payload["message"] = self.message
        if self.extra:
            payload.update(self.extra)
        return payload


class _SpeedTracker:
    """Exponential moving average (EMA) speed estimator."""

    def __init__(self, alpha: float = 0.25) -> None:
        self._alpha = alpha
        self._last_time: float | None = None
        self._ema_bps: float = 0.0

    def record(self, delta_bytes: int, now: float) -> float:
        if self._last_time is None:
            self._last_time = now
            return self._ema_bps
        dt = now - self._last_time
        if dt <= 1e-6:
            return self._ema_bps
        instant = delta_bytes / dt
        if self._ema_bps <= 0.0:
            self._ema_bps = instant
        else:
            self._ema_bps = self._alpha * instant + (1.0 - self._alpha) * self._ema_bps
        self._last_time = now
        return self._ema_bps

    @property
    def speed_bps(self) -> float:
        return self._ema_bps


Observer = Callable[[TransferEvent], None]


class Reporter:
    """Central hub that tracks transfer metrics and notifies registered observers."""

    def __init__(self, min_progress_interval: float = 0.1) -> None:
        self._min_interval = min_progress_interval
        self._observers: list[Observer] = []
        self._global_speed = _SpeedTracker()
        self._task_speeds: dict[str, _SpeedTracker] = {}
        self._last_emit: dict[str, float] = {}
        self._pending_deltas: dict[str, int] = {}
        self.total_bytes_done: int = 0
        self.total_bytes_expected: int = 0

    def subscribe(self, observer: Observer) -> None:
        self._observers.append(observer)

    @property
    def global_speed_bps(self) -> float:
        return self._global_speed.speed_bps

    def emit(self, event: TransferEvent) -> None:
        for observer in self._observers:
            observer(event)

    def on_plan_ready(self, tasks: list[DownloadTask]) -> None:
        self.total_bytes_expected = sum(t.bytes_total or 0 for t in tasks)
        self.total_bytes_done = sum(t.bytes_done for t in tasks)
        self.emit(
            TransferEvent(
                event=EventType.PLAN_READY,
                bytes_done=self.total_bytes_done,
                bytes_total=self.total_bytes_expected,
                extra={"task_count": len(tasks)},
            )
        )

    def on_task_started(self, task: DownloadTask) -> None:
        now = time.monotonic()
        tracker = _SpeedTracker()
        tracker.record(0, now)
        self._task_speeds[task.task_id] = tracker
        self._last_emit[task.task_id] = 0.0
        self._pending_deltas[task.task_id] = 0
        self.emit(
            TransferEvent(
                event=EventType.TASK_STARTED,
                task_id=task.task_id,
                name=task.item.name,
                relative_path=task.relative_path.as_posix(),
                state=task.state,
                bytes_done=task.bytes_done,
                bytes_total=task.bytes_total,
                attempt=task.attempts,
            )
        )

    def on_task_progress(
        self,
        task: DownloadTask,
        delta_bytes: int,
        *,
        force: bool = False,
    ) -> None:
        now = time.monotonic()
        self.total_bytes_done += delta_bytes
        self._pending_deltas[task.task_id] = self._pending_deltas.get(task.task_id, 0) + delta_bytes

        last = self._last_emit.get(task.task_id, 0.0)
        if not force and (now - last) < self._min_interval:
            return

        accumulated = self._pending_deltas.pop(task.task_id, 0)
        self._last_emit[task.task_id] = now
        tracker = self._task_speeds.setdefault(task.task_id, _SpeedTracker())
        speed = tracker.record(accumulated, now)
        self._global_speed.record(accumulated, now)

        eta: float | None = None
        if speed > 0 and task.bytes_total is not None and task.bytes_total > task.bytes_done:
            eta = (task.bytes_total - task.bytes_done) / speed

        self.emit(
            TransferEvent(
                event=EventType.TASK_PROGRESS,
                task_id=task.task_id,
                name=task.item.name,
                relative_path=task.relative_path.as_posix(),
                state=task.state,
                bytes_done=task.bytes_done,
                bytes_total=task.bytes_total,
                speed_bps=speed,
                eta_seconds=eta,
                attempt=task.attempts,
            )
        )

    def on_task_retry(self, task: DownloadTask, delay: float, reason: str) -> None:
        self.emit(
            TransferEvent(
                event=EventType.TASK_RETRY,
                task_id=task.task_id,
                name=task.item.name,
                relative_path=task.relative_path.as_posix(),
                state=task.state,
                bytes_done=task.bytes_done,
                bytes_total=task.bytes_total,
                attempt=task.attempts,
                message=reason,
                extra={"retry_delay": round(delay, 2)},
            )
        )

    def on_task_done(self, task: DownloadTask) -> None:
        if self._pending_deltas.get(task.task_id, 0) > 0:
            self.on_task_progress(task, 0, force=True)
        tracker = self._task_speeds.get(task.task_id)
        speed = tracker.speed_bps if tracker else 0.0
        self.emit(
            TransferEvent(
                event=EventType.TASK_DONE,
                task_id=task.task_id,
                name=task.item.name,
                relative_path=task.relative_path.as_posix(),
                state=TaskState.DONE,
                bytes_done=task.bytes_done,
                bytes_total=task.bytes_total,
                speed_bps=speed,
                attempt=task.attempts,
            )
        )

    def on_task_skipped(self, task: DownloadTask) -> None:
        self.emit(
            TransferEvent(
                event=EventType.TASK_SKIPPED,
                task_id=task.task_id,
                name=task.item.name,
                relative_path=task.relative_path.as_posix(),
                state=TaskState.SKIPPED,
                bytes_done=task.bytes_done,
                bytes_total=task.bytes_total,
            )
        )

    def on_task_failed(self, task: DownloadTask, error: str) -> None:
        self.emit(
            TransferEvent(
                event=EventType.TASK_FAILED,
                task_id=task.task_id,
                name=task.item.name,
                relative_path=task.relative_path.as_posix(),
                state=TaskState.FAILED,
                bytes_done=task.bytes_done,
                bytes_total=task.bytes_total,
                attempt=task.attempts,
                message=error,
            )
        )

    def on_summary(self, tasks: list[DownloadTask], elapsed_seconds: float) -> None:
        done = sum(1 for t in tasks if t.state == TaskState.DONE)
        skipped = sum(1 for t in tasks if t.state == TaskState.SKIPPED)
        failed = sum(1 for t in tasks if t.state == TaskState.FAILED)
        self.emit(
            TransferEvent(
                event=EventType.SUMMARY,
                bytes_done=self.total_bytes_done,
                bytes_total=self.total_bytes_expected,
                speed_bps=(
                    self.total_bytes_done / elapsed_seconds if elapsed_seconds > 0 else 0.0
                ),
                extra={
                    "done": done,
                    "skipped": skipped,
                    "failed": failed,
                    "elapsed_seconds": round(elapsed_seconds, 2),
                },
            )
        )

