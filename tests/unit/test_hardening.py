"""Regression tests for resume safety, configuration precedence, and task identity."""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath

from gdown2.application.planner import Planner
from gdown2.domain.models import DownloadTask, DriveItem, TransferState
from gdown2.domain.policies import ExistingFilePolicy, RangePolicy
from gdown2.domain.tree import format_size
from gdown2.infrastructure.config import AppConfig


def _item(tmp_path: Path, payload: bytes) -> DriveItem:
    return DriveItem(
        id="file-id",
        name="payload.bin",
        size=len(payload),
        md5=hashlib.md5(payload).hexdigest(),
        etag="etag-v1",
    )


def test_task_id_disambiguates_duplicate_names() -> None:
    item = DriveItem(id="same-id", name="data.bin")
    first = DownloadTask(
        item=item,
        local_path=Path("a/data.bin"),
        relative_path=PurePosixPath("a/data.bin"),
    )
    second = DownloadTask(
        item=item,
        local_path=Path("b/data.bin"),
        relative_path=PurePosixPath("b/data.bin"),
    )
    assert first.task_id != second.task_id
    assert first.task_id == "same-id:a/data.bin"


def test_format_size_petabytes() -> None:
    assert format_size(1024**5) == "1.0 PB"
    assert format_size(1536 * 1024**4) == "1.5 PB"


def test_range_policy_clamps_parts_to_total_size() -> None:
    policy = RangePolicy(min_multipart_size=1)
    ranges = policy.compute_ranges(total_size=3, parts=10)
    assert ranges == [(0, 0), (1, 1), (2, 2)]


def test_config_precedence_and_invalid_values(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[download]
workers = 2
chunks_per_file = 2
rate_limit_rps = 1.0
existing_policy = "skip"
verify_checksums = false
""",
        encoding="utf-8",
    )
    env = {
        "GDOWN2_WORKERS": "4",
        "GDOWN2_CHUNKS": "not-a-number",
        "GDOWN2_RATE": "2.5",
        "GDOWN2_TIMEOUT": "invalid",
    }
    cfg = AppConfig.load(
        config_file=config_file,
        environ=env,
        cli_overrides={"workers": 8, "existing_policy": "overwrite"},
    )

    assert cfg.workers == 8
    assert cfg.chunks_per_file == 2
    assert cfg.rate_limit_rps == 2.5
    assert cfg.timeout_seconds == 30.0
    assert cfg.existing_policy == ExistingFilePolicy.OVERWRITE
    assert cfg.verify_checksums is False


def test_planner_skips_verified_existing_file(tmp_path: Path) -> None:
    payload = b"already-downloaded"
    item = _item(tmp_path, payload)
    local_path = tmp_path / "payload.bin"
    local_path.write_bytes(payload)

    plan = Planner().plan_single(item, local_path)
    assert plan.tasks[0].state.value == "skipped"
    assert plan.tasks[0].bytes_done == len(payload)


def test_planner_does_not_skip_corrupt_same_size_file(tmp_path: Path) -> None:
    payload = b"good-payload"
    item = _item(tmp_path, payload)
    local_path = tmp_path / "payload.bin"
    local_path.write_bytes(b"bad-payload!")

    plan = Planner().plan_single(item, local_path)
    assert plan.tasks[0].state.value == "pending"


def test_planner_overwrite_discards_stale_part(tmp_path: Path) -> None:
    payload = b"fresh-content"
    item = _item(tmp_path, payload)
    local_path = tmp_path / "payload.bin"
    part_path = local_path.with_name("payload.bin.part")
    part_path.write_bytes(b"stale")

    planner = Planner(existing_policy=ExistingFilePolicy.OVERWRITE)
    task = planner.plan_single(item, local_path).tasks[0]

    assert task.stale_part is True
    assert task.resume_offset == 0
    assert task.bytes_done == 0


def test_planner_resume_discards_part_on_etag_change(tmp_path: Path) -> None:
    payload = b"fresh-content"
    item = _item(tmp_path, payload)
    local_path = tmp_path / "payload.bin"
    part_path = local_path.with_name("payload.bin.part")
    part_path.write_bytes(payload[:4])

    saved = TransferState(
        task_key="file-id:payload.bin",
        item_id=item.id,
        name=item.name,
        mime_type=item.mime_type,
        local_path=str(local_path),
        relative_path="payload.bin",
        bytes_total=item.size,
        resume_offset=4,
        etag="etag-old",
        md5=item.md5,
    )
    plan = Planner().plan_single(item, local_path, saved_states={saved.task_key: saved})
    task = plan.tasks[0]

    assert task.stale_part is True
    assert task.resume_offset == 0
    assert task.bytes_done == 0


def test_planner_resume_keeps_part_on_matching_etag(tmp_path: Path) -> None:
    payload = b"fresh-content"
    item = _item(tmp_path, payload)
    local_path = tmp_path / "payload.bin"
    part_path = local_path.with_name("payload.bin.part")
    part_path.write_bytes(payload[:4])

    saved = TransferState(
        task_key="file-id:payload.bin",
        item_id=item.id,
        name=item.name,
        mime_type=item.mime_type,
        local_path=str(local_path),
        relative_path="payload.bin",
        bytes_total=item.size,
        resume_offset=4,
        etag="etag-v1",
        md5=item.md5,
    )
    plan = Planner().plan_single(item, local_path, saved_states={saved.task_key: saved})
    task = plan.tasks[0]

    assert task.stale_part is False
    assert task.resume_offset == 4
    assert task.bytes_done == 4


def test_planner_ignores_interrupted_mpart_for_resume(tmp_path: Path) -> None:
    payload = b"0123456789" * 10
    # Simulate web mode item (no MD5, only size)
    item = DriveItem(id="web-id", name="large.bin", size=len(payload), md5=None)
    local_path = tmp_path / "large.bin"
    mpart_path = local_path.with_name("large.bin.mpart")
    # Pre-allocated .mpart of full size filled with zeros
    mpart_path.write_bytes(b"\x00" * len(payload))

    plan = Planner().plan_single(item, local_path)
    task = plan.tasks[0]

    assert task.resume_offset == 0
    assert task.bytes_done == 0
    assert task.stale_part is True

