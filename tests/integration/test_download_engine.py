"""Integration tests for gdown2 with mocked Google Drive HTTP endpoints (respx)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath

import httpx
import pytest
import respx
from typer.testing import CliRunner

from gdown2.application.crawler import FolderCrawler
from gdown2.application.planner import Planner
from gdown2.application.reporter import Reporter
from gdown2.application.resolver import Resolver
from gdown2.application.scheduler import Scheduler
from gdown2.application.verifier import Verifier
from gdown2.application.worker import DownloadWorker
from gdown2.domain.errors import UntrustedRedirectError
from gdown2.domain.models import DownloadTask, DriveItem, TaskState
from gdown2.domain.policies import ConcurrencyPolicy, RetryPolicy
from gdown2.infrastructure.auth import ApiKeyAuth
from gdown2.infrastructure.drive import DriveApiSource, DriveWebSource
from gdown2.infrastructure.http import HttpClient
from gdown2.infrastructure.storage import FileSystemSink, ResumeStateRepository
from gdown2.interfaces.cli.app import app

runner = CliRunner()


@pytest.mark.asyncio
async def test_web_virus_scan_interstitial_and_download(tmp_path: Path) -> None:
    file_id = "1VirusScanFile_123456789"
    payload = b"Hello from a large Google Drive file behind a virus scan warning!" * 64
    expected_md5 = hashlib.md5(payload).hexdigest()

    interstitial_html = f"""
    <html>
      <head><title>Google Drive - Virus scan warning</title></head>
      <body>
        <span class="uc-name-size"><a href="/open?id={file_id}">weights.bin</a> (4K)</span>
        <form id="download-form"
              action="https://drive.usercontent.google.com/download" method="get">
          <input type="hidden" name="id" value="{file_id}">
          <input type="hidden" name="export" value="download">
          <input type="hidden" name="confirm" value="t">
          <input type="hidden" name="uuid" value="998877-uuid">
        </form>
      </body>
    </html>
    """

    with respx.mock(assert_all_called=False) as router:
        def _handler(request: httpx.Request) -> httpx.Response:
            if request.url.params.get("uuid") == "998877-uuid":
                return httpx.Response(
                    200,
                    content=payload,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Content-Disposition": 'attachment; filename="weights.bin"',
                        "Content-Length": str(len(payload)),
                    },
                )
            return httpx.Response(
                200,
                text=interstitial_html,
                headers={"Content-Type": "text/html; charset=utf-8"},
            )

        router.get("https://drive.usercontent.google.com/download").mock(side_effect=_handler)

        async with HttpClient() as http:
            source = DriveWebSource(http)
            resolver = Resolver(source)
            item = await resolver.resolve(f"https://drive.google.com/file/d/{file_id}/view")
            assert item.name == "weights.bin"
            item.md5 = expected_md5

            planner = Planner()
            plan = planner.plan_single(item, tmp_path)
            scheduler = Scheduler(
                source,
                FileSystemSink(),
                Verifier(),
                Reporter(),
            )
            tasks = await scheduler.run(plan)

    assert len(tasks) == 1
    assert tasks[0].state == TaskState.DONE
    final_file = tmp_path / "weights.bin"
    assert final_file.exists()
    assert final_file.read_bytes() == payload
    assert not (tmp_path / "weights.bin.part").exists()


@pytest.mark.asyncio
async def test_resume_206_partial_content(tmp_path: Path) -> None:
    file_id = "1Resume206_1234567890"
    full_payload = b"0123456789ABCDEF" * 100  # 1600 bytes
    md5_hex = hashlib.md5(full_payload).hexdigest()

    local_path = tmp_path / "archive.tar"
    part_path = tmp_path / "archive.tar.part"
    part_path.write_bytes(full_payload[:600])

    item = DriveItem(
        id=file_id,
        name="archive.tar",
        size=len(full_payload),
        md5=md5_hex,
        etag="etag-v1",
    )
    task = DownloadTask(
        item=item,
        local_path=local_path,
        relative_path=PurePosixPath("archive.tar"),
        bytes_done=600,
        bytes_total=len(full_payload),
        resume_offset=600,
    )

    with respx.mock as router:
        def _range_handler(request: httpx.Request) -> httpx.Response:
            assert request.headers.get("Range") == "bytes=600-"
            remainder = full_payload[600:]
            return httpx.Response(
                206,
                content=remainder,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Disposition": 'attachment; filename="archive.tar"',
                    "Content-Length": str(len(remainder)),
                    "Accept-Ranges": "bytes",
                },
            )

        router.get("https://drive.usercontent.google.com/download").mock(side_effect=_range_handler)

        async with HttpClient() as http:
            worker = DownloadWorker(
                DriveWebSource(http),
                FileSystemSink(),
                Verifier(),
                Reporter(),
            )
            result = await worker.execute(task)

    assert result.state == TaskState.DONE
    assert local_path.read_bytes() == full_payload
    assert not part_path.exists()


@pytest.mark.asyncio
async def test_resume_200_server_ignores_range_restarts_cleanly(tmp_path: Path) -> None:
    file_id = "1IgnoreRange_12345678"
    full_payload = b"CLEAN_FULL_FILE_CONTENT_12345"
    md5_hex = hashlib.md5(full_payload).hexdigest()

    local_path = tmp_path / "data.bin"
    part_path = tmp_path / "data.bin.part"
    part_path.write_bytes(b"STALE_PARTIAL")

    item = DriveItem(
        id=file_id,
        name="data.bin",
        size=len(full_payload),
        md5=md5_hex,
    )
    task = DownloadTask(
        item=item,
        local_path=local_path,
        relative_path=PurePosixPath("data.bin"),
        bytes_done=13,
        bytes_total=len(full_payload),
        resume_offset=13,
    )

    with respx.mock as router:
        router.get("https://drive.usercontent.google.com/download").mock(
            return_value=httpx.Response(
                200,
                content=full_payload,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Disposition": 'attachment; filename="data.bin"',
                    "Content-Length": str(len(full_payload)),
                },
            )
        )

        async with HttpClient() as http:
            worker = DownloadWorker(
                DriveWebSource(http),
                FileSystemSink(),
                Verifier(),
                Reporter(),
            )
            result = await worker.execute(task)

    assert result.state == TaskState.DONE
    assert local_path.read_bytes() == full_payload


@pytest.mark.asyncio
async def test_resume_416_range_not_satisfiable_promotes_complete_part(tmp_path: Path) -> None:
    file_id = "1AlreadyDone416_12345"
    full_payload = b"COMPLETE_PART_CONTENT"
    md5_hex = hashlib.md5(full_payload).hexdigest()

    local_path = tmp_path / "done.bin"
    part_path = tmp_path / "done.bin.part"
    part_path.write_bytes(full_payload)

    item = DriveItem(id=file_id, name="done.bin", size=len(full_payload), md5=md5_hex)
    task = DownloadTask(
        item=item,
        local_path=local_path,
        relative_path=PurePosixPath("done.bin"),
        bytes_done=len(full_payload),
        bytes_total=len(full_payload),
        resume_offset=len(full_payload),
    )

    with respx.mock as router:
        router.get("https://drive.usercontent.google.com/download").mock(
            return_value=httpx.Response(416)
        )

        async with HttpClient() as http:
            worker = DownloadWorker(
                DriveWebSource(http),
                FileSystemSink(),
                Verifier(),
                Reporter(),
            )
            result = await worker.execute(task)

    assert result.state == TaskState.DONE
    assert local_path.read_bytes() == full_payload
    assert not part_path.exists()


@pytest.mark.asyncio
async def test_interrupted_multipart_download_cleans_up_mpart_and_resumes_safely(
    tmp_path: Path,
) -> None:
    import asyncio

    from gdown2.domain.errors import DownloadCancelledError
    from gdown2.domain.policies import RangePolicy

    file_id = "1MultipartCancel_1234"
    full_payload = b"ABCD" * 256  # 1024 bytes
    local_path = tmp_path / "weights.bin"
    mpart_path = tmp_path / "weights.bin.mpart"
    part_path = tmp_path / "weights.bin.part"

    # Web mode item without MD5
    item = DriveItem(id=file_id, name="weights.bin", size=len(full_payload), md5=None)
    cancel_event = asyncio.Event()

    with respx.mock(assert_all_called=False) as router:
        def _cancel_mid_multipart(request: httpx.Request) -> httpx.Response:
            range_hdr = request.headers.get("Range", "")
            if range_hdr == "bytes=0-511":
                cancel_event.set()
                return httpx.Response(
                    206,
                    content=full_payload[:512],
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Content-Length": "512",
                    },
                )
            return httpx.Response(
                206,
                content=full_payload[512:],
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Length": "512",
                },
            )

        router.get("https://drive.usercontent.google.com/download").mock(
            side_effect=_cancel_mid_multipart
        )

        async with HttpClient() as http:
            worker = DownloadWorker(
                DriveWebSource(http),
                FileSystemSink(),
                Verifier(),
                Reporter(),
                concurrency_policy=ConcurrencyPolicy(chunks_per_file=2),
                range_policy=RangePolicy(min_multipart_size=512),
                cancel_event=cancel_event,
            )
            task1 = Planner().plan_single(item, local_path).tasks[0]
            with pytest.raises(DownloadCancelledError):
                await worker.execute(task1)

    # Cancelled multipart download must not leave a full-size .part or .mpart on disk
    assert not mpart_path.exists()
    assert not part_path.exists()
    assert not local_path.exists()
    assert task1.resume_offset == 0
    assert task1.bytes_done == 0

    # Even if a crash (SIGKILL) left an orphan .mpart with zero holes, the next run must
    # discard it and download the real bytes instead of promoting zero-filled data on 416.
    mpart_path.write_bytes(full_payload[:512] + b"\x00" * 512)

    with respx.mock(assert_all_called=False) as router:
        def _full_handler(request: httpx.Request) -> httpx.Response:
            range_hdr = request.headers.get("Range", "")
            if range_hdr == "bytes=0-511":
                return httpx.Response(
                    206,
                    content=full_payload[:512],
                    headers={"Content-Type": "application/octet-stream", "Content-Length": "512"},
                )
            if range_hdr == "bytes=512-1023":
                return httpx.Response(
                    206,
                    content=full_payload[512:],
                    headers={"Content-Type": "application/octet-stream", "Content-Length": "512"},
                )
            return httpx.Response(416)

        router.get("https://drive.usercontent.google.com/download").mock(side_effect=_full_handler)

        async with HttpClient() as http:
            worker2 = DownloadWorker(
                DriveWebSource(http),
                FileSystemSink(),
                Verifier(),
                Reporter(),
                concurrency_policy=ConcurrencyPolicy(chunks_per_file=2),
                range_policy=RangePolicy(min_multipart_size=512),
            )
            task2 = Planner().plan_single(item, local_path).tasks[0]
            assert task2.resume_offset == 0
            result = await worker2.execute(task2)

    assert result.state == TaskState.DONE
    assert local_path.read_bytes() == full_payload
    assert not mpart_path.exists()
    assert not part_path.exists()



@pytest.mark.asyncio
async def test_429_rate_limit_and_checksum_mismatch_recovery(tmp_path: Path) -> None:
    file_id = "1FlakyFile_1234567890"
    good_payload = b"VERIFIED_PAYLOAD_BYTES"
    bad_payload = b"CORRUPTED_BYTES_______"
    md5_hex = hashlib.md5(good_payload).hexdigest()

    item = DriveItem(id=file_id, name="flaky.bin", size=len(good_payload), md5=md5_hex)
    task = DownloadTask(
        item=item,
        local_path=tmp_path / "flaky.bin",
        relative_path=PurePosixPath("flaky.bin"),
        bytes_total=len(good_payload),
    )

    calls = 0

    def _flaky_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0.01"})
        if calls == 2:
            return httpx.Response(
                200,
                content=bad_payload,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Disposition": 'attachment; filename="flaky.bin"',
                },
            )
        return httpx.Response(
            200,
            content=good_payload,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Disposition": 'attachment; filename="flaky.bin"',
            },
        )

    concurrency = ConcurrencyPolicy(max_files=4, min_files=1)
    retry = RetryPolicy(max_attempts=4, base_delay=0.01, jitter=False)

    with respx.mock as router:
        router.get("https://drive.usercontent.google.com/download").mock(side_effect=_flaky_handler)

        async with HttpClient() as http:
            worker = DownloadWorker(
                DriveWebSource(http),
                FileSystemSink(),
                Verifier(),
                Reporter(),
                retry_policy=retry,
                concurrency_policy=concurrency,
            )
            result = await worker.execute(task)

    assert result.state == TaskState.DONE
    assert result.attempts == 3
    # Rate limit halved concurrency from 4 -> 2.
    assert concurrency.current_limit == 2
    assert (tmp_path / "flaky.bin").read_bytes() == good_payload


@pytest.mark.asyncio
async def test_untrusted_redirect_blocked() -> None:
    with respx.mock as router:
        router.get("https://drive.google.com/uc").mock(
            return_value=httpx.Response(
                302,
                headers={"Location": "https://malicious.example.org/payload.exe"},
            )
        )
        async with HttpClient() as http:
            with pytest.raises(UntrustedRedirectError):
                await http.request("GET", "https://drive.google.com/uc")


@pytest.mark.asyncio
async def test_api_folder_crawler_pagination_and_cycle_guard(tmp_path: Path) -> None:
    root_id = "1RootFolder_123456789"
    sub_id = "1SubFolder_1234567890"
    doc_id = "1GoogleDoc_1234567890"

    with respx.mock as router:
        def _list_handler(request: httpx.Request) -> httpx.Response:
            q = request.url.params.get("q", "")
            page_token = request.url.params.get("pageToken")
            if f"'{root_id}' in parents" in q:
                if not page_token:
                    return httpx.Response(
                        200,
                        json={
                            "nextPageToken": "page-2",
                            "files": [
                                {
                                    "id": sub_id,
                                    "name": "Sub",
                                    "mimeType": "application/vnd.google-apps.folder",
                                }
                            ],
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "files": [
                            {
                                "id": doc_id,
                                "name": "Notes",
                                "mimeType": "application/vnd.google-apps.document",
                            }
                        ]
                    },
                )
            if f"'{sub_id}' in parents" in q:
                # Contains a shortcut pointing back to root_id (cycle!).
                return httpx.Response(
                    200,
                    json={
                        "files": [
                            {
                                "id": "1ShortcutCycle_12345",
                                "name": "LoopBackToRoot",
                                "mimeType": "application/vnd.google-apps.shortcut",
                                "shortcutDetails": {
                                    "targetId": root_id,
                                    "targetMimeType": "application/vnd.google-apps.folder",
                                },
                            }
                        ]
                    },
                )
            return httpx.Response(200, json={"files": []})

        router.get("https://www.googleapis.com/drive/v3/files").mock(side_effect=_list_handler)
        router.get(f"https://www.googleapis.com/drive/v3/files/{root_id}").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": root_id,
                    "name": "Root",
                    "mimeType": "application/vnd.google-apps.folder",
                },
            )
        )
        router.get(f"https://www.googleapis.com/drive/v3/files/{doc_id}/export").mock(
            return_value=httpx.Response(200, content=b"PK\x03\x04DOCX_CONTENT")
        )

        async with HttpClient() as http:
            source = DriveApiSource(http, ApiKeyAuth("test-api-key"))
            resolver = Resolver(source)
            root_item = await resolver.resolve(root_id)
            crawler = FolderCrawler(source)
            tree = await crawler.crawl(root_item)

            assert len(tree.subfolders) == 1
            assert tree.subfolders[0].item.name == "Sub"
            # Cycle back to root was skipped!
            assert len(tree.subfolders[0].subfolders) == 0
            assert len(tree.files) == 1
            assert tree.files[0].relative_path == PurePosixPath("Root/Notes.docx")

            planner = Planner()
            plan = planner.plan_tree(tree, tmp_path)
            scheduler = Scheduler(
                source,
                FileSystemSink(),
                Verifier(),
                Reporter(),
                resume_repo=ResumeStateRepository(tmp_path),
            )
            tasks = await scheduler.run(plan)

    assert len(tasks) == 1
    assert tasks[0].state == TaskState.DONE
    assert (tmp_path / "Root" / "Notes.docx").read_bytes() == b"PK\x03\x04DOCX_CONTENT"


def test_cli_info_and_get_json(tmp_path: Path) -> None:
    file_id = "1CliTestFile_123456789"
    content = b"CLI_TEST_BINARY_DATA"

    with respx.mock:
        respx.get("https://drive.usercontent.google.com/download").mock(
            return_value=httpx.Response(
                200,
                content=content,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Disposition": 'attachment; filename="cli_sample.txt"',
                    "Content-Length": str(len(content)),
                },
            )
        )

        info_res = runner.invoke(app, ["info", file_id, "--json"])
        assert info_res.exit_code == 0
        info_data = json.loads(info_res.stdout.strip())
        assert info_data["id"] == file_id
        assert info_data["name"] == "cli_sample.txt"

        get_res = runner.invoke(
            app,
            ["get", file_id, "--output", str(tmp_path), "--json"],
        )
        assert get_res.exit_code == 0
        events = [json.loads(line) for line in get_res.stdout.strip().splitlines()]
        event_names = [e["event"] for e in events]
        assert "plan_ready" in event_names
        assert "task_done" in event_names
        assert "summary" in event_names
        assert (tmp_path / "cli_sample.txt").read_bytes() == content

