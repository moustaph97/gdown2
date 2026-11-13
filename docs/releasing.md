# Releasing and PyPI publishing

`gdown2` publishes to PyPI from GitHub Actions using
[Trusted Publishing](https://docs.pypi.org/trusted-publishers/). No long-lived PyPI token
is stored in the repository or in GitHub secrets.

## One-time PyPI setup

1. Create a PyPI account and enable two-factor authentication.
2. Open **Account settings → Publishing** on PyPI.
3. Add a **pending trusted publisher** with:

   | Field | Value |
   |---|---|
   | PyPI project name | `gdown2` |
   | Owner | `moustaph97` |
   | Repository name | `gdown2` |
   | Workflow name | `publish.yml` |
   | Environment name | `pypi` |

4. In the GitHub repository, open **Settings → Environments** and create an environment
   named `pypi`. Optionally add required reviewers or restrict deployment branches to
   `main`.

The pending publisher is converted into a normal publisher after the first successful
upload.

## Release process

1. Update the version in `pyproject.toml` (and the fallback in `gdown2/__init__.py`).
2. Run the local checks:

   ```bash
   pytest
   ruff check .
   mypy gdown2
   python -m build
   ```

3. Commit and push the version bump.
4. Create a GitHub release with a matching tag, for example `v0.2.0`:

   ```bash
   gh release create v0.2.0 --generate-notes --title "gdown2 v0.2.0"
   ```

5. The `Publish to PyPI` workflow builds the sdist and wheel, verifies the wheel, and
   uploads both to PyPI through OIDC.
6. Verify the published package:

   ```bash
   python -m pip install --upgrade gdown2
   gdown2 --version
   ```

## Manual workflow dispatch

The workflow also supports `workflow_dispatch` for retrying a failed publication. It
still uses Trusted Publishing, so the PyPI publisher must already be configured.

## Rules

- PyPI filenames and versions are immutable. Never reuse a version after a successful
  upload; publish a new patch version instead.
- Test the build before creating the release:

  ```bash
  python -m build
  python -m venv /tmp/gdown2-verify
  /tmp/gdown2-verify/bin/pip install dist/*.whl
  /tmp/gdown2-verify/bin/gdown2 --version
  ```

- Do not add `PYPI_API_TOKEN` or `TWINE_PASSWORD` secrets. Trusted Publishing removes the
  need for them.
- If a release fails after artifacts were uploaded, do not delete the release assets
  blindly; inspect the workflow log and publish a new version.

## TestPyPI (optional)

To rehearse a release, configure a separate pending publisher on
[TestPyPI](https://test.pypi.org/) and add a temporary workflow or dispatch step that
publishes to `https://test.pypi.org/legacy/` with its own environment. TestPyPI does not
share names, credentials, or packages with PyPI.
