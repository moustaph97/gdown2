"""Persistent JSON transfer state for cross-session resume."""

from __future__ import annotations

import contextlib
import json
import os
import threading
from pathlib import Path

from gdown2.domain.models import TransferState

STATE_FILE_NAME = ".gdown2.state.json"


class ResumeStateRepository:
    """Thread-safe JSON store for active :class:`TransferState` entries."""

    def __init__(self, target_dir: Path) -> None:
        self._target_dir = target_dir.resolve()
        self._state_path = self._target_dir / STATE_FILE_NAME
        self._lock = threading.Lock()
        self._records: dict[str, TransferState] = {}
        self._loaded = False

    @property
    def state_path(self) -> Path:
        return self._state_path

    def load_all(self) -> dict[str, TransferState]:
        with self._lock:
            if not self._loaded:
                self._read_from_disk()
                self._loaded = True
            return dict(self._records)

    def save_task(self, state: TransferState) -> None:
        with self._lock:
            if not self._loaded:
                self._read_from_disk()
                self._loaded = True
            self._records[state.task_key] = state
            self._flush_to_disk()

    def remove_task(self, task_key: str) -> None:
        with self._lock:
            if not self._loaded:
                self._read_from_disk()
                self._loaded = True
            if task_key in self._records:
                del self._records[task_key]
                self._flush_to_disk()

    def clear_if_empty(self) -> None:
        with self._lock:
            if not self._loaded:
                self._read_from_disk()
                self._loaded = True
            if not self._records and self._state_path.exists():
                with contextlib.suppress(OSError):
                    self._state_path.unlink()

    def _read_from_disk(self) -> None:
        self._records.clear()
        if not self._state_path.exists():
            return
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            for item in raw.get("tasks", []):
                state = TransferState.from_dict(item)
                self._records[state.task_key] = state
        except (OSError, ValueError, KeyError):
            # Ignore corrupted state files rather than crashing the resume command.
            self._records.clear()

    def _flush_to_disk(self) -> None:
        if not self._records:
            if self._state_path.exists():
                with contextlib.suppress(OSError):
                    self._state_path.unlink()
            return

        self._target_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = self._state_path.with_suffix(".tmp")
        payload = {
            "version": 1,
            "tasks": [s.to_dict() for s in self._records.values()],
        }
        try:
            tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp_path, self._state_path)
        except OSError:
            pass

