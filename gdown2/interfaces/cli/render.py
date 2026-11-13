"""Rich terminal renderers for metadata tables, folder trees, and live progress."""

from __future__ import annotations

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.table import Table
from rich.tree import Tree

from gdown2.application.reporter import EventType, TransferEvent
from gdown2.domain.models import DriveItem, FolderNode
from gdown2.domain.tree import compute_tree_stats, format_size


def render_item_info(console: Console, item: DriveItem) -> None:
    """Print a formatted metadata table for a :class:`DriveItem`."""
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Field", style="bold cyan", justify="right")
    table.add_column("Value")

    if item.is_folder:
        kind = "Folder"
    elif item.is_google_doc:
        kind = "Google Workspace Doc"
    else:
        kind = "File"
    table.add_row("ID", item.id)
    table.add_row("Name", item.name)
    table.add_row("Type", f"{kind} ({item.mime_type})")
    table.add_row("Size", "export on the fly" if item.is_google_doc else format_size(item.size))
    if item.md5:
        table.add_row("MD5", item.md5)
    if item.etag:
        table.add_row("ETag", item.etag)
    if item.modified_time:
        table.add_row("Modified", item.modified_time.isoformat())
    if item.is_google_doc:
        export_mime, ext = item.resolve_export()
        table.add_row("Default Export", f"{ext} ({export_mime})")

    console.print(Panel(table, title="[bold]Google Drive Resource[/bold]", expand=False))


def render_folder_tree(console: Console, root: FolderNode) -> None:
    """Render a :class:`FolderNode` hierarchy using Rich's Tree widget."""
    rich_tree = Tree(f"[bold blue]{root.item.name}/[/bold blue]")

    def _populate(node: FolderNode, branch: Tree) -> None:
        for sub in sorted(node.subfolders, key=lambda d: d.item.name.lower()):
            sub_branch = branch.add(f"[bold blue]{sub.item.name}/[/bold blue]")
            _populate(sub, sub_branch)
        for entry in sorted(node.files, key=lambda f: f.item.name.lower()):
            badge = (
                "[magenta][export][/magenta]"
                if entry.item.is_google_doc
                else f"[dim][{format_size(entry.item.size)}][/dim]"
            )
            branch.add(f"{entry.item.name}  {badge}")

    _populate(root, rich_tree)
    console.print(rich_tree)

    stats = compute_tree_stats(root)
    console.print(
        f"\n[dim]{stats.folder_count} folders, {stats.file_count} files "
        f"({stats.google_doc_count} docs), total known size: {format_size(stats.total_bytes)}[/dim]"
    )


class RichProgressObserver:
    """Live multi-bar display showing active file transfers and a global folder bar.

    Completed tasks are removed from the active progress table and printed above the
    live view so the active bar region never exceeds the terminal height (avoiding
    Rich's ``...`` vertical overflow indicator).
    """

    def __init__(self, console: Console) -> None:
        self._console = console
        self.file_progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold]{task.description}[/bold]"),
            BarColumn(bar_width=28),
            TaskProgressColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=console,
        )
        self.overall_progress = Progress(
            TextColumn("[bold cyan]{task.description}[/bold cyan]"),
            BarColumn(bar_width=36, complete_style="cyan", finished_style="green"),
            TaskProgressColumn(),
            TextColumn("[dim]{task.fields[stats]}[/dim]"),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            console=console,
        )
        self._live = Live(
            Group(self.file_progress, self.overall_progress),
            console=console,
            refresh_per_second=10,
        )
        self._task_map: dict[str, TaskID] = {}
        self._active_ratios: dict[str, float] = {}
        self._active_bytes: dict[str, int] = {}
        self._completed_bytes: int = 0
        self._overall_task_id: TaskID | None = None
        self._total_tasks: int = 0
        self._finished_tasks: int = 0

    def __enter__(self) -> RichProgressObserver:
        self._live.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._live.stop()

    def __call__(self, event: TransferEvent) -> None:
        if event.event == EventType.PLAN_READY:
            self._total_tasks = int(event.extra.get("task_count", 0))
            self._finished_tasks = 0
            self._completed_bytes = 0
            if self._total_tasks > 1:
                self._overall_task_id = self.overall_progress.add_task(
                    f"Overall (0/{self._total_tasks} files)",
                    total=float(self._total_tasks),
                    completed=0,
                    stats="0 B",
                )

        elif event.event == EventType.TASK_STARTED and event.task_id:
            label = event.relative_path or event.name or event.task_id
            started_id = self.file_progress.add_task(
                label,
                total=event.bytes_total,
                completed=event.bytes_done,
            )
            self._task_map[event.task_id] = started_id
            self._update_active_metrics(event.task_id, event.bytes_done, event.bytes_total)
            self._refresh_overall()

        elif event.event == EventType.TASK_PROGRESS and event.task_id:
            progress_id = self._task_map.get(event.task_id)
            if progress_id is not None:
                self.file_progress.update(
                    progress_id,
                    completed=event.bytes_done,
                    total=event.bytes_total,
                )
            self._update_active_metrics(event.task_id, event.bytes_done, event.bytes_total)
            self._refresh_overall()

        elif event.event == EventType.TASK_RETRY and event.task_id:
            delay = event.extra.get("retry_delay", "?")
            self._live.console.print(
                f"[yellow]↻ Retry #{event.attempt} for {event.name} in {delay}s: "
                f"{event.message}[/yellow]"
            )

        elif event.event == EventType.TASK_DONE and event.task_id:
            done_id = self._task_map.pop(event.task_id, None)
            if done_id is not None:
                self.file_progress.remove_task(done_id)
            self._active_ratios.pop(event.task_id, None)
            self._active_bytes.pop(event.task_id, None)
            self._completed_bytes += event.bytes_done
            self._finished_tasks += 1
            label = event.relative_path or event.name or event.task_id or "unknown"
            self._live.console.print(
                f"[green]✓[/green] {label} [dim]({format_size(event.bytes_done)})[/dim]"
            )
            self._refresh_overall()

        elif event.event == EventType.TASK_SKIPPED:
            if event.task_id:
                skipped_id = self._task_map.pop(event.task_id, None)
                if skipped_id is not None:
                    self.file_progress.remove_task(skipped_id)
                self._active_ratios.pop(event.task_id, None)
                self._active_bytes.pop(event.task_id, None)
            self._finished_tasks += 1
            label = event.relative_path or event.name or event.task_id or "unknown"
            self._live.console.print(f"[dim]⏭ Skipped (already complete): {label}[/dim]")
            self._refresh_overall()

        elif event.event == EventType.TASK_FAILED:
            if event.task_id:
                failed_id = self._task_map.pop(event.task_id, None)
                if failed_id is not None:
                    self.file_progress.remove_task(failed_id)
                self._active_ratios.pop(event.task_id, None)
                self._active_bytes.pop(event.task_id, None)
            self._finished_tasks += 1
            label = event.relative_path or event.name or event.task_id or "unknown"
            self._live.console.print(f"[bold red]✗ Failed {label}: {event.message}[/bold red]")
            self._refresh_overall()

        elif event.event == EventType.SUMMARY:
            done = event.extra.get("done", 0)
            skipped = event.extra.get("skipped", 0)
            failed = event.extra.get("failed", 0)
            elapsed = event.extra.get("elapsed_seconds", 0.0)
            color = "green" if failed == 0 else "yellow"
            self._live.console.print(
                f"\n[{color}]Finished in {elapsed:.1f}s — "
                f"{done} downloaded, {skipped} skipped, {failed} failed "
                f"({format_size(event.bytes_done)})[/{color}]"
            )

    def _update_active_metrics(
        self,
        task_id: str,
        bytes_done: int,
        bytes_total: int | None,
    ) -> None:
        self._active_bytes[task_id] = bytes_done
        if bytes_total and bytes_total > 0:
            self._active_ratios[task_id] = min(0.999, bytes_done / bytes_total)
        else:
            self._active_ratios[task_id] = 0.0

    def _refresh_overall(self) -> None:
        if self._overall_task_id is None or self._total_tasks <= 0:
            return
        completed_units = float(self._finished_tasks) + sum(self._active_ratios.values())
        completed_units = min(float(self._total_tasks), completed_units)
        total_transferred = self._completed_bytes + sum(self._active_bytes.values())
        self.overall_progress.update(
            self._overall_task_id,
            description=f"Overall ({self._finished_tasks}/{self._total_tasks} files)",
            completed=completed_units,
            stats=format_size(total_transferred),
        )
