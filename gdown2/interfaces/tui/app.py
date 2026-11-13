"""Textual TUI for interactive multi-file Google Drive transfers."""

from __future__ import annotations

from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Header, RichLog, Static, Tree

from gdown2.application.planner import DownloadPlan
from gdown2.application.reporter import EventType, Reporter, TransferEvent
from gdown2.application.scheduler import Scheduler
from gdown2.domain.models import FolderNode
from gdown2.domain.tree import format_size


class GdownTuiApp(App[int]):
    """Three-pane terminal interface displaying the remote tree, transfer table, and log."""

    TITLE = "gdown2 — Google Drive Downloader"
    CSS = """
    Screen {
        layout: vertical;
    }
    #main-panes {
        height: 3fr;
    }
    #tree-pane {
        width: 1fr;
        border: solid $accent;
        padding: 0 1;
    }
    #transfers-pane {
        width: 2fr;
        border: solid $primary;
    }
    #log-pane {
        height: 1fr;
        border: solid $surface-lighten-2;
        padding: 0 1;
    }
    #status-bar {
        height: 1;
        background: $boost;
        color: $text;
        padding: 0 1;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("p", "toggle_pause", "Pause / Resume"),
        Binding("q", "cancel_and_quit", "Quit (Save State)"),
    ]

    def __init__(
        self,
        plan: DownloadPlan,
        scheduler: Scheduler,
        reporter: Reporter,
        folder_tree: FolderNode | None = None,
    ) -> None:
        super().__init__()
        self._plan = plan
        self._scheduler = scheduler
        self._reporter = reporter
        self._folder_tree = folder_tree
        self._row_keys: dict[str, str] = {}
        self._exit_code = 0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main-panes"):
            yield Tree("Remote Target", id="tree-pane")
            with Vertical(id="transfers-pane"):
                yield DataTable(id="transfers-table")
        yield RichLog(id="log-pane", highlight=True, markup=True)
        yield Static("Initializing download engine...", id="status-bar")
        yield Footer()

    async def on_mount(self) -> None:
        tree_widget = self.query_one("#tree-pane", Tree)
        tree_widget.root.expand()
        if self._folder_tree is not None:
            self._populate_tree(tree_widget.root, self._folder_tree)
        else:
            for task in self._plan.tasks:
                tree_widget.root.add_leaf(
                    f"{task.item.name} [{format_size(task.bytes_total)}]"
                )

        table = self.query_one("#transfers-table", DataTable)
        table.add_columns("File", "State", "Progress", "Speed", "ETA")
        for task in self._plan.tasks:
            pct = (
                f"{(task.bytes_done * 100) // task.bytes_total}%"
                if task.bytes_total
                else "0%"
            )
            table.add_row(
                task.relative_path.as_posix(),
                task.state.value,
                f"{format_size(task.bytes_done)} / {format_size(task.bytes_total)} ({pct})",
                "-",
                "-",
                key=task.task_id,
            )
            self._row_keys[task.task_id] = task.task_id

        self._reporter.subscribe(self._on_engine_event)
        self.run_worker(self._execute_plan(), exclusive=True)

    def _populate_tree(self, branch: object, node: FolderNode) -> None:
        from textual.widgets.tree import TreeNode

        assert isinstance(branch, TreeNode)
        branch.set_label(f"{node.item.name}/")
        for sub in node.subfolders:
            sub_node = branch.add(f"{sub.item.name}/", expand=True)
            self._populate_tree(sub_node, sub)
        for entry in node.files:
            tag = "[export]" if entry.item.is_google_doc else f"[{format_size(entry.item.size)}]"
            branch.add_leaf(f"{entry.item.name} {tag}")

    async def _execute_plan(self) -> None:
        tasks = await self._scheduler.run(self._plan)
        failed = sum(1 for t in tasks if t.state.value == "failed")
        cancelled = sum(1 for t in tasks if t.state.value == "cancelled")
        if cancelled:
            self._exit_code = 130
        elif failed:
            self._exit_code = 1
        else:
            self._exit_code = 0

    def _on_engine_event(self, event: TransferEvent) -> None:
        table = self.query_one("#transfers-table", DataTable)
        log = self.query_one("#log-pane", RichLog)
        status = self.query_one("#status-bar", Static)

        if event.task_id and event.task_id in self._row_keys:
            pct = (
                f"{(event.bytes_done * 100) // event.bytes_total}%"
                if event.bytes_total
                else "..."
            )
            prog_str = f"{format_size(event.bytes_done)} / {format_size(event.bytes_total)} ({pct})"
            speed_str = f"{format_size(int(event.speed_bps))}/s" if event.speed_bps > 0 else "-"
            eta_str = f"{int(event.eta_seconds)}s" if event.eta_seconds is not None else "-"
            state_str = event.state.value if event.state else ""

            table.update_cell(event.task_id, "State", state_str)
            table.update_cell(event.task_id, "Progress", prog_str)
            table.update_cell(event.task_id, "Speed", speed_str)
            table.update_cell(event.task_id, "ETA", eta_str)

        if event.event == EventType.TASK_STARTED:
            log.write(f"[cyan]▶ Started[/cyan] {event.relative_path}")
        elif event.event == EventType.TASK_RETRY:
            log.write(
                f"[yellow]↻ Retry #{event.attempt}[/yellow] {event.relative_path}: {event.message}"
            )
        elif event.event == EventType.TASK_DONE:
            log.write(f"[green]✓ Completed[/green] {event.relative_path}")
        elif event.event == EventType.TASK_SKIPPED:
            log.write(f"[dim]⏭ Skipped[/dim] {event.relative_path}")
        elif event.event == EventType.TASK_FAILED:
            log.write(f"[bold red]✗ Failed[/bold red] {event.relative_path}: {event.message}")
        elif event.event == EventType.SUMMARY:
            done = event.extra.get("done", 0)
            skipped = event.extra.get("skipped", 0)
            failed = event.extra.get("failed", 0)
            elapsed = event.extra.get("elapsed_seconds", 0.0)
            msg = (
                f"Done: {done} | Skipped: {skipped} | Failed: {failed} "
                f"| Elapsed: {elapsed:.1f}s (Press 'q' to exit)"
            )
            status.update(msg)
            log.write(f"[bold]{msg}[/bold]")
            return

        global_speed = format_size(int(self._reporter.global_speed_bps))
        done_bytes = format_size(self._reporter.total_bytes_done)
        total_bytes = format_size(self._reporter.total_bytes_expected)
        paused_tag = " [PAUSED]" if self._scheduler.is_paused else ""
        status.update(
            f"Transferred: {done_bytes} / {total_bytes} | "
            f"Speed: {global_speed}/s{paused_tag}"
        )

    def action_toggle_pause(self) -> None:
        log = self.query_one("#log-pane", RichLog)
        if self._scheduler.is_paused:
            self._scheduler.resume()
            log.write("[green]▶ Resumed transfers[/green]")
        else:
            self._scheduler.pause()
            log.write("[yellow]⏸ Paused transfers[/yellow]")

    def action_cancel_and_quit(self) -> None:
        self._scheduler.cancel()
        self._exit_code = 130
        self.exit(self._exit_code)

