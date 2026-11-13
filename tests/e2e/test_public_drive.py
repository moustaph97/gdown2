"""Opt-in end-to-end tests against a real public Google Drive resource.

Run with:
    GDOWN2_E2E_URL="https://drive.google.com/file/d/<id>/view" pytest tests/e2e
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gdown2.domain.models import TaskState
from gdown2.sdk import download, get_info

_E2E_URL = os.environ.get("GDOWN2_E2E_URL")


@pytest.mark.skipif(not _E2E_URL, reason="Set GDOWN2_E2E_URL to run live Google Drive E2E tests.")
@pytest.mark.asyncio
async def test_live_public_download(tmp_path: Path) -> None:
    assert _E2E_URL is not None
    info = await get_info(_E2E_URL)
    assert info.id

    tasks = await download(_E2E_URL, tmp_path)
    assert len(tasks) >= 1
    assert all(t.state in {TaskState.DONE, TaskState.SKIPPED} for t in tasks)

