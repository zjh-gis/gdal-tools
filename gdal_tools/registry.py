"""Map public tool ids to algorithm functions in gdal_tools.core."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from gdal_tools.core import gdal as core

Handler = Callable[..., dict[str, Any]]


@dataclass(frozen=True)
class ToolSpec:
    tool_id: str
    mcp_name: str
    handler: Handler
    heavy: bool
    aliases: tuple[str, ...] = ()

    def names(self) -> tuple[str, ...]:
        return (self.tool_id, self.mcp_name, *self.aliases)


SPECS: tuple[ToolSpec, ...] = (
    ToolSpec("gdal.raster_info", "raster_info", core.raster_info, False),
    ToolSpec("gdal.vector_info", "vector_info", core.vector_info, False),
    ToolSpec("gdal.raster_convert", "raster_convert", core.translate_raster, True, ("gdal.translate",)),
    ToolSpec("gdal.raster_warp", "raster_warp", core.warp_raster, True, ("gdal.warp",)),
    ToolSpec("gdal.raster_build_vrt", "raster_build_vrt", core.build_vrt, True, ("gdal.build_vrt",)),
    ToolSpec("gdal.raster_calc", "raster_calc", core.raster_calc, True),
    ToolSpec("gdal.raster_overviews", "raster_overviews", core.raster_overviews, True, ("gdal.overviews",)),
    ToolSpec("gdal.raster_polygonize", "raster_polygonize", core.raster_polygonize, True, ("gdal.polygonize",)),
    ToolSpec("gdal.dem_slope", "dem_slope", core.dem_slope, True),
    ToolSpec("gdal.dem_aspect", "dem_aspect", core.dem_aspect, True),
    ToolSpec("gdal.dem_hillshade", "dem_hillshade", core.dem_hillshade, True),
    ToolSpec("gdal.dem_terrain_index", "dem_terrain_index", core.dem_terrain_index, True),
    ToolSpec("gdal.dem_contour", "dem_contour", core.dem_contour, True),
    ToolSpec("gdal.vector_convert", "vector_convert", core.convert_vector, True),
)

_BY_NAME: dict[str, ToolSpec] = {}
for _spec in SPECS:
    for _name in _spec.names():
        if _name in _BY_NAME:
            raise RuntimeError(f"duplicate tool name {_name}")
        _BY_NAME[_name] = _spec


def get_spec(name: str) -> ToolSpec:
    spec = _BY_NAME.get(name)
    if spec is None:
        known = ", ".join(spec.tool_id for spec in SPECS)
        raise ValueError(f"unknown tool {name!r}. Known tools: {known}")
    return spec


def all_specs() -> tuple[ToolSpec, ...]:
    return SPECS
