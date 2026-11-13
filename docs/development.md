# Development guide

## Requirements

- Python 3.11 or newer
- Git
- A POSIX shell, PowerShell, or Command Prompt

## Environment setup

```bash
git clone https://github.com/moustaph97/gdown2.git
cd gdown2

python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS / Linux

pip install -e ".[dev]"
```

The `dev` extra installs `pytest`, `pytest-asyncio`, `respx`, `ruff`, `mypy`, and
`google-auth` for service-account tests.

## Running tests

```bash
pytest
```

The suite is fully offline. Google Drive endpoints are mocked with `respx`; no network
access or credentials are required.

Useful selections:

```bash
pytest tests/unit
pytest tests/integration
pytest -k resume
pytest -x --pdb
```

Live end-to-end tests are opt-in and are skipped unless `GDOWN2_E2E_URL` is set:

```bash
GDOWN2_E2E_URL="https://drive.google.com/file/d/<id>/view" pytest tests/e2e
```

## Linting and type checking

```bash
ruff check .
ruff format --check .
mypy gdown2
```

Auto-fix safe lint issues:

```bash
ruff check . --fix
```

Formatting is optional but recommended before opening a pull request:

```bash
ruff format .
```

## Code organization

| Layer | Location | Rules |
|---|---|---|
| Domain | `gdown2/domain/` | Pure Python. No HTTP, filesystem, or third-party I/O libraries. |
| Application | `gdown2/application/` | Orchestration. Depends on protocols in `ports.py`, not concrete adapters. |
| Infrastructure | `gdown2/infrastructure/` | HTTP, Drive, auth, cache, config, and storage implementations. |
| Interfaces | `gdown2/interfaces/` | CLI, TUI, and NDJSON. Translate user input into application calls. |

Keep new behavior covered by a unit test at the lowest possible layer. Prefer testing a
policy or planner method over spinning up a full runtime.

## Adding a CLI option

1. Add the Typer option in `gdown2/interfaces/cli/app.py`.
2. Add the corresponding field to `AppConfig` and its environment mapping if needed.
3. Include it in the CLI override dictionary only when the user explicitly supplied it.
4. Document it in `docs/cli.md` and `docs/configuration.md`.
5. Add a CLI test or a config precedence test.

## Adding an authentication provider

1. Implement the `AuthProvider` protocol from
   `gdown2/infrastructure/auth/base.py`.
2. Update `AppConfig.build_auth_provider()` selection order.
3. Add environment and TOML keys.
4. Add tests with mocked HTTP responses.
5. Update the authentication section in the README and configuration docs.

## HTTP mocking

`respx` intercepts `httpx` requests. Register responses by URL or route:

```python
import httpx
import respx


@respx.mock
async def test_download() -> None:
    respx.get("https://drive.google.com/file/d/abc/view").mock(
        return_value=httpx.Response(200, text="<html>...</html>")
    )
```

Prefer realistic fixtures that mirror Google's HTML and JSON shapes, including error
pages, interstitials, and pagination tokens.

## Testing failure paths

The engine is designed around failure recovery. When changing transfer code, cover:

- `206 Partial Content` resume from a `.part` offset.
- `200 OK` when the server ignores `Range`.
- `416 Range Not Satisfiable` with complete and incomplete parts.
- MD5 mismatch and retry from zero.
- ETag change invalidating stale partial data.
- `429` with and without `Retry-After`.
- Cancellation leaving resumable state.
- Path traversal and Windows reserved names.

## Documentation

Documentation lives in `docs/`. Keep it in English and update it in the same pull request
as the behavior change. The README is the entry point; detailed material belongs in the
topic-specific pages.

## Release checklist

1. Update the version in `pyproject.toml` and `gdown2/__init__.py` fallback.
2. Run `pytest`, `ruff check .`, and `mypy gdown2`.
3. Update the changelog or release notes.
4. Build the distribution:

   ```bash
   python -m pip install build
   python -m build
   ```

5. Verify the wheel contents include `py.typed` and all subpackages.
6. Tag the release and publish to your package index.
