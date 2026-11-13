"""Typer CLI entry point for gdown2."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console

from gdown2.application.reporter import Reporter
from gdown2.application.scheduler import Scheduler
from gdown2.application.verifier import Verifier
from gdown2.domain.errors import ExitCode, GdownError
from gdown2.domain.models import TaskState
from gdown2.domain.policies import ConcurrencyPolicy, ExistingFilePolicy, RangePolicy, RetryPolicy
from gdown2.domain.tree import compute_tree_stats
from gdown2.infrastructure.cache import DiskMetadataCache
from gdown2.infrastructure.config import AppConfig, default_cache_path
from gdown2.infrastructure.storage import FileSystemSink
from gdown2.interfaces.cli.render import (
    RichProgressObserver,
    render_folder_tree,
    render_item_info,
)
from gdown2.interfaces.json_events import NdjsonEventWriter
from gdown2.sdk import create_runtime, get_info, get_tree, prepare_download, resume_directory

app = typer.Typer(
    name="gdown2",
    help="Fast, resumable Google Drive file and folder downloader.",
    add_completion=False,
    no_args_is_help=True,
)

console = Console()
err_console = Console(stderr=True)


def _version_callback(value: bool) -> None:
    if value:
        from gdown2 import __version__

        console.print(f"gdown2 {__version__}")
        raise typer.Exit(0)


@app.callback()
def _main_callback(
    version: Annotated[
        bool | None,
        typer.Option(
            "--version",
            "-V",
            callback=_version_callback,
            is_eager=True,
            help="Show version and exit.",
        ),
    ] = None,
) -> None:
    """gdown2 command line interface."""


@app.command("info")
def cmd_info(
    url: Annotated[str, typer.Argument(help="Google Drive URL or resource ID.")],
    api_key: Annotated[
        str | None,
        typer.Option("--api-key", help="Google Drive API v3 key."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit metadata as JSON."),
    ] = False,
) -> None:
    """Resolve and display metadata for a Google Drive file, folder, or document."""
    cfg = AppConfig.load(cli_overrides={"api_key": api_key})
    try:
        item = asyncio.run(get_info(url, config=cfg))
    except GdownError as exc:
        _handle_cli_error(exc, json_output=json_output)
        return

    if json_output:
        NdjsonEventWriter().write_object(
            {
                "event": "info",
                "id": item.id,
                "name": item.name,
                "mime_type": item.mime_type,
                "is_folder": item.is_folder,
                "is_google_doc": item.is_google_doc,
                "size": item.size,
                "md5": item.md5,
                "etag": item.etag,
            }
        )
    else:
        render_item_info(console, item)


@app.command("tree")
def cmd_tree(
    url: Annotated[str, typer.Argument(help="Google Drive folder URL or ID.")],
    max_depth: Annotated[
        int,
        typer.Option("--max-depth", help="Maximum folder recursion depth."),
    ] = 32,
    api_key: Annotated[
        str | None,
        typer.Option("--api-key", help="Google Drive API v3 key."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit folder tree summary as JSON."),
    ] = False,
) -> None:
    """Inspect a remote Google Drive folder hierarchy without downloading (dry-run)."""
    cfg = AppConfig.load(cli_overrides={"api_key": api_key})
    try:
        tree = asyncio.run(get_tree(url, config=cfg, max_depth=max_depth))
    except GdownError as exc:
        _handle_cli_error(exc, json_output=json_output)
        return

    if json_output:
        stats = compute_tree_stats(tree)
        NdjsonEventWriter().write_object(
            {
                "event": "tree",
                "root_id": tree.item.id,
                "root_name": tree.item.name,
                "folder_count": stats.folder_count,
                "file_count": stats.file_count,
                "google_doc_count": stats.google_doc_count,
                "total_bytes": stats.total_bytes,
            }
        )
    else:
        render_folder_tree(console, tree)


@app.command("get")
def cmd_get(
    url: Annotated[str, typer.Argument(help="Google Drive URL or resource ID.")],
    output: Annotated[
        Path,
        typer.Option(
            "--output",
            "-o",
            help="Destination directory or file path (defaults to current directory).",
        ),
    ] = Path("."),
    workers: Annotated[
        int | None,
        typer.Option(
            "--workers",
            "-w",
            help="Maximum concurrent file transfers (default: 6).",
        ),
    ] = None,
    chunks: Annotated[
        int | None,
        typer.Option(
            "--chunks",
            "-c",
            help="Parallel Range streams per large file (default: 4).",
        ),
    ] = None,
    rate: Annotated[
        float | None,
        typer.Option(
            "--rate",
            "-r",
            help="Per-host HTTP request rate limit in req/s (default: 5.0).",
        ),
    ] = None,
    flat: Annotated[
        bool,
        typer.Option("--flat", help="Flatten folder hierarchy into the output directory."),
    ] = False,
    overwrite: Annotated[
        bool,
        typer.Option("--overwrite", help="Overwrite existing local files."),
    ] = False,
    skip: Annotated[
        bool,
        typer.Option("--skip", help="Skip files that already exist locally."),
    ] = False,
    export_format: Annotated[
        str | None,
        typer.Option(
            "--export-format",
            help="Export format for Google Docs/Sheets/Slides (docx, xlsx, pptx, pdf, csv, txt).",
        ),
    ] = None,
    verify: Annotated[
        bool | None,
        typer.Option(
            "--verify/--no-verify",
            help="Verify MD5 checksums after download (default: from config).",
        ),
    ] = None,
    api_key: Annotated[
        str | None,
        typer.Option("--api-key", help="Google Drive API v3 key."),
    ] = None,
    service_account: Annotated[
        Path | None,
        typer.Option("--service-account", help="Path to GCP service account JSON key."),
    ] = None,
    tui: Annotated[
        bool,
        typer.Option("--tui", help="Launch interactive Textual TUI."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Stream progress as NDJSON events."),
    ] = False,
) -> None:
    """Download a file, folder, or Google Workspace document."""
    overrides: dict[str, Any] = {
        "workers": workers,
        "chunks_per_file": chunks,
        "rate_limit_rps": rate,
        "export_format": export_format,
        "api_key": api_key,
        "service_account_file": str(service_account) if service_account else None,
    }
    if verify is not None:
        overrides["verify_checksums"] = verify
    if overwrite:
        overrides["existing_policy"] = ExistingFilePolicy.OVERWRITE.value
    elif skip:
        overrides["existing_policy"] = ExistingFilePolicy.SKIP.value

    cfg = AppConfig.load(cli_overrides={k: v for k, v in overrides.items() if v is not None})

    try:
        if tui:
            exit_code = _run_with_tui(url, output, cfg, flat=flat)
            raise typer.Exit(exit_code)

        exit_code = asyncio.run(
            _run_cli_download(url, output, cfg, flat=flat, json_output=json_output)
        )
    except KeyboardInterrupt:
        err_console.print("\n[yellow]Interrupted by user. Partial state saved for resume.[/yellow]")
        raise typer.Exit(int(ExitCode.CANCELLED)) from None
    except GdownError as exc:
        _handle_cli_error(exc, json_output=json_output)
        return

    raise typer.Exit(exit_code)


@app.command("resume")
def cmd_resume(
    directory: Annotated[
        Path,
        typer.Argument(help="Directory containing a .gdown2.state.json file."),
    ] = Path("."),
    workers: Annotated[
        int | None,
        typer.Option("--workers", "-w", help="Maximum concurrent file transfers."),
    ] = None,
    rate: Annotated[
        float | None,
        typer.Option("--rate", "-r", help="Per-host HTTP request rate limit (req/s)."),
    ] = None,
    api_key: Annotated[
        str | None,
        typer.Option("--api-key", help="Google Drive API v3 key."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Stream progress as NDJSON events."),
    ] = False,
) -> None:
    """Resume interrupted downloads from saved transfer state in a directory."""
    overrides: dict[str, Any] = {
        "workers": workers,
        "rate_limit_rps": rate,
        "api_key": api_key,
    }
    cfg = AppConfig.load(cli_overrides={k: v for k, v in overrides.items() if v is not None})

    try:
        if json_output:
            tasks = asyncio.run(
                resume_directory(directory, config=cfg, observer=NdjsonEventWriter())
            )
        else:
            with RichProgressObserver(console) as obs:
                tasks = asyncio.run(resume_directory(directory, config=cfg, observer=obs))
    except KeyboardInterrupt:
        err_console.print("\n[yellow]Interrupted by user. Partial state saved.[/yellow]")
        raise typer.Exit(int(ExitCode.CANCELLED)) from None
    except GdownError as exc:
        _handle_cli_error(exc, json_output=json_output)
        return

    if not tasks and not json_output:
        console.print(f"[dim]No pending transfers found in {directory.resolve()}[/dim]")
        raise typer.Exit(0)

    failed = sum(1 for t in tasks if t.state == TaskState.FAILED)
    raise typer.Exit(int(ExitCode.GENERAL_ERROR if failed > 0 else ExitCode.OK))


@app.command("cache-clear")
def cmd_cache_clear() -> None:
    """Clear the local metadata cache."""
    cache = DiskMetadataCache(default_cache_path())
    cache.clear()
    console.print("[green]Metadata cache cleared.[/green]")


async def _run_cli_download(
    url: str,
    output: Path,
    cfg: AppConfig,
    *,
    flat: bool,
    json_output: bool,
) -> int:
    async with create_runtime(cfg) as rt:
        plan, _, resume_repo = await prepare_download(url, output, rt=rt, flat=flat)
        reporter = Reporter()
        scheduler = Scheduler(
            rt.source,
            FileSystemSink(),
            Verifier(verify_checksums=cfg.verify_checksums),
            reporter,
            concurrency_policy=ConcurrencyPolicy(
                max_files=cfg.workers,
                chunks_per_file=cfg.chunks_per_file,
            ),
            retry_policy=RetryPolicy(max_attempts=cfg.max_retries),
            range_policy=RangePolicy(),
            resume_repo=resume_repo,
        )

        if json_output:
            reporter.subscribe(NdjsonEventWriter())
            tasks = await scheduler.run(plan)
        else:
            with RichProgressObserver(console) as obs:
                reporter.subscribe(obs)
                tasks = await scheduler.run(plan)

        resume_repo.clear_if_empty()
        failed = sum(1 for t in tasks if t.state == TaskState.FAILED)
        return int(ExitCode.GENERAL_ERROR if failed > 0 else ExitCode.OK)


def _run_with_tui(url: str, output: Path, cfg: AppConfig, *, flat: bool) -> int:
    from gdown2.interfaces.tui.app import GdownTuiApp

    async def _prepare_and_launch() -> int:
        async with create_runtime(cfg) as rt:
            plan, tree, resume_repo = await prepare_download(url, output, rt=rt, flat=flat)
            reporter = Reporter()
            scheduler = Scheduler(
                rt.source,
                FileSystemSink(),
                Verifier(verify_checksums=cfg.verify_checksums),
                reporter,
                concurrency_policy=ConcurrencyPolicy(
                    max_files=cfg.workers,
                    chunks_per_file=cfg.chunks_per_file,
                ),
                retry_policy=RetryPolicy(max_attempts=cfg.max_retries),
                range_policy=RangePolicy(),
                resume_repo=resume_repo,
            )
            tui_app = GdownTuiApp(plan, scheduler, reporter, folder_tree=tree)
            res = await tui_app.run_async()
            resume_repo.clear_if_empty()
            return int(res or 0)

    return asyncio.run(_prepare_and_launch())


def _handle_cli_error(exc: GdownError, *, json_output: bool) -> None:
    if json_output:
        NdjsonEventWriter().write_object(
            {
                "event": "error",
                "error_type": type(exc).__name__,
                "message": exc.message,
                "item_id": exc.item_id,
                "exit_code": int(exc.exit_code),
            }
        )
    else:
        err_console.print(f"[bold red]Error:[/bold red] {exc.message}")
    raise typer.Exit(int(exc.exit_code))


def main() -> None:
    """Console script entry point."""
    app()

