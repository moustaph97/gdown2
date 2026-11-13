# gdown2

**Fast, crash-safe Google Drive downloads for files, folders, and Google Workspace documents.**

`gdown2` is a ground-up rewrite of [`gdown`](https://github.com/wkentaro/gdown) built on
`asyncio` and `httpx`. It keeps the zero-config convenience of public Drive links while
adding a resumable transfer engine, adaptive rate limiting, recursive folder crawling,
checksum verification, three user interfaces, and a clean Python API.

[![CI](https://github.com/moustaph97/gdown2/actions/workflows/ci.yml/badge.svg)](https://github.com/moustaph97/gdown2/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Code style: Ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)

---

## Why gdown2?

The original `gdown` is excellent for quick scripts. It becomes fragile when you pull
multi-gigabyte checkpoints, deeply nested folders with hundreds of files, or unstable
connections that drop mid-transfer.

`gdown2` was designed for those cases:

| Capability | What it means in practice |
|---|---|
| **Crash-safe resume** | Data is streamed to `<filename>.part`; completed files are promoted with an atomic `os.replace`. Interrupted transfers resume from the last byte. |
| **Google Drive API v3 + public web** | Works out of the box on public links, then transparently switches to the official API when given an API key, OAuth token, or service account. |
| **Recursive folder crawling** | Full BFS traversal with pagination, shortcut-cycle protection, filename sanitization, and collision handling. |
| **Two-level concurrency** | Parallel file downloads (`--workers`) plus optional multi-Range chunking for large files (`--chunks`). |
| **Adaptive throttling** | Per-host token bucket (`--rate`) and AIMD backoff when Google returns `429` or transient `403` responses. |
| **Integrity verification** | Size and MD5 checks after every transfer, with automatic retry and clean restart on corruption. |
| **Workspace exports** | Google Docs, Sheets, Slides, and Drawings are exported on the fly to Office, PDF, CSV, and other formats. |
| **Three interfaces** | Rich CLI, interactive Textual TUI, and newline-delimited JSON for automation. |

---

## Table of contents

- [Installation](#installation)
- [Quick start](#quick-start)
- [CLI overview](#cli-overview)
- [Python API](#python-api)
- [Authentication](#authentication)
- [Configuration](#configuration)
- [Reliability model](#reliability-model)
- [Exit codes](#exit-codes)
- [Documentation](#documentation)
- [Development](#development)
- [Project layout](#project-layout)
- [Contributing](#contributing)
- [License](#license)

---

## Installation

Requires **Python 3.11 or newer**.

### From source

```bash
git clone https://github.com/moustaph97/gdown2.git
cd gdown2
pip install .
```

### Editable development install

```bash
git clone https://github.com/moustaph97/gdown2.git
cd gdown2
pip install -e ".[dev]"
```

### Optional service-account support

Service-account authentication requires `google-auth`:

```bash
pip install "gdown2[service-account]"
```

Verify the installation:

```bash
gdown2 --version
gdown2 --help
```

---

## Quick start

### 1. Inspect metadata

```bash
gdown2 info "https://drive.google.com/file/d/1A2B3C4D5E6F7G8H9I0J/view?usp=sharing"
```

### 2. Preview a remote folder without downloading

```bash
gdown2 tree "https://drive.google.com/drive/folders/1FolderId_9876543210"
```

```text
Dataset/
├── train/
│   ├── part-000.parquet  [412.5 MB]
│   └── part-001.parquet  [410.1 MB]
├── Notes.docx  [export]
└── README.md  [4.2 KB]

1 folders, 4 files (1 docs), total known size: 822.6 MB
```

### 3. Download a file or an entire folder

```bash
# Single file or recursive folder download.
gdown2 get "https://drive.google.com/drive/folders/1FolderId_9876543210" -o ./downloads

# Six parallel files, four Range chunks per large file, five requests per second.
gdown2 get <url-or-id> -o ./downloads --workers 6 --chunks 4 --rate 5.0

# Export Google Docs/Sheets/Slides as PDF instead of their Office default.
gdown2 get "https://docs.google.com/document/d/1DocId_1234567890abc/edit" --export-format pdf

# Flatten the remote hierarchy into one directory.
gdown2 get <folder-url> -o ./downloads --flat

# Launch the interactive three-pane TUI.
gdown2 get <folder-url> -o ./downloads --tui

# Stream machine-readable NDJSON events for CI and automation.
gdown2 get <url-or-id> -o ./downloads --json
```

### 4. Resume an interrupted session

If a transfer is interrupted by `Ctrl+C`, a network drop, or a Drive quota limit:

```bash
gdown2 resume ./downloads
```

Resume state is stored in `<output>/.gdown2.state.json`; partial data lives in
`<filename>.part` files. Run the command from the same output directory to continue
exactly where the previous session stopped.

---

## CLI overview

| Command | Purpose |
|---|---|
| `gdown2 info <url-or-id>` | Resolve and display metadata for a file, folder, or document. |
| `gdown2 tree <folder-url-or-id>` | Crawl a remote folder and print its hierarchy without downloading. |
| `gdown2 get <url-or-id> [-o DIR]` | Download a file, folder, or Google Workspace document. |
| `gdown2 resume [DIR]` | Resume incomplete transfers recorded in a directory. |
| `gdown2 cache-clear` | Delete the local metadata cache. |

Common `get` options:

| Option | Description |
|---|---|
| `-o, --output PATH` | Destination directory or file path. Defaults to the current directory. |
| `-w, --workers N` | Maximum concurrent file transfers. |
| `-c, --chunks N` | Parallel HTTP Range streams per large file. |
| `-r, --rate RPS` | Per-host request rate limit in requests per second. |
| `--flat` | Flatten the remote tree into the output directory. |
| `--overwrite` | Overwrite existing local files. |
| `--skip` | Skip files that already exist locally. |
| `--export-format FORMAT` | Export format for Google Workspace documents (`docx`, `xlsx`, `pptx`, `pdf`, `csv`, `txt`, ...). |
| `--verify / --no-verify` | Enable or disable MD5 verification. |
| `--api-key KEY` | Use Google Drive API v3 with an API key. |
| `--service-account FILE` | Use a GCP service-account JSON key. |
| `--tui` | Launch the interactive terminal UI. |
| `--json` | Emit newline-delimited JSON events. |

See the full [CLI reference](docs/cli.md) for every command and option.

---

## Python API

`gdown2` exposes both `async` functions and a synchronous convenience wrapper.

```python
import asyncio
import gdown2


def main() -> None:
    # Synchronous helper for scripts and notebooks.
    tasks = gdown2.download_sync(
        "https://drive.google.com/file/d/1A2B3C4D5E6F7G8H9I0J/view",
        output="./data",
    )
    print(f"Downloaded {len(tasks)} task(s)")


async def async_example() -> None:
    info = await gdown2.get_info("1A2B3C4D5E6F7G8H9I0J")
    print(f"Resolved: {info.name} ({info.size} bytes)")

    tree = await gdown2.get_tree(
        "https://drive.google.com/drive/folders/1FolderId_9876543210"
    )
    tasks = await gdown2.download(tree.item.id, output="./data")
    print(f"Completed {sum(t.state == 'done' for t in tasks)} file(s)")


main()
asyncio.run(async_example())
```

For custom observers, runtime configuration, and cancellation patterns, see
[Python API](docs/python-api.md).

---

## Authentication

Public links work with no configuration. Providing credentials switches `gdown2` from
web scraping to the official Drive API v3, which is recommended for large folders,
private files, and higher rate limits.

Credential sources can be combined with CLI flags, environment variables, and
`config.toml`. When more than one credential is available, the selected provider order
is:

1. Service account (`--service-account` / `GDOWN2_SERVICE_ACCOUNT_FILE`)
2. OAuth user file (`GDOWN2_OAUTH_FILE`)
3. OAuth bearer token (`GDOWN2_ACCESS_TOKEN`)
4. API key (`--api-key` / `GDOWN2_API_KEY` / `GOOGLE_API_KEY`)
5. Anonymous public access

Environment examples:

```bash
# API key
export GDOWN2_API_KEY="AIza..."

# OAuth bearer token
export GDOWN2_ACCESS_TOKEN="ya29..."

# OAuth authorized_user JSON file
export GDOWN2_OAUTH_FILE="$HOME/.config/gdown2/authorized_user.json"

# GCP service account
export GDOWN2_SERVICE_ACCOUNT_FILE="$HOME/.config/gdown2/service_account.json"
```

See [Configuration](docs/configuration.md) for the complete authentication and settings
reference.

---

## Configuration

Configuration is merged with the following precedence:

**CLI flags > environment variables > `config.toml` > built-in defaults**

Default config location: `~/.config/gdown2/config.toml` (or your OS user config
directory when that path does not exist).

```toml
[download]
workers = 6
chunks_per_file = 4
rate_limit_rps = 5.0
max_retries = 5
timeout_seconds = 30.0
verify_checksums = true
existing_policy = "resume"   # "resume" | "skip" | "overwrite"
export_format = "pdf"

[auth]
# Supplying credentials switches gdown2 to Drive API v3.
api_key = "AIza..."
# oauth_token_file = "~/.config/gdown2/authorized_user.json"
# service_account_file = "~/.config/gdown2/service_account.json"
```

---

## Reliability model

`gdown2` treats downloads as recoverable transactions rather than one-shot streams.

- **Never writes directly to the final filename.** Incoming bytes go to
  `<filename>.part`. The final file appears only after verification succeeds.
- **Handles `206`, `200`, and `416` correctly.** If the server honours `Range`, the
  transfer resumes. If it ignores `Range` and returns `200`, the partial file is reset
  instead of being corrupted. If it returns `416`, the existing part is verified and
  promoted when complete.
- **Protects against stale partial data.** When the remote ETag changes or overwrite mode
  is requested, stale `.part` files are discarded before a new transfer starts.
- **Verifies before promoting.** Size and MD5 checks run before the atomic rename.
  Checksum failures delete the corrupted part and retry from byte zero.
- **Survives cancellation.** `Ctrl+C` flushes resume state and leaves partial files
  ready for `gdown2 resume`.
- **Rejects untrusted redirects.** Only HTTPS Google domains are followed during
  redirects and initial download URLs are validated before use.

The complete design and edge-case walkthrough is in [Architecture](docs/architecture.md).

---

## Exit codes

| Code | Name | Meaning |
|---|---|---|
| `0` | `OK` | All requested files downloaded (or skipped) and verified. |
| `1` | `GENERAL_ERROR` | One or more transfers failed. |
| `2` | `INVALID_ARGUMENT` | Malformed URL or unrecognized Google Drive ID. |
| `3` | `AUTH_REQUIRED` | HTTP 401 or expired/missing credentials. |
| `4` | `PERMISSION_DENIED` | HTTP 403 permission error (private file or folder). |
| `5` | `NOT_FOUND` | HTTP 404 / 410 resource does not exist. |
| `6` | `QUOTA_EXCEEDED` | Per-file download quota or rate limit exhausted. |
| `7` | `NETWORK_ERROR` | Unrecoverable transport error or untrusted redirect. |
| `8` | `INTEGRITY_ERROR` | Size or MD5 checksum mismatch after retries. |
| `9` | `STORAGE_ERROR` | Insufficient disk space, permission error, or path traversal. |
| `130` | `CANCELLED` | Interrupted by the user (`Ctrl+C`). |

This allows shell scripts and CI jobs to distinguish transient network problems from
permission or quota failures.

---

## Documentation

- [CLI reference](docs/cli.md)
- [Python API](docs/python-api.md)
- [Configuration](docs/configuration.md)
- [Architecture](docs/architecture.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Development guide](docs/development.md)

---

## Development

```bash
# Create an isolated environment.
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS / Linux

# Install with development dependencies.
pip install -e ".[dev]"

# Run the test suite.
pytest

# Lint and type-check.
ruff check .
mypy gdown2
```

The test suite uses `respx` to mock Google Drive endpoints. It covers URL parsing,
cross-platform path sanitization, tree operations, retry/AIMD policies, virus-scan
interstitials, `206`/`200`/`416` resume behaviour, rate-limit backoff, checksum
recovery, folder pagination, and shortcut cycles.

Live end-to-end tests are opt-in:

```bash
GDOWN2_E2E_URL="https://drive.google.com/file/d/<id>/view" pytest tests/e2e
```

See [Development guide](docs/development.md) for the full workflow.

---

## Project layout

```text
gdown2/
├── domain/            # Pure models, URL parser, tree operations, path policy, errors
├── application/       # Resolver, crawler, planner, scheduler, worker, verifier, reporter
├── infrastructure/    # HTTP, Drive API/web adapters, auth, cache, config, storage
├── interfaces/        # Typer/Rich CLI, Textual TUI, NDJSON event writer
├── sdk.py             # Public async and synchronous Python API
└── py.typed           # PEP 561 typing marker
```

The `domain` layer has no network or filesystem dependencies. Application services
coordinate work through protocol interfaces, and infrastructure adapters can be replaced
without touching the engine. See [Architecture](docs/architecture.md) for details.

---

## Contributing

Contributions are welcome.

1. Fork the repository and create a feature branch.
2. Install the development dependencies with `pip install -e ".[dev]"`.
3. Add or update tests for your change.
4. Run `pytest`, `ruff check .`, and `mypy gdown2`.
5. Open a pull request with a clear description of the problem and solution.

Please keep user-facing strings, comments, and documentation in English.

---

## License

MIT. See [LICENSE](LICENSE).
