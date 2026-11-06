"""SSE listen address from env (Docker agent compose sets FASTMCP_*)."""

from __future__ import annotations

import os


def fastmcp_host_port() -> tuple[str, int]:
    host = os.environ.get("GDAL_TOOLS_HOST") or os.environ.get("FASTMCP_HOST") or "127.0.0.1"
    port = int(os.environ.get("GDAL_TOOLS_PORT") or os.environ.get("FASTMCP_PORT") or "8000")
    return host, port
