#!/usr/bin/env python3
"""Backfill the full-sample 80 m GFS wind feature from NCAR GDEX to GCS.

This restores the historical coverage of the existing ``gfs_wind80_5d``
feature without changing its economic or geographic definition:

* one 00 UTC GFS initialization per issue date;
* forecast days 1 through 5;
* four instantaneous samples per target day (00/06/12/18 UTC);
* the same 28 representative U.S. locations used by the existing weather
  pipeline;
* equal-weight location and lead-day aggregation.

The source is NCAR GDEX d084001 via its public THREDDS NetCDF Subset Service.
Outputs are monthly, point-in-time safe, and resumable in Google Cloud Storage.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import re
import struct
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode

import gcsfs
import numpy as np
import pandas as pd
import requests


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from naturalgas.open_meteo_us_ng_backfill import LOCATIONS  # noqa: E402


DATASET_ID = "d084001"
MODEL = "ncep_gfs_0p25"
TDS_BASE = "https://tds.gdex.ucar.edu/thredds"
from naturalgas.storage_config import PERSONAL_GCS_ROOT


BUCKET = PERSONAL_GCS_ROOT
RAW_PREFIX = (
    "raw/weather/ncar_gdex/d084001/wind_points/"
    "model=ncep_gfs_0p25/cycle=00"
)
PROCESSED_PREFIX = (
    "processed/weather/ncar_gdex/d084001/"
    "model=ncep_gfs_0p25/cycle=00"
)
DEFAULT_START = date(2016, 1, 1)
DEFAULT_END = date.today() - timedelta(days=2)
DEFAULT_LEAD_DAYS = (1, 2, 3, 4, 5)
DEFAULT_VALID_HOURS = (0, 6, 12, 18)
MAX_NCAR_WORKERS = 10
USER_AGENT = "braeswood-naturalgas-ncar-wind-backfill/1.0"
U_VARIABLE = "u-component_of_wind_height_above_ground"
V_VARIABLE = "v-component_of_wind_height_above_ground"


class BackfillError(RuntimeError):
    """Raised when a partition cannot be completed without data loss."""


@dataclass(frozen=True)
class BoundingBox:
    north: float
    south: float
    west: float
    east: float


@dataclass(frozen=True)
class ForecastTask:
    initialization_time_utc: datetime
    lead_days: int
    forecast_lead_hours: int

    @property
    def valid_time_utc(self) -> datetime:
        return self.initialization_time_utc + timedelta(
            hours=self.forecast_lead_hours
        )

    @property
    def source_filename(self) -> str:
        return (
            f"gfs.0p25.{self.initialization_time_utc:%Y%m%d%H}."
            f"f{self.forecast_lead_hours:03d}.grib2"
        )

    @property
    def source_path(self) -> str:
        return (
            f"files/g/{DATASET_ID}/{self.initialization_time_utc:%Y}/"
            f"{self.initialization_time_utc:%Y%m%d}/{self.source_filename}"
        )

    @property
    def ncss_url(self) -> str:
        return f"{TDS_BASE}/ncss/grid/{self.source_path}"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def location_bbox(padding_degrees: float = 0.5) -> BoundingBox:
    step = 0.25

    def ceil_grid(value: float) -> float:
        return math.ceil(value / step) * step

    def floor_grid(value: float) -> float:
        return math.floor(value / step) * step

    return BoundingBox(
        north=ceil_grid(max(item.latitude for item in LOCATIONS) + padding_degrees),
        south=floor_grid(min(item.latitude for item in LOCATIONS) - padding_degrees),
        west=floor_grid(min(item.longitude for item in LOCATIONS) - padding_degrees),
        east=ceil_grid(max(item.longitude for item in LOCATIONS) + padding_degrees),
    )


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def parse_int_csv(
    value: str,
    *,
    minimum: int,
    maximum: int,
) -> tuple[int, ...]:
    try:
        parsed = tuple(
            sorted({int(item.strip()) for item in value.split(",") if item.strip()})
        )
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"expected comma-separated integers, got {value!r}"
        ) from exc
    if not parsed or parsed[0] < minimum or parsed[-1] > maximum:
        raise argparse.ArgumentTypeError(
            f"values must be between {minimum} and {maximum}"
        )
    return parsed


def month_ranges(start: date, end: date) -> list[tuple[date, date]]:
    if start > end:
        raise ValueError("start must not be after end")
    starts = pd.date_range(start=start.replace(day=1), end=end, freq="MS").date
    result = []
    for month_start in starts:
        month_end = (pd.Timestamp(month_start) + pd.offsets.MonthEnd(0)).date()
        result.append((max(start, month_start), min(end, month_end)))
    return result


def forecast_tasks(
    start: date,
    end: date,
    lead_days: Iterable[int],
    valid_hours: Iterable[int],
) -> list[ForecastTask]:
    tasks: list[ForecastTask] = []
    for issue_date in pd.date_range(start=start, end=end, freq="D"):
        initialization = issue_date.to_pydatetime().replace(tzinfo=timezone.utc)
        for lead_day in lead_days:
            for valid_hour in valid_hours:
                tasks.append(
                    ForecastTask(
                        initialization_time_utc=initialization,
                        lead_days=lead_day,
                        forecast_lead_hours=lead_day * 24 + valid_hour,
                    )
                )
    return tasks


class ClassicNetcdf:
    """Minimal reader for the classic CDF-1 responses emitted by NCAR NCSS."""

    TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 4, 6: 8}
    TYPE_DTYPES = {
        1: ">i1",
        2: "S1",
        3: ">i2",
        4: ">i4",
        5: ">f4",
        6: ">f8",
    }

    def __init__(self, content: bytes) -> None:
        if content[:4] != b"CDF\x01":
            raise BackfillError("NCSS response is not a classic CDF-1 NetCDF file")
        self.content = content
        self.position = 4
        self.dimensions: list[tuple[str, int]] = []
        self.variables: dict[str, dict[str, Any]] = {}
        self._parse_header()

    def _u32(self) -> int:
        value = struct.unpack_from(">I", self.content, self.position)[0]
        self.position += 4
        return value

    def _string(self) -> str:
        length = self._u32()
        value = self.content[
            self.position : self.position + length
        ].decode("utf-8")
        self.position += length + (-length % 4)
        return value

    def _skip_attributes(self) -> None:
        tag = self._u32()
        count = self._u32()
        if tag == 0 and count == 0:
            return
        if tag != 12:
            raise BackfillError(f"unexpected NetCDF attribute tag {tag}")
        for _ in range(count):
            self._string()
            nc_type = self._u32()
            value_count = self._u32()
            length = self.TYPE_SIZES[nc_type] * value_count
            self.position += length + (-length % 4)

    def _parse_header(self) -> None:
        self._u32()  # number of records; NCSS grid responses are non-record data
        dimension_tag = self._u32()
        dimension_count = self._u32()
        if dimension_tag not in {0, 10}:
            raise BackfillError(
                f"unexpected NetCDF dimension tag {dimension_tag}"
            )
        for _ in range(dimension_count):
            self.dimensions.append((self._string(), self._u32()))

        self._skip_attributes()
        variable_tag = self._u32()
        variable_count = self._u32()
        if variable_tag not in {0, 11}:
            raise BackfillError(f"unexpected NetCDF variable tag {variable_tag}")
        for _ in range(variable_count):
            name = self._string()
            dimension_ids = [self._u32() for _ in range(self._u32())]
            self._skip_attributes()
            nc_type = self._u32()
            value_size = self._u32()
            begin = self._u32()
            self.variables[name] = {
                "dimension_names": [
                    self.dimensions[index][0] for index in dimension_ids
                ],
                "shape": tuple(
                    self.dimensions[index][1] for index in dimension_ids
                ),
                "nc_type": nc_type,
                "value_size": value_size,
                "begin": begin,
            }

    def read(self, name: str) -> np.ndarray:
        if name not in self.variables:
            raise BackfillError(f"NetCDF variable is missing: {name}")
        item = self.variables[name]
        count = math.prod(item["shape"]) if item["shape"] else 1
        values = np.frombuffer(
            self.content,
            dtype=self.TYPE_DTYPES[item["nc_type"]],
            count=count,
            offset=item["begin"],
        )
        return values.reshape(item["shape"]) if item["shape"] else values

    def latitude_longitude_grid(self, name: str) -> np.ndarray:
        item = self.variables[name]
        values = self.read(name)
        dimension_names = list(item["dimension_names"])
        for index in range(len(dimension_names) - 1, -1, -1):
            dimension = dimension_names[index]
            if dimension not in {"latitude", "longitude"}:
                if values.shape[index] != 1:
                    raise BackfillError(
                        f"{name} has non-singleton unsupported axis {dimension}"
                    )
                values = np.take(values, 0, axis=index)
                dimension_names.pop(index)
        if set(dimension_names) != {"latitude", "longitude"}:
            raise BackfillError(
                f"{name} does not have latitude/longitude axes: {dimension_names}"
            )
        latitude_axis = dimension_names.index("latitude")
        longitude_axis = dimension_names.index("longitude")
        values = np.moveaxis(
            values,
            (latitude_axis, longitude_axis),
            (0, 1),
        )
        return np.asarray(values, dtype=np.float32)


def ncss_params(bbox: BoundingBox) -> list[tuple[str, str]]:
    return [
        ("var", U_VARIABLE),
        ("var", V_VARIABLE),
        ("north", str(bbox.north)),
        ("south", str(bbox.south)),
        ("west", str(bbox.west)),
        ("east", str(bbox.east)),
        ("vertCoord", "80"),
        ("horizStride", "1"),
        ("addLatLon", "true"),
        ("accept", "netcdf"),
    ]


def fetch_task(
    task: ForecastTask,
    *,
    bbox: BoundingBox,
    attempts: int,
    timeout_seconds: float,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    params = ncss_params(bbox)
    last_error: Exception | None = None
    response: requests.Response | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(
                task.ncss_url,
                params=params,
                headers={"User-Agent": USER_AGENT},
                timeout=timeout_seconds,
            )
            if response.status_code in {429, 500, 502, 503, 504}:
                raise BackfillError(
                    f"retryable NCSS HTTP {response.status_code}: "
                    f"{response.text[:200]}"
                )
            if response.status_code != 200:
                raise BackfillError(
                    f"NCSS HTTP {response.status_code} for {task.source_filename}: "
                    f"{response.text[:300]}"
                )
            dataset = ClassicNetcdf(response.content)
            break
        except (requests.RequestException, BackfillError) as exc:
            last_error = exc
            if attempt == attempts:
                raise BackfillError(
                    f"failed {task.source_filename} after {attempts} attempts"
                ) from exc
            time.sleep(min(2**attempt, 30))
    else:
        raise BackfillError(f"failed {task.source_filename}") from last_error

    assert response is not None
    latitude = np.asarray(dataset.read("latitude"), dtype=float)
    longitude = np.asarray(dataset.read("longitude"), dtype=float)
    longitude = np.where(longitude > 180, longitude - 360, longitude)
    u_grid = dataset.latitude_longitude_grid(U_VARIABLE)
    v_grid = dataset.latitude_longitude_grid(V_VARIABLE)
    u_grid = np.where(np.abs(u_grid) < 1e20, u_grid, np.nan)
    v_grid = np.where(np.abs(v_grid) < 1e20, v_grid, np.nan)

    retrieved_at = utc_now_iso()
    rows = []
    for location in LOCATIONS:
        latitude_index = int(np.abs(latitude - location.latitude).argmin())
        longitude_index = int(np.abs(longitude - location.longitude).argmin())
        u_value = float(u_grid[latitude_index, longitude_index])
        v_value = float(v_grid[latitude_index, longitude_index])
        speed = math.hypot(u_value, v_value)
        direction = (
            270.0 - math.degrees(math.atan2(v_value, u_value))
        ) % 360.0
        rows.append(
            {
                "dataset_id": DATASET_ID,
                "model": MODEL,
                "location_id": location.location_id,
                "city": location.city,
                "state": location.state,
                "census_division": location.census_division,
                "requested_latitude": np.float32(location.latitude),
                "requested_longitude": np.float32(location.longitude),
                "grid_latitude": np.float32(latitude[latitude_index]),
                "grid_longitude": np.float32(longitude[longitude_index]),
                "forecast_reference_time_utc": task.initialization_time_utc,
                "forecast_lead_hours": np.int16(task.forecast_lead_hours),
                "valid_time_utc": task.valid_time_utc,
                "target_date": task.valid_time_utc.date(),
                "lead_days": np.int8(task.lead_days),
                "u_wind_80m_mps": np.float32(u_value),
                "v_wind_80m_mps": np.float32(v_value),
                "wind_speed_80m_mps": np.float32(speed),
                "wind_speed_80m_kmh": np.float32(speed * 3.6),
                "wind_direction_80m_deg": np.float32(direction),
                "source_file": task.source_filename,
                "retrieved_at_utc": retrieved_at,
            }
        )

    request_url = f"{task.ncss_url}?{urlencode(params)}"
    inventory = {
        "dataset_id": DATASET_ID,
        "model": MODEL,
        "forecast_reference_time_utc": task.initialization_time_utc,
        "forecast_lead_hours": task.forecast_lead_hours,
        "valid_time_utc": task.valid_time_utc,
        "lead_days": task.lead_days,
        "source_file": task.source_filename,
        "ncss_request_url": request_url,
        "ncss_response_bytes": len(response.content),
        "ncss_response_sha256": hashlib.sha256(response.content).hexdigest(),
        "retrieved_at_utc": retrieved_at,
    }
    return pd.DataFrame(rows), inventory


def fetch_partition(
    start: date,
    end: date,
    *,
    lead_days: tuple[int, ...],
    valid_hours: tuple[int, ...],
    workers: int,
    attempts: int,
    timeout_seconds: float,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    bbox = location_bbox()
    tasks = forecast_tasks(start, end, lead_days, valid_hours)
    point_frames: list[pd.DataFrame] = []
    inventory_rows: list[dict[str, Any]] = []
    completed = 0
    print(
        f"fetch {start}..{end}: {len(tasks):,} NCSS files, "
        f"{len(LOCATIONS)} locations, bbox={asdict(bbox)}",
        flush=True,
    )
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(
                fetch_task,
                task,
                bbox=bbox,
                attempts=attempts,
                timeout_seconds=timeout_seconds,
            ): task
            for task in tasks
        }
        try:
            for future in as_completed(future_map):
                task = future_map[future]
                points, inventory = future.result()
                point_frames.append(points)
                inventory_rows.append(inventory)
                completed += 1
                if completed % 50 == 0 or completed == len(tasks):
                    print(
                        f"  completed {completed:,}/{len(tasks):,} "
                        f"(latest {task.source_filename})",
                        flush=True,
                    )
        except Exception:
            for pending in future_map:
                pending.cancel()
            raise

    points = pd.concat(point_frames, ignore_index=True)
    points = points.sort_values(
        [
            "forecast_reference_time_utc",
            "forecast_lead_hours",
            "location_id",
        ]
    ).reset_index(drop=True)
    inventory = pd.DataFrame(inventory_rows).sort_values(
        ["forecast_reference_time_utc", "forecast_lead_hours"]
    ).reset_index(drop=True)

    expected_point_rows = len(tasks) * len(LOCATIONS)
    if len(points) != expected_point_rows:
        raise BackfillError(
            f"expected {expected_point_rows:,} point rows, got {len(points):,}"
        )
    source_key = ["forecast_reference_time_utc", "forecast_lead_hours"]
    if inventory.duplicated(source_key).any():
        raise BackfillError("duplicate source inventory rows")
    point_key = source_key + ["location_id"]
    if points.duplicated(point_key).any():
        raise BackfillError("duplicate point wind rows")
    return points, inventory


def make_daily(
    points: pd.DataFrame,
    expected_samples: int,
    minimum_samples: int | None = None,
) -> pd.DataFrame:
    if minimum_samples is None:
        minimum_samples = expected_samples
    if not 1 <= minimum_samples <= expected_samples:
        raise ValueError(
            "minimum_samples must be between 1 and expected_samples"
        )
    group_columns = [
        "dataset_id",
        "model",
        "forecast_reference_time_utc",
        "target_date",
        "lead_days",
        "location_id",
        "city",
        "state",
        "census_division",
        "requested_latitude",
        "requested_longitude",
        "grid_latitude",
        "grid_longitude",
    ]
    daily = (
        points.groupby(
            group_columns,
            observed=True,
            dropna=False,
            as_index=False,
        )
        .agg(
            wind_sample_count=("wind_speed_80m_mps", "count"),
            u_wind_80m_mean_mps=("u_wind_80m_mps", "mean"),
            v_wind_80m_mean_mps=("v_wind_80m_mps", "mean"),
            wind_speed_80m_mean_mps=("wind_speed_80m_mps", "mean"),
            wind_speed_80m_max_mps=("wind_speed_80m_mps", "max"),
        )
        .sort_values(
            ["forecast_reference_time_utc", "lead_days", "location_id"]
        )
        .reset_index(drop=True)
    )
    invalid_sample_count = (
        daily["wind_sample_count"].lt(minimum_samples)
        | daily["wind_sample_count"].gt(expected_samples)
    )
    if invalid_sample_count.any():
        bad = daily.loc[
            invalid_sample_count,
            [
                "forecast_reference_time_utc",
                "lead_days",
                "location_id",
                "wind_sample_count",
            ],
        ]
        raise BackfillError(
            "incomplete daily wind samples: "
            + bad.head(10).to_dict(orient="records").__repr__()
        )
    daily["wind_sample_complete"] = daily["wind_sample_count"].eq(
        expected_samples
    )
    daily["wind_speed_80m_mean_kmh"] = (
        daily["wind_speed_80m_mean_mps"] * 3.6
    ).astype("float32")
    daily["wind_speed_80m_max_kmh"] = (
        daily["wind_speed_80m_max_mps"] * 3.6
    ).astype("float32")
    daily["wind_direction_80m_mean_deg"] = (
        270.0
        - np.degrees(
            np.arctan2(
                daily["v_wind_80m_mean_mps"],
                daily["u_wind_80m_mean_mps"],
            )
        )
    ) % 360.0
    daily["nominal_issue_date"] = pd.to_datetime(
        daily["forecast_reference_time_utc"], utc=True
    ).dt.date
    return daily


def make_features(
    daily: pd.DataFrame,
    expected_lead_days: int,
    expected_locations: int = len(LOCATIONS),
    require_complete: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    city_mean = (
        daily.groupby(
            [
                "forecast_reference_time_utc",
                "nominal_issue_date",
                "target_date",
                "lead_days",
            ],
            observed=True,
            as_index=False,
        )
        .agg(
            wind80_kmh=("wind_speed_80m_mean_kmh", "mean"),
            location_count=("location_id", "nunique"),
        )
        .sort_values(["forecast_reference_time_utc", "lead_days"])
        .reset_index(drop=True)
    )
    feature = (
        city_mean.groupby(
            ["forecast_reference_time_utc", "nominal_issue_date"],
            observed=True,
            as_index=False,
        )
        .agg(
            gfs_wind80_5d=("wind80_kmh", "mean"),
            gfs_lead_count=("lead_days", "nunique"),
            gfs_min_locations=("location_count", "min"),
        )
        .sort_values("forecast_reference_time_utc")
        .reset_index(drop=True)
    )
    if require_complete:
        if not feature["gfs_lead_count"].eq(expected_lead_days).all():
            raise BackfillError(
                "not every issue date has all requested lead days"
            )
        if not feature["gfs_min_locations"].eq(expected_locations).all():
            raise BackfillError(
                "not every issue date has all configured locations"
            )
    return city_mean, feature


def partition_keys(
    *,
    bucket: str,
    raw_prefix: str,
    processed_prefix: str,
    year: int,
    month: int,
) -> dict[str, str]:
    partition = f"year={year:04d}/month={month:02d}"
    return {
        "raw_points": f"{bucket}/{raw_prefix}/{partition}/data.parquet",
        "source_inventory": (
            f"{bucket}/{raw_prefix}/{partition}/source_inventory.parquet"
        ),
        "daily": (
            f"{bucket}/{processed_prefix}/wind_daily/"
            f"{partition}/data.parquet"
        ),
        "city_leads": (
            f"{bucket}/{processed_prefix}/wind_city_leads/"
            f"{partition}/data.parquet"
        ),
        "features": (
            f"{bucket}/{processed_prefix}/wind_features/"
            f"{partition}/data.parquet"
        ),
        "manifest": f"{bucket}/{raw_prefix}/{partition}/manifest.json",
    }


def write_parquet(
    fs: gcsfs.GCSFileSystem,
    key: str,
    frame: pd.DataFrame,
) -> int:
    buffer = io.BytesIO()
    frame.to_parquet(buffer, index=False, compression="zstd")
    payload = buffer.getvalue()
    fs.pipe(key, payload)
    return len(payload)


def write_json(
    fs: gcsfs.GCSFileSystem,
    key: str,
    payload: dict[str, Any],
) -> int:
    encoded = (
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"
    ).encode("utf-8")
    fs.pipe(key, encoded)
    return len(encoded)


def report_status(
    fs: gcsfs.GCSFileSystem,
    *,
    bucket: str,
    raw_prefix: str,
    expected_start: date,
    expected_end: date,
) -> int:
    pattern = f"{bucket}/{raw_prefix}/year=*/month=*/manifest.json"
    manifest_keys = sorted(fs.glob(pattern))
    expected = month_ranges(expected_start, expected_end)
    manifests = []
    for key in manifest_keys:
        with fs.open(key, "rb") as handle:
            manifests.append(json.load(handle))
    completed_months = {
        (
            int(re.search(r"/year=(\d{4})/", key).group(1)),
            int(re.search(r"/month=(\d{2})/", key).group(1)),
        )
        for key in manifest_keys
    }
    expected_months = {(start.year, start.month) for start, _ in expected}
    missing = sorted(expected_months.difference(completed_months))
    print(f"manifest pattern: gs://{pattern}")
    print(
        f"complete monthly partitions: {len(completed_months)}/"
        f"{len(expected_months)}"
    )
    if manifests:
        coverage_start = min(
            item["coverage_start_issue_date"] for item in manifests
        )
        coverage_end = max(
            item["coverage_end_issue_date"] for item in manifests
        )
        raw_rows = sum(item["rows"]["raw_points"] for item in manifests)
        feature_rows = sum(item["rows"]["features"] for item in manifests)
        print(f"covered issue dates: {coverage_start}..{coverage_end}")
        print(f"raw point rows: {raw_rows:,}")
        print(f"feature rows: {feature_rows:,}")
    if missing:
        preview = ", ".join(f"{year:04d}-{month:02d}" for year, month in missing[:12])
        suffix = " ..." if len(missing) > 12 else ""
        print(f"missing partitions: {preview}{suffix}")
        return 1
    print("coverage status: complete")
    return 0


def process_partition(
    fs: gcsfs.GCSFileSystem,
    *,
    start: date,
    end: date,
    lead_days: tuple[int, ...],
    valid_hours: tuple[int, ...],
    workers: int,
    attempts: int,
    timeout_seconds: float,
    bucket: str,
    raw_prefix: str,
    processed_prefix: str,
    force: bool,
) -> dict[str, Any]:
    keys = partition_keys(
        bucket=bucket,
        raw_prefix=raw_prefix,
        processed_prefix=processed_prefix,
        year=start.year,
        month=start.month,
    )
    if all(fs.exists(key) for key in keys.values()) and not force:
        print(f"skip complete partition {start:%Y-%m}", flush=True)
        return {"status": "skipped", "start": start, "end": end, "keys": keys}

    points, inventory = fetch_partition(
        start,
        end,
        lead_days=lead_days,
        valid_hours=valid_hours,
        workers=workers,
        attempts=attempts,
        timeout_seconds=timeout_seconds,
    )
    daily = make_daily(points, expected_samples=len(valid_hours))
    city_leads, features = make_features(
        daily,
        expected_lead_days=len(lead_days),
    )

    byte_counts = {
        "raw_points": write_parquet(fs, keys["raw_points"], points),
        "source_inventory": write_parquet(
            fs, keys["source_inventory"], inventory
        ),
        "daily": write_parquet(fs, keys["daily"], daily),
        "city_leads": write_parquet(fs, keys["city_leads"], city_leads),
        "features": write_parquet(fs, keys["features"], features),
    }
    manifest = {
        "status": "complete",
        "dataset_id": DATASET_ID,
        "model": MODEL,
        "source_service": "NCAR GDEX THREDDS NetCDF Subset Service",
        "source_dataset_url": f"https://gdex.ucar.edu/datasets/{DATASET_ID}/",
        "coverage_start_issue_date": start.isoformat(),
        "coverage_end_issue_date": end.isoformat(),
        "cycle_hour_utc": 0,
        "lead_days": list(lead_days),
        "valid_hours_utc": list(valid_hours),
        "height_m": 80,
        "locations": len(LOCATIONS),
        "bbox": asdict(location_bbox()),
        "rows": {
            "raw_points": len(points),
            "source_inventory": len(inventory),
            "daily": len(daily),
            "city_leads": len(city_leads),
            "features": len(features),
        },
        "gcs_keys": {name: f"gs://{key}" for name, key in keys.items()},
        "gcs_object_bytes": byte_counts,
        "created_at_utc": utc_now_iso(),
        "feature_definition": (
            "Equal-weight mean 80 m wind speed across the configured locations "
            "and forecast lead days 1-5; four valid-time samples per day."
        ),
        "historical_availability_verified": True,
    }
    byte_counts["manifest"] = write_json(fs, keys["manifest"], manifest)
    print(
        f"uploaded {start:%Y-%m}: raw={len(points):,}, "
        f"daily={len(daily):,}, features={len(features):,}",
        flush=True,
    )
    return {
        "status": "uploaded",
        "start": start,
        "end": end,
        "keys": keys,
        "rows": manifest["rows"],
        "bytes": byte_counts,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=parse_date, default=DEFAULT_START)
    parser.add_argument("--end", type=parse_date, default=DEFAULT_END)
    parser.add_argument("--bucket", default=BUCKET)
    parser.add_argument("--raw-prefix", default=RAW_PREFIX)
    parser.add_argument("--processed-prefix", default=PROCESSED_PREFIX)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--attempts", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=240.0)
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--execute",
        action="store_true",
        help=(
            "Explicitly authorize NCAR requests and writes to GCS. "
            "Without this flag only --status is allowed."
        ),
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="report GCS partition coverage without downloading or writing",
    )
    parser.add_argument(
        "--lead-days",
        default=",".join(map(str, DEFAULT_LEAD_DAYS)),
    )
    parser.add_argument(
        "--valid-hours",
        default=",".join(map(str, DEFAULT_VALID_HOURS)),
    )
    parser.add_argument(
        "--max-partitions",
        type=int,
        help="process only the first N monthly partitions",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        lead_days = parse_int_csv(args.lead_days, minimum=1, maximum=5)
        valid_hours = parse_int_csv(args.valid_hours, minimum=0, maximum=23)
        if args.workers < 1 or args.workers > MAX_NCAR_WORKERS:
            raise ValueError(
                f"--workers must be between 1 and {MAX_NCAR_WORKERS}"
            )
        if args.attempts < 1:
            raise ValueError("--attempts must be at least 1")
        if not args.status and not args.execute:
            parser.error(
                "refusing remote requests/GCS writes without --execute"
            )
        fs = gcsfs.GCSFileSystem()
        if args.status:
            return report_status(
                fs,
                bucket=args.bucket,
                raw_prefix=args.raw_prefix,
                expected_start=args.start,
                expected_end=args.end,
            )
        ranges = month_ranges(args.start, args.end)
        if args.max_partitions is not None:
            if args.max_partitions < 1:
                raise ValueError("--max-partitions must be at least 1")
            ranges = ranges[: args.max_partitions]
        print(
            f"NCAR wind backfill: {args.start}..{args.end}, "
            f"{len(ranges)} monthly partitions, workers={args.workers}",
            flush=True,
        )
        print(
            f"raw=gs://{args.bucket}/{args.raw_prefix}\n"
            f"processed=gs://{args.bucket}/{args.processed_prefix}",
            flush=True,
        )
        results = []
        for start, end in ranges:
            results.append(
                process_partition(
                    fs,
                    start=start,
                    end=end,
                    lead_days=lead_days,
                    valid_hours=valid_hours,
                    workers=args.workers,
                    attempts=args.attempts,
                    timeout_seconds=args.timeout,
                    bucket=args.bucket,
                    raw_prefix=args.raw_prefix,
                    processed_prefix=args.processed_prefix,
                    force=args.force,
                )
            )
        uploaded = sum(item["status"] == "uploaded" for item in results)
        skipped = sum(item["status"] == "skipped" for item in results)
        print(
            f"finished: uploaded={uploaded}, skipped={skipped}, "
            f"partitions={len(results)}",
            flush=True,
        )
        return 0
    except (BackfillError, ValueError, requests.RequestException) as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
