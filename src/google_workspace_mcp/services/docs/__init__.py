"""Docs service package."""

from .constants import DOCS_SCOPES
from .extension import DocsExtension
from .factory import create_docs_app

__all__ = ['DOCS_SCOPES', 'DocsExtension', 'create_docs_app']
