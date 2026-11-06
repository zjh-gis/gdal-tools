"""MCP adapter for gdal-tools.

Each MCP tool is a thin typed wrapper around ``gdal_tools.runner.run_tool``.
Algorithms, contracts and the CLI live outside this module so platforms that
do not speak MCP can still run the same image.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import re
import sys
from pathlib import Path
from typing import Annotated, Any, Callable, Literal

# package root (gdal_tools/) + mcp/ on path
_HERE = Path(__file__).resolve().parent
_MCP_ROOT = _HERE.parent
_PKG_ROOT = _MCP_ROOT.parent
for candidate in (_MCP_ROOT, _PKG_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import anyio
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from gdal_tools import __version__ as PACKAGE_VERSION
from gdal_tools.core.gdal import gdal_executor
from gdal_tools.errors import classify
from gdal_tools.runner import run_tool
from shared.agent_surface import register_agent_surface
from shared.network_bind import fastmcp_host_port
from gdal_tools.workspace import ensure_allowed

ERROR_CODES = {
    "invalid_argument": "Parameter rejected before GDAL runs (range, enum, expression, layer name)",
    "path_not_allowed": "A path is outside GDAL_TOOLS_WORKSPACES",
    "not_found": "An input file does not exist",
    "output_exists": "The output exists and overwrite is false",
    "gdal_unavailable": "A required GDAL command is not installed",
    "gdal_timeout": "GDAL exceeded GDAL_COMMAND_TIMEOUT_SECONDS",
    "gdal_failed": "GDAL returned a non-zero exit code or reported ERROR lines",
    "internal_error": "Unexpected server error",
}


def _server_version() -> str:
    text = (_HERE / "metadata.yaml").read_text(encoding="utf-8")
    match = re.search(r'^version:\s*"?([^"\s]+)"?', text, re.MULTILINE)
    return match.group(1) if match else "0.0.0"


SERVER_VERSION = _server_version()

_mcp_host, _mcp_port = fastmcp_host_port()
mcp = FastMCP(
    "gdal-tools-mcp",
    host=_mcp_host,
    port=_mcp_port,
    instructions=(
        "Deterministic GDAL/OGR tools. "
        "Read gdal-tools://contracts for per-tool inputs, outputs and constraints. "
        "Paths must be under GDAL_TOOLS_WORKSPACES (image default: /data). "
        "Long-running tools stream progress when you pass a progressToken. "
        "raster_warp is the single tool for clipping a raster by a vector boundary. "
        "The same tools are also available as `gdal-tools run <id> --args '{...}'`."
    ),
)
mcp._mcp_server.version = SERVER_VERSION
register_agent_surface(mcp, _PKG_ROOT)

_job_limiter: anyio.CapacityLimiter | None = None

PathArg = Annotated[str, Field(description="Path inside the MCP workspace, e.g. /data/x.tif")]
OutputRaster = Annotated[
    str,
    Field(description="Output raster path (.tif recommended; GeoTIFF is written atomically)"),
]
OutputVector = Annotated[
    str,
    Field(description="Output vector path ending in .gpkg, .geojson/.json or .fgb"),
]
CreationOptions = Annotated[
    dict[str, str] | None,
    Field(
        description=(
            "GeoTIFF creation options, e.g. {\"COMPRESS\": \"DEFLATE\", \"TILED\": \"YES\", "
            "\"BIGTIFF\": \"IF_SAFER\"}. Allowed keys: BIGTIFF, BLOCKSIZE, BLOCKXSIZE, "
            "BLOCKYSIZE, COMPRESS, COPY_SRC_OVERVIEWS, INTERLEAVE, LEVEL, NUM_THREADS, "
            "PREDICTOR, QUALITY, SPARSE_OK, TILED, ZLEVEL"
        )
    ),
]
BBox = Annotated[
    list[float] | None,
    Field(min_length=4, max_length=4, description="[xmin, ymin, xmax, ymax]"),
]
DataType = Literal["Byte", "Int8", "UInt16", "Int16", "UInt32", "Int32", "Float32", "Float64"]
WarpResampling = Literal[
    "near", "bilinear", "cubic", "cubicspline", "lanczos", "average", "rms",
    "mode", "max", "min", "med", "q1", "q3", "sum",
]
DemAlgorithm = Annotated[
    Literal["Horn", "ZevenbergenThorne"] | None,
    Field(description="Horn (default, rough terrain) or ZevenbergenThorne (smooth terrain)"),
]
VectorFormat = Annotated[
    Literal["GPKG", "GeoJSON", "FlatGeobuf"] | None,
    Field(description="Defaults to the format implied by the output_path suffix"),
]


def _ok(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False, indent=2)


def error_code(exc: BaseException) -> str:
    return classify(exc).code


def gdal_tool(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Register an MCP tool whose failures carry a ``[error_code]`` message prefix."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ToolError(f"[{error_code(exc)}] {exc}") from exc

    return mcp.tool()(wrapper)


def _allowed(path: str | None, field: str) -> str | None:
    return str(ensure_allowed(path, field=field)) if path else None


def _payload(**values: Any) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def _limiter() -> anyio.CapacityLimiter:
    global _job_limiter
    if _job_limiter is None:
        raw = os.environ.get("GDAL_TOOLS_MAX_CONCURRENT_JOBS") or os.environ.get(
            "GDAL_MCP_MAX_CONCURRENT_JOBS", "2"
        )
        try:
            total = max(1, int(raw))
        except ValueError:
            total = 2
        _job_limiter = anyio.CapacityLimiter(total)
    return _job_limiter


async def _run_job(
    ctx: Context | None,
    tool_id: str,
    payload: dict[str, Any],
    *,
    heavy: bool = True,
) -> str:
    """Dispatch to the shared runner off the event loop."""
    loop = asyncio.get_running_loop()

    def report(percent: float) -> None:
        if ctx is None or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(
                ctx.report_progress(percent, 100.0, f"{tool_id} {percent:.0f}%"), loop
            )
        except RuntimeError:
            pass

    def call() -> dict[str, Any]:
        return run_tool(tool_id, payload, progress=report if heavy else None)

    if not heavy:
        result = await anyio.to_thread.run_sync(call)
        return _ok(result)
    limiter = _limiter()
    if ctx is not None and limiter.available_tokens < 1:
        try:
            await ctx.report_progress(0.0, 100.0, f"{tool_id} waiting for a free GDAL worker")
        except Exception:  # noqa: BLE001
            pass
    result = await anyio.to_thread.run_sync(call, limiter=limiter)
    return _ok(result)


# --------------------------------------------------------------------------- inspect


@gdal_tool
async def raster_info(
    data_path: PathArg,
    statistics: Annotated[bool, Field(description="Compute min/max/mean/stdDev per band (-stats)")] = False,
    checksum: Annotated[bool, Field(description="Compute band checksums (-checksum)")] = False,
) -> str:
    """Inspect raster metadata (CRS, size, bands, nodata, overviews, optional statistics) via gdalinfo."""
    payload = _payload(
        data_path=_allowed(data_path, "data_path"), statistics=statistics, checksum=checksum
    )
    return await _run_job(None, "gdal.raster_info", payload, heavy=False)


@gdal_tool
async def vector_info(
    data_path: PathArg,
    layer: Annotated[str | None, Field(description="Only describe this layer")] = None,
) -> str:
    """Inspect vector dataset layers, fields, CRS and feature counts via ogrinfo."""
    payload = _payload(data_path=_allowed(data_path, "data_path"), layer=layer)
    return await _run_job(None, "gdal.vector_info", payload, heavy=False)


@gdal_tool
async def gdal_status() -> str:
    """Report GDAL version, server version and whether the GDAL/OGR CLI tools are available."""
    status = await anyio.to_thread.run_sync(gdal_executor.status)
    return _ok(
        {
            **status,
            "server": "gdal-tools-mcp",
            "server_version": SERVER_VERSION,
            "package_version": PACKAGE_VERSION,
        }
    )


# --------------------------------------------------------------------------- raster transforms


@gdal_tool
async def raster_convert(
    data_path: PathArg,
    output_path: OutputRaster,
    ctx: Context,
    output_format: Literal["GTiff", "COG", "VRT", "PNG"] = "GTiff",
    bands: Annotated[list[int] | None, Field(description="1-based bands to keep, in order")] = None,
    bbox: Annotated[
        list[float] | None,
        Field(
            min_length=4,
            max_length=4,
            description="Rectangular subset [xmin, ymin, xmax, ymax] in the raster CRS (or bbox_crs)",
        ),
    ] = None,
    bbox_crs: Annotated[str | None, Field(description="CRS of bbox, e.g. EPSG:4326")] = None,
    output_type: DataType | None = None,
    nodata: Annotated[float | None, Field(description="Assign this nodata value (-a_nodata)")] = None,
    creation_options: CreationOptions = None,
) -> str:
    """Convert format, select bands, change data type or cut a rectangle with gdal_translate."""
    payload = _payload(
        data_path=_allowed(data_path, "data_path"),
        output_path=_allowed(output_path, "output_path"),
        output_format=output_format,
        bands=bands,
        bbox=bbox,
        bbox_crs=bbox_crs,
        output_type=output_type,
        nodata=nodata,
        creation_options=creation_options,
    )
    return await _run_job(ctx, "gdal.raster_convert", payload)


@gdal_tool
async def raster_warp(
    output_path: OutputRaster,
    ctx: Context,
    data_path: Annotated[str | None, Field(description="Single input raster")] = None,
    data_paths: Annotated[
        list[str] | None, Field(description="Several input rasters to mosaic (order preserved)")
    ] = None,
    target_crs: Annotated[str | None, Field(description="Target CRS such as EPSG:32650")] = None,
    resolution: Annotated[
        float | list[float] | None, Field(description="Target pixel size or [xres, yres]")
    ] = None,
    bbox: BBox = None,
    bbox_crs: Annotated[str | None, Field(description="CRS of bbox (default: target CRS)")] = None,
    resampling: WarpResampling = "near",
    cutline_path: Annotated[
        str | None, Field(description="Polygon file (GeoJSON/GPKG/SHP) to clip by; any CRS")
    ] = None,
    cutline_layer: Annotated[str | None, Field(description="Layer inside cutline_path")] = None,
    cutline_where: Annotated[
        str | None, Field(description="SQL filter on cutline features, e.g. NAME = 'Beijing'")
    ] = None,
    crop_to_cutline: Annotated[
        bool, Field(description="Shrink the output extent to the cutline (usually wanted)")
    ] = False,
    src_nodata: float | None = None,
    dst_nodata: Annotated[float | None, Field(description="Nodata for pixels outside the cutline")] = None,
    output_type: DataType | None = None,
    multithread: Annotated[bool, Field(description="Use all CPU cores (-multi)")] = False,
    creation_options: CreationOptions = None,
) -> str:
    """Reproject, resample, mosaic, or clip a raster by bbox / vector boundary with gdalwarp.

    This is the single tool for clipping a raster by a polygon: set cutline_path and
    crop_to_cutline=true. Output CRS and grid are verified after the write.
    """
    payload = _payload(
        output_path=_allowed(output_path, "output_path"),
        data_path=_allowed(data_path, "data_path"),
        data_paths=[_allowed(p, "data_paths") for p in data_paths] if data_paths else None,
        target_crs=target_crs,
        resolution=resolution,
        bbox=bbox,
        bbox_crs=bbox_crs,
        resampling=resampling,
        cutline_path=_allowed(cutline_path, "cutline_path"),
        cutline_layer=cutline_layer,
        cutline_where=cutline_where,
        crop_to_cutline=crop_to_cutline,
        src_nodata=src_nodata,
        dst_nodata=dst_nodata,
        output_type=output_type,
        multithread=multithread,
        creation_options=creation_options,
    )
    return await _run_job(ctx, "gdal.raster_warp", payload)


@gdal_tool
async def raster_build_vrt(
    output_path: Annotated[str, Field(description="Output .vrt path")],
    data_paths: Annotated[list[str], Field(min_length=1, description="Input rasters")],
    ctx: Context,
    separate: Annotated[
        bool, Field(description="Stack each input as its own band instead of mosaicking")
    ] = False,
    resolution: Literal["highest", "lowest", "average"] | None = None,
    target_resolution: Annotated[
        float | list[float] | None, Field(description="Explicit pixel size (overrides resolution)")
    ] = None,
    bbox: BBox = None,
    resampling: Literal["nearest", "bilinear", "cubic", "cubicspline", "lanczos", "average", "mode"]
    | None = None,
    bands: Annotated[list[int] | None, Field(description="Only use these input bands")] = None,
    src_nodata: float | None = None,
    vrt_nodata: float | None = None,
    add_alpha: bool = False,
) -> str:
    """Build a virtual mosaic or band stack (gdalbuildvrt) without copying pixels.

    Fast for many tiles; downstream tools can read the .vrt directly. The VRT keeps
    references to the source files, so do not move them afterwards.
    """
    payload = _payload(
        output_path=_allowed(output_path, "output_path"),
        data_paths=[_allowed(p, "data_paths") for p in data_paths],
        separate=separate,
        resolution=resolution,
        target_resolution=target_resolution,
        bbox=bbox,
        resampling=resampling,
        bands=bands,
        src_nodata=src_nodata,
        vrt_nodata=vrt_nodata,
        add_alpha=add_alpha,
    )
    return await _run_job(ctx, "gdal.raster_build_vrt", payload)


@gdal_tool
async def raster_calc(
    output_path: OutputRaster,
    inputs: Annotated[
        dict[str, str],
        Field(description="Map of single uppercase letters to rasters, e.g. {\"A\": \"/data/b4.tif\"}"),
    ],
    expression: Annotated[
        str,
        Field(
            description=(
                "numpy expression over the letters, e.g. (B - A) / (B + A) or "
                "where(A > 100, 1, 0). Allowed: + - * / // % **, comparisons, & | ~, "
                "and numpy functions such as where, logical_and, sqrt, log, clip, minimum, maximum"
            )
        ),
    ],
    ctx: Context,
    input_bands: Annotated[
        dict[str, int] | None,
        Field(description="Band per letter (default 1), e.g. {\"A\": 3, \"B\": 2}"),
    ] = None,
    output_type: DataType = "Float32",
    cast_inputs_to_float: Annotated[
        bool | None,
        Field(description="Wrap inputs in float64() to avoid integer overflow (default: on for Float outputs)"),
    ] = None,
    no_data_value: float | None = None,
    extent: Annotated[
        Literal["ignore", "fail", "union", "intersect"] | None,
        Field(description="How to combine inputs with different extents"),
    ] = None,
    creation_options: CreationOptions = None,
) -> str:
    """Band math with gdal_calc (NDVI and other indices, masks, thresholds, reclassification).

    The expression is validated against a numpy-only whitelist before execution.
    """
    payload = _payload(
        output_path=_allowed(output_path, "output_path"),
        inputs={name: _allowed(path, f"inputs.{name}") for name, path in inputs.items()},
        expression=expression,
        input_bands=input_bands,
        output_type=output_type,
        cast_inputs_to_float=cast_inputs_to_float,
        no_data_value=no_data_value,
        extent=extent,
        creation_options=creation_options,
    )
    return await _run_job(ctx, "gdal.raster_calc", payload)


@gdal_tool
async def raster_overviews(
    data_path: PathArg,
    ctx: Context,
    levels: Annotated[
        list[int] | None, Field(description="Decimation factors, default [2, 4, 8, 16]")
    ] = None,
    resampling: Literal[
        "nearest", "average", "rms", "bilinear", "gauss", "cubic",
        "cubicspline", "lanczos", "average_magphase", "mode",
    ] = "average",
    external: Annotated[
        bool, Field(description="Write a sidecar .ovr instead of modifying the input file")
    ] = False,
    clean: Annotated[bool, Field(description="Remove existing overviews instead of building")] = False,
    compress: Literal["NONE", "DEFLATE", "LZW", "JPEG", "ZSTD", "PACKBITS"] | None = None,
) -> str:
    """Build or remove overview pyramids with gdaladdo (faster display of large rasters).

    Internal overviews (default) are written into the input file itself.
    """
    payload = _payload(
        data_path=_allowed(data_path, "data_path"),
        levels=levels,
        resampling=resampling,
        external=external,
        clean=clean,
        compress=compress,
    )
    return await _run_job(ctx, "gdal.raster_overviews", payload)


@gdal_tool
async def raster_polygonize(
    data_path: PathArg,
    output_path: OutputVector,
    ctx: Context,
    band: int = 1,
    output_format: VectorFormat = None,
    layer_name: str = "polygons",
    field_name: Annotated[str, Field(description="Attribute that stores the pixel value")] = "value",
    connectedness: Literal[4, 8] = 4,
    mask_path: Annotated[
        str | None, Field(description="Raster whose zero pixels are excluded")
    ] = None,
    use_mask: Annotated[
        bool, Field(description="Use the band's nodata/mask (set false to polygonize everything)")
    ] = True,
    overwrite: bool = False,
) -> str:
    """Convert connected regions of equal pixel value into polygons (gdal_polygonize).

    Typical use: threshold with raster_calc to a Byte class raster, then polygonize.
    """
    payload = _payload(
        data_path=_allowed(data_path, "data_path"),
        output_path=_allowed(output_path, "output_path"),
        band=band,
        output_format=output_format,
        layer_name=layer_name,
        field_name=field_name,
        connectedness=connectedness,
        mask_path=_allowed(mask_path, "mask_path"),
        use_mask=use_mask,
        overwrite=overwrite,
    )
    return await _run_job(ctx, "gdal.raster_polygonize", payload)


# --------------------------------------------------------------------------- DEM


def _dem_payload(data_path: str, output_path: str, **options: Any) -> dict[str, Any]:
    return _payload(
        data_path=_allowed(data_path, "data_path"),
        output_path=_allowed(output_path, "output_path"),
        **options,
    )


ZScale = Annotated[
    float | None,
    Field(
        description=(
            "Vertical/horizontal unit ratio (-s). For DEMs in degrees with elevations in metres use 111120"
        )
    ),
]


@gdal_tool
async def dem_slope(
    data_path: PathArg,
    output_path: OutputRaster,
    ctx: Context,
    percent: Annotated[bool, Field(description="Percent rise instead of degrees")] = False,
    scale: ZScale = None,
    algorithm: DemAlgorithm = None,
    compute_edges: bool = False,
    band: int = 1,
    creation_options: CreationOptions = None,
) -> str:
    """Compute DEM slope (degrees or percent) with gdaldem slope."""
    payload = _dem_payload(
        data_path,
        output_path,
        percent=percent,
        scale=scale,
        algorithm=algorithm,
        compute_edges=compute_edges,
        band=band,
        creation_options=creation_options,
    )
    return await _run_job(ctx, "gdal.dem_slope", payload)


@gdal_tool
async def dem_aspect(
    data_path: PathArg,
    output_path: OutputRaster,
    ctx: Context,
    trigonometric: Annotated[
        bool, Field(description="0°=east counter-clockwise instead of 0°=north clockwise")
    ] = False,
    zero_for_flat: Annotated[bool, Field(description="Flat areas get 0 instead of -9999")] = False,
    algorithm: DemAlgorithm = None,
    compute_edges: bool = False,
    band: int = 1,
    creation_options: CreationOptions = None,
) -> str:
    """Compute DEM aspect (slope direction, degrees) with gdaldem aspect."""
    payload = _dem_payload(
        data_path,
        output_path,
        trigonometric=trigonometric,
        zero_for_flat=zero_for_flat,
        algorithm=algorithm,
        compute_edges=compute_edges,
        band=band,
        creation_options=creation_options,
    )
    return await _run_job(ctx, "gdal.dem_aspect", payload)


@gdal_tool
async def dem_hillshade(
    data_path: PathArg,
    output_path: OutputRaster,
    ctx: Context,
    azimuth: Annotated[float | None, Field(description="Light direction, degrees (default 315)")] = None,
    altitude: Annotated[float | None, Field(description="Light elevation, degrees (default 45)")] = None,
    z_factor: Annotated[float | None, Field(description="Vertical exaggeration (default 1)")] = None,
    scale: ZScale = None,
    shading: Literal["default", "combined", "multidirectional", "igor"] = "default",
    algorithm: DemAlgorithm = None,
    compute_edges: bool = False,
    band: int = 1,
    creation_options: CreationOptions = None,
) -> str:
    """Render a shaded-relief (hillshade) Byte raster from a DEM with gdaldem hillshade."""
    payload = _dem_payload(
        data_path,
        output_path,
        azimuth=azimuth,
        altitude=altitude,
        z_factor=z_factor,
        scale=scale,
        shading=shading,
        algorithm=algorithm,
        compute_edges=compute_edges,
        band=band,
        creation_options=creation_options,
    )
    return await _run_job(ctx, "gdal.dem_hillshade", payload)


@gdal_tool
async def dem_terrain_index(
    data_path: PathArg,
    output_path: OutputRaster,
    ctx: Context,
    metric: Annotated[
        Literal["TRI", "TPI", "roughness"],
        Field(description="TRI ruggedness, TPI topographic position, or roughness"),
    ] = "TRI",
    tri_algorithm: Literal["Wilson", "Riley"] | None = None,
    compute_edges: bool = False,
    band: int = 1,
    creation_options: CreationOptions = None,
) -> str:
    """Compute terrain ruggedness (TRI), topographic position (TPI) or roughness with gdaldem."""
    payload = _dem_payload(
        data_path,
        output_path,
        metric=metric,
        tri_algorithm=tri_algorithm,
        compute_edges=compute_edges,
        band=band,
        creation_options=creation_options,
    )
    return await _run_job(ctx, "gdal.dem_terrain_index", payload)


@gdal_tool
async def dem_contour(
    data_path: PathArg,
    output_path: OutputVector,
    ctx: Context,
    interval: Annotated[float | None, Field(description="Contour interval in DEM units")] = None,
    fixed_levels: Annotated[
        list[float] | None, Field(description="Explicit levels instead of interval")
    ] = None,
    offset: Annotated[float | None, Field(description="Base offset for interval contours")] = None,
    polygons: Annotated[
        bool, Field(description="Write filled contour bands (elev_min/elev_max) instead of lines")
    ] = False,
    attribute_name: str = "elev",
    layer_name: str = "contour",
    band: int = 1,
    src_nodata: float | None = None,
    output_format: VectorFormat = None,
    overwrite: bool = False,
) -> str:
    """Extract contour lines or filled contour polygons from a DEM with gdal_contour."""
    payload = _payload(
        data_path=_allowed(data_path, "data_path"),
        output_path=_allowed(output_path, "output_path"),
        interval=interval,
        fixed_levels=fixed_levels,
        offset=offset,
        polygons=polygons,
        attribute_name=attribute_name,
        layer_name=layer_name,
        band=band,
        src_nodata=src_nodata,
        output_format=output_format,
        overwrite=overwrite,
    )
    return await _run_job(ctx, "gdal.dem_contour", payload)


# --------------------------------------------------------------------------- vector


@gdal_tool
async def vector_convert(
    data_path: PathArg,
    output_path: Annotated[str, Field(description="Output vector path")],
    ctx: Context,
    output_format: Literal["GPKG", "GeoJSON", "ESRI Shapefile", "FlatGeobuf"] = "GPKG",
    target_crs: Annotated[str | None, Field(description="Reproject to this CRS, e.g. EPSG:4326")] = None,
    layer: Annotated[str | None, Field(description="Only convert this source layer")] = None,
    bbox: Annotated[
        list[float] | None,
        Field(min_length=4, max_length=4, description="Spatial filter [xmin, ymin, xmax, ymax] (-spat)"),
    ] = None,
    where: Annotated[str | None, Field(description="Attribute filter, e.g. TYPE = 'fault'")] = None,
    overwrite: bool = False,
) -> str:
    """Convert, reproject or filter vector data with ogr2ogr."""
    payload = _payload(
        data_path=_allowed(data_path, "data_path"),
        output_path=_allowed(output_path, "output_path"),
        output_format=output_format,
        target_crs=target_crs,
        layer=layer,
        bbox=bbox,
        where=where,
        overwrite=overwrite,
    )
    return await _run_job(ctx, "gdal.vector_convert", payload)


def main() -> None:
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "sse":
        mcp.run(transport="sse")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
