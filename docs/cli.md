# CLI reference

The `gdown2` command provides four subcommands plus a cache maintenance command.

```text
gdown2 [--version] COMMAND [ARGS]...
```

| Command | Description |
|---|---|
| `gdown2 info` | Resolve and display metadata for a file, folder, or Google Workspace document. |
| `gdown2 tree` | Crawl and render a remote folder hierarchy without downloading. |
| `gdown2 get` | Download a file, folder, or document. |
| `gdown2 resume` | Resume transfers recorded in `.gdown2.state.json`. |
| `gdown2 cache-clear` | Delete the local metadata cache. |

Global option:

| Option | Description |
|---|---|
| `-V, --version` | Print the installed version and exit. |
| `-h, --help` | Show command help. |

---

## `gdown2 info`

```bash
gdown2 info <url-or-id> [--api-key KEY] [--json]
```

Resolves a Google Drive URL or bare resource ID and prints its metadata.

| Option | Description |
|---|---|
| `URL` | Google Drive file/folder URL, Google Docs/Sheets/Slides URL, or bare ID. |
| `--api-key KEY` | Use Drive API v3 with this key. |
| `--json` | Emit a single NDJSON metadata object. |

Text output includes name, ID, MIME type, size, MD5, ETag, and whether the resource is a
folder or a Google Workspace document.

---

## `gdown2 tree`

```bash
gdown2 tree <folder-url-or-id> [--max-depth N] [--api-key KEY] [--json]
```

Performs a read-only breadth-first crawl and prints a tree with file sizes and aggregate
statistics. Use it to validate folder structure, estimate size, and check pagination
before starting a large download.

| Option | Description |
|---|---|
| `URL` | Google Drive folder URL or ID. |
| `--max-depth N` | Maximum recursion depth. Default: `32`. |
| `--api-key KEY` | Use Drive API v3 with this key. |
| `--json` | Emit a single NDJSON summary object. |

The JSON summary contains `root_id`, `root_name`, `folder_count`, `file_count`,
`google_doc_count`, and `total_bytes`.

---

## `gdown2 get`

```bash
gdown2 get <url-or-id> [OPTIONS]
```

Downloads a single file, a Google Workspace document, or an entire folder tree.

### Output

| Option | Description |
|---|---|
| `-o, --output PATH` | Destination directory, or a file path for a single file. Default: current directory. |
| `--flat` | Flatten the remote hierarchy into the output directory. Collisions are renamed deterministically. |

When `--output` ends with a path separator, it is always treated as a directory. Paths
are validated so that remote names cannot escape the output root.

### Concurrency and rate limiting

| Option | Description |
|---|---|
| `-w, --workers N` | Maximum concurrent file transfers. Default: `6`. |
| `-c, --chunks N` | Parallel HTTP Range streams per large file. Default: `4`. |
| `-r, --rate RPS` | Per-host HTTP request rate limit in requests per second. Default: `5.0`. |

Concurrency is adaptive: `429` and transient `403` responses reduce the active worker
limit, then it gradually recovers.

### Existing files

| Option | Description |
|---|---|
| `--overwrite` | Overwrite files that already exist locally. |
| `--skip` | Skip files that already exist locally. |
| *(default)* | Resume: verify and reuse existing data, or continue from `.part` offset. |

### Google Workspace exports

| Option | Description |
|---|---|
| `--export-format FORMAT` | Export format for Docs/Sheets/Slides/Drawings. Common values: `docx`, `xlsx`, `pptx`, `pdf`, `csv`, `txt`, `md`, `html`, `svg`, `png`. |

If omitted, each document type uses its default Office/PDF-compatible format.

### Integrity and authentication

| Option | Description |
|---|---|
| `--verify / --no-verify` | Enable or disable MD5 verification. Overrides configuration only when explicitly supplied. |
| `--api-key KEY` | Use Drive API v3 with this key. |
| `--service-account FILE` | Path to a GCP service-account JSON key. Requires the `service-account` extra. |

Credentials can also come from environment variables or `config.toml`; see
[Configuration](configuration.md).

### Interfaces

| Option | Description |
|---|---|
| `--tui` | Launch the interactive Textual UI instead of the standard progress display. |
| `--json` | Stream newline-delimited JSON events to stdout. |

`--tui` and `--json` are mutually exclusive in practice: the TUI takes over the terminal.

### Examples

```bash
# Single public file.
gdown2 get "https://drive.google.com/file/d/1A2B3C4D5E6F7G8H9I0J/view" -o ./data

# Recursive folder with tuned concurrency.
gdown2 get "https://drive.google.com/drive/folders/1FolderId" -o ./dataset \
  --workers 8 --chunks 4 --rate 8

# Private folder through a service account.
gdown2 get "https://drive.google.com/drive/folders/1PrivateFolder" -o ./private \
  --service-account ./keys/service-account.json

# Export a Google Doc as PDF.
gdown2 get "https://docs.google.com/document/d/1DocId/edit" --export-format pdf

# Automation output.
gdown2 get "1A2B3C4D5E6F7G8H9I0J" -o ./data --json | jq -c 'select(.event=="task_done")'
```

---

## `gdown2 resume`

```bash
gdown2 resume [DIRECTORY] [-w N] [-r RPS] [--api-key KEY] [--json]
```

Reads `<DIRECTORY>/.gdown2.state.json` and re-enqueues incomplete transfers. Default
directory is the current directory.

| Option | Description |
|---|---|
| `DIRECTORY` | Directory containing `.gdown2.state.json`. Default: `.`. |
| `-w, --workers N` | Override maximum concurrent transfers. |
| `-r, --rate RPS` | Override per-host request rate limit. |
| `--api-key KEY` | Use Drive API v3 with this key. |
| `--json` | Stream NDJSON progress events. |

Resume state is removed automatically when no incomplete transfers remain.

---

## `gdown2 cache-clear`

```bash
gdown2 cache-clear
```

Deletes the local metadata cache used to avoid repeated Drive API/HTML metadata calls.
The cache lives under the OS user cache directory (`gdown2/metadata_cache.json`).

---

## NDJSON event schema

With `--json`, each event is one JSON object per line. The `event` field identifies the
payload:

| Event | Key fields |
|---|---|
| `info` | `id`, `name`, `mime_type`, `is_folder`, `is_google_doc`, `size`, `md5`, `etag` |
| `tree` | `root_id`, `root_name`, `folder_count`, `file_count`, `google_doc_count`, `total_bytes` |
| `plan_ready` | `task_count`, `bytes_done`, `bytes_total` |
| `task_started` | `task_id`, `name`, `relative_path`, `bytes_done`, `bytes_total`, `attempt` |
| `task_progress` | `task_id`, `name`, `relative_path`, `bytes_done`, `bytes_total`, `speed_bps`, `eta_seconds` |
| `task_retry` | `task_id`, `attempt`, `message`, `retry_delay` |
| `task_done` | `task_id`, `name`, `relative_path`, `bytes_done`, `bytes_total`, `speed_bps` |
| `task_skipped` | `task_id`, `name`, `relative_path`, `bytes_done`, `bytes_total` |
| `task_failed` | `task_id`, `name`, `relative_path`, `message`, `attempt` |
| `summary` | `done`, `skipped`, `failed`, `elapsed_seconds`, `bytes_done`, `bytes_total`, `speed_bps` |
| `error` | `error_type`, `message`, `item_id`, `exit_code` |

All events also include a `timestamp` field.

Consumers should ignore unknown events for forward compatibility.
