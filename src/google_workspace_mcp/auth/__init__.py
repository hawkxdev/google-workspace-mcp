"""Слой авторизации «MCP-клиент → сервис»."""

from .state import (
    LEGACY_FULL,
    MCP_READONLY_V1,
    REAUTHORIZATION_REQUIRED,
    OAuthState,
    UnsafeStatePath,
    canonicalize_resource,
)

__all__ = [
    'LEGACY_FULL',
    'MCP_READONLY_V1',
    'REAUTHORIZATION_REQUIRED',
    'OAuthState',
    'UnsafeStatePath',
    'canonicalize_resource',
]
