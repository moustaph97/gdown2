# Architecture

`gdown2` is organized as a ports-and-adapters application. The domain layer is pure
Python with no network or filesystem access. Application services orchestrate work
through protocol interfaces, and infrastructure adapters provide HTTP, Drive, storage,
authentication, caching, and configuration implementations.

## Design goals

- **Crash-safe by default.** Every transfer can be interrupted and resumed without
  corrupting an existing file.
- **Zero-config first.** Public Google Drive links work without credentials.
- **Transparently upgradeable.** API keys, OAuth tokens, and service accounts switch the
  source adapter from web scraping to Drive API v3.
- **Testable offline.** All HTTP is mocked with `respx`; the engine has no dependency on
  live Google endpoints.
- **Cross-platform.** Filename sanitization handles Windows reserved names, forbidden
  characters, duplicate siblings, path-length limits, and traversal attempts.

## Why Python with asyncio and httpx?

Downloading files and crawling folder trees is strictly I/O-bound: network sockets and
disk writes dominate. Python's `asyncio` provides native I/O concurrency, and `httpx`
offers a modern HTTP/2-capable client with streaming, cookies, and redirect control.
A C++ or Rust rewrite would not increase wire throughput while making TLS, HTML/JSON
parsing, and Google endpoint quirks significantly more expensive to maintain.

## Layered view

```text
┌──────────────────────────────────────────────────────────────┐
│ Interfaces                                                   │
│ Typer/Rich CLI · Textual TUI · NDJSON events · Python SDK     │
├──────────────────────────────────────────────────────────────┤
│ Application services                                         │
│ Resolver · FolderCrawler · Planner · Scheduler · Worker       │
│ Verifier · ExportService · Reporter                           │
├──────────────────────────────────────────────────────────────┤
│ Domain (pure, zero I/O)                                       │
│ Models · URL parser · Tree · PathPolicy · Policies · Errors   │
├──────────────────────────────────────────────────────────────┤
│ Infrastructure adapters                                       │
│ HttpClient · Drive API/web sources · Auth · Cache · Config ·  │
│ FileSystemSink · ResumeStateRepository                        │
└──────────────────────────────────────────────────────────────┘
```

The `domain` package knows nothing about HTTP or the local filesystem. Application
services depend on `application/ports.py` protocols, so the engine can be tested against
mocked transports and adapted when Google changes its public HTML.

## Package layout

```text
gdown2/
├── __init__.py                  # Public exports and package version
├── __main__.py                  # python -m gdown2
├── sdk.py                       # High-level async and sync API
├── py.typed                     # PEP 561 marker
├── domain/
│   ├── models.py                # DriveItem, DownloadTask, FolderNode, TransferState
│   ├── url_parser.py            # URL and bare-ID normalization
│   ├── tree.py                  # Traversal, statistics, ASCII rendering
│   ├── path_policy.py           # Filename sanitization and traversal guards
│   ├── policies.py              # Retry, concurrency (AIMD), ranges, existing-file policy
│   └── errors.py                # Exception hierarchy and exit codes
├── application/
│   ├── ports.py                 # DriveSource, MediaStream, cache, resume protocols
│   ├── resolver.py              # URL/ID to DriveItem resolution
│   ├── crawler.py               # Breadth-first folder crawler
│   ├── planner.py               # Deterministic DownloadPlan construction
│   ├── scheduler.py             # Bounded queue, worker pool, pause/resume/cancel
│   ├── worker.py                # Single-task transfer, resume, retries, verification
│   ├── verifier.py              # Size and MD5 verification
│   ├── exporter.py              # Google Workspace export streaming
│   └── reporter.py              # Event hub, EMA speed, ETA, throttled notifications
├── infrastructure/
│   ├── http/
│   │   ├── client.py            # httpx wrapper, redirect whitelist, rate limiting
│   │   ├── retry.py             # HTTP status classification and Retry-After parsing
│   │   └── ratelimit.py         # Per-host async token bucket
│   ├── drive/
│   │   ├── source_api.py        # Drive API v3 adapter
│   │   ├── source_web.py        # Public web adapter and interstitial handling
│   │   ├── source_composite.py  # API when authenticated, web otherwise
│   │   └── metadata.py          # JSON/HTML/header normalization
│   ├── auth/
│   │   ├── base.py              # AuthProvider protocol
│   │   ├── anonymous.py         # Public access
│   │   ├── api_key.py           # API key query parameter
│   │   ├── oauth.py             # OAuth user token and refresh flow
│   │   └── service_account.py   # GCP service-account credentials
│   ├── storage/
│   │   ├── fs_sink.py           # Directory creation, .part files, atomic promotion
│   │   └── resume_state.py      # JSON transfer-state persistence
│   ├── cache.py                 # TTL metadata cache (memory + JSON)
│   └── config.py                # CLI > env > config.toml > defaults
└── interfaces/
    ├── cli/
    │   ├── app.py               # Typer commands
    │   └── render.py            # Rich panels, trees, progress bars
    ├── tui/
    │   └── app.py               # Textual three-pane UI
    └── json_events.py           # NDJSON writer
```

## Data pipeline

```text
URL / ID
  │  domain.url_parser.parse_target
  ▼
Resolver ──► DriveItem metadata (CompositeDriveSource + DiskMetadataCache)
  │
  ├─ Single file / Google Doc ────┐
  └─ Folder ─► FolderCrawler ─────┤  BFS traversal, pagination, cycle guard
                                  ▼
                              Planner ──► DownloadPlan (directories + tasks)
                                  ▼
                              Scheduler (bounded queue + AIMD worker pool)
                                  ▼
                              DownloadWorker (.part resume + retry + streaming MD5)
                                  ▼
                              Verifier (size + MD5)
                                  ▼
                              FileSystemSink.promote_part (.part -> final)
```

## Resumable transfer semantics

Workers never write directly to the final destination filename.

1. Incoming bytes are written to `<filename>.part`.
2. Resume state is persisted in `<output>/.gdown2.state.json` with the Drive item ID,
   relative path, ETag, expected size, MD5, and progress.
3. When resuming with `resume_offset > 0`, the worker requests `Range: bytes=<offset>-`
   and handles three server behaviours:

| Server response | Behaviour |
|---|---|
| `206 Partial Content` | Append incoming chunks to the existing `.part` file. |
| `200 OK` | The server ignored `Range`. Reset the offset to zero and truncate the part file from the beginning. |
| `416 Range Not Satisfiable` | Verify the existing part against expected metadata. Promote it if complete, otherwise discard and restart. |

4. After the stream finishes, the verifier checks the byte size and MD5 when available.
5. Only after verification succeeds is `<filename>.part` atomically renamed to
   `<filename>` with `os.replace`.
6. Stale partial data is discarded when the remote ETag changes or overwrite mode is
   selected.

## Multipart downloads

Large files can be split into inclusive byte ranges and fetched concurrently
(`--chunks`). Each range must return `206 Partial Content` with the expected
`Content-Length`. If a server ignores the range request, the transfer is retried rather
than writing full-file bytes at a range offset. Sibling range tasks are cancelled when
any range fails, preventing concurrent writes during retry.

## Rate limiting and adaptive concurrency

Google Drive throttles aggressive parallel connections from a single IP.

- `TokenBucketRateLimiter` paces outbound requests per host (`--rate`).
- `ConcurrencyPolicy` implements Additive-Increase / Multiplicative-Decrease (AIMD).
  A `429` or transient `403` halves the active worker limit. A streak of successful
  transfers gradually restores it toward the configured maximum.

## Folder crawling

`FolderCrawler` performs breadth-first traversal with a concurrency semaphore.

- Pagination is handled inside the source adapter.
- Shortcuts are resolved and cycles are skipped with a visited-folder set.
- Folder resource keys are propagated to child listings when present.
- Filenames are sanitized and sibling collisions are resolved deterministically.
- Metadata cache writes are batched per BFS level to avoid quadratic disk I/O.

## Cross-platform path safety

`PathPolicy` ensures remote names are safe on Windows, macOS, and Linux:

- Strips characters forbidden on Windows (`<>:"/\|?*` and ASCII control characters).
- Removes trailing dots and spaces.
- Prefixes reserved DOS device names (`CON`, `PRN`, `AUX`, `NUL`, `COM1..9`, `LPT1..9`),
  including compound extensions such as `NUL.tar.gz`.
- Disambiguates duplicate sibling names (`report.pdf`, `report (1).pdf`, ...).
- Enforces segment and total path length budgets, reserving room for the `.part`
  suffix.
- Verifies with `Path.relative_to()` that no resolved path escapes the output root.

## Error model

Every failure maps to a `GdownError` subclass with:

- a stable process exit code,
- a `retryable` flag consumed by the worker retry loop,
- optional `item_id`, `status_code`, and `retry_after` metadata.

HTTP status classification lives in `infrastructure/http/retry.py`. Rate limits,
quota exhaustion, permission errors, missing resources, redirects, transport failures,
storage failures, and integrity failures are all distinct.
