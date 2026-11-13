# Python API

The public API is exported from the top-level `gdown2` package.

```python
from gdown2 import (
    DownloadTask,
    DriveItem,
    FolderNode,
    TaskState,
    download,
    download_sync,
    get_info,
    get_tree,
    resume_directory,
)
```

Advanced helpers are available from `gdown2.sdk`:

```python
from gdown2.sdk import create_runtime, prepare_download
```

---

## `get_info`

```python
async def get_info(
    url_or_id: str,
    *,
    config: AppConfig | None = None,
    source: DriveSource | None = None,
) -> DriveItem
```

Resolves a URL or bare ID and returns metadata. Accepts Drive file/folder links, Google
Docs/Sheets/Slides links, `open?id=` links, `uc?id=` links, and raw IDs.

```python
import asyncio
import gdown2


async def main() -> None:
    item = await gdown2.get_info("1A2B3C4D5E6F7G8H9I0J")
    print(item.name, item.mime_type, item.size)


asyncio.run(main())
```

`DriveItem` fields:

| Field | Type | Description |
|---|---|---|
| `id` | `str` | Drive file or folder ID. |
| `name` | `str` | Display name. |
| `mime_type` | `str` | MIME type reported by Drive. |
| `size` | `int \| None` | Size in bytes when known. |
| `md5` | `str \| None` | MD5 checksum when provided by Drive. |
| `etag` | `str \| None` | Revision marker used for resume validation. |
| `resource_key` | `str \| None` | Sharing resource key for link-shared folders. |
| `is_folder` | `bool` | Whether the item is a folder. |
| `is_google_doc` | `bool` | Whether the item is a Google Workspace document. |

---

## `get_tree`

```python
async def get_tree(
    url_or_id: str,
    *,
    config: AppConfig | None = None,
    source: DriveSource | None = None,
    max_depth: int = 32,
) -> FolderNode
```

Crawls a remote folder and returns a `FolderNode` tree without downloading file content.

```python
tree = await gdown2.get_tree(
    "https://drive.google.com/drive/folders/1FolderId",
    max_depth=8,
)

print(tree.item.name)
for child in tree.children:
    print(child.relative_path, child.item.size)
```

Use `gdown2.domain.tree.compute_tree_stats(tree)` for aggregate counts and known bytes,
`walk_files(tree)` for a flat file iterator, and `render_tree(tree)` for an ASCII view.

---

## `download`

```python
async def download(
    url_or_id: str,
    output: Path | str = ".",
    *,
    config: AppConfig | None = None,
    source: DriveSource | None = None,
    flat: bool = False,
    observer: Observer | None = None,
) -> list[DownloadTask]
```

Downloads a file, folder, or Google Workspace document and returns the final task list.
Each task carries its state, progress counters, local path, and attempt count.

```python
tasks = await gdown2.download(
    "https://drive.google.com/drive/folders/1FolderId",
    output="./dataset",
    flat=False,
)

failed = [task for task in tasks if task.state == gdown2.TaskState.FAILED]
print(f"{len(tasks) - len(failed)} of {len(tasks)} completed")
```

`TaskState` values: `PENDING`, `RUNNING`, `DONE`, `SKIPPED`, `FAILED`, `CANCELLED`.

---

## `download_sync`

```python
def download_sync(
    url_or_id: str,
    output: Path | str = ".",
    *,
    config: AppConfig | None = None,
    flat: bool = False,
) -> list[DownloadTask]
```

Synchronous wrapper around `download()` for scripts and notebooks. It creates and closes
its own event loop, so do not call it from inside a running event loop.

```python
import gdown2

tasks = gdown2.download_sync("1A2B3C4D5E6F7G8H9I0J", output="./data")
```

---

## `resume_directory`

```python
async def resume_directory(
    directory: Path | str = ".",
    *,
    config: AppConfig | None = None,
    source: DriveSource | None = None,
    observer: Observer | None = None,
) -> list[DownloadTask]
```

Loads `.gdown2.state.json` from a directory and resumes incomplete transfers. Safe to
call when no state exists; returns an empty list. Saved paths that would escape the
target directory are ignored.

```python
tasks = await gdown2.resume_directory("./dataset")
```

---

## Custom observers

An observer is any callable that accepts a `TransferEvent`. Register it through the
`observer` argument and receive progress, retry, completion, skip, and failure events.

```python
from gdown2.application.reporter import EventType, TransferEvent


def log_event(event: TransferEvent) -> None:
    if event.event == EventType.TASK_PROGRESS:
        percent = 0.0
        if event.bytes_total:
            percent = 100.0 * event.bytes_done / event.bytes_total
        print(f"{event.name}: {percent:5.1f}% at {event.speed_bps / 1e6:.1f} MB/s")
    elif event.event == EventType.TASK_FAILED:
        print(f"{event.name}: {event.message}")


await gdown2.download("1A2B3C4D5E6F7G8H9I0J", "./data", observer=log_event)
```

For structured output, `gdown2.interfaces.json_events.NdjsonEventWriter` is a ready-made
observer that writes one JSON object per event.

---

## Runtime and configuration

```python
from gdown2.infrastructure.config import AppConfig

config = AppConfig.load(
    cli_overrides={
        "workers": 8,
        "chunks_per_file": 4,
        "rate_limit_rps": 6.0,
        "verify_checksums": True,
    }
)

tasks = await gdown2.download(url, "./data", config=config)
```

`AppConfig.load()` applies precedence: CLI overrides, then environment variables, then
`config.toml`, then defaults. Invalid numeric values in environment or TOML input are
ignored rather than raising.

For full control over the pipeline:

```python
from gdown2.sdk import create_runtime, prepare_download

async with create_runtime(config) as rt:
    plan, tree, resume_repo = await prepare_download(url, output, rt=rt)
    # plan.tasks contains fully resolved local paths and transfer metadata.
    # Construct a Scheduler with rt.source, a FileSystemSink, a Verifier,
    # and your reporter to run the plan with custom policies.
```

The CLI uses this pattern internally; see `gdown2/interfaces/cli/app.py` for a complete
example.

---

## Error handling

All failures derive from `GdownError` and expose a stable `exit_code`, a `retryable`
flag, and optional `item_id`/`status_code` metadata.

```python
from gdown2.domain.errors import (
    AuthRequiredError,
    GdownError,
    IntegrityError,
    PermissionDeniedError,
)

try:
    await gdown2.download(url, "./data")
except PermissionDeniedError as exc:
    print(f"Private file: {exc.message}")
except IntegrityError as exc:
    print(f"Checksum failed: {exc.message}")
except GdownError as exc:
    print(f"Download failed with exit code {int(exc.exit_code)}: {exc.message}")
```

See [Exit codes](../README.md#exit-codes) for the numeric mapping.

---

## Cancellation

`download()` and `resume_directory()` are regular coroutines. Cancel them through
`asyncio.Task.cancel()` or by raising `KeyboardInterrupt` from synchronous code. Resume
state and `.part` files are flushed before the exception propagates.

```python
task = asyncio.create_task(gdown2.download(url, "./data"))
await asyncio.sleep(5)
task.cancel()

try:
    await task
except asyncio.CancelledError:
    print("Cancelled; run resume_directory('./data') to continue")
```
