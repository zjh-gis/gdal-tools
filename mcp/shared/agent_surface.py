"""MCP resources for this image: machine-readable contracts plus optional catalog files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from gdal_tools.contracts.loader import resolved_bundle
from gdal_tools.errors import ERRORS


def register_agent_surface(mcp: Any, pkg_root: Path) -> None:
    metadata_dir = pkg_root / "metadata"
    mcp_dir = pkg_root / "mcp"

    @mcp.resource("gdal-tools://contracts")
    def package_contracts() -> str:
        return yaml.safe_dump(resolved_bundle(), allow_unicode=True, sort_keys=False)

    @mcp.resource("gdal-tools://errors")
    def error_catalog() -> str:
        payload = {
            code: {
                "exit_code": spec.exit_code,
                "retryable": spec.retryable,
                "description": spec.description,
            }
            for code, spec in ERRORS.items()
        }
        return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)

    @mcp.prompt(
        name="gdal_tools_usage",
        description="如何按契约调用本镜像中的 GDAL 工具",
    )
    def prompt_usage() -> str:
        return (
            "本镜像提供一组独立的 GDAL 计算任务，MCP 工具名与契约 id 对应。\n"
            "- 先读资源 gdal-tools://contracts，确认输入语义类型、格式、参数与输出。\n"
            "- 所有文件路径必须位于工作区（GDAL_TOOLS_WORKSPACES，镜像内默认 /data）。\n"
            "- 输入是本地文件路径；平台若只有对象存储，须先把数据落到工作区再调用。\n"
            "- 经纬度 DEM（度）计算坡度/山体阴影时设置 scale=111120。\n"
            "- 失败时文本以 [error_code] 开头，错误码见 gdal-tools://errors。\n"
            "- 不通过 MCP 时可用同一镜像：gdal-tools run <tool-id> --args '{...}'。\n"
        )

    def _read_if_exists(path: Path) -> str:
        if not path.is_file():
            return f"(missing {path.name})"
        return path.read_text(encoding="utf-8")

    if (mcp_dir / "capability_classes.yaml").is_file():

        @mcp.resource("mineral://capability-classes")
        def capability_classes() -> str:
            return _read_if_exists(mcp_dir / "capability_classes.yaml")

    if (metadata_dir / "tools_metadata.yaml").is_file():

        @mcp.resource("mineral://tools-metadata")
        def tools_metadata() -> str:
            return _read_if_exists(metadata_dir / "tools_metadata.yaml")

    if (metadata_dir / "capabilities.yaml").is_file():

        @mcp.resource("mineral://capabilities-vocabulary")
        def capabilities_vocabulary() -> str:
            return _read_if_exists(metadata_dir / "capabilities.yaml")

    if (metadata_dir / "execution_policies.yaml").is_file():

        @mcp.resource("mineral://execution-policies")
        def execution_policies() -> str:
            return _read_if_exists(metadata_dir / "execution_policies.yaml")

    if (metadata_dir / "environments.yaml").is_file():

        @mcp.resource("mineral://environments")
        def environments_catalog() -> str:
            return _read_if_exists(metadata_dir / "environments.yaml")
