"""Register MCP tools from static metadata/mcp_tool_manifest.yaml (runtime source)."""

from __future__ import annotations

import importlib
import inspect
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError, create_model

from shared.manifest_types import parse_annotation
from shared.rest_registry_bridge import _normalize_payload_paths


def _pkg_root() -> Path:
    return Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def load_mcp_tool_manifest() -> dict[str, Any]:
    path = _pkg_root() / "metadata" / "mcp_tool_manifest.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def manifest_mcp_tool_ids_from_manifest() -> frozenset[str]:
    tools = (load_mcp_tool_manifest().get("tools") or {}).keys()
    return frozenset(str(t) for t in tools)


def _resolve_run_callable(handler_ref: str) -> Any:
    if ":" not in handler_ref:
        raise RuntimeError(f"invalid handler ref: {handler_ref!r}")
    mod_name, _, fn_name = handler_ref.partition(":")
    mod = importlib.import_module(mod_name.strip())
    fn = getattr(mod, fn_name.strip(), None)
    if fn is None:
        raise RuntimeError(f"callable not found: {handler_ref}")
    return fn


def _model_from_parameters(tool_id: str, parameters: list[dict[str, Any]]) -> type[BaseModel]:
    fields: dict[str, Any] = {}
    for row in parameters:
        name = str(row["name"])
        ann = parse_annotation(str(row["annotation"]))
        required = bool(row.get("required"))
        description = row.get("description")
        if required:
            fields[name] = (ann, Field(description=description) if description else ...)
        else:
            default = row.get("default")
            fields[name] = (
                ann,
                Field(default=default, description=description) if description else default,
            )
    return create_model(f"Manifest_{tool_id.replace('.', '_')}", **fields)  # type: ignore[call-overload]


def _ok(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False, indent=2)


def _build_typed_handler(*, tool_def: dict[str, Any], run_fn: Any) -> Any:
    tool_id = str(tool_def["tool_id"])
    mcp_name = str(tool_def["mcp_name"])
    description = str(tool_def.get("description") or tool_id)
    model = _model_from_parameters(tool_id, list(tool_def.get("parameters") or []))

    params: list[inspect.Parameter] = []
    annotations: dict[str, Any] = {}
    for name, field in model.model_fields.items():
        ann = field.annotation
        annotations[name] = ann
        default = inspect.Parameter.empty if field.is_required() else field.default
        params.append(
            inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, default=default, annotation=ann)
        )

    def _impl(**kwargs: Any) -> str:
        try:
            validated = model.model_validate(kwargs)
        except ValidationError as exc:
            return json.dumps(
                {
                    "status": "error",
                    "tool_id": tool_id,
                    "validation_errors": exc.errors(include_url=False),
                },
                ensure_ascii=False,
                indent=2,
            )
        payload = _normalize_payload_paths(validated.model_dump(exclude_none=True))
        result = run_fn(payload)
        if isinstance(result, dict):
            return _ok(result)
        return json.dumps({"result": result}, ensure_ascii=False, indent=2)

    _impl.__name__ = mcp_name
    _impl.__doc__ = (
        f"{description}\n\n"
        f"Static manifest MCP tool `{tool_id}` (see metadata/mcp_tool_manifest.yaml)."
    )
    _impl.__annotations__ = {**annotations, "return": str}
    _impl.__signature__ = inspect.Signature(params, return_annotation=str)
    return _impl


def register_manifest_mcp_tools(mcp: Any, *, server_id: str = "mineral-core-mcp") -> int:
    """Register tools defined in metadata/mcp_tool_manifest.yaml."""
    if server_id != "mineral-core-mcp":
        return 0

    doc = load_mcp_tool_manifest()
    tools: dict[str, Any] = doc.get("tools") or {}
    if not tools:
        return 0

    count = 0
    for tool_id in sorted(tools.keys()):
        tool_def = dict(tools[tool_id])
        handler_ref = str(tool_def.get("handler") or "")
        run_fn = _resolve_run_callable(handler_ref)
        mcp_name = str(tool_def.get("mcp_name") or tool_id)
        handler = _build_typed_handler(tool_def=tool_def, run_fn=run_fn)
        mcp.tool(name=mcp_name)(handler)
        count += 1
    return count
