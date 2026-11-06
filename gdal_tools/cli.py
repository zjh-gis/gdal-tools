"""Command-line entry: list / describe / run / status / serve-mcp.

Platforms that do not speak MCP can treat this as a Job command:

    gdal-tools run gdal.dem_slope --args '{"data_path":"...","output_path":"..."}'
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml

from gdal_tools import __version__
from gdal_tools.contracts.loader import load_all_tool_contracts, load_package, load_tool_contract, resolved_bundle
from gdal_tools.errors import ERRORS, exit_code
from gdal_tools.registry import all_specs, get_spec
from gdal_tools.runner import run_envelope


def _dump(data: Any, *, as_json: bool) -> None:
    if as_json:
        json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return
    yaml.safe_dump(data, sys.stdout, allow_unicode=True, sort_keys=False)


def _parse_args_json(raw: str) -> dict[str, Any]:
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("--args must be a JSON object")
    return data


def _kwargs_from_unknown(tokens: list[str]) -> dict[str, Any]:
    """Turn leftover ``--foo-bar value`` flags into ``{foo_bar: value}``."""
    result: dict[str, Any] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("--") or token == "--":
            raise ValueError(f"unexpected argument: {token}")
        key = token[2:].replace("-", "_")
        if index + 1 >= len(tokens) or tokens[index + 1].startswith("--"):
            result[key] = True
            index += 1
            continue
        result[key] = _coerce_cli_value(tokens[index + 1])
        index += 2
    return result


def _coerce_cli_value(raw: str) -> Any:
    lowered = raw.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered == "null":
        return None
    if raw.startswith("{") or raw.startswith("["):
        return json.loads(raw)
    try:
        if "." in raw:
            return float(raw)
        return int(raw)
    except ValueError:
        return raw


def cmd_list(ns: argparse.Namespace) -> int:
    rows = []
    for spec in all_specs():
        try:
            contract = load_tool_contract(spec.tool_id)
            title = contract.get("tool", {}).get("title", spec.tool_id)
            description = contract.get("tool", {}).get("description", "")
        except FileNotFoundError:
            title, description = spec.tool_id, ""
        rows.append(
            {
                "id": spec.tool_id,
                "mcp": spec.mcp_name,
                "title": title,
                "description": description.strip(),
                "heavy": spec.heavy,
            }
        )
    _dump(rows, as_json=not ns.yaml)
    return 0


def cmd_describe(ns: argparse.Namespace) -> int:
    if ns.errors:
        _dump(
            {
                code: {
                    "exit_code": spec.exit_code,
                    "retryable": spec.retryable,
                    "description": spec.description,
                }
                for code, spec in ERRORS.items()
            },
            as_json=not ns.yaml,
        )
        return 0
    if ns.all:
        _dump(resolved_bundle(), as_json=not ns.yaml)
        return 0
    if not ns.tool_id:
        print("describe requires a tool id, --all, or --errors", file=sys.stderr)
        return 2
    spec = get_spec(ns.tool_id)
    _dump(load_tool_contract(spec.tool_id), as_json=not ns.yaml)
    return 0


def cmd_run(ns: argparse.Namespace) -> int:
    arguments: dict[str, Any] = {}
    if ns.args:
        arguments.update(_parse_args_json(ns.args))
    if ns.unknown:
        arguments.update(_kwargs_from_unknown(ns.unknown))
    envelope = run_envelope(ns.tool_id, arguments)
    json.dump(envelope, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    if envelope.get("ok"):
        return 0
    return int(envelope.get("exit_code") or exit_code(envelope.get("error_code", "internal_error")))


def cmd_status(_ns: argparse.Namespace) -> int:
    from gdal_tools.core.gdal import gdal_executor

    status = gdal_executor.status()
    status["package"] = "gdal-tools"
    status["version"] = __version__
    json.dump(status, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0 if status.get("gdal_available", True) or "gdal" in status else 0


def cmd_serve_mcp(ns: argparse.Namespace) -> int:
    os.environ["MCP_TRANSPORT"] = ns.transport
    if ns.host:
        os.environ["FASTMCP_HOST"] = ns.host
        os.environ["GDAL_TOOLS_HOST"] = ns.host
    if ns.port:
        os.environ["FASTMCP_PORT"] = str(ns.port)
        os.environ["GDAL_TOOLS_PORT"] = str(ns.port)
    pkg_root = Path(__file__).resolve().parents[1]
    mcp_root = pkg_root / "mcp"
    sys.path.insert(0, str(mcp_root))
    sys.path.insert(0, str(pkg_root))
    from importlib.machinery import SourceFileLoader

    server_path = mcp_root / "gdal-tools-mcp" / "server.py"
    module = SourceFileLoader("gdal_tools_mcp_server", str(server_path)).load_module()
    module.main()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gdal-tools",
        description="GDAL 地理处理工具：一工具一契约，共享镜像，CLI 与 MCP 共用同一实现。",
    )
    parser.add_argument("--version", action="version", version=f"gdal-tools {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    list_p = sub.add_parser("list", help="列出全部工具 id")
    list_p.add_argument("--yaml", action="store_true", help="以 YAML 输出（默认 JSON）")
    list_p.set_defaults(func=cmd_list)

    describe_p = sub.add_parser("describe", help="打印工具契约或整包契约")
    describe_p.add_argument("tool_id", nargs="?", help="工具 id，例如 gdal.dem_slope")
    describe_p.add_argument("--all", action="store_true", help="输出整包解析后的契约")
    describe_p.add_argument("--errors", action="store_true", help="输出公共错误码表")
    describe_p.add_argument("--yaml", action="store_true", default=True, help="YAML 输出（默认）")
    describe_p.add_argument("--json", dest="yaml", action="store_false", help="JSON 输出")
    describe_p.set_defaults(func=cmd_describe)

    run_p = sub.add_parser("run", help="按契约执行一个工具，stdout 输出 JSON 信封")
    run_p.add_argument("tool_id", help="工具 id 或 MCP 名，例如 gdal.dem_slope / dem_slope")
    run_p.add_argument("--args", help="JSON 对象，字段名与契约一致")
    run_p.set_defaults(func=cmd_run)

    status_p = sub.add_parser("status", help="报告本镜像内 GDAL/OGR 是否可用")
    status_p.set_defaults(func=cmd_status)

    serve_p = sub.add_parser("serve-mcp", help="启动 MCP 服务（stdio 或 SSE）")
    serve_p.add_argument("--transport", choices=("stdio", "sse"), default=os.environ.get("MCP_TRANSPORT", "stdio"))
    serve_p.add_argument("--host", default=os.environ.get("GDAL_TOOLS_HOST") or os.environ.get("FASTMCP_HOST"))
    serve_p.add_argument("--port", type=int, default=None)
    serve_p.set_defaults(func=cmd_serve_mcp)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    ns, unknown = parser.parse_known_args(argv)
    if ns.command != "run" and unknown:
        parser.error(f"unrecognized arguments: {' '.join(unknown)}")
    if ns.command == "run":
        ns.unknown = unknown
    try:
        return int(ns.func(ns))
    except ValueError as exc:
        print(json.dumps({"ok": False, "error_code": "invalid_argument", "message": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
