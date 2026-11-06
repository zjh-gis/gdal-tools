"""Register REST executable tools (GeoToolRegistry) as additional MCP tools."""

from __future__ import annotations

import json
from typing import Any

from shared.manifest_tool_ids import manifest_mcp_tool_ids
from shared.paths import ensure_allowed, ensure_parent

# REST tool_ids already exposed as hand-written MCP tools on mineral-core-mcp
_MINERAL_NATIVE_REST_IDS = frozenset(
    {
        "geochem_anomaly",
        "geochem_cleaning",
        "element_combination",
        "alteration_extraction",
        "lineament_extraction",
        "vegetation_index",
        "prospectivity_wofe",
        "geophysics_filter",
    }
)

_PATH_LIKE_KEYS = frozenset(
    {
        "data_path",
        "output_path",
        "training_path",
        "gravity_path",
        "elevation_path",
        "layer_a",
        "layer_b",
        "clip_path",
        "vector_path",
        "cutline_path",
        "study_area",
        "mask_path",
        "input_path",
        "reference_path",
        "grid_path",
        "evidence_path",
        "anomaly_path",
        "table_path",
        "collar_path",
        "survey_path",
        "assay_path",
    }
)


def mcp_name_for_rest_tool_id(tool_id: str) -> str:
    return tool_id.replace(".", "_")


def _normalize_payload_paths(payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = dict(payload)
    for key, value in list(out.items()):
        if key in _PATH_LIKE_KEYS and isinstance(value, str) and value.strip():
            out[key] = str(ensure_allowed(value, field=key))
        elif key in ("evidence_paths", "data_paths") and isinstance(value, list):
            out[key] = [str(ensure_allowed(p, field=key)) for p in value if p]
        elif key.endswith("_paths") and isinstance(value, list):
            out[key] = [str(ensure_allowed(p, field=key)) for p in value if isinstance(p, str) and p.strip()]
        elif key == "output_dir" and isinstance(value, str) and value.strip():
            out[key] = str(ensure_parent(ensure_allowed(value, field="output_dir")))
    return out


def rest_tool_ids_for_mineral_bridge() -> list[str]:
    """REST registry tool_ids to expose via MCP bridge (deterministic order)."""
    import app.registered_exploration_ext  # noqa: F401
    import app.registered_tools  # noqa: F401
    from app.tool_registry import tool_registry

    ids: list[str] = []
    for spec in sorted(tool_registry.list(), key=lambda s: s.id):
        if spec.id.startswith("gdal."):
            continue
        if spec.id in _MINERAL_NATIVE_REST_IDS:
            continue
        if spec.id in manifest_mcp_tool_ids():
            continue
        ids.append(spec.id)
    return ids


def register_rest_executable_tools(mcp: Any, *, server_id: str = "mineral-core-mcp") -> int:
    """Attach bridged MCP tools for REST registry entries. Returns count registered."""
    if server_id != "mineral-core-mcp":
        return 0

    import app.registered_exploration_ext  # noqa: F401
    import app.registered_tools  # noqa: F401
    from app.tool_registry import ToolInputError, tool_registry

    count = 0
    for spec in sorted(tool_registry.list(), key=lambda s: s.id):
        if spec.id.startswith("gdal."):
            continue
        if spec.id in _MINERAL_NATIVE_REST_IDS:
            continue
        if spec.id in manifest_mcp_tool_ids():
            continue

        mcp_name = mcp_name_for_rest_tool_id(spec.id)
        tool_id = spec.id
        description = (spec.description or spec.name or tool_id).strip()

        def _make_handler(_spec=spec, _tool_id=tool_id):
            def _handler(payload: dict[str, Any] | None = None) -> str:
                arguments = _normalize_payload_paths(dict(payload or {}))
                try:
                    result = _spec.invoke(arguments)
                except ToolInputError as exc:
                    return json.dumps(
                        {
                            "status": "error",
                            "tool_id": _tool_id,
                            "validation_errors": exc.errors,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                if isinstance(result, dict):
                    return json.dumps(result, ensure_ascii=False, indent=2)
                return json.dumps({"result": result}, ensure_ascii=False, indent=2)

            _handler.__name__ = mcp_name_for_rest_tool_id(_tool_id)
            _handler.__doc__ = (
                f"{description}\n\n"
                f"Bridged from REST tool_id `{_tool_id}`. "
                "Pass the same JSON object as POST /api/v1/tools/{tool_id}/execute body in `payload`."
            )
            return _handler

        mcp.tool(name=mcp_name)(_make_handler())
        count += 1
    return count
