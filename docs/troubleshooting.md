# Troubleshooting

## The download stops and leaves a `.part` file

That is expected. `gdown2` streams into `<filename>.part` and only promotes the file
after verification. Resume with:

```bash
gdown2 resume ./output-directory
```

Keep the `.part` file and `.gdown2.state.json` together. Deleting either forces a
restart from byte zero.

## "Resource not found" or HTTP 404/410

The file may have been deleted, moved, or the link may be malformed. Confirm the link
with:

```bash
gdown2 info "<url-or-id>"
```

Bare IDs are supported, but they must be the Drive file/folder ID, not a sharing token
or a full query string.

## "Permission denied" or HTTP 403

Common causes:

1. **Private resource.** Public links cannot access private files. Configure an API key,
   OAuth token, or service account.
2. **Service-account access.** Share the file or folder with the service-account email
   (`...@....iam.gserviceaccount.com`). Granting access to your personal account does not
   grant it to the service account.
3. **Download quota.** Google may return `403` with a quota message. Wait, retry later,
   or copy the file to your own Drive and download that copy.
4. **Transient abuse detection.** `gdown2` retries transient `403` responses with
   exponential backoff and reduces concurrency. Lower `--workers` and `--rate` if the
   problem persists.

## "Authentication required" or HTTP 401

The token is missing, expired, or lacks the required scope.

- OAuth file: ensure `refresh_token`, `client_id`, and `client_secret` are present.
- Bearer token: verify it has not expired.
- Service account: verify the JSON key is valid and the Drive API is enabled in the GCP
  project.

## Quota exceeded

Drive enforces per-file download quotas that are independent of your API limits. The
error is usually transient. Retry after the interval suggested by Google, or download
the file from a copy in your own Drive.

## "Integrity error" or checksum mismatch

The transferred bytes did not match Drive metadata. `gdown2` deletes the corrupted part
and retries automatically. If failures persist:

1. Retry with `--no-verify` only to diagnose whether the mismatch is in the local file or
   the server-provided checksum.
2. Use fewer workers and chunks: `--workers 1 --chunks 1`.
3. Clear stale state and start fresh:

```bash
rm -f ./output/.gdown2.state.json ./output/<filename>.part
gdown2 get "<url-or-id>" -o ./output
```

## "Storage error" or permission denied on disk

- Ensure the output directory is writable.
- Ensure there is enough free space, including room for the `.part` copy during
  promotion.
- On Windows, close applications that may hold the target file open.
- Paths are sanitized, but antivirus or endpoint protection can still block writes.

## Filenames are renamed unexpectedly

`gdown2` sanitizes remote names for cross-platform safety:

- Characters forbidden on Windows are replaced.
- Reserved device names such as `CON` and `NUL` are prefixed.
- Duplicate sibling names receive ` (1)`, ` (2)`, and so on.
- Very long names are truncated within the platform path budget, with room reserved for
  the `.part` suffix.

Use `--flat` carefully: flattening can create many sibling collisions, which are renamed
deterministically.

## The wrong file is downloaded, or an HTML page is saved

For Google Workspace documents, pass an explicit export format:

```bash
gdown2 get "https://docs.google.com/document/d/1DocId/edit" --export-format pdf
```

If a normal Drive file returns HTML, the link likely points to an interstitial or an
error page. `gdown2` detects HTML error pages and virus-scan interstitials and reports a
typed error instead of saving HTML as the payload. Verify the link with `gdown2 info`.

## "Untrusted redirect" error

`gdown2` follows only HTTPS redirects to Google-owned hosts. A redirect to an unexpected
domain is rejected to prevent credential leakage and malicious payloads. If you believe
the link is legitimate, verify it in a browser and open an issue with the URL pattern
(without any private token).

## Resume says "No pending transfers found"

The state file is absent, empty, or all recorded tasks are complete. Check that you are
running the command in the same output directory:

```bash
gdown2 resume ./output-directory
```

If the state file is corrupted, it is ignored and no tasks are resumed. Remove the
corrupt file and start a new download.

## Service-account import error

Install the optional dependency:

```bash
pip install "gdown2[service-account]"
```

## Proxy or custom CA

Set the standard `HTTPS_PROXY`, `HTTP_PROXY`, and `SSL_CERT_FILE` environment variables
before running `gdown2`. `httpx` honours the standard proxy environment variables.

## Verbose diagnostics

Use NDJSON output to capture per-task events without terminal rendering:

```bash
gdown2 get "<url-or-id>" -o ./output --json > events.ndjson
```

Inspect with:

```bash
jq -c 'select(.event=="task_failed" or .event=="summary")' events.ndjson
```

Exit codes are documented in the [README](../README.md#exit-codes).
