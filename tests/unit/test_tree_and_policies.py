"""Unit tests for tree traversal, formatting, and retry/concurrency policies."""

from __future__ import annotations

import random
from pathlib import PurePosixPath

from gdown2.domain.models import FOLDER_MIME_TYPE, DriveItem, FileEntry, FolderNode
from gdown2.domain.policies import ConcurrencyPolicy, RangePolicy, RetryPolicy
from gdown2.domain.tree import (
    compute_tree_stats,
    format_size,
    render_ascii_tree,
    walk_files,
)


def _sample_tree() -> FolderNode:
    root = FolderNode(
        item=DriveItem(id="root", name="Project", mime_type=FOLDER_MIME_TYPE),
        relative_path=PurePosixPath("Project"),
    )
    root.files.append(
        FileEntry(
            item=DriveItem(id="f1", name="readme.md", size=2048),
            relative_path=PurePosixPath("Project/readme.md"),
            depth=1,
        )
    )
    sub = FolderNode(
        item=DriveItem(id="sub1", name="data", mime_type=FOLDER_MIME_TYPE),
        relative_path=PurePosixPath("Project/data"),
        depth=1,
    )
    sub.files.append(
        FileEntry(
            item=DriveItem(
                id="doc1",
                name="Spec",
                mime_type="application/vnd.google-apps.document",
            ),
            relative_path=PurePosixPath("Project/data/Spec.docx"),
            depth=2,
        )
    )
    root.subfolders.append(sub)
    return root


def test_tree_stats_and_rendering() -> None:
    tree = _sample_tree()
    stats = compute_tree_stats(tree)
    assert stats.folder_count == 1
    assert stats.file_count == 2
    assert stats.google_doc_count == 1
    assert stats.total_bytes == 2048

    files = list(walk_files(tree))
    assert [f.item.id for f in files] == ["f1", "doc1"]

    rendered = render_ascii_tree(tree)
    assert "Project/" in rendered
    assert "├── data/" in rendered
    assert "└── Spec  [export]" in rendered
    assert "└── readme.md  [2.0 KB]" in rendered


def test_format_size() -> None:
    assert format_size(None) == "unknown"
    assert format_size(512) == "512 B"
    assert format_size(1024 * 1024) == "1.0 MB"


def test_retry_policy_backoff_and_retry_after() -> None:
    policy = RetryPolicy(base_delay=1.0, multiplier=2.0, max_delay=10.0, jitter=False)
    assert policy.compute_delay(1) == 1.0
    assert policy.compute_delay(2) == 2.0
    assert policy.compute_delay(3) == 4.0
    assert policy.compute_delay(5) == 10.0
    assert policy.compute_delay(1, retry_after=7.5) == 7.5

    jitter_policy = RetryPolicy(base_delay=2.0, multiplier=2.0, max_delay=10.0, jitter=True)
    rng = random.Random(42)
    delay = jitter_policy.compute_delay(2, rng=rng)
    assert 2.0 <= delay <= 4.0


def test_concurrency_policy_aimd() -> None:
    policy = ConcurrencyPolicy(max_files=8, min_files=1, success_streak_to_increase=2)
    assert policy.current_limit == 8

    assert policy.on_rate_limit() == 4
    assert policy.on_rate_limit() == 2

    # Require 2 consecutive successes to step up by 1.
    assert policy.on_success() == 2
    assert policy.on_success() == 3


def test_range_policy_partitioning() -> None:
    policy = RangePolicy(min_multipart_size=100)
    assert policy.compute_ranges(50, 4) == [(0, 49)]
    assert policy.compute_ranges(100, 4) == [(0, 24), (25, 49), (50, 74), (75, 99)]

