"""OAuth 2.0 user credential authentication with automatic token refresh."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from gdown2.domain.errors import AuthenticationError

GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"


class OAuthUserAuth:
    """Manages an OAuth2 access token and optional refresh-token lifecycle."""

    def __init__(
        self,
        access_token: str | None = None,
        *,
        refresh_token: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        token_uri: str = GOOGLE_TOKEN_URI,
        expires_at: float | None = None,
    ) -> None:
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._client_id = client_id
        self._client_secret = client_secret
        self._token_uri = token_uri
        self._expires_at = expires_at
        self._lock = asyncio.Lock()

    @classmethod
    def from_authorized_user_file(cls, path: Path) -> OAuthUserAuth:
        """Load credentials from a standard ``authorized_user`` JSON file."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AuthenticationError(f"Failed to read OAuth token file {path}: {exc}") from exc

        return cls(
            access_token=data.get("token") or data.get("access_token"),
            refresh_token=data.get("refresh_token"),
            client_id=data.get("client_id"),
            client_secret=data.get("client_secret"),
            token_uri=data.get("token_uri", GOOGLE_TOKEN_URI),
            expires_at=_parse_expiry(data.get("expiry") or data.get("expires_at")),
        )

    @property
    def is_authenticated(self) -> bool:
        return bool(self._access_token or self._refresh_token)

    async def apply(
        self,
        headers: dict[str, str],
        params: dict[str, Any],
    ) -> None:
        async with self._lock:
            if self._needs_refresh():
                await self.refresh()
            if not self._access_token:
                raise AuthenticationError("No active OAuth2 access token available.")
            headers["Authorization"] = f"Bearer {self._access_token}"

    def _needs_refresh(self) -> bool:
        if not self._access_token:
            return True
        if self._expires_at is None:
            return False
        return time.time() >= (self._expires_at - 60.0)

    async def refresh(self) -> str:
        """Exchange the refresh token for a fresh access token."""
        if not (self._refresh_token and self._client_id and self._client_secret):
            raise AuthenticationError(
                "OAuth access token expired and refresh credentials are incomplete."
            )

        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                resp = await client.post(
                    self._token_uri,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": self._refresh_token,
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                    },
                )
            except httpx.HTTPError as exc:
                raise AuthenticationError(f"OAuth token refresh request failed: {exc}") from exc

        if resp.status_code != 200:
            raise AuthenticationError(
                f"OAuth token refresh rejected with HTTP {resp.status_code}.",
                status_code=resp.status_code,
            )

        payload = resp.json()
        token = payload.get("access_token")
        if not token:
            raise AuthenticationError("OAuth token endpoint returned no access_token.")
        self._access_token = str(token)
        expires_in = payload.get("expires_in")
        if expires_in is not None:
            self._expires_at = time.time() + float(expires_in)
        return self._access_token


def _parse_expiry(value: Any) -> float | None:
    """Normalize a cached OAuth expiry (ISO-8601 or epoch seconds) to a timestamp."""
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None

