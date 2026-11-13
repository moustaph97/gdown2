"""Authentication strategies for Google Drive API and web endpoints."""

from gdown2.infrastructure.auth.anonymous import AnonymousAuth
from gdown2.infrastructure.auth.api_key import ApiKeyAuth
from gdown2.infrastructure.auth.base import AuthProvider
from gdown2.infrastructure.auth.oauth import OAuthUserAuth
from gdown2.infrastructure.auth.service_account import ServiceAccountAuth

__all__ = [
    "AnonymousAuth",
    "ApiKeyAuth",
    "AuthProvider",
    "OAuthUserAuth",
    "ServiceAccountAuth",
]

