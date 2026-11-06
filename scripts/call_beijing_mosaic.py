"""Call remote gdal-tools MCP: probe Beijing DEM tiles, then mosaic."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.sse import sse_client

URL = "http://10.0.85.29:7861/sse"

BEIJING_TILES = [
    "N39E115",
    "N39E116",
    "N39E117",
    "N40E115",
    "N40E116",
    "N40E117",
    "N41E115",
    "N41E116",
    "N41E117",
]

ROOTS = [
    "/data",
    "/data/dem",
    "/data/beijing",
    "/data/dem/beijing",
    "/data/dem/srtm",
    "/data/dem/gdem",
    "/data/dem/SRTM",
    "/data/SRTM",
    "/data/gdem",
    "/data/gis",
    "/data/gis/dem",
]


def candidate_paths() -> list[str]:
    paths: list[str] = []
    suffixes = [".hgt", ".tif", ".tiff", ".TIF"]
    for root in ROOTS:
        for tile in BEIJING_TILES:
            for suf in suffixes:
                paths.append(f"{root}/{tile}{suf}")
                paths.append(f"{root}/{tile.lower()}{suf}")
            paths.append(f"{root}/ASTGTMV003_{tile}_dem.tif")
            paths.append(f"{root}/{tile}.hgt.zip")
    return paths


def unwrap(result) -> str:
    if result.content:
        parts = []
        for item in result.content:
            text = getattr(item, "text", None)
            if text:
                parts.append(text)
        return "\n".join(parts)
    return json.dumps(result.model_dump(mode="json"), ensure_ascii=False, default=str)


async def call(session: ClientSession, name: str, arguments: dict, timeout: float = 120.0):
    result = await asyncio.wait_for(session.call_tool(name, arguments), timeout=timeout)
    text = unwrap(result)
    if result.isError:
        raise RuntimeError(f"{name} error: {text[:2000]}")
    return text


async def main() -> int:
    async with sse_client(URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            print("tools:", names)
            status = await call(session, "gdal_status", {})
            print("status:", status[:800])

            found: list[str] = []
            for path in candidate_paths():
                try:
                    text = await call(session, "raster_info", {"data_path": path}, timeout=8)
                except Exception:
                    continue
                found.append(path)
                print("FOUND", path)
                if len(found) == 1:
                    print(text[:600])

            if not found:
                print("NO_TILES_FOUND: 无法列出目录，已按北京范围常见分幅名探测 /data 下若干路径。")
                return 2

            print(f"tile_count={len(found)}")
            vrt = "/data/outputs/beijing_dem_mosaic.vrt"
            tif = "/data/outputs/beijing_dem_mosaic.tif"
            vrt_text = await call(
                session,
                "raster_build_vrt",
                {
                    "data_paths": found,
                    "output_path": vrt,
                    "resolution": "highest",
                },
                timeout=600,
            )
            print("VRT:", vrt_text[:1500])
            warp_text = await call(
                session,
                "raster_warp",
                {
                    "data_path": vrt,
                    "output_path": tif,
                    "resampling": "bilinear",
                    "creation_options": {"COMPRESS": "DEFLATE", "TILED": "YES", "BIGTIFF": "IF_SAFER"},
                },
                timeout=1800,
            )
            print("WARP:", warp_text[:2000])
            info = await call(session, "raster_info", {"data_path": tif, "statistics": True}, timeout=300)
            print("MOSAIC_INFO:", info[:2000])
            out = Path(__file__).resolve().parents[1] / "outputs"
            out.mkdir(exist_ok=True)
            (out / "beijing_mosaic_result.json").write_text(
                json.dumps({"tiles": found, "vrt": vrt, "tif": tif, "info": info}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception as exc:
        print("FAILED:", type(exc).__name__, exc)
        raise SystemExit(1)
