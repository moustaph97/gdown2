# Configuration

`gdown2` accepts configuration from CLI flags, environment variables, and a TOML file.

## Precedence

```text
CLI flags  >  environment variables  >  config.toml  >  built-in defaults
```

Each layer only overrides keys that it supplies. Invalid numeric or boolean values are
ignored so that a typo in one setting does not prevent the command from running.

## Config file location

1. `~/.config/gdown2/config.toml` when that file exists.
2. Otherwise the platform user config directory, for example:
   - Windows: `%APPDATA%\gdown2\config.toml`
   - macOS: `~/Library/Application Support/gdown2/config.toml`
   - Linux: `~/.config/gdown2/config.toml`

Both flat keys and `[download]` / `[auth]` sections are supported.

```toml
[download]
workers = 6
chunks_per_file = 4
rate_limit_rps = 5.0
max_retries = 5
timeout_seconds = 30.0
verify_checksums = true
existing_policy = "resume"
export_format = "pdf"
cache_ttl_seconds = 3600.0

[auth]
api_key = "AIza..."
# access_token = "ya29..."
# oauth_token_file = "~/.config/gdown2/authorized_user.json"
# service_account_file = "~/.config/gdown2/service_account.json"
```

## Settings reference

| TOML key | Environment variable | Type | Default | Description |
|---|---|---|---|---|
| `workers` | `GDOWN2_WORKERS` | int | `6` | Maximum concurrent file transfers. |
| `chunks_per_file` | `GDOWN2_CHUNKS` | int | `4` | Parallel Range streams per large file. |
| `rate_limit_rps` | `GDOWN2_RATE` | float | `5.0` | Per-host request rate limit. `0` disables pacing. |
| `max_retries` | `GDOWN2_MAX_RETRIES` | int | `5` | Maximum attempts per transfer. |
| `timeout_seconds` | `GDOWN2_TIMEOUT` | float | `30.0` | HTTP timeout in seconds. |
| `verify_checksums` | *(CLI only)* | bool | `true` | Verify MD5 after download. |
| `existing_policy` | *(CLI only)* | enum | `resume` | `resume`, `skip`, or `overwrite`. |
| `export_format` | `GDOWN2_EXPORT_FORMAT` | string | auto | Google Workspace export format. |
| `cache_ttl_seconds` | *(config only)* | float | `3600.0` | Metadata cache lifetime. |
| `api_key` | `GDOWN2_API_KEY` / `GOOGLE_API_KEY` | string | none | Drive API v3 key. |
| `access_token` | `GDOWN2_ACCESS_TOKEN` | string | none | OAuth bearer token. |
| `oauth_token_file` | `GDOWN2_OAUTH_FILE` | path | none | OAuth `authorized_user` JSON file. |
| `service_account_file` | `GDOWN2_SERVICE_ACCOUNT_FILE` / `GOOGLE_APPLICATION_CREDENTIALS` | path | none | GCP service-account JSON key. |

CLI flags override all of the above for a single invocation. `--verify/--no-verify`,
`--overwrite`, and `--skip` only override the corresponding configuration values when
explicitly supplied.

## Authentication providers

When multiple credentials are configured, the provider is selected in this order:

1. Service account (`service_account_file`)
2. OAuth user credentials from a file (`oauth_token_file`)
3. OAuth bearer token (`access_token`)
4. API key (`api_key`)
5. Anonymous public access

### API key

An API key is the simplest way to use Drive API v3 for public resources.

```bash
export GDOWN2_API_KEY="AIza..."
gdown2 get "1A2B3C4D5E6F7G8H9I0J" -o ./data
```

### OAuth bearer token

Useful in CI when another process provides a short-lived token.

```bash
export GDOWN2_ACCESS_TOKEN="ya29..."
gdown2 get "1A2B3C4D5E6F7G8H9I0J" -o ./data
```

### OAuth authorized-user file

A JSON file with the standard Google `authorized_user` shape. `gdown2` refreshes expired
tokens with the configured client ID and secret and serializes concurrent refreshes with
an async lock.

```json
{
  "type": "authorized_user",
  "client_id": "....apps.googleusercontent.com",
  "client_secret": "...",
  "refresh_token": "1//...",
  "token": "ya29...",
  "expiry": "2026-01-01T00:00:00+00:00"
}
```

```bash
export GDOWN2_OAUTH_FILE="$HOME/.config/gdown2/authorized_user.json"
gdown2 get "1PrivateFileId" -o ./data
```

If `token` is absent or `expiry` is in the past, gdown2 refreshes the access token before
use. `expiry` may be an ISO-8601 string or a Unix timestamp.

### Service account

Requires the optional dependency:

```bash
pip install "gdown2[service-account]"
```

Point to a GCP service-account JSON key:

```bash
gdown2 get "1PrivateFolderId" -o ./private \
  --service-account ./keys/service-account.json
```

Token minting runs in a worker thread so it does not block the event loop. Service
accounts only see resources explicitly shared with the service-account email.

## Existing-file policy

| Value | Behaviour |
|---|---|
| `resume` | Reuse a completed file after verification, or continue from the `.part` offset. |
| `skip` | Leave existing files untouched and mark the task `SKIPPED`. |
| `overwrite` | Discard stale local data and download a fresh copy. |

Pass `--overwrite` or `--skip` on the CLI, or set `existing_policy` in `config.toml`.

## Metadata cache

Resolved item metadata is cached to reduce Drive API and HTML requests. The cache lives
under the platform user cache directory:

- Windows: `%LOCALAPPDATA%\gdown2\metadata_cache.json`
- macOS: `~/Library/Caches/gdown2/metadata_cache.json`
- Linux: `~/.cache/gdown2/metadata_cache.json`

Entries expire after `cache_ttl_seconds`. Clear the cache at any time:

```bash
gdown2 cache-clear
```

## Resume state

Each output directory contains `.gdown2.state.json`, written atomically after progress
checkpoints and removed when no incomplete transfers remain. Keep this file together
with the `.part` files when moving an interrupted download to another machine.

```json
{
  "version": 1,
  "tasks": [
    {
      "task_key": "1A2B3C4D5E6F7G8H9I0J:model.bin",
      "item_id": "1A2B3C4D5E6F7G8H9I0J",
      "name": "model.bin",
      "mime_type": "application/octet-stream",
      "local_path": "model.bin",
      "relative_path": "model.bin",
      "bytes_total": 4294967296,
      "resume_offset": 1073741824,
      "etag": "\"abc123\"",
      "md5": "d41d8cd98f00b204e9800998ecf8427e",
      "state": "running",
      "export_format": null
    }
  ]
}
```
