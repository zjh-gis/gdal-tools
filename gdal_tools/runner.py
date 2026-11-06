"""Platform-neutral execution: workspace check, call algorithm, JSON envelope."""

from __future__ import annotations

import inspect
from typing import Any, Callable

from gdal_tools.errors import ERRORS, ToolError, classify
from gdal_tools.registry import ToolSpec, get_spec
from gdal_tools.workspace import ensure_allowed

ProgressCallback = Callable[[float], None]

FILE_FIELDS = ("data_path", "output_path", "cutline_path", "mask_path")
FILE_LIST_FIELDS = ("data_paths",)
FILE_MAP_FIELDS = ("inputs",)


def apply_workspace(arguments: dict[str, Any]) -> dict[str, Any]:
    """Resolve path arguments against the configured workspace roots."""
    payload = dict(arguments)
    for field in FILE_FIELDS:
        value = payload.get(field)
        if value:
            payload[field] = str(ensure_allowed(value, field=field))
    for field in FILE_LIST_FIELDS:
        value = payload.get(field)
        if value:
            payload[field] = [str(ensure_allowed(item, field=field)) for item in value]
    for field in FILE_MAP_FIELDS:
        value = payload.get(field)
        if value:
            payload[field] = {
                name: str(ensure_allowed(path, field=f"{field}.{name}"))
                for name, path in value.items()
            }
    return {key: value for key, value in payload.items() if value is not None}


def _invoke(spec: ToolSpec, payload: dict[str, Any], progress: ProgressCallback | None) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    signature = inspect.signature(spec.handler)
    if progress is not None and "progress" in signature.parameters:
        kwargs["progress"] = progress
    result = spec.handler(payload, **kwargs)
    if not isinstance(result, dict):
        raise TypeError(f"{spec.tool_id} returned {type(result).__name__}, expected dict")
    return result


def envelope(
    *,
    tool_id: str,
    ok: bool,
    result: dict[str, Any] | None = None,
    error_code: str | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"ok": ok, "tool_id": tool_id}
    if ok:
        body["result"] = result or {}
        return body
    body["error_code"] = error_code or "internal_error"
    body["message"] = message or ""
    spec = ERRORS.get(body["error_code"])
    if spec is not None:
        body["retryable"] = spec.retryable
        body["exit_code"] = spec.exit_code
    return body


def run_tool(
    name: str,
    arguments: dict[str, Any] | None = None,
    *,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Execute one tool. Raises ``ToolError`` on failure; never talks to MCP or a platform."""
    spec = get_spec(name)
    payload = apply_workspace(arguments or {})
    try:
        return _invoke(spec, payload, progress)
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise classify(exc) from exc


def run_envelope(
    name: str,
    arguments: dict[str, Any] | None = None,
    *,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Same as ``run_tool`` but always returns a JSON envelope (for CLI / Job adapters)."""
    try:
        spec = get_spec(name)
    except ValueError as exc:
        err = classify(exc)
        return envelope(tool_id=name, ok=False, error_code=err.code, message=err.message)
    try:
        result = run_tool(spec.tool_id, arguments, progress=progress)
        return envelope(tool_id=spec.tool_id, ok=True, result=result)
    except ToolError as exc:
        return envelope(tool_id=spec.tool_id, ok=False, error_code=exc.code, message=exc.message)
