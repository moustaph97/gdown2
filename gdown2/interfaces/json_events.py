"""NDJSON (Newline-Delimited JSON) event emitter for machine-readable CLI output."""

from __future__ import annotations

import json
import sys
from typing import TextIO

from gdown2.application.reporter import TransferEvent


class NdjsonEventWriter:
    """Writes :class:`TransferEvent` records as single-line JSON objects."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream or sys.stdout

    def __call__(self, event: TransferEvent) -> None:
        line = json.dumps(event.to_dict(), ensure_ascii=False)
        self._stream.write(f"{line}\n")
        self._stream.flush()

    def write_object(self, payload: dict[str, object]) -> None:
        line = json.dumps(payload, ensure_ascii=False)
        self._stream.write(f"{line}\n")
        self._stream.flush()

