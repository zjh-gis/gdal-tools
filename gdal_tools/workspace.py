"""Workspace allow-list for every path a tool reads or writes."""

from __future__ import annotations

import os
import re
from pathlib import Path

# Canonical name for platforms. MCP_WORKSPACES is accepted as a compatibility alias
# for existing compose files; do not document additional aliases.
WORKSPACE_ENV = "GDAL_TOOLS_WORKSPACES"
WORKSPACE_ENV_ALIASES = (WORKSPACE_ENV, "MCP_WORKSPACES")


def workspace_roots() -> list[Path]:
    raw = next((os.environ[name] for name in WORKSPACE_ENV_ALIASES if os.environ.get(name, "").strip()), "")
    if not raw:
        return []
    # Windows paths contain a drive colon, so only ";" separates roots there.
    separators = r";" if os.name == "nt" else r"[:;]"
    return [Path(chunk.strip()).resolve() for chunk in re.split(separators, raw) if chunk.strip()]


def ensure_allowed(path: str | Path, *, field: str = "path") -> Path:
    """Resolve ``path``; when a workspace is configured, require it under one of its roots."""
    if not path or not str(path).strip():
        raise ValueError(f"{field} is required")
    resolved = Path(path).expanduser().resolve()
    roots = workspace_roots()
    if not roots:
        return resolved
    for root in roots:
        if resolved == root or root in resolved.parents:
            return resolved
    raise PermissionError(
        f"{field} is outside the allowed workspaces {[str(r) for r in roots]}: {resolved}"
    )
