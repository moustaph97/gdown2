"""Configuration loader with CLI > environment > TOML file precedence."""

from __future__ import annotations

import contextlib
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from platformdirs import user_cache_dir, user_config_dir

from gdown2.domain.policies import ExistingFilePolicy
from gdown2.infrastructure.auth import (
    AnonymousAuth,
    ApiKeyAuth,
    AuthProvider,
    OAuthUserAuth,
    ServiceAccountAuth,
)


def default_config_path() -> Path:
    """Return the default location of ``config.toml``."""
    xdg_like = Path.home() / ".config" / "gdown2" / "config.toml"
    if xdg_like.exists():
        return xdg_like
    return Path(user_config_dir("gdown2", appauthor=False)) / "config.toml"


def default_cache_path() -> Path:
    """Return the default location of the metadata cache JSON file."""
    return Path(user_cache_dir("gdown2", appauthor=False)) / "metadata_cache.json"


@dataclass(slots=True)
class AppConfig:
    """Resolved runtime configuration for gdown2."""

    workers: int = 6
    chunks_per_file: int = 4
    rate_limit_rps: float = 5.0
    max_retries: int = 5
    timeout_seconds: float = 30.0
    verify_checksums: bool = True
    existing_policy: ExistingFilePolicy = ExistingFilePolicy.RESUME
    export_format: str | None = None
    cache_ttl_seconds: float = 3600.0
    api_key: str | None = None
    access_token: str | None = None
    oauth_token_file: Path | None = None
    service_account_file: Path | None = None

    @classmethod
    def load(
        cls,
        *,
        config_file: Path | None = None,
        environ: Mapping[str, str] | None = None,
        cli_overrides: Mapping[str, Any] | None = None,
    ) -> AppConfig:
        """Load configuration applying precedence: CLI > env > config.toml > defaults."""
        cfg = cls()
        file_path = config_file or default_config_path()

        if file_path.exists():
            try:
                raw_toml = tomllib.loads(file_path.read_text(encoding="utf-8"))
                cfg._apply_dict(raw_toml)
            except (OSError, tomllib.TOMLDecodeError):
                pass

        env = environ if environ is not None else os.environ
        cfg._apply_env(env)

        if cli_overrides:
            cfg._apply_dict(cli_overrides)

        return cfg

    def build_auth_provider(self) -> AuthProvider:
        """Instantiate the configured :class:`AuthProvider` strategy."""
        if self.service_account_file is not None:
            return ServiceAccountAuth.from_json_file(self.service_account_file)
        if self.oauth_token_file is not None:
            return OAuthUserAuth.from_authorized_user_file(self.oauth_token_file)
        if self.access_token:
            return OAuthUserAuth(access_token=self.access_token)
        if self.api_key:
            return ApiKeyAuth(self.api_key)
        return AnonymousAuth()

    def _apply_dict(self, data: Mapping[str, Any]) -> None:
        # Support both flat keys and [download] / [auth] sections in config.toml.
        value: Any
        merged: dict[str, Any] = {}
        for key, value in data.items():
            if isinstance(value, Mapping):
                merged.update(value)
            else:
                merged[key] = value

        if (value := _as_int(merged.get("workers"))) is not None:
            self.workers = value
        if (value := _as_int(merged.get("chunks_per_file"))) is not None:
            self.chunks_per_file = value
        if (value := _as_float(merged.get("rate_limit_rps"))) is not None:
            self.rate_limit_rps = value
        if (value := _as_int(merged.get("max_retries"))) is not None:
            self.max_retries = value
        if (value := _as_float(merged.get("timeout_seconds"), minimum=1.0)) is not None:
            self.timeout_seconds = value
        if (value := _as_bool(merged.get("verify_checksums"))) is not None:
            self.verify_checksums = value
        if merged.get("existing_policy") is not None:
            with contextlib.suppress(ValueError):
                self.existing_policy = ExistingFilePolicy(str(merged["existing_policy"]))
        if merged.get("export_format") is not None:
            self.export_format = str(merged["export_format"])
        if (value := _as_float(merged.get("cache_ttl_seconds"))) is not None:
            self.cache_ttl_seconds = value
        if merged.get("api_key") is not None:
            self.api_key = str(merged["api_key"])
        if merged.get("access_token") is not None:
            self.access_token = str(merged["access_token"])
        if merged.get("oauth_token_file") is not None:
            self.oauth_token_file = Path(str(merged["oauth_token_file"])).expanduser()
        if merged.get("service_account_file") is not None:
            self.service_account_file = Path(str(merged["service_account_file"])).expanduser()

    def _apply_env(self, env: Mapping[str, str]) -> None:
        value: Any
        if (value := _as_int(env.get("GDOWN2_WORKERS"))) is not None:
            self.workers = value
        if (value := _as_int(env.get("GDOWN2_CHUNKS"))) is not None:
            self.chunks_per_file = value
        if (value := _as_float(env.get("GDOWN2_RATE"))) is not None:
            self.rate_limit_rps = value
        if (value := _as_int(env.get("GDOWN2_MAX_RETRIES"))) is not None:
            self.max_retries = value
        if (value := _as_float(env.get("GDOWN2_TIMEOUT"), minimum=1.0)) is not None:
            self.timeout_seconds = value
        if value := env.get("GDOWN2_EXPORT_FORMAT"):
            self.export_format = value.strip()
        if value := env.get("GDOWN2_API_KEY") or env.get("GOOGLE_API_KEY"):
            self.api_key = value.strip()
        if value := env.get("GDOWN2_ACCESS_TOKEN"):
            self.access_token = value.strip()
        if value := env.get("GDOWN2_OAUTH_FILE"):
            self.oauth_token_file = Path(value).expanduser()
        if value := (
            env.get("GDOWN2_SERVICE_ACCOUNT_FILE")
            or env.get("GOOGLE_APPLICATION_CREDENTIALS")
        ):
            self.service_account_file = Path(value).expanduser()


def _as_int(value: Any, minimum: int = 1) -> int | None:
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return None


def _as_float(value: Any, minimum: float = 0.0) -> float | None:
    try:
        return max(minimum, float(value))
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)

