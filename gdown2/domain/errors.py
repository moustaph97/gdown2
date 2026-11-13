"""Error taxonomy and exit codes for gdown2.

Every domain or infrastructure failure maps to a subclass of :class:`GdownError`
with a stable process exit code and a ``retryable`` flag so the retry engine and
CLI layer don't have to guess based on string matching.
"""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    """Standard process exit codes returned by the CLI."""

    OK = 0
    GENERAL_ERROR = 1
    INVALID_ARGUMENT = 2
    AUTH_REQUIRED = 3
    PERMISSION_DENIED = 4
    NOT_FOUND = 5
    QUOTA_EXCEEDED = 6
    NETWORK_ERROR = 7
    INTEGRITY_ERROR = 8
    STORAGE_ERROR = 9
    CANCELLED = 130


class GdownError(Exception):
    """Base exception for all gdown2 errors."""

    exit_code: ExitCode = ExitCode.GENERAL_ERROR
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        item_id: str | None = None,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.item_id = item_id
        self.status_code = status_code
        self.retry_after = retry_after


class InvalidUrlError(GdownError):
    """Raised when a string is neither a recognized Google Drive URL nor a valid file ID."""

    exit_code = ExitCode.INVALID_ARGUMENT
    retryable = False


class PathTraversalError(GdownError):
    """Raised when a remote path attempts to escape the target output directory."""

    exit_code = ExitCode.STORAGE_ERROR
    retryable = False


class InsufficientDiskSpaceError(GdownError):
    """Raised when the destination volume lacks enough free bytes for the download."""

    exit_code = ExitCode.STORAGE_ERROR
    retryable = False


class StorageError(GdownError):
    """Raised on local filesystem write/rename failures."""

    exit_code = ExitCode.STORAGE_ERROR
    retryable = False


class AuthenticationError(GdownError):
    """Raised on HTTP 401 or invalid/expired credentials."""

    exit_code = ExitCode.AUTH_REQUIRED
    retryable = False


class PermissionDeniedError(GdownError):
    """Raised on HTTP 403 when the user lacks access to the Drive resource."""

    exit_code = ExitCode.PERMISSION_DENIED
    retryable = False


class ResourceNotFoundError(GdownError):
    """Raised on HTTP 404 or 410 when a Drive file or folder does not exist."""

    exit_code = ExitCode.NOT_FOUND
    retryable = False


class QuotaExceededError(GdownError):
    """Raised when Google Drive blocks a download due to per-file or IP quota limits."""

    exit_code = ExitCode.QUOTA_EXCEEDED
    retryable = False


class RateLimitError(GdownError):
    """Raised on HTTP 429 or transient 403 rate-limit responses."""

    exit_code = ExitCode.QUOTA_EXCEEDED
    retryable = True


class TransientNetworkError(GdownError):
    """Raised on timeouts, connection resets, and HTTP 5xx responses."""

    exit_code = ExitCode.NETWORK_ERROR
    retryable = True


class UntrustedRedirectError(GdownError):
    """Raised when an HTTP redirect points to an untrusted host or non-HTTPS scheme."""

    exit_code = ExitCode.NETWORK_ERROR
    retryable = False


class ChecksumMismatchError(GdownError):
    """Raised when the downloaded file's size or hash does not match expected metadata."""

    exit_code = ExitCode.INTEGRITY_ERROR
    retryable = True

    def __init__(
        self,
        message: str,
        *,
        expected: str | int | None = None,
        actual: str | int | None = None,
        item_id: str | None = None,
    ) -> None:
        super().__init__(message, item_id=item_id)
        self.expected = expected
        self.actual = actual


class DownloadCancelledError(GdownError):
    """Raised when a transfer is cancelled by the user or a shutdown signal."""

    exit_code = ExitCode.CANCELLED
    retryable = False

