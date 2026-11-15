"""High-level Python API for gdown2.

Provides async and synchronous helpers for resolving metadata, crawling remote
folder trees, downloading files/folders, and resuming interrupted sessions.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from gdown2.application.crawler import FolderCrawler
from gdown2.application.planner import DownloadPlan, Planner
from gdown2.application.ports import DriveSource
from gdown2.application.reporter import Observer, Reporter
from gdown2.application.resolver import Resolver
from gdown2.application.scheduler import Scheduler
from gdown2.application.verifier import Verifier
from gdown2.domain.models import DownloadTask, DriveItem, FolderNode, TaskState
from gdown2.domain.path_policy import PathPolicy
from gdown2.domain.policies import ConcurrencyPolicy, ExistingFilePolicy, RangePolicy, RetryPolicy
from gdown2.infrastructure.cache import DiskMetadataCache
from gdown2.infrastructure.config import AppConfig, default_cache_path
from gdown2.infrastructure.drive import CompositeDriveSource, DriveApiSource, DriveWebSource
from gdown2.infrastructure.http import HttpClient, TokenBucketRateLimiter
from gdown2.infrastructure.storage import FileSystemSink, ResumeStateRepository


@dataclass(slots=True)
class RuntimeContext:
    """Bundled infrastructure and application components for a single session."""

    config: AppConfig
    http: HttpClient
    source: DriveSource
    cache: DiskMetadataCache
    resolver: Resolver


@asynccontextmanager
async def create_runtime(
    config: AppConfig | None = None,
    *,
    source_override: DriveSource | None = None,
) -> AsyncIterator[RuntimeContext]:
    """Build and tear down an async :class:`RuntimeContext`."""
    cfg = config or AppConfig.load()
    limiter = TokenBucketRateLimiter(rate_per_sec=cfg.rate_limit_rps)

    async with HttpClient(
        rate_limiter=limiter,
        timeout=cfg.timeout_seconds,
    ) as http:
        auth = cfg.build_auth_provider()
        api_source = DriveApiSource(http, auth)
        web_source = DriveWebSource(http)
        source: DriveSource = source_override or CompositeDriveSource(api_source, web_source, auth)
        cache = DiskMetadataCache(
            default_cache_path(),
            ttl_seconds=cfg.cache_ttl_seconds,
        )
        resolver = Resolver(source, cache=cache)
        yield RuntimeContext(
            config=cfg,
            http=http,
            source=source,
            cache=cache,
            resolver=resolver,
        )


async def get_info(
    url_or_id: str,
    *,
    config: AppConfig | None = None,
    source: DriveSource | None = None,
) -> DriveItem:
    """Resolve and return metadata for a Google Drive URL or ID."""
    async with create_runtime(config, source_override=source) as rt:
        return await rt.resolver.resolve(url_or_id)


async def get_tree(
    url_or_id: str,
    *,
    config: AppConfig | None = None,
    source: DriveSource | None = None,
    max_depth: int = 32,
) -> FolderNode:
    """Resolve and crawl a remote Google Drive folder without downloading any files."""
    async with create_runtime(config, source_override=source) as rt:
        item = await rt.resolver.resolve(url_or_id)
        if not item.is_folder:
            node = FolderNode(item=item, relative_path=PurePosixPath(item.name))
            return node
        crawler = FolderCrawler(
            rt.source,
            cache=rt.cache,
            max_depth=max_depth,
            preferred_export_format=rt.config.export_format,
        )
        return await crawler.crawl(item)


async def prepare_download(
    url_or_id: str,
    output: Path,
    *,
    rt: RuntimeContext,
    flat: bool = False,
    existing_policy: ExistingFilePolicy | None = None,
    export_format: str | None = None,
) -> tuple[DownloadPlan, FolderNode | None, ResumeStateRepository]:
    """Resolve target, crawl if folder, and build the :class:`DownloadPlan`."""
    output = output.expanduser()
    policy = existing_policy or rt.config.existing_policy
    fmt = export_format or rt.config.export_format
    path_policy = PathPolicy()
    planner = Planner(
        path_policy=path_policy,
        existing_policy=policy,
        flat=flat,
        export_format=fmt,
    )

    item = await rt.resolver.resolve(url_or_id)
    is_directory_output = output.is_dir() or str(output).endswith(("/", "\\"))
    target_dir = output.resolve() if is_directory_output else output.parent.resolve()
    resume_repo = ResumeStateRepository(output.resolve() if item.is_folder else target_dir)
    saved_states = resume_repo.load_all()

    if item.is_folder:
        crawler = FolderCrawler(
            rt.source,
            path_policy=path_policy,
            cache=rt.cache,
            preferred_export_format=fmt,
        )
        tree = await crawler.crawl(item)
        plan = planner.plan_tree(tree, output, saved_states=saved_states)
        return plan, tree, resume_repo

    plan = planner.plan_single(item, output, saved_states=saved_states)
    return plan, None, resume_repo


async def download(
    url_or_id: str,
    output: Path | str = ".",
    *,
    config: AppConfig | None = None,
    source: DriveSource | None = None,
    flat: bool = False,
    observer: Observer | None = None,
) -> list[DownloadTask]:
    """Download a Google Drive file, folder, or native document asynchronously."""
    out_path = Path(output).expanduser()
    async with create_runtime(config, source_override=source) as rt:
        plan, _, resume_repo = await prepare_download(
            url_or_id,
            out_path,
            rt=rt,
            flat=flat,
        )
        reporter = Reporter()
        if observer is not None:
            reporter.subscribe(observer)

        scheduler = _build_scheduler(rt, reporter, resume_repo)
        tasks = await scheduler.run(plan)
        resume_repo.clear_if_empty()
        return tasks


async def resume_directory(
    directory: Path | str = ".",
    *,
    config: AppConfig | None = None,
    source: DriveSource | None = None,
    observer: Observer | None = None,
) -> list[DownloadTask]:
    """Resume all incomplete tasks recorded in ``<directory>/.gdown2.state.json``."""
    target_dir = Path(directory).expanduser().resolve()
    resume_repo = ResumeStateRepository(target_dir)
    saved = resume_repo.load_all()
    if not saved:
        return []

    async with create_runtime(config, source_override=source) as rt:
        tasks: list[DownloadTask] = []
        dirs_set: set[Path] = {target_dir}

        for state in saved.values():
            raw_path = Path(state.local_path)
            candidate = raw_path if raw_path.is_absolute() else target_dir / raw_path
            local_path = candidate.resolve()
            try:
                local_path.relative_to(target_dir)
            except ValueError:
                continue
            dirs_set.add(local_path.parent)
            part_path = local_path.with_name(f"{local_path.name}.part")
            mpart_path = local_path.with_name(f"{local_path.name}.mpart")
            if mpart_path.exists():
                with contextlib.suppress(OSError):
                    mpart_path.unlink()
            offset = part_path.stat().st_size if part_path.exists() else 0

            item = DriveItem(
                id=state.item_id,
                name=state.name,
                mime_type=state.mime_type,
                size=state.bytes_total,
                md5=state.md5,
                etag=state.etag,
            )
            task = DownloadTask(
                item=item,
                local_path=local_path,
                relative_path=PurePosixPath(state.relative_path),
                state=TaskState.PENDING,
                bytes_done=offset,
                bytes_total=state.bytes_total,
                etag=state.etag,
                resume_offset=offset,
                export_format=state.export_format,
            )
            tasks.append(task)

        plan = DownloadPlan(
            root_dir=target_dir,
            directories=sorted(dirs_set),
            tasks=tasks,
        )
        reporter = Reporter()
        if observer is not None:
            reporter.subscribe(observer)

        scheduler = _build_scheduler(rt, reporter, resume_repo)
        result = await scheduler.run(plan)
        resume_repo.clear_if_empty()
        return result


def download_sync(
    url_or_id: str,
    output: Path | str = ".",
    *,
    config: AppConfig | None = None,
    flat: bool = False,
) -> list[DownloadTask]:
    """Synchronous convenience wrapper around :func:`download`."""
    return asyncio.run(download(url_or_id, output, config=config, flat=flat))


def _build_scheduler(
    rt: RuntimeContext,
    reporter: Reporter,
    resume_repo: ResumeStateRepository,
) -> Scheduler:
    sink = FileSystemSink()
    verifier = Verifier(verify_checksums=rt.config.verify_checksums)
    concurrency = ConcurrencyPolicy(
        max_files=rt.config.workers,
        chunks_per_file=rt.config.chunks_per_file,
    )
    retry = RetryPolicy(max_attempts=rt.config.max_retries)
    range_policy = RangePolicy()
    return Scheduler(
        rt.source,
        sink,
        verifier,
        reporter,
        concurrency_policy=concurrency,
        retry_policy=retry,
        range_policy=range_policy,
        resume_repo=resume_repo,
    )

