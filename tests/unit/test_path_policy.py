"""Unit tests for gdown2.domain.path_policy."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from gdown2.domain.errors import PathTraversalError
from gdown2.domain.path_policy import PathPolicy


def test_sanitize_forbidden_chars_and_trailing_dots() -> None:
    policy = PathPolicy()
    assert policy.sanitize_segment('my:file*name?.txt.. ') == "my_file_name_.txt"
    assert policy.sanitize_segment("...") == "unnamed"
    assert policy.sanitize_segment("   ") == "unnamed"


@pytest.mark.parametrize("reserved", ["CON", "prn", "Aux.txt", "NUL.tar.gz", "COM1", "lpt9.doc"])
def test_windows_reserved_device_names_escaped(reserved: str) -> None:
    policy = PathPolicy()
    sanitized = policy.sanitize_segment(reserved)
    stem = sanitized.split(".")[0]
    assert stem.startswith("_")


def test_collision_resolution_among_siblings() -> None:
    policy = PathPolicy()
    parent = PurePosixPath("Dataset")
    first = policy.allocate_child_path(parent, "results.csv")
    second = policy.allocate_child_path(parent, "results.csv")
    third = policy.allocate_child_path(parent, "RESULTS.CSV")

    assert first == PurePosixPath("Dataset/results.csv")
    assert second == PurePosixPath("Dataset/results (1).csv")
    assert third == PurePosixPath("Dataset/RESULTS (2).CSV")


def test_forced_extension_on_google_doc() -> None:
    policy = PathPolicy()
    rel = policy.allocate_child_path(
        PurePosixPath("Docs"),
        "Quarterly Report",
        forced_extension=".docx",
    )
    assert rel == PurePosixPath("Docs/Quarterly Report.docx")


def test_segment_truncation_preserves_extension() -> None:
    policy = PathPolicy(max_segment_length=20)
    long_name = "a" * 50 + ".parquet"
    result = policy.sanitize_segment(long_name)
    assert len(result) == 20
    assert result.endswith(".parquet")


def test_path_traversal_rejected(tmp_path: Path) -> None:
    policy = PathPolicy()
    with pytest.raises(PathTraversalError):
        policy.resolve_local_path(tmp_path, PurePosixPath("../escape.txt"))

