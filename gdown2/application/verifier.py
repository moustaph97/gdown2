"""Integrity verification service (file size and MD5 checksums)."""

from __future__ import annotations

import hashlib
from pathlib import Path

from gdown2.domain.errors import ChecksumMismatchError
from gdown2.domain.models import DriveItem

_HASH_BUFFER_SIZE = 256 * 1024


class Verifier:
    """Validates downloaded files against expected size and checksum metadata."""

    def __init__(self, verify_checksums: bool = True) -> None:
        self._verify_checksums = verify_checksums

    def verify_file(
        self,
        path: Path,
        item: DriveItem,
        *,
        expected_size: int | None = None,
        computed_md5: str | None = None,
    ) -> None:
        """Verify ``path`` on disk against ``item`` size and MD5 digest.

        Raises:
            ChecksumMismatchError: If the file size or MD5 hash does not match.
        """
        if not path.exists():
            raise ChecksumMismatchError(
                f"Downloaded file is missing at {path}",
                item_id=item.id,
            )

        actual_size = path.stat().st_size

        if item.is_google_doc:
            # Exported Google Docs don't carry a fixed size or MD5 in metadata,
            # but an empty file indicates a failed export stream.
            if actual_size == 0:
                raise ChecksumMismatchError(
                    f"Exported Google Doc {item.name!r} is empty (0 bytes).",
                    expected=">0",
                    actual=0,
                    item_id=item.id,
                )
            return

        target_size = expected_size if expected_size is not None else item.size
        if target_size is not None and actual_size != target_size:
            raise ChecksumMismatchError(
                f"Size mismatch for {item.name!r}: expected {target_size} bytes, got {actual_size}",
                expected=target_size,
                actual=actual_size,
                item_id=item.id,
            )

        if self._verify_checksums and item.md5:
            digest = (computed_md5 or self.compute_md5(path)).lower()
            expected_md5 = item.md5.lower()
            if digest != expected_md5:
                raise ChecksumMismatchError(
                    f"MD5 mismatch for {item.name!r}: expected {expected_md5}, got {digest}",
                    expected=expected_md5,
                    actual=digest,
                    item_id=item.id,
                )

    @staticmethod
    def compute_md5(path: Path) -> str:
        """Calculate the hexadecimal MD5 digest of ``path``."""
        hasher = hashlib.md5()
        with path.open("rb") as fh:
            while chunk := fh.read(_HASH_BUFFER_SIZE):
                hasher.update(chunk)
        return hasher.hexdigest()

