"""Reusable GitHub issue snapshots, backed by local SQLite."""

from .connector import import_issues, read_issues

__all__ = ["import_issues", "read_issues"]
