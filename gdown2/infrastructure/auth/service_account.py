"""Google Cloud Service Account authentication."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from gdown2.domain.errors import AuthenticationError

DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"


class ServiceAccountAuth:
    """Authenticates requests using a Google Cloud service account key file."""

    def __init__(
        self,
        *,
        client_email: str,
        private_key: str,
        token_uri: str = "https://oauth2.googleapis.com/token",
        access_token: str | None = None,
    ) -> None:
        self.client_email = client_email
        self._private_key = private_key
        self._token_uri = token_uri
        self._access_token = access_token

    @classmethod
    def from_json_file(cls, path: Path) -> ServiceAccountAuth:
        """Load service account details from a standard GCP JSON key file."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AuthenticationError(
                f"Failed to read service account key file {path}: {exc}"
            ) from exc

        client_email = data.get("client_email")
        private_key = data.get("private_key")
        if not client_email or not private_key:
            raise AuthenticationError(
                f"Service account file {path} is missing 'client_email' or 'private_key'."
            )
        return cls(
            client_email=str(client_email),
            private_key=str(private_key),
            token_uri=str(data.get("token_uri", "https://oauth2.googleapis.com/token")),
            access_token=data.get("access_token"),
        )

    @property
    def is_authenticated(self) -> bool:
        return bool(self._access_token or (self.client_email and self._private_key))

    async def apply(
        self,
        headers: dict[str, str],
        params: dict[str, Any],
    ) -> None:
        if not self._access_token:
            self._access_token = await asyncio.to_thread(self._mint_token_via_google_auth)
        headers["Authorization"] = f"Bearer {self._access_token}"

    def _mint_token_via_google_auth(self) -> str:
        try:
            from google.auth.transport.requests import Request  # type: ignore[import-not-found]
            from google.oauth2 import service_account  # type: ignore[import-not-found]
        except ImportError as exc:
            raise AuthenticationError(
                "Service account JWT signing requires 'google-auth' to be installed "
                "(pip install google-auth) or a pre-issued bearer token."
            ) from exc

        info = {
            "type": "service_account",
            "client_email": self.client_email,
            "private_key": self._private_key,
            "token_uri": self._token_uri,
        }
        creds = service_account.Credentials.from_service_account_info(
            info,
            scopes=[DRIVE_READONLY_SCOPE],
        )
        creds.refresh(Request())
        if not creds.token:
            raise AuthenticationError("Failed to obtain access token for service account.")
        return str(creds.token)

