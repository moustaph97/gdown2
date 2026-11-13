"""Authentication provider protocol shared by all credential strategies."""

from __future__ import annotations

from typing import Any, Protocol


class AuthProvider(Protocol):
    """Strategy interface for injecting credentials into Drive requests."""

    @property
    def is_authenticated(self) -> bool:
        ...

    async def apply(
        self,
        headers: dict[str, str],
        params: dict[str, Any],
    ) -> None:
        """Mutate ``headers`` and/or ``params`` in place with auth credentials."""
        ...
