from __future__ import annotations

from dataclasses import dataclass

from gdal_tools.core.gdal import GDALCommandError


@dataclass(frozen=True)
class ErrorSpec:
    code: str
    exit_code: int
    retryable: bool
    description: str


ERRORS: dict[str, ErrorSpec] = {
    spec.code: spec
    for spec in (
        ErrorSpec("invalid_argument", 2, False, "参数不符合契约或取值非法"),
        ErrorSpec("path_not_allowed", 3, False, "路径不在允许的工作区内"),
        ErrorSpec("not_found", 4, False, "输入文件不存在"),
        ErrorSpec("output_exists", 5, False, "输出已存在且未设置 overwrite=true"),
        ErrorSpec("gdal_unavailable", 10, False, "运行环境缺少所需的 GDAL 命令"),
        ErrorSpec("gdal_timeout", 11, True, "GDAL 命令超时"),
        ErrorSpec("gdal_failed", 12, False, "GDAL 命令失败或输出未通过校验"),
        ErrorSpec("internal_error", 1, False, "未预期的内部错误"),
    )
}


class ToolError(Exception):
    """An error already classified into the public error catalog."""

    def __init__(self, code: str, message: str):
        if code not in ERRORS:
            raise ValueError(f"unknown error code: {code}")
        super().__init__(message)
        self.code = code
        self.message = message


def classify(exc: BaseException) -> ToolError:
    if isinstance(exc, ToolError):
        return exc
    if isinstance(exc, GDALCommandError):
        return ToolError(exc.code, str(exc))
    if isinstance(exc, PermissionError):
        return ToolError("path_not_allowed", str(exc))
    if isinstance(exc, FileExistsError):
        return ToolError("output_exists", str(exc))
    if isinstance(exc, FileNotFoundError):
        return ToolError("not_found", str(exc))
    if isinstance(exc, (ValueError, TypeError)):
        return ToolError("invalid_argument", str(exc))
    return ToolError("internal_error", f"{type(exc).__name__}: {exc}")


def exit_code(code: str) -> int:
    return ERRORS[code].exit_code
