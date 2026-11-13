"""HTTP status classification and Retry-After header parsing."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from gdown2.domain.errors import (
    AuthenticationError,
    GdownError,
    PermissionDeniedError,
    QuotaExceededError,
    RateLimitError,
    ResourceNotFoundError,
    TransientNetworkError,
)


def parse_retry_after(headers: Mapping[str, str]) -> float | None:
    """Parse an HTTP ``Retry-After`` header as seconds or HTTP-date."""
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if not raw:
        return None
    raw = raw.strip()
    try:
        seconds = float(raw)
        return max(0.0, seconds)
    except ValueError:
        pass

    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        delta = (dt - datetime.now(UTC)).total_seconds()
        return max(0.0, delta)
    except (TypeError, ValueError):
        return None


def classify_http_error(
    status_code: int,
    *,
    body_snippet: str = "",
    headers: Mapping[str, str] | None = None,
    item_id: str | None = None,
) -> GdownError:
    """Translate an HTTP error response into the corresponding :class:`GdownError`."""
    retry_after = parse_retry_after(headers or {})
    lower_body = body_snippet.lower()

    if status_code == 401:
        return AuthenticationError(
            "Authentication required or credentials expired (HTTP 401).",
            item_id=item_id,
            status_code=status_code,
        )

    if status_code == 403:
        if any(
            token in lower_body
            for token in ("rate limit", "userratelimitexceeded", "ratelimitexceeded")
        ):
            return RateLimitError(
                "Google Drive rate limit exceeded (HTTP 403).",
                item_id=item_id,
                status_code=status_code,
                retry_after=retry_after,
            )
        if any(
            token in lower_body
            for token in ("quota", "downloadquotaexceeded", "too many users have viewed")
        ):
            return QuotaExceededError(
                "Download quota exceeded for this file on Google Drive (HTTP 403).",
                item_id=item_id,
                status_code=status_code,
            )
        return PermissionDeniedError(
            "Permission denied for this Google Drive resource (HTTP 403).",
            item_id=item_id,
            status_code=status_code,
        )

    if status_code in {404, 410}:
        return ResourceNotFoundError(
            f"Google Drive resource not found (HTTP {status_code}).",
            item_id=item_id,
            status_code=status_code,
        )

    if status_code == 429:
        return RateLimitError(
            "Too many requests sent to Google Drive (HTTP 429).",
            item_id=item_id,
            status_code=status_code,
            retry_after=retry_after,
        )

    if status_code >= 500 or status_code == 408:
        return TransientNetworkError(
            f"Google Drive server returned HTTP {status_code}.",
            item_id=item_id,
            status_code=status_code,
            retry_after=retry_after,
        )

    return GdownError(
        f"Unexpected HTTP status {status_code} from Google Drive.",
        item_id=item_id,
        status_code=status_code,
    )

