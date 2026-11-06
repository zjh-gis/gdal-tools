"""Parse annotation strings stored in mcp_tool_manifest.yaml."""

from __future__ import annotations

from typing import Any, Literal, get_args, get_origin


def parse_annotation(text: str) -> Any:
    text = text.strip()
    if text == "Any":
        return Any
    if text == "str":
        return str
    if text == "int":
        return int
    if text == "float":
        return float
    if text == "bool":
        return bool
    if text == "None":
        return type(None)
    if " | " in text:
        parts = [p.strip() for p in text.split(" | ")]
        if parts[-1] == "None":
            if len(parts) == 2:
                return parse_annotation(parts[0]) | None
            union_parts = [parse_annotation(p) for p in parts[:-1]]
            ann = union_parts[0]
            for part in union_parts[1:]:
                ann = ann | part
            return ann | None
        parsed = [parse_annotation(p) for p in parts]
        ann = parsed[0]
        for part in parsed[1:]:
            ann = ann | part
        return ann
    if text.startswith("list[") and text.endswith("]"):
        inner = parse_annotation(text[5:-1].strip())
        return list[inner]  # type: ignore[valid-type]
    if text.startswith("dict[") and text.endswith("]"):
        inner = text[5:-1]
        if "," in inner:
            k, v = inner.split(",", 1)
            return dict[parse_annotation(k.strip()), parse_annotation(v.strip())]  # type: ignore[valid-type]
        return dict
    if text.startswith("Literal"):
        # Literal['a', 'b'] style from annotation_to_str
        value = eval(text, {"__builtins__": {}}, {"Literal": Literal})  # noqa: S307
        return value
    raise ValueError(f"unsupported manifest annotation: {text!r}")
