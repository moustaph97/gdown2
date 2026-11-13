"""Google Cloud API key authentication provider."""

from __future__ import annotations

from typing import Any


class ApiKeyAuth:
    """Attaches a Google Drive API v3 key query parameter to requests."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key.strip()

    @property
    def is_authenticated(self) -> bool:
        return bool(self._api_key)

    async def apply(
        self,
        headers: dict[str, str],
        params: dict[str, Any],
    ) -> None:
        if self._api_key:
            params["key"] = self._api_key

