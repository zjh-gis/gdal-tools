from __future__ import annotations

import ast
import codecs
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from math import radians
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Iterable

from gdal_tools.core.result import ServiceResult, require_path


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
ProgressCallback = Callable[[float], None]

DEFAULT_COMMAND_TIMEOUT_SECONDS = 4 * 3600
METADATA_TIMEOUT_SECONDS = 600

ALLOWED_CREATION_OPTIONS = {
    "BIGTIFF",
    "BLOCKSIZE",
    "BLOCKXSIZE",
    "BLOCKYSIZE",
    "COMPRESS",
    "COPY_SRC_OVERVIEWS",
    "INTERLEAVE",
    "LEVEL",
    "NUM_THREADS",
    "PREDICTOR",
    "QUALITY",
    "SPARSE_OK",
    "TILED",
    "ZLEVEL",
}
ALLOWED_RESAMPLING = {
    "near",
    "bilinear",
    "cubic",
    "cubicspline",
    "lanczos",
    "average",
    "rms",
    "mode",
    "max",
    "min",
    "med",
    "q1",
    "q3",
    "sum",
}
OVERVIEW_RESAMPLING = {
    "nearest",
    "average",
    "rms",
    "bilinear",
    "gauss",
    "cubic",
    "cubicspline",
    "lanczos",
    "average_magphase",
    "mode",
}
VRT_RESAMPLING = {"nearest", "bilinear", "cubic", "cubicspline", "lanczos", "average", "mode"}
VRT_RESOLUTION_STRATEGIES = {"highest", "lowest", "average"}
OVERVIEW_COMPRESSION = {"NONE", "DEFLATE", "LZW", "JPEG", "ZSTD", "PACKBITS"}
RASTER_TRANSLATE_FORMATS = {"GTiff", "COG", "VRT", "PNG"}
RASTER_DATA_TYPES = {
    "Byte",
    "Int8",
    "UInt16",
    "Int16",
    "UInt32",
    "Int32",
    "Float32",
    "Float64",
}
VECTOR_OUTPUT_FORMATS: dict[str, tuple[str, ...]] = {
    "GPKG": (".gpkg",),
    "GeoJSON": (".geojson", ".json"),
    "FlatGeobuf": (".fgb",),
}
VECTOR_CONVERT_FORMATS = {"GPKG", "GeoJSON", "ESRI Shapefile", "FlatGeobuf"}
DEM_ALGORITHMS = {"Horn", "ZevenbergenThorne"}
HILLSHADE_MODES = {"default", "combined", "multidirectional", "igor"}
TERRAIN_INDEX_METRICS = {"TRI": "TRI", "TPI": "TPI", "ROUGHNESS": "roughness"}
TRI_ALGORITHMS = {"Wilson", "Riley"}
CALC_INPUT_NAME = re.compile(r"^[A-Z]$")
CALC_MAX_EXPRESSION_LENGTH = 1000
CALC_FUNCTIONS = frozenset(
    {
        "abs",
        "absolute",
        "arccos",
        "arcsin",
        "arctan",
        "arctan2",
        "ceil",
        "clip",
        "cos",
        "degrees",
        "exp",
        "fabs",
        "float32",
        "float64",
        "floor",
        "fmax",
        "fmin",
        "hypot",
        "int16",
        "int32",
        "isfinite",
        "isnan",
        "log",
        "log10",
        "log1p",
        "log2",
        "logical_and",
        "logical_not",
        "logical_or",
        "logical_xor",
        "maximum",
        "minimum",
        "nan_to_num",
        "power",
        "radians",
        "sign",
        "sin",
        "sqrt",
        "square",
        "tan",
        "uint8",
        "uint16",
        "where",
    }
)
_CALC_BINARY_OPERATORS = (
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.BitAnd,
    ast.BitOr,
    ast.BitXor,
)
_CALC_UNARY_OPERATORS = (ast.UAdd, ast.USub, ast.Invert)
_CALC_COMPARE_OPERATORS = (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE)
_LAYER_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_GDAL_ERROR_LINE = re.compile(r"^\s*ERROR \d+:")
WEB_MERCATOR_MAX_LATITUDE = 85.0511287798066
WEB_MERCATOR_RADIUS = 6378137.0
GEOTIFF_FILE_SUFFIXES = {".tif", ".tiff"}
GEOTIFF_OUTPUT_FORMATS = {"GTiff", "COG"}
_STAGED_SIDECAR_SUFFIXES = (".aux.xml", ".ovr", ".xml")


class GDALCommandError(RuntimeError):
    """A GDAL command could not run or failed.

    ``code`` is part of the tool error contract: gdal_failed, gdal_timeout or
    gdal_unavailable.
    """

    def __init__(self, message: str, code: str = "gdal_failed"):
        super().__init__(message)
        self.code = code


def default_command_timeout() -> float:
    raw = os.environ.get("GDAL_COMMAND_TIMEOUT_SECONDS", "").strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            value = 0.0
        if math.isfinite(value) and value > 0:
            return value
    return float(DEFAULT_COMMAND_TIMEOUT_SECONDS)


class GDALProgressParser:
    """Turn GDAL terminal progress (``0...10...20...100 - done.``) into percentages.

    Each dot between two decade markers is 2.5 %. Callback errors are swallowed so a
    disconnected client can never abort the GDAL process.
    """

    def __init__(self, callback: ProgressCallback):
        self._callback = callback
        self._digits = ""
        self._base: int | None = None
        self._dots = 0
        self._last = -1.0

    @property
    def last(self) -> float:
        return self._last

    def feed(self, text: str) -> None:
        for char in text:
            if char.isdigit():
                self._digits += char
                continue
            if self._digits:
                value = int(self._digits)
                self._digits = ""
                self._base = None
                if char == "." and value <= 100:
                    self._base = value
                    self._dots = 0
                    self._emit(float(value))
                elif value == 100 and (char.isspace() or char == "-"):
                    self._emit(100.0)
                if char != ".":
                    continue
            if char == "." and self._base is not None:
                self._dots += 1
                if self._dots <= 3:
                    self._emit(min(self._base + 2.5 * self._dots, 100.0))
            elif not char.isspace() and char != "-":
                self._base = None

    def _emit(self, value: float) -> None:
        if value <= self._last:
            return
        self._last = value
        try:
            self._callback(value)
        except Exception:  # noqa: BLE001
            pass


def _run_with_progress(
    argv: list[str],
    timeout: float,
    progress: ProgressCallback,
) -> subprocess.CompletedProcess[str]:
    """Run a GDAL CLI while streaming stdout into a progress parser."""
    parser = GDALProgressParser(progress)
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
    )
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []

    def pump(stream, sink: list[str], feed: Callable[[str], None] | None) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        reader = getattr(stream, "read1", stream.read)
        while True:
            chunk = reader(4096)
            if not chunk:
                break
            text = decoder.decode(chunk)
            sink.append(text)
            if feed is not None and text:
                feed(text)
        tail = decoder.decode(b"", final=True)
        if tail:
            sink.append(tail)

    readers = [
        threading.Thread(target=pump, args=(process.stdout, stdout_parts, parser.feed), daemon=True),
        threading.Thread(target=pump, args=(process.stderr, stderr_parts, None), daemon=True),
    ]
    for reader in readers:
        reader.start()
    try:
        return_code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        for reader in readers:
            reader.join(timeout=5)
        raise
    for reader in readers:
        reader.join()
    if return_code == 0 and parser.last < 100:
        parser._emit(100.0)
    return subprocess.CompletedProcess(
        args=argv,
        returncode=return_code,
        stdout="".join(stdout_parts),
        stderr="".join(stderr_parts),
    )


def _script_interpreter(script: str) -> list[str]:
    if os.name != "nt":
        return [script]
    script_dir = Path(script).parent
    for candidate in (script_dir / "python.exe", script_dir.parent / "python.exe"):
        if candidate.is_file():
            return [str(candidate), script]
    return [sys.executable, script]


class GDALCommandExecutor:
    """Execute a curated GDAL command surface without invoking a shell."""

    command_candidates: dict[str, tuple[str, ...]] = {
        "gdalinfo": ("gdalinfo",),
        "gdal_translate": ("gdal_translate",),
        "gdalwarp": ("gdalwarp",),
        "gdaldem": ("gdaldem",),
        "gdalbuildvrt": ("gdalbuildvrt",),
        "gdaladdo": ("gdaladdo",),
        "gdal_contour": ("gdal_contour",),
        "gdal_calc": ("gdal_calc", "gdal_calc.py"),
        "gdal_polygonize": ("gdal_polygonize", "gdal_polygonize.py"),
        "ogrinfo": ("ogrinfo",),
        "ogr2ogr": ("ogr2ogr",),
    }
    commands = tuple(command_candidates)

    def __init__(self, runner: CommandRunner | None = None):
        self.runner = runner or subprocess.run
        self._version: str | None = None

    def version(self) -> str | None:
        """Installed GDAL release (``gdalinfo --version``); None when GDAL is unavailable."""
        if self._version is not None or self.runner is not subprocess.run:
            return self._version
        prefix = self.resolve("gdalinfo")
        if prefix is None:
            return None
        try:
            process = subprocess.run(
                [*prefix, "--version"],
                capture_output=True,
                text=True,
                errors="replace",
                stdin=subprocess.DEVNULL,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        match = re.search(r"GDAL (\d+\.\d+\.\d+)", process.stdout or "")
        if match:
            self._version = match.group(1)
        return self._version

    def resolve(self, command: str) -> list[str] | None:
        for candidate in self.command_candidates.get(command, ()):
            found = shutil.which(candidate)
            if not found:
                continue
            if found.lower().endswith(".py"):
                return _script_interpreter(found)
            return [found]
        return None

    def status(self) -> dict[str, Any]:
        resolved = {command: self.resolve(command) for command in self.commands}
        return {
            "integration": "gdal",
            "available": all(resolved.values()),
            "gdal_version": self.version() if resolved.get("gdalinfo") else None,
            "commands": {
                command: (argv[-1] if argv else None) for command, argv in resolved.items()
            },
            "missing_commands": [
                command for command, argv in resolved.items() if argv is None
            ],
            "command_timeout_seconds": default_command_timeout(),
        }

    def execute(
        self,
        command: str,
        arguments: list[str],
        *,
        expect_json: bool = False,
        timeout: float | None = None,
        progress: ProgressCallback | None = None,
    ) -> Any:
        if command not in self.command_candidates:
            raise ValueError(f"Unsupported GDAL command: {command}")
        if not isinstance(arguments, list) or not all(isinstance(arg, str) for arg in arguments):
            raise TypeError("arguments must be a list of strings")
        if timeout is None:
            timeout = default_command_timeout()
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("timeout must be a positive number of seconds")
        uses_real_processes = self.runner is subprocess.run
        if uses_real_processes:
            prefix = self.resolve(command)
            if prefix is None:
                raise GDALCommandError(
                    f"GDAL command '{command}' is unavailable. "
                    "Install gdal-bin (and python3-gdal for gdal_calc/gdal_polygonize) "
                    "or run the provided Docker image.",
                    code="gdal_unavailable",
                )
        else:
            prefix = [command]
        argv = [*prefix, *arguments]
        try:
            if progress is not None and uses_real_processes and not expect_json:
                process = _run_with_progress(argv, timeout, progress)
            else:
                process = self.runner(
                    argv,
                    capture_output=True,
                    text=True,
                    errors="replace",
                    stdin=subprocess.DEVNULL,
                    timeout=timeout,
                    check=False,
                    shell=False,
                )
        except subprocess.TimeoutExpired as exc:
            raise GDALCommandError(
                f"{command} timed out after {timeout} seconds "
                "(raise GDAL_COMMAND_TIMEOUT_SECONDS for very large jobs)",
                code="gdal_timeout",
            ) from exc
        except OSError as exc:
            raise GDALCommandError(
                f"Unable to start {command}: {exc}", code="gdal_unavailable"
            ) from exc
        if process.returncode != 0:
            stderr = (process.stderr or "").strip()
            stdout = (process.stdout or "").strip()
            detail = stderr or stdout
            raise GDALCommandError(
                f"{command} failed with exit code {process.returncode}: "
                f"{detail[-4000:]}"
            )
        # Some GDAL errors (e.g. an unknown field in -cwhere) are reported but ignored with
        # exit code 0, which would silently produce a wrong result.
        reported_errors = [
            line.strip()
            for line in (process.stderr or "").splitlines()
            if _GDAL_ERROR_LINE.match(line)
        ]
        if reported_errors:
            raise GDALCommandError(
                f"{command} reported errors despite exit code 0: "
                f"{' | '.join(reported_errors)[-4000:]}"
            )
        stdout = (process.stdout or "").strip()
        if expect_json:
            try:
                return json.loads(stdout)
            except json.JSONDecodeError as exc:
                raise GDALCommandError(
                    f"{command} returned invalid JSON: {stdout[:500]}"
                ) from exc
        return stdout


gdal_executor = GDALCommandExecutor()


# --------------------------------------------------------------------------- inspect


def raster_info(payload: dict, executor: GDALCommandExecutor = gdal_executor) -> dict:
    data_path = require_path(payload.get("data_path", ""))
    arguments = ["-json"]
    if _coerce_bool(payload.get("statistics", False), "statistics"):
        arguments.append("-stats")
    if _coerce_bool(payload.get("checksum", False), "checksum"):
        arguments.append("-checksum")
    arguments.append(str(data_path))
    started_at = datetime.now(timezone.utc)
    started_clock = perf_counter()
    info = executor.execute(
        "gdalinfo", arguments, expect_json=True, timeout=METADATA_TIMEOUT_SECONDS
    )
    finished_at = datetime.now(timezone.utc)
    result = ServiceResult(
        service_id="gdal_raster_info",
        status="ok",
        summary="GDAL栅格元数据读取完成。",
        outputs={
            "input": str(data_path),
            "raster": _summarize_raster_metadata(info, data_path),
            "metadata": info,
        },
    ).to_dict()
    result["execution"] = _execution_record(
        "gdalinfo", arguments, started_at, finished_at, _elapsed_ms(started_clock)
    )
    return result


def vector_info(payload: dict, executor: GDALCommandExecutor = gdal_executor) -> dict:
    data_path = require_path(payload.get("data_path", ""))
    arguments = ["-ro", "-so", "-al", str(data_path)]
    if payload.get("layer"):
        arguments = ["-ro", "-so", str(data_path), str(payload["layer"])]
    info = executor.execute("ogrinfo", arguments, timeout=METADATA_TIMEOUT_SECONDS)
    return ServiceResult(
        service_id="ogr_info",
        status="ok",
        summary="OGR矢量数据集信息读取完成。",
        outputs={
            "input": str(data_path),
            "feature_count": _feature_count(info),
            "layers": _summarize_ogrinfo_layers(info),
            "info": info,
        },
    ).to_dict()


# --------------------------------------------------------------------------- raster transforms


def translate_raster(
    payload: dict,
    executor: GDALCommandExecutor = gdal_executor,
    *,
    progress: ProgressCallback | None = None,
) -> dict:
    data_path = require_path(payload.get("data_path", ""))
    output_path = _prepare_output(
        payload.get("output_path", ""),
        "output_path",
        inputs=[data_path],
        allow_existing=True,
    )
    output_format = _choice(
        payload.get("output_format", "GTiff"), RASTER_TRANSLATE_FORMATS, "output_format"
    )
    options = ["-of", output_format]
    for band in _validated_band_list(payload.get("bands", [])):
        options.extend(["-b", str(band)])
    if payload.get("output_type"):
        options.extend(["-ot", _choice(payload["output_type"], RASTER_DATA_TYPES, "output_type")])
    if payload.get("nodata") is not None:
        options.extend(["-a_nodata", _number_text(payload["nodata"], "nodata")])
    bbox = _optional_bbox(payload.get("bbox"), "bbox")
    projwin: list[float] | None = None
    if bbox:
        projwin = _bbox_xyxy_to_gdal_projwin(bbox)
        options.extend(["-projwin", *(str(value) for value in projwin)])
        if payload.get("bbox_crs"):
            options.extend(["-projwin_srs", _crs_text(payload["bbox_crs"], "bbox_crs")])
    options.extend(_creation_option_args(payload.get("creation_options", {})))

    arguments, verify_meta, execution, atomic = _write_raster(
        executor,
        "gdal_translate",
        lambda write_path, _staged: [*options, str(data_path), str(write_path)],
        output_path,
        output_format=output_format,
        progress=progress,
    )
    result = ServiceResult(
        service_id="gdal_translate",
        status="ok",
        summary="GDAL栅格转换完成。",
        outputs={"input": str(data_path), "output_path": str(output_path)},
    ).to_dict()
    result["execution"] = execution
    verification = _raster_output_verification(output_path, verify_meta, atomic)
    if bbox is not None:
        verification["requested_bbox_xyxy"] = bbox
        verification["applied_projwin_ul_lr"] = projwin
    result["verification"] = verification
    return result


def warp_raster(
    payload: dict,
    executor: GDALCommandExecutor = gdal_executor,
    *,
    progress: ProgressCallback | None = None,
) -> dict:
    input_paths = _resolve_input_paths(payload, "gdal.warp")
    primary_path = input_paths[0]
    output_path = _prepare_output(
        payload.get("output_path", ""),
        "output_path",
        inputs=input_paths,
        allow_existing=True,
    )
    crop_to_cutline = _coerce_bool(payload.get("crop_to_cutline", False), "crop_to_cutline")
    if crop_to_cutline and not payload.get("cutline_path"):
        raise ValueError("crop_to_cutline requires cutline_path")
    if (payload.get("cutline_layer") or payload.get("cutline_where")) and not payload.get(
        "cutline_path"
    ):
        raise ValueError("cutline_layer/cutline_where require cutline_path")
    cutline_layer = (
        _layer_name(payload["cutline_layer"], "cutline_layer") if payload.get("cutline_layer") else None
    )
    cutline_where = (
        _attribute_filter(payload["cutline_where"]) if payload.get("cutline_where") else None
    )

    target_crs_value = str(payload.get("target_crs") or "").strip() or None
    target_authority = (
        _normalize_authority_crs(target_crs_value) if target_crs_value else None
    )
    source_crs_record = _read_raster_crs(primary_path, executor)
    requested_bbox = _optional_bbox(payload.get("bbox"), "bbox")
    effective_bbox, effective_bbox_crs, projection_protection = (
        _web_mercator_extent_protection(
            source_crs_record,
            target_authority,
            requested_bbox,
            payload.get("bbox_crs"),
        )
        if target_authority
        else (requested_bbox, payload.get("bbox_crs"), None)
    )
    effective_resolution = payload.get("resolution")
    if effective_resolution is not None:
        effective_resolution = _resolution_pair(effective_resolution)
    if effective_resolution is None and projection_protection:
        auto_resolution = _web_mercator_resolution(
            projection_protection["applied_bbox"],
            source_crs_record.get("size"),
        )
        if auto_resolution is not None:
            effective_resolution = _resolution_pair(auto_resolution)
            projection_protection["auto_target_resolution"] = [
                effective_resolution[0],
                effective_resolution[1],
            ]

    resampling = _validate_resampling(payload.get("resampling", "near"))
    options = ["-overwrite", "-r", resampling]
    if target_crs_value:
        options.extend(["-t_srs", target_crs_value])
    if payload.get("src_nodata") is not None:
        options.extend(["-srcnodata", _number_text(payload["src_nodata"], "src_nodata")])
    if effective_resolution is not None:
        xres, yres = effective_resolution
        options.extend(["-tr", str(xres), str(yres)])
    if effective_bbox:
        options.extend(["-te", *[str(value) for value in effective_bbox]])
    if effective_bbox_crs:
        options.extend(["-te_srs", _crs_text(effective_bbox_crs, "bbox_crs")])
    if payload.get("cutline_path"):
        cutline = require_path(payload["cutline_path"], "cutline_path")
        if cutline.resolve() == output_path.resolve():
            raise ValueError("output_path must not be the same path as cutline_path")
        options.extend(["-cutline", str(cutline)])
        if cutline_layer:
            options.extend(["-cl", cutline_layer])
        if cutline_where:
            options.extend(["-cwhere", cutline_where])
    if crop_to_cutline:
        options.append("-crop_to_cutline")
    if payload.get("dst_nodata") is not None:
        options.extend(["-dstnodata", _number_text(payload["dst_nodata"], "dst_nodata")])
    if payload.get("output_type"):
        options.extend(["-ot", _choice(payload["output_type"], RASTER_DATA_TYPES, "output_type")])
    if _coerce_bool(payload.get("multithread", False), "multithread"):
        options.extend(["-multi", "-wo", "NUM_THREADS=ALL_CPUS"])
    options.extend(_creation_option_args(payload.get("creation_options", {})))

    def build(write_path: Path, staged: bool) -> list[str]:
        # Staged writes use a `.gdal-*.tmp` name, so the driver cannot be guessed from the suffix.
        driver = ["-of", "GTiff"] if staged else []
        return [*options, *driver, *[str(path) for path in input_paths], str(write_path)]

    arguments, _verify_meta, execution, atomic = _write_raster(
        executor,
        "gdalwarp",
        build,
        output_path,
        output_format="GTiff",
        progress=progress,
    )

    output_crs_record = _read_raster_crs(output_path, executor)
    warnings: list[str] = []
    projection_changed = False
    if target_authority:
        if output_crs_record["authority"] != target_authority:
            raise GDALCommandError(
                "Output CRS verification failed: "
                f"requested={target_authority}, "
                f"actual={output_crs_record['authority'] or output_crs_record['name']}"
            )
        projection_changed = (
            source_crs_record["authority"] != output_crs_record["authority"]
            if source_crs_record["authority"]
            else source_crs_record["wkt"] != output_crs_record["wkt"]
        )
        if not projection_changed:
            warnings.append(
                "The input raster was already in the requested target CRS; "
                "gdalwarp completed but the CRS did not change."
            )
    if projection_protection:
        warnings.append(projection_protection["message"])
    if len(input_paths) > 1:
        warnings.append(
            f"Mosaicked {len(input_paths)} input rasters via gdalwarp multi-source."
        )

    summary = (
        f"GDAL raster mosaic/reproject completed ({len(input_paths)} input(s))."
        if len(input_paths) > 1
        else "GDAL raster reprojection completed and the output CRS was verified."
        if target_authority
        else "GDAL raster warp completed."
    )
    result = ServiceResult(
        service_id="gdal_warp",
        status="ok",
        summary=summary,
        outputs={"output_path": str(output_path)},
        warnings=warnings,
    ).to_dict()
    result["inputs"] = {
        "data_path": str(primary_path),
        "data_paths": [str(path) for path in input_paths],
        "input_count": len(input_paths),
    }
    execution["protections"] = [projection_protection] if projection_protection else []
    result["execution"] = execution
    verification: dict[str, Any] = {
        "verified": True,
        "output_exists": True,
        "output_size_bytes": output_path.stat().st_size,
        "requested_crs": {"authority": target_authority} if target_authority else None,
        "source_crs": _public_crs_record(source_crs_record),
        "output_crs": _public_crs_record(output_crs_record),
        "source_grid": _grid_record(source_crs_record),
        "output_grid": _grid_record(output_crs_record),
        "projection_changed": projection_changed,
        "mosaic": len(input_paths) > 1,
        "applied_extent": {
            "bbox": effective_bbox,
            "bbox_crs": effective_bbox_crs,
        }
        if effective_bbox
        else None,
    }
    if atomic:
        verification["commit"] = "atomic_geotiff_replace"
    result["verification"] = verification
    return result


def build_vrt(
    payload: dict,
    executor: GDALCommandExecutor = gdal_executor,
    *,
    progress: ProgressCallback | None = None,
) -> dict:
    """Wrap `gdalbuildvrt`: virtual mosaic / band stack without copying pixels."""
    input_paths = _resolve_input_paths(payload, "gdal.build_vrt")
    output_path = _prepare_output(
        payload.get("output_path", ""),
        "output_path",
        inputs=input_paths,
        allow_existing=True,
    )
    if output_path.suffix.lower() != ".vrt":
        raise ValueError("output_path for gdal.build_vrt must end with .vrt")
    options: list[str] = []
    if payload.get("target_resolution") is not None:
        xres, yres = _resolution_pair(payload["target_resolution"])
        options.extend(["-tr", str(xres), str(yres)])
    elif payload.get("resolution"):
        options.extend(
            ["-resolution", _choice(payload["resolution"], VRT_RESOLUTION_STRATEGIES, "resolution")]
        )
    if _coerce_bool(payload.get("separate", False), "separate"):
        options.append("-separate")
    bbox = _optional_bbox(payload.get("bbox"), "bbox")
    if bbox:
        options.extend(["-te", *[str(value) for value in bbox]])
    if payload.get("resampling"):
        options.extend(["-r", _choice(payload["resampling"], VRT_RESAMPLING, "resampling")])
    for band in _validated_band_list(payload.get("bands", [])):
        options.extend(["-b", str(band)])
    if payload.get("src_nodata") is not None:
        options.extend(["-srcnodata", _number_text(payload["src_nodata"], "src_nodata")])
    if payload.get("vrt_nodata") is not None:
        options.extend(["-vrtnodata", _number_text(payload["vrt_nodata"], "vrt_nodata")])
    if _coerce_bool(payload.get("add_alpha", False), "add_alpha"):
        options.append("-addalpha")

    staged_path = _staged_sibling_path(output_path, keep_suffix=True)
    arguments = [*options, str(staged_path), *[str(path) for path in input_paths]]
    started_at = datetime.now(timezone.utc)
    started_clock = perf_counter()
    try:
        _ensure_output_parent(output_path)
        executor.execute("gdalbuildvrt", arguments, progress=progress)
        finished_at = datetime.now(timezone.utc)
        verify_meta = _verify_raster_output(staged_path, executor)
        _commit_staged(staged_path, output_path)
    except BaseException:
        _cleanup_staged(staged_path)
        raise
    result = ServiceResult(
        service_id="gdal_build_vrt",
        status="ok",
        summary=f"GDAL虚拟栅格(VRT)构建完成，共引用 {len(input_paths)} 个输入。",
        outputs={"output_path": str(output_path)},
        warnings=[
            "VRT 只记录对源文件的引用；移动或删除源文件会使 VRT 失效。"
        ],
    ).to_dict()
    result["inputs"] = {
        "data_paths": [str(path) for path in input_paths],
        "input_count": len(input_paths),
    }
    result["execution"] = _execution_record(
        "gdalbuildvrt", arguments, started_at, finished_at, _elapsed_ms(started_clock)
    )
    result["verification"] = _raster_output_verification(output_path, verify_meta, True)
    return result


def raster_calc(
    payload: dict,
    executor: GDALCommandExecutor = gdal_executor,
    *,
    progress: ProgressCallback | None = None,
) -> dict:
    """Wrap `gdal_calc`: band math over one or more rasters (NDVI, masks, thresholds)."""
    inputs = payload.get("inputs")
    if not isinstance(inputs, dict) or not inputs:
        raise ValueError("inputs must be a non-empty mapping such as {\"A\": \"/data/a.tif\"}")
    input_bands = payload.get("input_bands") or {}
    if not isinstance(input_bands, dict):
        raise ValueError("input_bands must be a mapping such as {\"A\": 3}")
    resolved_inputs: dict[str, Path] = {}
    for name, path_value in inputs.items():
        key = str(name).strip()
        if not CALC_INPUT_NAME.match(key):
            raise ValueError(f"input name must be a single uppercase letter A-Z: {name!r}")
        resolved_inputs[key] = require_path(str(path_value), f"inputs.{key}")
    for name in input_bands:
        if str(name) not in resolved_inputs:
            raise ValueError(f"input_bands refers to unknown input {name!r}")
    output_path = _prepare_output(
        payload.get("output_path", ""),
        "output_path",
        inputs=resolved_inputs.values(),
        allow_existing=True,
    )
    output_type = _choice(payload.get("output_type", "Float32"), RASTER_DATA_TYPES, "output_type")
    cast_default = output_type.startswith("Float")
    cast_inputs = _coerce_bool(
        payload.get("cast_inputs_to_float", cast_default), "cast_inputs_to_float"
    )
    expression = validate_calc_expression(
        str(payload.get("expression") or ""),
        set(resolved_inputs),
        cast_inputs_to_float=cast_inputs,
    )

    options: list[str] = []
    for name in sorted(resolved_inputs):
        options.extend([f"-{name}", str(resolved_inputs[name])])
        band = input_bands.get(name)
        if band is not None:
            options.append(f"--{name}_band={_positive_int(band, f'input_bands.{name}')}")
    options.extend(
        [
            f"--calc={expression}",
            "--format=GTiff",
            f"--type={output_type}",
        ]
    )
    if payload.get("no_data_value") is not None:
        options.append(f"--NoDataValue={_number_text(payload['no_data_value'], 'no_data_value')}")
    if payload.get("extent"):
        options.append(
            f"--extent={_choice(payload['extent'], {'ignore', 'fail', 'union', 'intersect'}, 'extent')}"
        )
    for co in _creation_option_args(payload.get("creation_options", {}))[1::2]:
        options.append(f"--co={co}")

    arguments, verify_meta, execution, atomic = _write_raster(
        executor,
        "gdal_calc",
        lambda write_path, _staged: [*options, f"--outfile={write_path}"],
        output_path,
        output_format="GTiff",
        progress=progress,
    )
    result = ServiceResult(
        service_id="gdal_raster_calc",
        status="ok",
        summary="GDAL栅格计算(gdal_calc)完成。",
        outputs={
            "output_path": str(output_path),
            "expression": expression,
            "output_type": output_type,
        },
    ).to_dict()
    result["inputs"] = {
        name: {"path": str(path), "band": int(input_bands.get(name) or 1)}
        for name, path in sorted(resolved_inputs.items())
    }
    result["execution"] = execution
    result["verification"] = _raster_output_verification(output_path, verify_meta, atomic)
    return result


def raster_overviews(
    payload: dict,
    executor: GDALCommandExecutor = gdal_executor,
    *,
    progress: ProgressCallback | None = None,
) -> dict:
    """Wrap `gdaladdo`: build (or clean) overview pyramids.

    Internal overviews modify the input file in place; use external=true to write a
    sidecar `.ovr` and leave the source untouched.
    """
    data_path = require_path(payload.get("data_path", ""))
    clean = _coerce_bool(payload.get("clean", False), "clean")
    external = _coerce_bool(payload.get("external", False), "external")
    levels = _overview_levels(payload.get("levels"))
    resampling = _choice(
        payload.get("resampling", "average"), OVERVIEW_RESAMPLING, "resampling"
    )
    arguments: list[str] = []
    if payload.get("compress"):
        compress = _choice(str(payload["compress"]).upper(), OVERVIEW_COMPRESSION, "compress")
        arguments.extend(["--config", "COMPRESS_OVERVIEW", compress])
    if clean:
        arguments.append("-clean")
    else:
        arguments.extend(["-r", resampling])
    if external:
        arguments.append("-ro")
    arguments.append(str(data_path))
    if not clean:
        arguments.extend(str(level) for level in levels)

    started_at = datetime.now(timezone.utc)
    started_clock = perf_counter()
    executor.execute("gdaladdo", arguments, progress=progress)
    finished_at = datetime.now(timezone.utc)
    metadata = executor.execute(
        "gdalinfo", ["-json", str(data_path)], expect_json=True, timeout=METADATA_TIMEOUT_SECONDS
    )
    bands = metadata.get("bands") if isinstance(metadata.get("bands"), list) else []
    overview_count = len((bands[0] or {}).get("overviews") or []) if bands else 0
    if not clean and overview_count == 0:
        raise GDALCommandError(f"gdaladdo completed but no overviews are visible: {data_path}")
    if clean and overview_count:
        raise GDALCommandError(f"gdaladdo -clean completed but overviews remain: {data_path}")
    sidecar = Path(f"{data_path}.ovr")
    result = ServiceResult(
        service_id="gdal_raster_overviews",
        status="ok",
        summary="GDAL金字塔已清除。" if clean else f"GDAL金字塔构建完成（{overview_count} 级）。",
        outputs={
            "data_path": str(data_path),
            "overview_file": str(sidecar) if external and sidecar.exists() else None,
            "levels": [] if clean else levels,
            "resampling": None if clean else resampling,
        },
        warnings=[]
        if external or clean
        else ["内部金字塔直接写入输入文件；如需保持源文件不变请使用 external=true。"],
    ).to_dict()
    result["execution"] = _execution_record(
        "gdaladdo", arguments, started_at, finished_at, _elapsed_ms(started_clock)
    )
    result["verification"] = {
        "verified": True,
        "overview_count": overview_count,
        "overview_sizes": [
            overview.get("size") for overview in ((bands[0] or {}).get("overviews") or [])
        ]
        if bands
        else [],
    }
    return result


# --------------------------------------------------------------------------- raster → vector


def raster_polygonize(
    payload: dict,
    executor: GDALCommandExecutor = gdal_executor,
    *,
    progress: ProgressCallback | None = None,
) -> dict:
    """Wrap `gdal_polygonize`: turn connected pixel regions into polygons."""
    data_path = require_path(payload.get("data_path", ""))
    overwrite = _coerce_bool(payload.get("overwrite", False), "overwrite")
    output_path = _prepare_output(
        payload.get("output_path", ""),
        "output_path",
        inputs=[data_path],
        allow_existing=overwrite,
    )
    output_format = _vector_output_format(output_path, payload.get("output_format"))
    band = _positive_int(payload.get("band", 1), "band")
    layer_name = _layer_name(payload.get("layer_name") or "polygons", "layer_name")
    field_name = _layer_name(payload.get("field_name") or "value", "field_name")
    connectedness = int(payload.get("connectedness", 4))
    if connectedness not in (4, 8):
        raise ValueError("connectedness must be 4 or 8")
    options: list[str] = []
    if connectedness == 8:
        options.append("-8")
    if payload.get("mask_path"):
        mask = require_path(payload["mask_path"], "mask_path")
        options.extend(["-mask", str(mask)])
    elif not _coerce_bool(payload.get("use_mask", True), "use_mask"):
        options.append("-nomask")
    options.extend(["-b", str(band), "-f", output_format])

    arguments, feature_count, execution = _write_vector(
        executor,
        "gdal_polygonize",
        lambda write_path: [*options, str(data_path), str(write_path), layer_name, field_name],
        output_path,
        progress=progress,
    )
    result = ServiceResult(
        service_id="gdal_raster_polygonize",
        status="ok",
        summary=f"GDAL栅格转矢量完成，生成 {feature_count} 个多边形。",
        outputs={
            "input": str(data_path),
            "output_path": str(output_path),
            "layer_name": layer_name,
            "field_name": field_name,
        },
        warnings=[]
        if feature_count
        else ["输出图层没有要素：请检查波段取值、nodata 或掩膜设置。"],
    ).to_dict()
    result["execution"] = execution
    result["verification"] = _vector_output_verification(output_path, feature_count)
    return result


# --------------------------------------------------------------------------- DEM


def dem_slope(
    payload: dict,
    executor: GDALCommandExecutor = gdal_executor,
    *,
    progress: ProgressCallback | None = None,
) -> dict:
    """Wrap `gdaldem slope` for DEM slope generation."""
    percent = _coerce_bool(payload.get("percent", False), "percent")
    mode_args: list[str] = []
    if percent:
        mode_args.append("-p")
    if payload.get("scale") is not None:
        mode_args.extend(["-s", str(_positive_float(payload["scale"], "scale"))])
    mode_args.extend(_dem_algorithm_args(payload))
    return _run_gdaldem(
        "slope",
        payload,
        mode_args,
        executor,
        progress,
        service_id="gdal_dem_slope",
        summary="GDAL DEM 坡度计算完成。",
        extra_outputs={"percent": percent, "scale": payload.get("scale")},
    )


def dem_aspect(
    payload: dict,
    executor: GDALCommandExecutor = gdal_executor,
    *,
    progress: ProgressCallback | None = None,
) -> dict:
    """Wrap `gdaldem aspect` (0°=north, clockwise; flat areas → -9999 unless zero_for_flat)."""
    trigonometric = _coerce_bool(payload.get("trigonometric", False), "trigonometric")
    zero_for_flat = _coerce_bool(payload.get("zero_for_flat", False), "zero_for_flat")
    mode_args: list[str] = []
    if trigonometric:
        mode_args.append("-trigonometric")
    if zero_for_flat:
        mode_args.append("-zero_for_flat")
    mode_args.extend(_dem_algorithm_args(payload))
    return _run_gdaldem(
        "aspect",
        payload,
        mode_args,
        executor,
        progress,
        service_id="gdal_dem_aspect",
        summary="GDAL DEM 坡向计算完成。",
        extra_outputs={"trigonometric": trigonometric, "zero_for_flat": zero_for_flat},
    )


def dem_hillshade(
    payload: dict,
    executor: GDALCommandExecutor = gdal_executor,
    *,
    progress: ProgressCallback | None = None,
) -> dict:
    """Wrap `gdaldem hillshade` (Byte output, 0 = nodata)."""
    shading = _choice(payload.get("shading", "default"), HILLSHADE_MODES, "shading")
    mode_args: list[str] = []
    z_factor = payload.get("z_factor")
    if z_factor is not None:
        mode_args.extend(["-z", str(_positive_float(z_factor, "z_factor"))])
    if payload.get("scale") is not None:
        mode_args.extend(["-s", str(_positive_float(payload["scale"], "scale"))])
    azimuth = payload.get("azimuth")
    if azimuth is not None:
        if shading == "multidirectional":
            raise ValueError("azimuth cannot be combined with shading='multidirectional'")
        azimuth_value = _finite_float(azimuth, "azimuth")
        if not 0 <= azimuth_value <= 360:
            raise ValueError("azimuth must be within [0, 360]")
        mode_args.extend(["-az", str(azimuth_value)])
    altitude = payload.get("altitude")
    if altitude is not None:
        altitude_value = _finite_float(altitude, "altitude")
        if not 0 <= altitude_value <= 90:
            raise ValueError("altitude must be within [0, 90]")
        mode_args.extend(["-alt", str(altitude_value)])
    if shading != "default":
        mode_args.append(f"-{shading}")
    mode_args.extend(_dem_algorithm_args(payload))
    return _run_gdaldem(
        "hillshade",
        payload,
        mode_args,
        executor,
        progress,
        service_id="gdal_dem_hillshade",
        summary="GDAL DEM 山体阴影计算完成。",
        extra_outputs={
            "shading": shading,
            "azimuth": azimuth,
            "altitude": altitude,
            "z_factor": z_factor,
        },
    )


def dem_terrain_index(
    payload: dict,
    executor: GDALCommandExecutor = gdal_executor,
    *,
    progress: ProgressCallback | None = None,
) -> dict:
    """Wrap `gdaldem TRI|TPI|roughness` terrain ruggedness indices."""
    metric_key = str(payload.get("metric", "TRI")).strip().upper()
    if metric_key not in TERRAIN_INDEX_METRICS:
        raise ValueError("metric must be one of: TRI, TPI, roughness")
    mode = TERRAIN_INDEX_METRICS[metric_key]
    mode_args: list[str] = []
    if payload.get("tri_algorithm"):
        if mode != "TRI":
            raise ValueError("tri_algorithm only applies to metric='TRI'")
        mode_args.extend(["-alg", _choice(payload["tri_algorithm"], TRI_ALGORITHMS, "tri_algorithm")])
    return _run_gdaldem(
        mode,
        payload,
        mode_args,
        executor,
        progress,
        service_id="gdal_dem_terrain_index",
        summary=f"GDAL DEM 地形指数({mode})计算完成。",
        extra_outputs={"metric": mode},
    )


def dem_contour(
    payload: dict,
    executor: GDALCommandExecutor = gdal_executor,
    *,
    progress: ProgressCallback | None = None,
) -> dict:
    """Wrap `gdal_contour`: contour lines (or filled contour polygons) from a DEM."""
    data_path = require_path(payload.get("data_path", ""))
    overwrite = _coerce_bool(payload.get("overwrite", False), "overwrite")
    output_path = _prepare_output(
        payload.get("output_path", ""),
        "output_path",
        inputs=[data_path],
        allow_existing=overwrite,
    )
    output_format = _vector_output_format(output_path, payload.get("output_format"))
    interval = payload.get("interval")
    fixed_levels = payload.get("fixed_levels")
    if (interval is None) == (not fixed_levels):
        raise ValueError("Provide exactly one of interval or fixed_levels")
    polygons = _coerce_bool(payload.get("polygons", False), "polygons")
    attribute = _layer_name(payload.get("attribute_name") or "elev", "attribute_name")
    layer_name = _layer_name(payload.get("layer_name") or "contour", "layer_name")
    options = ["-b", str(_positive_int(payload.get("band", 1), "band"))]
    if polygons:
        options.extend(["-p", "-amin", f"{attribute}_min", "-amax", f"{attribute}_max"])
    else:
        options.extend(["-a", attribute])
    if interval is not None:
        options.extend(["-i", str(_positive_float(interval, "interval"))])
        if payload.get("offset") is not None:
            options.extend(["-off", str(_finite_float(payload["offset"], "offset"))])
    else:
        if not isinstance(fixed_levels, list):
            raise ValueError("fixed_levels must be a list of numbers")
        levels = sorted({_finite_float(value, "fixed_levels") for value in fixed_levels})
        options.extend(["-fl", *[str(value) for value in levels]])
    if payload.get("src_nodata") is not None:
        options.extend(["-snodata", _number_text(payload["src_nodata"], "src_nodata")])
    options.extend(["-f", output_format, "-nln", layer_name])

    arguments, feature_count, execution = _write_vector(
        executor,
        "gdal_contour",
        lambda write_path: [*options, str(data_path), str(write_path)],
        output_path,
        progress=progress,
    )
    result = ServiceResult(
        service_id="gdal_dem_contour",
        status="ok",
        summary=f"GDAL等值线提取完成，生成 {feature_count} 个要素。",
        outputs={
            "input": str(data_path),
            "output_path": str(output_path),
            "layer_name": layer_name,
            "polygons": polygons,
        },
        warnings=[]
        if feature_count
        else ["输出图层没有要素：请检查等值距是否超出 DEM 取值范围。"],
    ).to_dict()
    result["execution"] = execution
    result["verification"] = _vector_output_verification(output_path, feature_count)
    return result


# --------------------------------------------------------------------------- vector


def convert_vector(
    payload: dict,
    executor: GDALCommandExecutor = gdal_executor,
    *,
    progress: ProgressCallback | None = None,
) -> dict:
    data_path = require_path(payload.get("data_path", ""))
    overwrite = _coerce_bool(payload.get("overwrite", False), "overwrite")
    output_path = _prepare_output(
        payload.get("output_path", ""),
        "output_path",
        inputs=[data_path],
        allow_existing=overwrite,
    )
    output_format = _choice(
        payload.get("output_format", "GPKG"), VECTOR_CONVERT_FORMATS, "output_format"
    )
    arguments = ["-f", output_format]
    if overwrite:
        arguments.append("-overwrite")
    if progress is not None:
        arguments.append("-progress")
    if payload.get("target_crs"):
        arguments.extend(["-t_srs", _crs_text(payload["target_crs"], "target_crs")])
    bbox = _optional_bbox(payload.get("bbox"), "bbox")
    if bbox:
        arguments.extend(["-spat", *[str(value) for value in bbox]])
    if payload.get("where"):
        arguments.extend(["-where", _attribute_filter(payload["where"])])
    arguments.extend([str(output_path), str(data_path)])
    if payload.get("layer"):
        arguments.append(str(payload["layer"]))
    started_at = datetime.now(timezone.utc)
    started_clock = perf_counter()
    _ensure_output_parent(output_path)
    executor.execute("ogr2ogr", arguments, progress=progress)
    finished_at = datetime.now(timezone.utc)
    feature_count = _verify_vector_output(output_path, executor)
    result = ServiceResult(
        service_id="ogr_convert",
        status="ok",
        summary="OGR矢量格式转换完成。",
        outputs={"input": str(data_path), "output_path": str(output_path)},
    ).to_dict()
    result["execution"] = _execution_record(
        "ogr2ogr", arguments, started_at, finished_at, _elapsed_ms(started_clock)
    )
    result["verification"] = _vector_output_verification(output_path, feature_count)
    return result


# --------------------------------------------------------------------------- band math safety


def validate_calc_expression(
    expression: str,
    input_names: set[str],
    *,
    cast_inputs_to_float: bool = False,
) -> str:
    """Validate a gdal_calc expression against a numpy-only whitelist.

    gdal_calc evaluates ``--calc`` with Python ``eval``; only arithmetic, comparisons,
    the declared input letters and whitelisted numpy functions are accepted.
    """
    text = expression.strip()
    if not text:
        raise ValueError("expression is required")
    if len(text) > CALC_MAX_EXPRESSION_LENGTH:
        raise ValueError(f"expression must be at most {CALC_MAX_EXPRESSION_LENGTH} characters")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"expression is not valid: {exc.msg}") from exc
    used: set[str] = set()
    _check_calc_node(tree.body, input_names, used)
    if not used:
        raise ValueError("expression must reference at least one input (e.g. A)")
    if cast_inputs_to_float:
        tree = ast.fix_missing_locations(_FloatCast(input_names).visit(tree))
    return ast.unparse(tree)


class _FloatCast(ast.NodeTransformer):
    def __init__(self, input_names: set[str]):
        self.input_names = input_names

    def visit_Name(self, node: ast.Name) -> ast.AST:
        if node.id in self.input_names:
            return ast.Call(
                func=ast.Name(id="float64", ctx=ast.Load()), args=[node], keywords=[]
            )
        return node

    def visit_Call(self, node: ast.Call) -> ast.AST:
        node.args = [self.visit(arg) for arg in node.args]
        return node


def _is_constant_subtree(node: ast.AST) -> bool:
    return not any(isinstance(child, ast.Name) for child in ast.walk(node))


def _check_calc_node(node: ast.AST, input_names: set[str], used: set[str]) -> None:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError("expression constants must be numbers")
        return
    if isinstance(node, ast.Name):
        if node.id not in input_names:
            raise ValueError(
                f"unknown name {node.id!r}; use declared inputs {sorted(input_names)} "
                "or call a whitelisted numpy function"
            )
        used.add(node.id)
        return
    if isinstance(node, ast.BinOp):
        if not isinstance(node.op, _CALC_BINARY_OPERATORS):
            raise ValueError(f"operator {type(node.op).__name__} is not allowed")
        if isinstance(node.op, ast.Pow) and _is_constant_subtree(node.right):
            exponent = node.right
            if not (
                isinstance(exponent, ast.Constant)
                and isinstance(exponent.value, (int, float))
                and abs(exponent.value) <= 64
            ):
                raise ValueError("constant exponents must be plain numbers with |x| <= 64")
        _check_calc_node(node.left, input_names, used)
        _check_calc_node(node.right, input_names, used)
        return
    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, _CALC_UNARY_OPERATORS):
            raise ValueError(f"operator {type(node.op).__name__} is not allowed")
        _check_calc_node(node.operand, input_names, used)
        return
    if isinstance(node, ast.Compare):
        if not all(isinstance(op, _CALC_COMPARE_OPERATORS) for op in node.ops):
            raise ValueError("only ==, !=, <, <=, >, >= comparisons are allowed")
        _check_calc_node(node.left, input_names, used)
        for comparator in node.comparators:
            _check_calc_node(comparator, input_names, used)
        return
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in CALC_FUNCTIONS:
            name = node.func.id if isinstance(node.func, ast.Name) else ast.unparse(node.func)
            raise ValueError(
                f"function {name!r} is not allowed; allowed: {sorted(CALC_FUNCTIONS)}"
            )
        if node.keywords:
            raise ValueError("keyword arguments are not allowed in expressions")
        if not node.args:
            raise ValueError(f"function {node.func.id!r} needs at least one argument")
        for arg in node.args:
            _check_calc_node(arg, input_names, used)
        return
    raise ValueError(f"expression element {type(node).__name__} is not allowed")


# --------------------------------------------------------------------------- write helpers


def _write_raster(
    executor: GDALCommandExecutor,
    command: str,
    build_arguments: Callable[[Path, bool], list[str]],
    output_path: Path,
    *,
    output_format: str | None,
    progress: ProgressCallback | None,
    check: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[list[str], dict[str, Any], dict[str, Any], bool]:
    """Run a raster-producing command with write → verify → atomic replace for GeoTIFF.

    ``check`` receives the output gdalinfo metadata and may raise to reject the result
    before it replaces the final path.
    """
    write_path, staged_path = _resolve_geotiff_write_path(output_path, output_format)
    arguments = build_arguments(write_path, staged_path is not None)
    started_at = datetime.now(timezone.utc)
    started_clock = perf_counter()
    committed = False
    try:
        _ensure_output_parent(output_path)
        executor.execute(command, arguments, progress=progress)
        finished_at = datetime.now(timezone.utc)
        duration_ms = _elapsed_ms(started_clock)
        verify_meta = _verify_raster_output(write_path, executor)
        if check is not None:
            check(verify_meta)
        if staged_path is not None:
            _commit_staged_geotiff(staged_path, output_path)
            committed = True
    except BaseException:
        if staged_path is not None and not committed:
            _cleanup_staged_geotiff(staged_path)
        raise
    execution = _execution_record(command, arguments, started_at, finished_at, duration_ms)
    return arguments, verify_meta, execution, staged_path is not None


def _write_vector(
    executor: GDALCommandExecutor,
    command: str,
    build_arguments: Callable[[Path], list[str]],
    output_path: Path,
    *,
    progress: ProgressCallback | None,
) -> tuple[list[str], int | None, dict[str, Any]]:
    """Write a single-file vector to a staged sibling, verify it, then replace atomically."""
    staged_path = _staged_sibling_path(output_path, keep_suffix=True)
    arguments = build_arguments(staged_path)
    started_at = datetime.now(timezone.utc)
    started_clock = perf_counter()
    try:
        _ensure_output_parent(output_path)
        executor.execute(command, arguments, progress=progress)
        finished_at = datetime.now(timezone.utc)
        duration_ms = _elapsed_ms(started_clock)
        feature_count = _verify_vector_output(staged_path, executor)
        _commit_staged(staged_path, output_path)
    except BaseException:
        _cleanup_staged(staged_path)
        raise
    execution = _execution_record(command, arguments, started_at, finished_at, duration_ms)
    return arguments, feature_count, execution


def _run_gdaldem(
    mode: str,
    payload: dict,
    mode_args: list[str],
    executor: GDALCommandExecutor,
    progress: ProgressCallback | None,
    *,
    service_id: str,
    summary: str,
    extra_outputs: dict[str, Any],
) -> dict:
    data_path = require_path(payload.get("data_path", ""))
    output_path = _prepare_output(
        payload.get("output_path", ""),
        "output_path",
        inputs=[data_path],
        allow_existing=True,
    )
    band = _positive_int(payload.get("band") or 1, "band")
    source = executor.execute(
        "gdalinfo", ["-json", str(data_path)], expect_json=True, timeout=METADATA_TIMEOUT_SECONDS
    )
    source_bands = source.get("bands") if isinstance(source.get("bands"), list) else []
    if source_bands and band > len(source_bands):
        raise ValueError(f"band {band} exceeds the {len(source_bands)} band(s) of {data_path}")
    options = [mode, *mode_args]
    if _coerce_bool(payload.get("compute_edges", False), "compute_edges"):
        options.append("-compute_edges")
    if band != 1:
        options.extend(["-b", str(band)])
    options.extend(_creation_option_args(payload.get("creation_options", {})))

    def build(write_path: Path, staged: bool) -> list[str]:
        driver = ["-of", "GTiff"] if staged else []
        return [*options, *driver, str(data_path), str(write_path)]

    preservation: dict[str, bool] = {}

    def check(output_meta: dict[str, Any]) -> None:
        preservation.update(_grid_preservation(source, output_meta))
        if not all(preservation.values()):
            raise GDALCommandError(
                f"gdaldem output does not keep the input CRS/grid ({preservation}): {output_path}"
            )

    arguments, verify_meta, execution, atomic = _write_raster(
        executor,
        "gdaldem",
        build,
        output_path,
        output_format="GTiff",
        progress=progress,
        check=check,
    )
    result = ServiceResult(
        service_id=service_id,
        status="ok",
        summary=summary,
        outputs={
            "input": str(data_path),
            "output_path": str(output_path),
            "band": band,
            **extra_outputs,
        },
        warnings=_dem_scale_warnings(mode, payload, source),
    ).to_dict()
    result["execution"] = execution
    result["verification"] = {
        **_raster_output_verification(output_path, verify_meta, atomic),
        **preservation,
    }
    return result


_SCALE_SENSITIVE_DEM_MODES = {"slope", "hillshade"}


def _dem_scale_warnings(mode: str, payload: dict, source: dict[str, Any]) -> list[str]:
    if mode not in _SCALE_SENSITIVE_DEM_MODES or payload.get("scale") is not None:
        return []
    wkt = str((source.get("coordinateSystem") or {}).get("wkt") or "").lstrip()
    if not wkt.startswith(("GEOGCRS[", "GEOGCS[")):
        return []
    return [
        "输入 DEM 为经纬度坐标系，而高程单位通常为米；未设置 scale 时结果会严重失真。"
        "建议设置 scale=111120，或先用 raster_warp 投影到米制坐标系。"
    ]


def _grid_preservation(source: dict[str, Any], output: dict[str, Any]) -> dict[str, bool]:
    source_wkt = (source.get("coordinateSystem") or {}).get("wkt") or None
    output_wkt = (output.get("coordinateSystem") or {}).get("wkt") or None
    source_transform = source.get("geoTransform")
    output_transform = output.get("geoTransform")
    transform_equal = (source_transform is None and output_transform is None) or (
        isinstance(source_transform, list)
        and isinstance(output_transform, list)
        and len(source_transform) == len(output_transform)
        and all(
            math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)
            for a, b in zip(source_transform, output_transform)
        )
    )
    return {
        "crs_preserved": source_wkt == output_wkt,
        "grid_preserved": source.get("size") == output.get("size") and transform_equal,
    }


def _dem_algorithm_args(payload: dict) -> list[str]:
    if not payload.get("algorithm"):
        return []
    return ["-alg", _choice(payload["algorithm"], DEM_ALGORITHMS, "algorithm")]


def _resolve_input_paths(payload: dict, tool_id: str) -> list[Path]:
    paths: list[Path] = []
    seen: set[str] = set()
    candidates: list[str] = []
    if payload.get("data_path"):
        candidates.append(str(payload["data_path"]))
    for item in payload.get("data_paths") or []:
        candidates.append(str(item))
    if not candidates:
        raise ValueError(f"Provide data_path and/or data_paths for {tool_id}")
    for candidate in candidates:
        path = require_path(candidate, "data_path")
        key = str(path.resolve()) if path.exists() else str(path)
        if key in seen:
            continue
        seen.add(key)
        paths.append(path)
    return paths


def _prepare_output(
    value: str,
    field_name: str,
    *,
    inputs: Iterable[Path] | None = None,
    allow_existing: bool = True,
) -> Path:
    if not value or not str(value).strip():
        raise ValueError(f"{field_name} is required")
    output_path = Path(value).expanduser().resolve()
    input_paths = {Path(p).expanduser().resolve() for p in (inputs or [])}
    if output_path in input_paths:
        raise ValueError(f"{field_name} must not resolve to the same path as an input")
    if output_path.exists() and not allow_existing:
        raise FileExistsError(
            f"Output already exists; set overwrite=true to replace it: {output_path}"
        )
    return output_path


def _ensure_output_parent(output_path: Path) -> None:
    """Create parent directories immediately before a write (after validation)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)


def _uses_atomic_geotiff_commit(output_path: Path, output_format: str | None) -> bool:
    """Single-file GeoTIFF/COG outputs use write-verify-replace in the destination directory."""
    if output_format and output_format in GEOTIFF_OUTPUT_FORMATS:
        return True
    return output_path.suffix.lower() in GEOTIFF_FILE_SUFFIXES


def _staged_geotiff_path(final_path: Path) -> Path:
    token = uuid.uuid4().hex
    return final_path.parent / f".{final_path.name}.gdal-{token}.tmp"


def _staged_sibling_path(final_path: Path, *, keep_suffix: bool) -> Path:
    token = uuid.uuid4().hex
    suffix = final_path.suffix if keep_suffix else ".tmp"
    return final_path.parent / f".{final_path.stem}.gdal-{token}{suffix}"


def _resolve_geotiff_write_path(
    final_path: Path,
    output_format: str | None,
) -> tuple[Path, Path | None]:
    if _uses_atomic_geotiff_commit(final_path, output_format):
        staged = _staged_geotiff_path(final_path)
        return staged, staged
    return final_path, None


def _cleanup_staged_geotiff(staged_path: Path) -> None:
    """Remove staged raster and GDAL sidecars after a failed commit."""
    _cleanup_staged(staged_path)


def _cleanup_staged(staged_path: Path) -> None:
    if staged_path.is_file():
        staged_path.unlink(missing_ok=True)
    base = str(staged_path)
    for suffix in (*_STAGED_SIDECAR_SUFFIXES, "-wal", "-shm", "-journal"):
        sidecar = Path(base + suffix)
        if sidecar.is_file():
            sidecar.unlink(missing_ok=True)


def _commit_staged_geotiff(staged_path: Path, final_path: Path) -> None:
    """Atomically publish a verified GeoTIFF (same directory / filesystem).

    Concurrent writers targeting the same final_path can still race on replace;
    use allow_existing=False or external locking when exclusive creation is required.
    """
    _commit_staged(staged_path, final_path)


def _commit_staged(staged_path: Path, final_path: Path) -> None:
    if not staged_path.is_file() or staged_path.stat().st_size <= 0:
        raise GDALCommandError(
            f"Cannot commit empty staged output: {staged_path}"
        )
    _ensure_output_parent(final_path)
    os.replace(staged_path, final_path)
    base = str(staged_path)
    final_base = str(final_path)
    for suffix in _STAGED_SIDECAR_SUFFIXES:
        staged_side = Path(base + suffix)
        if staged_side.is_file():
            os.replace(staged_side, Path(final_base + suffix))


# --------------------------------------------------------------------------- validation


def _coerce_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "1", "yes"):
            return True
        if normalized in ("false", "0", "no"):
            return False
    raise ValueError(f"{field} must be a boolean (or 'true'/'false' string)")


def _choice(value: Any, allowed: set[str], field: str) -> str:
    text = str(value).strip()
    if text in allowed:
        return text
    lowered = {item.lower(): item for item in allowed}
    if text.lower() in lowered:
        return lowered[text.lower()]
    raise ValueError(f"Unsupported {field} {value!r}; allowed: {sorted(allowed)}")


def _finite_float(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    return number


def _positive_float(value: Any, field: str) -> float:
    number = _finite_float(value, field)
    if number <= 0:
        raise ValueError(f"{field} must be a finite positive number")
    return number


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a positive integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a positive integer") from exc
    if number != float(value) or number < 1:
        raise ValueError(f"{field} must be a positive integer")
    return number


def _number_text(value: Any, field: str) -> str:
    if isinstance(value, str) and value.strip().lower() in ("nan", "none"):
        return value.strip().lower()
    number = _finite_float(value, field)
    return str(int(number)) if number.is_integer() else repr(number)


def _crs_text(value: Any, field: str) -> str:
    text = str(value).strip()
    if not text or text.startswith("-") or len(text) > 2000:
        raise ValueError(f"{field} must be a CRS such as EPSG:4326")
    return text


def _layer_name(value: Any, field: str) -> str:
    text = str(value).strip()
    if not _LAYER_NAME.match(text):
        raise ValueError(
            f"{field} must start with a letter/underscore and contain only letters, digits, _"
        )
    return text


def _attribute_filter(value: Any) -> str:
    text = str(value).strip()
    if (
        not text
        or len(text) > 1000
        or text.startswith("-")
        or ";" in text
        or "--" in text
        or "/*" in text
        or any(ord(char) < 32 for char in text)
    ):
        raise ValueError("attribute filter must be a single-line SQL WHERE expression")
    return text


def _overview_levels(value: Any) -> list[int]:
    if value is None:
        return [2, 4, 8, 16]
    if not isinstance(value, list) or not value:
        raise ValueError("levels must be a non-empty list of integers >= 2")
    levels: list[int] = []
    for level in value:
        number = _positive_int(level, "levels")
        if number < 2:
            raise ValueError("levels must be integers >= 2")
        levels.append(number)
    return sorted(set(levels))


def _vector_output_format(output_path: Path, requested: Any) -> str:
    suffix = output_path.suffix.lower()
    if requested:
        output_format = _choice(requested, set(VECTOR_OUTPUT_FORMATS), "output_format")
        if suffix not in VECTOR_OUTPUT_FORMATS[output_format]:
            raise ValueError(
                f"output_path suffix {suffix!r} does not match output_format {output_format} "
                f"(expected {VECTOR_OUTPUT_FORMATS[output_format]})"
            )
        return output_format
    for output_format, suffixes in VECTOR_OUTPUT_FORMATS.items():
        if suffix in suffixes:
            return output_format
    raise ValueError(
        "output_path must end with .gpkg, .geojson/.json or .fgb "
        "(single-file vector formats are required for atomic writes)"
    )


def _bbox_xyxy_to_gdal_projwin(bbox_xyxy: list[float]) -> list[float]:
    """Convert [xmin, ymin, xmax, ymax] to gdal_translate -projwin ulx uly lrx lry.

    For north-up georeferenced rasters, upper-left y is ymax and lower-right y is ymin.
    Assumes bbox coordinates are in the source dataset SRS (same as GDAL -projwin).
    """
    xmin, ymin, xmax, ymax = bbox_xyxy
    return [xmin, ymax, xmax, ymin]


def _optional_bbox(value: Any, field: str) -> list[float] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError(f"{field} must be [xmin, ymin, xmax, ymax]")
    try:
        result = [float(v) for v in value]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} values must be finite numbers") from exc
    if not all(math.isfinite(v) for v in result):
        raise ValueError(f"{field} values must be finite numbers")
    if result[0] >= result[2] or result[1] >= result[3]:
        raise ValueError(f"{field} must satisfy xmin < xmax and ymin < ymax")
    return result


def _validated_band_list(bands: Any) -> list[int]:
    if not bands:
        return []
    if not isinstance(bands, list):
        raise ValueError("bands must be a list of positive integers")
    validated: list[int] = []
    for band in bands:
        if isinstance(band, bool) or not isinstance(band, int) or band < 1:
            raise ValueError("each band must be a positive integer")
        validated.append(band)
    return validated


def _validate_resampling(value: Any) -> str:
    name = str(value).strip().lower()
    if name not in ALLOWED_RESAMPLING:
        raise ValueError(
            f"Unsupported resampling {value!r}; allowed: {sorted(ALLOWED_RESAMPLING)}"
        )
    return name


# --------------------------------------------------------------------------- verification


def _verify_raster_output(path: Path, executor: GDALCommandExecutor) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size <= 0:
        raise GDALCommandError(
            f"GDAL command completed but output is missing or empty: {path}"
        )
    metadata = executor.execute(
        "gdalinfo", ["-json", str(path)], expect_json=True, timeout=METADATA_TIMEOUT_SECONDS
    )
    size = metadata.get("size")
    bands = metadata.get("bands")
    if not (
        isinstance(size, list)
        and len(size) >= 2
        and all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in size[:2])
    ):
        raise GDALCommandError(f"Output raster has invalid dimensions: {path}")
    if not isinstance(bands, list) or not bands:
        raise GDALCommandError(f"Output raster has no readable bands: {path}")
    return metadata


def _verify_vector_output(path: Path, executor: GDALCommandExecutor) -> int | None:
    if not path.exists() or (path.is_file() and path.stat().st_size <= 0):
        raise GDALCommandError(
            f"OGR command completed but output is missing or empty: {path}"
        )
    info = executor.execute(
        "ogrinfo", ["-ro", "-so", "-al", str(path)], timeout=METADATA_TIMEOUT_SECONDS
    )
    return _feature_count(info)


def _feature_count(info: Any) -> int | None:
    counts = re.findall(r"Feature Count:\s*(-?\d+)", str(info or ""))
    if not counts:
        return None
    return sum(max(int(count), 0) for count in counts)


_OGR_EXTENT = re.compile(
    r"^Extent:\s*\(([^,]+),\s*([^)]+)\)\s*-\s*\(([^,]+),\s*([^)]+)\)", re.MULTILINE
)
_WKT_ROOT_LINE = re.compile(r"^[A-Z][A-Z0-9_]*\[")


def _summarize_ogrinfo_layers(info: Any) -> list[dict[str, Any]]:
    """Per-layer geometry type, feature count, CRS and extent from ``ogrinfo -so`` text.

    ogrinfo -json needs GDAL >= 3.7, while Debian bookworm ships 3.6.
    """
    text = str(info or "")
    blocks = re.split(r"^Layer name:\s*", text, flags=re.MULTILINE)[1:]
    layers: list[dict[str, Any]] = []
    for block in blocks:
        lines = block.splitlines()
        name = lines[0].strip() if lines else ""
        geometry = re.search(r"^Geometry:\s*(.+)$", block, re.MULTILINE)
        count = re.search(r"^Feature Count:\s*(-?\d+)", block, re.MULTILINE)
        extent = _OGR_EXTENT.search(block)
        wkt_lines: list[str] = []
        in_wkt = False
        for line in lines:
            if line.startswith("Layer SRS WKT:"):
                in_wkt = True
                continue
            if in_wkt:
                if line[:1].isspace() or _WKT_ROOT_LINE.match(line):
                    wkt_lines.append(line)
                    continue
                break
        authorities = re.findall(r'ID\["([^"]+)",\s*"?(\d+)"?\]', "\n".join(wkt_lines))
        layers.append(
            {
                "name": name,
                "geometry_type": geometry.group(1).strip() if geometry else None,
                "feature_count": int(count.group(1)) if count else None,
                "crs_authority": (
                    f"{authorities[-1][0].upper()}:{authorities[-1][1]}" if authorities else None
                ),
                "extent": (
                    [float(extent.group(index)) for index in (1, 2, 3, 4)] if extent else None
                ),
            }
        )
    return layers


def _vector_output_verification(path: Path, feature_count: int | None) -> dict[str, Any]:
    return {
        "verified": True,
        "output_exists": True,
        "output_size_bytes": path.stat().st_size if path.is_file() else None,
        "feature_count": feature_count,
    }


def _raster_output_verification(
    path: Path,
    metadata: dict[str, Any],
    atomic: bool = False,
) -> dict[str, Any]:
    size = metadata.get("size") or []
    bands = metadata.get("bands") or []
    verification = {
        "verified": True,
        "output_exists": True,
        "output_size_bytes": path.stat().st_size,
        "width": size[0] if len(size) > 0 else None,
        "height": size[1] if len(size) > 1 else None,
        "band_count": len(bands) if isinstance(bands, list) else None,
        "band_types": [band.get("type") for band in bands if isinstance(band, dict)],
    }
    if atomic:
        verification["commit"] = "atomic_geotiff_replace"
    return verification


def _execution_record(
    command: str,
    arguments: list[str],
    started_at: datetime,
    finished_at: datetime,
    duration_ms: float,
) -> dict[str, Any]:
    return {
        "command": command,
        "argv": [command, *arguments],
        "arguments": arguments,
        "return_code": 0,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_ms": duration_ms,
    }


def _elapsed_ms(started_clock: float) -> float:
    return round((perf_counter() - started_clock) * 1000, 3)


# --------------------------------------------------------------------------- CRS helpers


def _read_raster_crs(
    path: Path,
    executor: GDALCommandExecutor,
) -> dict[str, Any]:
    metadata = executor.execute(
        "gdalinfo",
        ["-json", str(path)],
        expect_json=True,
        timeout=METADATA_TIMEOUT_SECONDS,
    )
    coordinate_system = metadata.get("coordinateSystem") or {}
    wkt = coordinate_system.get("wkt")
    if not wkt:
        raise GDALCommandError(f"Raster has no readable CRS: {path}")
    authorities = re.findall(r'ID\["([^"]+)",\s*"?(\d+)"?\]', wkt)
    authority = (
        f"{authorities[-1][0].upper()}:{authorities[-1][1]}"
        if authorities
        else None
    )
    name_match = re.match(
        r'\s*(?:PROJCRS|GEOGCRS|COMPOUNDCRS|BOUNDCRS)\["([^"]+)"',
        wkt,
    )
    geo_transform = metadata.get("geoTransform")
    return {
        "name": name_match.group(1) if name_match else "Unknown CRS",
        "authority": authority,
        "is_projected": wkt.lstrip().startswith("PROJCRS["),
        "wkt": wkt,
        "size": metadata.get("size"),
        "pixel_size": (
            [geo_transform[1], geo_transform[5]]
            if isinstance(geo_transform, list) and len(geo_transform) >= 6
            else None
        ),
        "bounds": _raster_bounds(metadata),
    }


def _summarize_raster_metadata(
    metadata: dict[str, Any],
    path: Path,
) -> dict[str, Any]:
    coordinate_system = metadata.get("coordinateSystem") or {}
    wkt = str(coordinate_system.get("wkt") or "")
    authorities = re.findall(r'ID\["([^"]+)",\s*"?(\d+)"?\]', wkt)
    authority = (
        f"{authorities[-1][0].upper()}:{authorities[-1][1]}"
        if authorities
        else None
    )
    name_match = re.match(
        r'\s*(?:PROJCRS|GEOGCRS|COMPOUNDCRS|BOUNDCRS)\["([^"]+)"',
        wkt,
    )
    size = metadata.get("size")
    geo_transform = metadata.get("geoTransform")
    bands = metadata.get("bands") if isinstance(metadata.get("bands"), list) else []
    return {
        "path": str(path),
        "file_size_bytes": path.stat().st_size,
        "driver": metadata.get("driverShortName") or metadata.get("driverLongName"),
        "size": {
            "width": size[0] if isinstance(size, list) and len(size) >= 2 else None,
            "height": size[1] if isinstance(size, list) and len(size) >= 2 else None,
        },
        "crs": {
            "authority": authority,
            "name": name_match.group(1) if name_match else None,
            "is_projected": wkt.lstrip().startswith("PROJCRS[") if wkt else None,
        },
        "pixel_size": (
            [geo_transform[1], geo_transform[5]]
            if isinstance(geo_transform, list) and len(geo_transform) >= 6
            else None
        ),
        "bounds": _raster_bounds(metadata),
        "band_count": len(bands),
        "bands": [
            {
                "band": band.get("band"),
                "type": band.get("type"),
                "color_interpretation": band.get("colorInterpretation"),
                "nodata": band.get("noDataValue"),
                "overview_count": len(band.get("overviews") or []),
                **_band_statistics(band),
            }
            for band in bands
            if isinstance(band, dict)
        ],
    }


_BAND_STATISTICS = {
    "minimum": "STATISTICS_MINIMUM",
    "maximum": "STATISTICS_MAXIMUM",
    "mean": "STATISTICS_MEAN",
    "stdDev": "STATISTICS_STDDEV",
}


def _band_statistics(band: dict[str, Any]) -> dict[str, Any]:
    """Band statistics at full precision.

    gdalinfo -json rounds minimum/maximum/mean/stdDev to 3 decimals; the STATISTICS_*
    metadata written by -stats keeps the full value.
    """
    if "mean" not in band:
        return {}
    metadata = (band.get("metadata") or {}).get("") or {}
    statistics: dict[str, Any] = {}
    for key, metadata_key in _BAND_STATISTICS.items():
        value = band.get(key)
        try:
            precise = float(metadata[metadata_key])
        except (KeyError, TypeError, ValueError):
            precise = None
        statistics[key] = precise if precise is not None and math.isfinite(precise) else value
    return statistics


def _normalize_authority_crs(value: str) -> str:
    match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_]*):(\d+)", value.strip())
    if not match:
        raise ValueError(
            "target_crs must be an authority code such as EPSG:3857 or EPSG:32650"
        )
    return f"{match.group(1).upper()}:{match.group(2)}"


def _public_crs_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": record["name"],
        "authority": record["authority"],
        "is_projected": record["is_projected"],
    }


def _grid_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "size": record.get("size"),
        "pixel_size": record.get("pixel_size"),
        "bounds": record.get("bounds"),
    }


def _raster_bounds(metadata: dict[str, Any]) -> list[float] | None:
    corners = metadata.get("cornerCoordinates") or {}
    points = [
        corners.get(name)
        for name in ("upperLeft", "lowerLeft", "lowerRight", "upperRight")
    ]
    valid_points = [
        point
        for point in points
        if isinstance(point, list) and len(point) >= 2
    ]
    if valid_points:
        xs = [float(point[0]) for point in valid_points]
        ys = [float(point[1]) for point in valid_points]
        return [min(xs), min(ys), max(xs), max(ys)]

    geo_transform = metadata.get("geoTransform")
    size = metadata.get("size")
    if (
        not isinstance(geo_transform, list)
        or len(geo_transform) < 6
        or not isinstance(size, list)
        or len(size) < 2
    ):
        return None
    width, height = float(size[0]), float(size[1])
    points = []
    for pixel, line in ((0, 0), (width, 0), (0, height), (width, height)):
        x = geo_transform[0] + pixel * geo_transform[1] + line * geo_transform[2]
        y = geo_transform[3] + pixel * geo_transform[4] + line * geo_transform[5]
        points.append((x, y))
    return [
        min(point[0] for point in points),
        min(point[1] for point in points),
        max(point[0] for point in points),
        max(point[1] for point in points),
    ]


def _web_mercator_extent_protection(
    source_crs_record: dict[str, Any],
    target_authority: str,
    requested_bbox: list[float] | None,
    requested_bbox_crs: str | None,
) -> tuple[list[float] | None, str | None, dict[str, Any] | None]:
    """Clamp geographic latitude when reprojecting WGS84 → Web Mercator (EPSG:3857).

    Heuristic for full-world or high-latitude sources; validate with real rasters
    (polar edge, partial bbox, explicit resolution) before treating as production-safe.
    """
    if (
        target_authority != "EPSG:3857"
        or source_crs_record.get("authority") != "EPSG:4326"
    ):
        return requested_bbox, requested_bbox_crs, None

    if requested_bbox:
        if not requested_bbox_crs:
            return requested_bbox, requested_bbox_crs, None
        try:
            bbox_authority = _normalize_authority_crs(requested_bbox_crs)
        except ValueError:
            return requested_bbox, requested_bbox_crs, None
        if bbox_authority != "EPSG:4326":
            return requested_bbox, requested_bbox_crs, None
        candidate = [float(value) for value in requested_bbox]
    else:
        source_bounds = source_crs_record.get("bounds")
        if not source_bounds:
            return requested_bbox, requested_bbox_crs, None
        candidate = [float(value) for value in source_bounds]

    if (
        candidate[1] >= -WEB_MERCATOR_MAX_LATITUDE
        and candidate[3] <= WEB_MERCATOR_MAX_LATITUDE
    ):
        return requested_bbox, requested_bbox_crs, None

    clamped = [
        candidate[0],
        max(candidate[1], -WEB_MERCATOR_MAX_LATITUDE),
        candidate[2],
        min(candidate[3], WEB_MERCATOR_MAX_LATITUDE),
    ]
    if clamped[1] >= clamped[3]:
        raise ValueError(
            "The requested latitude extent is outside the valid EPSG:3857 domain"
        )
    protection = {
        "id": "web_mercator_latitude_clamp",
        "applied": True,
        "original_bbox": candidate,
        "applied_bbox": clamped,
        "bbox_crs": "EPSG:4326",
        "valid_latitude": [
            -WEB_MERCATOR_MAX_LATITUDE,
            WEB_MERCATOR_MAX_LATITUDE,
        ],
        "message": (
            "EPSG:3857不能表示南北极；GeoInvoke已将纬度范围自动限制为"
            "±85.05112878°，极区数据未写入输出；未指定目标分辨率时，"
            "将依据源栅格经向像元密度自动设置米制分辨率。"
        ),
    }
    return clamped, "EPSG:4326", protection


def _web_mercator_resolution(
    geographic_bbox: list[float],
    source_size: list[int] | None,
) -> float | None:
    if not source_size or len(source_size) < 2 or int(source_size[0]) <= 0:
        return None
    longitude_span = geographic_bbox[2] - geographic_bbox[0]
    projected_width = WEB_MERCATOR_RADIUS * radians(longitude_span)
    if projected_width <= 0:
        return None
    return projected_width / int(source_size[0])


def _resolution_pair(value: float | list[float]) -> tuple[float, float]:
    if isinstance(value, bool):
        raise ValueError("resolution must be a positive number or [xres, yres]")
    if isinstance(value, (int, float)):
        xres = yres = float(value)
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            xres, yres = float(value[0]), float(value[1])
        except (TypeError, ValueError) as exc:
            raise ValueError("resolution must be a positive number or [xres, yres]") from exc
    else:
        raise ValueError("resolution must be a positive number or [xres, yres]")
    if not math.isfinite(xres) or not math.isfinite(yres) or xres <= 0 or yres <= 0:
        raise ValueError("resolution values must be finite and positive")
    return xres, yres


def _creation_option_args(options: dict[str, str]) -> list[str]:
    if not isinstance(options, dict):
        raise ValueError("creation_options must be a dictionary")
    arguments: list[str] = []
    for key, value in options.items():
        normalized_key = str(key).upper()
        if normalized_key not in ALLOWED_CREATION_OPTIONS:
            raise ValueError(f"Unsupported GDAL creation option: {key}")
        value_text = str(value).strip()
        if not value_text or any(char.isspace() for char in value_text) or "=" in value_text:
            raise ValueError(f"Invalid value for creation option {key}: {value!r}")
        arguments.extend(["-co", f"{normalized_key}={value_text}"])
    return arguments
