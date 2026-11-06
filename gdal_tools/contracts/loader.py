"""Load and expand per-tool GeoTool contracts."""

from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

CONTRACTS_DIR = Path(__file__).resolve().parent
PACKAGE_PATH = CONTRACTS_DIR / "package.yaml"
TOOLS_DIR = CONTRACTS_DIR / "tools"


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"contract file is not a mapping: {path}")
    return data


def _expand(node: Any, shared: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        if "$shared" in node:
            ref = node["$shared"]
            if ref not in shared:
                raise KeyError(f"unknown shared fragment: {ref}")
            base = deepcopy(shared[ref])
            extra = {key: value for key, value in node.items() if key != "$shared"}
            extra = _expand(extra, shared)
            if isinstance(base, dict) and isinstance(extra, dict):
                base.update(extra)
                return base
            return extra if extra else base
        return {key: _expand(value, shared) for key, value in node.items()}
    if isinstance(node, list):
        return [_expand(item, shared) for item in node]
    return node


@lru_cache(maxsize=1)
def load_package() -> dict[str, Any]:
    return _load_yaml(PACKAGE_PATH)


def shared_fragments() -> dict[str, Any]:
    return deepcopy(load_package().get("shared") or {})


def list_tool_ids() -> list[str]:
    return sorted(path.stem for path in TOOLS_DIR.glob("*.yaml"))


def load_tool_contract(tool_id: str) -> dict[str, Any]:
    path = TOOLS_DIR / f"{tool_id}.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"no contract for tool {tool_id!r}")
    raw = _load_yaml(path)
    return _expand(raw, shared_fragments())


def load_all_tool_contracts() -> list[dict[str, Any]]:
    return [load_tool_contract(tool_id) for tool_id in list_tool_ids()]


def resolved_bundle() -> dict[str, Any]:
    package = deepcopy(load_package())
    package.pop("shared", None)
    package["tools"] = load_all_tool_contracts()
    return package
