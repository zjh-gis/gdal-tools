"""Compatibility wrapper. Workspace policy lives in gdal_tools.workspace."""

from gdal_tools.workspace import ensure_allowed, workspace_roots

__all__ = ["ensure_allowed", "workspace_roots"]
