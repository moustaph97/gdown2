"""Anonymous (public link) authentication implementation."""

from __future__ import annotations

from typing import Any


class AnonymousAuth:
    """No-op authentication used for public Google Drive links."""

    @property
    def is_authenticated(self) -> bool:
        return False

    async def apply(
        self,
        headers: dict[str, str],
        params: dict[str, Any],
    ) -> None:
        return None

