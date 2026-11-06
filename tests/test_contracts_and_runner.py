from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gdal_tools.cli import main
from gdal_tools.contracts.loader import list_tool_ids, load_tool_contract
from gdal_tools.errors import classify
from gdal_tools.registry import all_specs, get_spec
from gdal_tools.runner import apply_workspace, run_envelope
from gdal_tools.workspace import ensure_allowed


def test_contracts_cover_registry():
    contract_ids = set(list_tool_ids())
    registry_ids = {spec.tool_id for spec in all_specs()}
    assert contract_ids == registry_ids
    for tool_id in registry_ids:
        contract = load_tool_contract(tool_id)
        assert contract["tool"]["id"] == tool_id
        assert contract["interfaces"]["mcp"]
        slope = load_tool_contract("gdal.dem_slope")
        assert slope["inputs"]["data_path"]["semantic_type"] == "geo:DEM"
        assert "Horn" in slope["parameters"]["algorithm"]["enum"]


def test_shared_fragment_expanded():
    contract = load_tool_contract("gdal.dem_slope")
    assert "$shared" not in json.dumps(contract)


def test_workspace_canonical_and_alias(tmp_path, monkeypatch):
    monkeypatch.delenv("GDAL_TOOLS_WORKSPACES", raising=False)
    monkeypatch.delenv("MCP_WORKSPACES", raising=False)
    inside = tmp_path / "ok" / "a.tif"
    inside.parent.mkdir()
    inside.write_text("x", encoding="utf-8")
    assert ensure_allowed(inside).exists()

    allowed = tmp_path / "ok"
    monkeypatch.setenv("GDAL_TOOLS_WORKSPACES", str(allowed))
    outside = tmp_path / "nope" / "b.tif"
    outside.parent.mkdir()
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(PermissionError):
        ensure_allowed(outside)

    monkeypatch.delenv("GDAL_TOOLS_WORKSPACES")
    monkeypatch.setenv("MCP_WORKSPACES", str(allowed))
    assert ensure_allowed(inside).exists()


def test_apply_workspace_and_unknown_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("GDAL_TOOLS_WORKSPACES", str(tmp_path))
    payload = apply_workspace({"data_path": str(tmp_path / "dem.tif"), "percent": False})
    assert payload["data_path"].startswith(str(tmp_path))
    envelope = run_envelope("not.a.tool", {})
    assert envelope["ok"] is False
    assert envelope["error_code"] == "invalid_argument"


def test_classify_value_error():
    err = classify(ValueError("bad"))
    assert err.code == "invalid_argument"


def test_cli_list_and_describe(capsys):
    assert main(["list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    ids = {row["id"] for row in listed}
    assert "gdal.dem_slope" in ids
    assert "gdal.raster_warp" in ids
    assert main(["describe", "dem_slope", "--json"]) == 0
    described = json.loads(capsys.readouterr().out)
    assert described["tool"]["id"] == "gdal.dem_slope"
    assert get_spec("dem_slope").tool_id == "gdal.dem_slope"
