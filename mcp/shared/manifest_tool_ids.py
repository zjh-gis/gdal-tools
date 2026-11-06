"""Manifest-driven MCP tool ids (enrollment: metadata/mcp_manifest_tools.yaml)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

_PKG_ROOT = Path(__file__).resolve().parents[2]
_ENROLLMENT_PATH = _PKG_ROOT / "metadata" / "mcp_manifest_tools.yaml"


@lru_cache(maxsize=1)
def manifest_mcp_tool_ids() -> frozenset[str]:
    if not _ENROLLMENT_PATH.exists():
        return frozenset()
    doc = yaml.safe_load(_ENROLLMENT_PATH.read_text(encoding="utf-8")) or {}
    return frozenset(str(t) for t in (doc.get("tools") or []) if t)
