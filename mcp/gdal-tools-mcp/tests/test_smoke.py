"""Smoke tests for MCP shared helpers and GDAL server import."""

from __future__ import annotations

import sys
from pathlib import Path

MCP_ROOT = Path(__file__).resolve().parents[2]
PKG_ROOT = MCP_ROOT.parent
sys.path.insert(0, str(MCP_ROOT))
sys.path.insert(0, str(PKG_ROOT))

from shared.paths import ensure_allowed, workspace_roots  # noqa: E402


def test_workspace_open_when_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("GDAL_TOOLS_WORKSPACES", raising=False)
    monkeypatch.delenv("MCP_WORKSPACES", raising=False)
    p = tmp_path / "a.tif"
    p.write_text("x", encoding="utf-8")
    assert ensure_allowed(p).exists()


def test_workspace_rejects_outside(tmp_path, monkeypatch):
    allowed = tmp_path / "ok"
    allowed.mkdir()
    monkeypatch.setenv("MCP_WORKSPACES", str(allowed))
    outside = tmp_path / "nope" / "x.tif"
    outside.parent.mkdir()
    outside.write_text("x", encoding="utf-8")
    try:
        ensure_allowed(outside)
        raised = False
    except PermissionError:
        raised = True
    assert raised
    assert workspace_roots()


def test_gdal_server_registers_tools():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "gdal_tools_server",
        MCP_ROOT / "gdal-tools-mcp" / "server.py",
    )
    assert spec and spec.loader
    try:
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except ModuleNotFoundError as exc:
        if "mcp" in str(exc):
            return
        raise
    # FastMCP API: list tools via tool manager
    manager = getattr(module.mcp, "_tool_manager", None) or getattr(module.mcp, "_tools", None)
    if manager is None:
        tools = getattr(module.mcp, "list_tools", lambda: [])()
        names = {getattr(t, "name", t) for t in tools}
    elif hasattr(manager, "list_tools"):
        names = {t.name for t in manager.list_tools()}
    else:
        names = set(manager.keys()) if isinstance(manager, dict) else set()
    assert {
        "raster_info",
        "raster_warp",
        "vector_info",
        "raster_calc",
        "raster_polygonize",
        "raster_build_vrt",
        "raster_overviews",
        "dem_aspect",
        "dem_hillshade",
    } <= names
