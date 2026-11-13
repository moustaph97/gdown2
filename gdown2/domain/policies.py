"""Domain policies governing retries, concurrency, byte ranges, and file collisions."""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import StrEnum


class ExistingFilePolicy(StrEnum):
    """Behavior when the target output file already exists on disk."""

    RESUME = "resume"
    SKIP = "skip"
    OVERWRITE = "overwrite"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Exponential backoff configuration with optional full jitter."""

    max_attempts: int = 5
    base_delay: float = 0.5
    max_delay: float = 30.0
    multiplier: float = 2.0
    jitter: bool = True

    def compute_delay(
        self,
        attempt: int,
        *,
        retry_after: float | None = None,
        rng: random.Random | None = None,
    ) -> float:
        """Compute sleep duration in seconds before retry ``attempt`` (1-indexed)."""
        if retry_after is not None and retry_after > 0:
            return min(retry_after, self.max_delay * 2.0)

        exp = max(0, attempt - 1)
        ceiling = min(self.max_delay, self.base_delay * (self.multiplier**exp))
        if not self.jitter:
            return ceiling

        generator = rng or random
        # Equal jitter keeps at least half the exponential delay while decorrelating workers.
        return ceiling * 0.5 + generator.uniform(0.0, ceiling * 0.5)


@dataclass(slots=True)
class ConcurrencyPolicy:
    """Concurrency limits with additive-increase / multiplicative-decrease (AIMD) scaling.

    Google Drive aggressively throttles connections from a single IP when too many
    parallel streams open simultaneously. When a worker hits a 429 or transient 403,
    ``on_rate_limit()`` halves the active concurrency floor; ``on_success()`` slowly
    steps it back up toward ``max_files``.
    """

    max_files: int = 6
    min_files: int = 1
    chunks_per_file: int = 4
    queue_capacity: int = 128
    success_streak_to_increase: int = 8

    _current_limit: int = 0
    _success_streak: int = 0

    def __post_init__(self) -> None:
        self.max_files = max(1, self.max_files)
        self.min_files = max(1, min(self.min_files, self.max_files))
        self.chunks_per_file = max(1, self.chunks_per_file)
        if self._current_limit <= 0:
            self._current_limit = self.max_files

    @property
    def current_limit(self) -> int:
        return self._current_limit

    def on_rate_limit(self) -> int:
        """Reduce active concurrency after a 429 / rate-limit signal."""
        self._success_streak = 0
        self._current_limit = max(self.min_files, self._current_limit // 2)
        return self._current_limit

    def on_success(self) -> int:
        """Record a successful transfer and gradually restore concurrency."""
        if self._current_limit < self.max_files:
            self._success_streak += 1
            if self._success_streak >= self.success_streak_to_increase:
                self._current_limit += 1
                self._success_streak = 0
        return self._current_limit


@dataclass(frozen=True, slots=True)
class RangePolicy:
    """Parameters for HTTP Range requests and streaming buffer sizes."""

    stream_chunk_size: int = 64 * 1024
    min_multipart_size: int = 16 * 1024 * 1024

    def compute_ranges(self, total_size: int, parts: int) -> list[tuple[int, int]]:
        """Divide ``total_size`` into ``parts`` inclusive ``(start, end)`` byte ranges."""
        if total_size <= 0:
            return []
        if parts <= 1 or total_size < self.min_multipart_size:
            return [(0, total_size - 1)]

        parts = min(parts, total_size)
        part_size = total_size // parts
        ranges: list[tuple[int, int]] = []
        for idx in range(parts):
            start = idx * part_size
            end = (total_size - 1) if idx == parts - 1 else (start + part_size - 1)
            ranges.append((start, end))
        return ranges

