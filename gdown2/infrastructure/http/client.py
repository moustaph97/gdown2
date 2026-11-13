"""Managed httpx.AsyncClient wrapper with safe redirect enforcement and rate limiting."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from gdown2.domain.errors import TransientNetworkError, UntrustedRedirectError
from gdown2.infrastructure.http.ratelimit import TokenBucketRateLimiter
from gdown2.infrastructure.http.retry import classify_http_error

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36 gdown2/0.2.0"
)

_TRUSTED_REDIRECT_SUFFIXES: tuple[str, ...] = (
    ".google.com",
    ".googleapis.com",
    ".googleusercontent.com",
    ".gvt1.com",
)
_TRUSTED_EXACT_HOSTS: frozenset[str] = frozenset(
    {
        "google.com",
        "drive.google.com",
        "docs.google.com",
        "drive.usercontent.google.com",
        "www.googleapis.com",
        "content.googleapis.com",
        "accounts.google.com",
    }
)


def is_trusted_redirect_url(url: str) -> bool:
    """Return True if ``url`` uses HTTPS and points to a trusted Google domain."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if host in _TRUSTED_EXACT_HOSTS:
        return True
    return any(host.endswith(suffix) for suffix in _TRUSTED_REDIRECT_SUFFIXES)


class HttpClient:
    """Async HTTP client enforcing domain whitelists, rate limits, and error mapping."""

    def __init__(
        self,
        *,
        rate_limiter: TokenBucketRateLimiter | None = None,
        timeout: float = 30.0,
        max_redirects: int = 10,
        user_agent: str = DEFAULT_USER_AGENT,
        extra_headers: Mapping[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._rate_limiter = rate_limiter or TokenBucketRateLimiter(rate_per_sec=0)
        self._max_redirects = max_redirects
        headers = {"User-Agent": user_agent}
        if extra_headers:
            headers.update(extra_headers)
        self._client = httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(timeout, connect=min(15.0, timeout)),
            follow_redirects=False,
            transport=transport,
        )

    @property
    def cookies(self) -> httpx.Cookies:
        return self._client.cookies

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> HttpClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        item_id: str | None = None,
        raise_for_status: bool = True,
    ) -> httpx.Response:
        """Perform an HTTP request following only verified HTTPS Google redirects."""
        current_url = url
        current_params = params

        for _ in range(self._max_redirects + 1):
            host = urlparse(current_url).hostname or "default"
            await self._rate_limiter.acquire(host)
            try:
                response = await self._client.request(
                    method,
                    current_url,
                    params=current_params,
                    headers=headers,
                )
            except httpx.HTTPError as exc:
                raise TransientNetworkError(
                    f"Network error while requesting {current_url}: {exc}",
                    item_id=item_id,
                ) from exc

            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    break
                next_url = urljoin(str(response.url), location)
                if not is_trusted_redirect_url(next_url):
                    raise UntrustedRedirectError(
                        f"Refusing redirect to untrusted URL: {next_url}",
                        item_id=item_id,
                    )
                current_url = next_url
                current_params = None
                continue

            if raise_for_status and response.status_code >= 400:
                raise classify_http_error(
                    response.status_code,
                    body_snippet=response.text[:1024],
                    headers=response.headers,
                    item_id=item_id,
                )
            return response

        raise TransientNetworkError(
            f"Exceeded maximum redirects ({self._max_redirects}) for {url}",
            item_id=item_id,
        )

    @asynccontextmanager
    async def stream(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        item_id: str | None = None,
    ) -> AsyncIterator[httpx.Response]:
        """Open a streaming HTTP response, following verified HTTPS Google redirects."""
        current_url = url
        current_params = params

        for _ in range(self._max_redirects + 1):
            host = urlparse(current_url).hostname or "default"
            await self._rate_limiter.acquire(host)
            try:
                req = self._client.build_request(
                    method,
                    current_url,
                    params=current_params,
                    headers=headers,
                )
                response = await self._client.send(req, stream=True)
            except httpx.HTTPError as exc:
                raise TransientNetworkError(
                    f"Network error while opening stream to {current_url}: {exc}",
                    item_id=item_id,
                ) from exc

            if response.is_redirect:
                location = response.headers.get("location")
                await response.aclose()
                if not location:
                    raise TransientNetworkError(
                        f"Redirect without Location header from {current_url}",
                        item_id=item_id,
                    )
                next_url = urljoin(str(response.url), location)
                if not is_trusted_redirect_url(next_url):
                    raise UntrustedRedirectError(
                        f"Refusing redirect to untrusted URL: {next_url}",
                        item_id=item_id,
                    )
                current_url = next_url
                current_params = None
                continue

            try:
                yield response
            finally:
                await response.aclose()
            return

        raise TransientNetworkError(
            f"Exceeded maximum redirects ({self._max_redirects}) while streaming {url}",
            item_id=item_id,
        )

