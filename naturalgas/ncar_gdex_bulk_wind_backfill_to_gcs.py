#!/usr/bin/env python3
"""Backfill four-cycle GFS 80 m wind data through GDEX batch subsets.

The GDEX batch service prepares tar archives on NCAR infrastructure.  This
client can submit either monthly requests or one request spanning every
remaining month.  It streams the resulting NetCDF members, extracts the
configured U.S. points, builds the same monthly Parquet layers as the NCSS
backfill, and uploads those partitions to GCS.

All four GFS initialization cycles (00/06/12/18 UTC) are retained.  Forecast
members from f006 through f138 are requested so that every initialization has
four valid-time samples (00/06/12/18 UTC) for each calendar target day 1-5.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import re
import shutil
import sys
import tarfile
import tempfile
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import gcsfs
import numpy as np
import pandas as pd
import requests
from scipy.io import netcdf_file


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from naturalgas.ncar_gdex_gfs_wind import (  # noqa: E402
    GdexApi,
    HttpClient,
    NcarDataError,
    gdex_product,
    response_data,
    token_from_environment,
)
from naturalgas.ncar_gdex_wind_backfill_to_gcs import (  # noqa: E402
    BUCKET,
    DATASET_ID,
    DEFAULT_END,
    DEFAULT_LEAD_DAYS,
    DEFAULT_START,
    DEFAULT_VALID_HOURS,
    LOCATIONS,
    MODEL,
    BackfillError,
    ForecastTask,
    fetch_task,
    location_bbox,
    make_daily,
    make_features,
    month_ranges,
    parse_date,
    parse_int_csv,
    partition_keys,
    report_status,
    utc_now_iso,
    write_json,
    write_parquet,
)


GDEX_SUBSET_DATASET_ID = "ds084.1"
GDEX_SOURCE_SERVICE = "NCAR GDEX asynchronous dataset subset service"
RAW_PREFIX = (
    "raw/weather/ncar_gdex/d084001/wind_points/"
    "model=ncep_gfs_0p25/cycle=all"
)
PROCESSED_PREFIX = (
    "processed/weather/ncar_gdex/d084001/"
    "model=ncep_gfs_0p25/cycle=all"
)
DEFAULT_CYCLES = (0, 6, 12, 18)
MAX_OPEN_REQUESTS = 6
MEMBER_PATTERN = re.compile(
    r"^gfs\.0p25\.(?P<init>\d{10})\.f(?P<lead>\d{3})"
    r"\.grib2\.nc$"
)
TERMINAL_FAILURE_STATUSES = {
    "error",
    "failed",
    "purged",
    "cancelled",
    "canceled",
}


@dataclass(frozen=True)
class ExpectedSource:
    member_name: str
    source_file: str
    initialization_time_utc: datetime
    forecast_lead_hours: int
    valid_time_utc: datetime
    target_date: date
    lead_days: int
    cycle_hour_utc: int


def parse_cycles(value: str) -> tuple[int, ...]:
    cycles = parse_int_csv(value, minimum=0, maximum=23)
    if any(cycle not in {0, 6, 12, 18} for cycle in cycles):
        raise argparse.ArgumentTypeError(
            "cycles must be selected from 0,6,12,18"
        )
    return cycles


def requested_product_leads(
    cycles: Iterable[int],
    lead_days: Iterable[int],
    valid_hours: Iterable[int],
) -> tuple[int, ...]:
    leads = {
        lead_day * 24 + valid_hour - cycle
        for cycle in cycles
        for lead_day in lead_days
        for valid_hour in valid_hours
        if lead_day * 24 + valid_hour - cycle >= 0
    }
    if not leads or min(leads) < 0 or max(leads) > 384:
        raise ValueError("requested forecast products are outside GFS coverage")
    return tuple(sorted(leads))


def expected_sources(
    start: date,
    end: date,
    *,
    cycles: Iterable[int],
    lead_days: Iterable[int],
    valid_hours: Iterable[int],
) -> dict[str, ExpectedSource]:
    expected: dict[str, ExpectedSource] = {}
    for issue in pd.date_range(start=start, end=end, freq="D"):
        issue_date = issue.date()
        for cycle in cycles:
            initialization = datetime.combine(
                issue_date,
                datetime_time(hour=cycle),
                tzinfo=timezone.utc,
            )
            for lead_day in lead_days:
                target = issue_date + timedelta(days=lead_day)
                for valid_hour in valid_hours:
                    valid_time = datetime.combine(
                        target,
                        datetime_time(hour=valid_hour),
                        tzinfo=timezone.utc,
                    )
                    lead_hour = int(
                        (valid_time - initialization).total_seconds() // 3600
                    )
                    source_file = (
                        f"gfs.0p25.{initialization:%Y%m%d%H}."
                        f"f{lead_hour:03d}.grib2"
                    )
                    member_name = f"{source_file}.nc"
                    expected[member_name] = ExpectedSource(
                        member_name=member_name,
                        source_file=source_file,
                        initialization_time_utc=initialization,
                        forecast_lead_hours=lead_hour,
                        valid_time_utc=valid_time,
                        target_date=target,
                        lead_days=lead_day,
                        cycle_hour_utc=cycle,
                    )
    return expected


def build_batch_control(
    start: date,
    end: date,
    *,
    cycles: tuple[int, ...],
    lead_days: tuple[int, ...],
    valid_hours: tuple[int, ...],
) -> dict[str, str]:
    bbox = location_bbox()
    products = requested_product_leads(cycles, lead_days, valid_hours)
    start_stamp = f"{start:%Y%m%d}{min(cycles):02d}00"
    end_stamp = f"{end:%Y%m%d}{max(cycles):02d}00"
    return {
        "dataset": GDEX_SUBSET_DATASET_ID,
        "date": f"{start_stamp}/to/{end_stamp}",
        "datetype": "init",
        "param": "U GRD/V GRD",
        "level": "HTGL:80",
        "nlat": str(bbox.north),
        "slat": str(bbox.south),
        "wlon": str(bbox.west),
        "elon": str(bbox.east),
        "product": "/".join(gdex_product(lead) for lead in products),
        "oformat": "netCDF",
    }


def request_state_key(
    *,
    bucket: str,
    raw_prefix: str,
    year: int,
    month: int,
) -> str:
    return (
        f"{bucket}/{raw_prefix}/year={year:04d}/month={month:02d}/"
        "gdex_request.json"
    )


def range_request_state_key(
    *,
    bucket: str,
    raw_prefix: str,
    start: date,
    end: date,
) -> str:
    return (
        f"{bucket}/{raw_prefix}/_range_requests/"
        f"start={start.isoformat()}_end={end.isoformat()}.json"
    )


def read_json(
    fs: gcsfs.GCSFileSystem,
    key: str,
) -> dict[str, Any] | None:
    if not fs.exists(key):
        return None
    with fs.open(key, "rb") as handle:
        return json.load(handle)


def submit_partition_request(
    api: GdexApi,
    fs: gcsfs.GCSFileSystem,
    *,
    start: date,
    end: date,
    cycles: tuple[int, ...],
    lead_days: tuple[int, ...],
    valid_hours: tuple[int, ...],
    bucket: str,
    raw_prefix: str,
) -> dict[str, Any]:
    control = build_batch_control(
        start,
        end,
        cycles=cycles,
        lead_days=lead_days,
        valid_hours=valid_hours,
    )
    request_id = str(response_data(api.submit(control))["request_id"])
    state = {
        "dataset_id": DATASET_ID,
        "request_id": request_id,
        "partition_start": start.isoformat(),
        "partition_end": end.isoformat(),
        "cycles_utc": list(cycles),
        "lead_days": list(lead_days),
        "valid_hours_utc": list(valid_hours),
        "product_lead_hours": list(
            requested_product_leads(cycles, lead_days, valid_hours)
        ),
        "control": control,
        "status": "Submitted",
        "submitted_at_utc": utc_now_iso(),
        "updated_at_utc": utc_now_iso(),
    }
    key = request_state_key(
        bucket=bucket,
        raw_prefix=raw_prefix,
        year=start.year,
        month=start.month,
    )
    write_json(fs, key, state)
    print(
        f"submitted {start:%Y-%m}: GDEX request {request_id}",
        flush=True,
    )
    return state


def submit_range_request(
    api: GdexApi,
    fs: gcsfs.GCSFileSystem,
    *,
    start: date,
    end: date,
    cycles: tuple[int, ...],
    lead_days: tuple[int, ...],
    valid_hours: tuple[int, ...],
    state_key: str,
) -> dict[str, Any]:
    """Submit one GDEX request covering the full remaining date range."""

    control = build_batch_control(
        start,
        end,
        cycles=cycles,
        lead_days=lead_days,
        valid_hours=valid_hours,
    )
    request_id = str(response_data(api.submit(control))["request_id"])
    state = {
        "dataset_id": DATASET_ID,
        "request_id": request_id,
        "request_layout": "single_remaining_range",
        "partition_start": start.isoformat(),
        "partition_end": end.isoformat(),
        "cycles_utc": list(cycles),
        "lead_days": list(lead_days),
        "valid_hours_utc": list(valid_hours),
        "product_lead_hours": list(
            requested_product_leads(cycles, lead_days, valid_hours)
        ),
        "control": control,
        "status": "Submitted",
        "submitted_at_utc": utc_now_iso(),
        "updated_at_utc": utc_now_iso(),
    }
    write_json(fs, state_key, state)
    print(
        f"submitted single remaining-range request "
        f"{start.isoformat()}..{end.isoformat()}: "
        f"GDEX request {request_id}",
        flush=True,
    )
    return state


def _decode_char_variable(values: np.ndarray) -> str:
    flat = np.asarray(values).astype("S1").reshape(-1)
    return b"".join(flat.tolist()).decode("ascii").strip("\x00 ")


def _wind_variable_name(
    variables: dict[str, Any],
    prefix: str,
) -> str:
    candidates = sorted(
        name for name in variables if name.upper().startswith(prefix.upper())
    )
    if len(candidates) != 1:
        raise BackfillError(
            f"expected one {prefix} variable, found {candidates}"
        )
    return candidates[0]


def parse_netcdf_member(
    payload: bytes,
    source: ExpectedSource,
    *,
    archive_url: str,
    retrieved_at_utc: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    with netcdf_file(io.BytesIO(payload), "r", mmap=False) as dataset:
        variables = dataset.variables
        latitude = np.asarray(variables["lat"][:], dtype=float)
        longitude = np.asarray(variables["lon"][:], dtype=float)
        longitude = np.where(longitude > 180, longitude - 360, longitude)
        u_name = _wind_variable_name(variables, "U_GRD")
        v_name = _wind_variable_name(variables, "V_GRD")
        u_grid = np.asarray(variables[u_name][:], dtype=np.float32).squeeze()
        v_grid = np.asarray(variables[v_name][:], dtype=np.float32).squeeze()
        if u_grid.shape != (len(latitude), len(longitude)):
            raise BackfillError(
                f"{source.member_name} U grid has shape {u_grid.shape}"
            )
        if v_grid.shape != u_grid.shape:
            raise BackfillError(
                f"{source.member_name} V grid has shape {v_grid.shape}"
            )
        if "forecast_hour" in variables:
            forecast_hour = int(
                np.asarray(variables["forecast_hour"][:]).reshape(-1)[0]
            )
            if forecast_hour != source.forecast_lead_hours:
                raise BackfillError(
                    f"{source.member_name} forecast hour is {forecast_hour}"
                )
        if "ref_date_time" in variables:
            reference_stamp = _decode_char_variable(
                variables["ref_date_time"][:]
            )
            if reference_stamp != source.initialization_time_utc.strftime(
                "%Y%m%d%H"
            ):
                raise BackfillError(
                    f"{source.member_name} reference time is {reference_stamp}"
                )
        if "valid_date_time" in variables:
            valid_stamp = _decode_char_variable(
                variables["valid_date_time"][:]
            )
            if valid_stamp != source.valid_time_utc.strftime("%Y%m%d%H"):
                raise BackfillError(
                    f"{source.member_name} valid time is {valid_stamp}"
                )

    u_grid = np.where(np.abs(u_grid) < 1e20, u_grid, np.nan)
    v_grid = np.where(np.abs(v_grid) < 1e20, v_grid, np.nan)
    rows: list[dict[str, Any]] = []
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
                "forecast_reference_time_utc": (
                    source.initialization_time_utc
                ),
                "forecast_cycle_hour_utc": np.int8(source.cycle_hour_utc),
                "forecast_lead_hours": np.int16(
                    source.forecast_lead_hours
                ),
                "valid_time_utc": source.valid_time_utc,
                "target_date": source.target_date,
                "lead_days": np.int8(source.lead_days),
                "u_wind_80m_mps": np.float32(u_value),
                "v_wind_80m_mps": np.float32(v_value),
                "wind_speed_80m_mps": np.float32(speed),
                "wind_speed_80m_kmh": np.float32(speed * 3.6),
                "wind_direction_80m_deg": np.float32(direction),
                "source_file": source.source_file,
                "retrieved_at_utc": retrieved_at_utc,
            }
        )
    inventory = {
        "dataset_id": DATASET_ID,
        "model": MODEL,
        "forecast_reference_time_utc": source.initialization_time_utc,
        "forecast_cycle_hour_utc": source.cycle_hour_utc,
        "forecast_lead_hours": source.forecast_lead_hours,
        "valid_time_utc": source.valid_time_utc,
        "target_date": source.target_date,
        "lead_days": source.lead_days,
        "source_file": source.source_file,
        "source_member": source.member_name,
        "source_status": "gdex_batch",
        "source_access_method": "GDEX batch NetCDF tar member",
        "source_url": archive_url,
        "source_response_bytes": len(payload),
        "source_response_sha256": hashlib.sha256(payload).hexdigest(),
        "retrieved_at_utc": retrieved_at_utc,
    }
    return rows, inventory


def archive_urls(files_payload: dict[str, Any]) -> tuple[str, ...]:
    urls = {
        str(item["web_path"])
        for item in files_payload.get("web_files", [])
        if item.get("web_path")
    }
    if not urls:
        raise BackfillError("completed GDEX request has no archive URL")
    return tuple(sorted(urls))


def parse_archive_once(
    url: str,
    expected: dict[str, ExpectedSource],
    *,
    timeout_seconds: float,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    set[str],
    dict[str, str],
]:
    retrieved_at = utc_now_iso()
    point_rows: list[dict[str, Any]] = []
    inventory_rows: list[dict[str, Any]] = []
    parsed: set[str] = set()
    seen: set[str] = set()
    invalid_member_errors: dict[str, str] = {}
    with requests.get(
        url,
        stream=True,
        timeout=(30, timeout_seconds),
        headers={
            "User-Agent": "braeswood-naturalgas-gdex-batch-wind/1.0"
        },
    ) as response:
        response.raise_for_status()
        response.raw.decode_content = True
        with tarfile.open(fileobj=response.raw, mode="r|*") as archive:
            for member in archive:
                if not member.isfile():
                    continue
                name = Path(member.name).name
                source = expected.get(name)
                if source is None:
                    continue
                if name in seen:
                    raise BackfillError(
                        f"duplicate member across archive: {name}"
                    )
                seen.add(name)
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise BackfillError(f"unable to read tar member {name}")
                payload = extracted.read()
                try:
                    rows, inventory = parse_netcdf_member(
                        payload,
                        source,
                        archive_url=url,
                        retrieved_at_utc=retrieved_at,
                    )
                except (
                    BackfillError,
                    IndexError,
                    KeyError,
                    OSError,
                    TypeError,
                    ValueError,
                ) as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    invalid_member_errors[name] = error
                    print(
                        f"  invalid archive member {name}; "
                        f"trying NCSS fallback later: {error}",
                        flush=True,
                    )
                    continue
                point_rows.extend(rows)
                inventory_rows.append(inventory)
                parsed.add(name)
                if len(parsed) % 250 == 0:
                    print(
                        f"  parsed {len(parsed):,}/{len(expected):,} "
                        "required NetCDF members",
                        flush=True,
                    )
    return point_rows, inventory_rows, parsed, invalid_member_errors


def parse_archives(
    urls: Iterable[str],
    expected: dict[str, ExpectedSource],
    *,
    attempts: int,
    timeout_seconds: float,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    set[str],
    dict[str, str],
]:
    all_points: list[dict[str, Any]] = []
    all_inventory: list[dict[str, Any]] = []
    all_parsed: set[str] = set()
    all_invalid_member_errors: dict[str, str] = {}
    for url in urls:
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                (
                    points,
                    inventory,
                    parsed,
                    invalid_member_errors,
                ) = parse_archive_once(
                    url,
                    expected,
                    timeout_seconds=timeout_seconds,
                )
                overlap = all_parsed.intersection(parsed)
                if overlap:
                    raise BackfillError(
                        f"duplicate members across tar files: "
                        f"{sorted(overlap)[:5]}"
                    )
                all_points.extend(points)
                all_inventory.extend(inventory)
                all_parsed.update(parsed)
                all_invalid_member_errors.update(invalid_member_errors)
                break
            except (
                requests.RequestException,
                tarfile.TarError,
                BackfillError,
                OSError,
            ) as exc:
                last_error = exc
                if attempt == attempts:
                    raise BackfillError(
                        f"failed to parse GDEX archive after {attempts} "
                        f"attempts: {url}"
                    ) from exc
                print(
                    f"  retry archive {attempt}/{attempts}: "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )
                time.sleep(min(2**attempt, 30))
        if last_error is not None and not all_parsed:
            raise BackfillError(f"unable to read archive {url}") from last_error
    return (
        all_points,
        all_inventory,
        all_parsed,
        all_invalid_member_errors,
    )


def _fallback_one(
    source: ExpectedSource,
    *,
    attempts: int,
    timeout_seconds: float,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    task = ForecastTask(
        initialization_time_utc=source.initialization_time_utc,
        lead_days=source.lead_days,
        forecast_lead_hours=source.forecast_lead_hours,
    )
    points, inventory = fetch_task(
        task,
        bbox=location_bbox(),
        attempts=attempts,
        timeout_seconds=timeout_seconds,
    )
    points["forecast_cycle_hour_utc"] = np.int8(source.cycle_hour_utc)
    unified = {
        "dataset_id": DATASET_ID,
        "model": MODEL,
        "forecast_reference_time_utc": source.initialization_time_utc,
        "forecast_cycle_hour_utc": source.cycle_hour_utc,
        "forecast_lead_hours": source.forecast_lead_hours,
        "valid_time_utc": source.valid_time_utc,
        "target_date": source.target_date,
        "lead_days": source.lead_days,
        "source_file": source.source_file,
        "source_member": source.member_name,
        "source_status": "ncss_fallback",
        "source_access_method": "THREDDS NCSS fallback",
        "source_url": inventory["ncss_request_url"],
        "source_response_bytes": inventory["ncss_response_bytes"],
        "source_response_sha256": inventory["ncss_response_sha256"],
        "retrieved_at_utc": inventory["retrieved_at_utc"],
    }
    return points, unified


def fill_missing_sources(
    missing: Iterable[ExpectedSource],
    *,
    workers: int,
    attempts: int,
    timeout_seconds: float,
) -> tuple[list[pd.DataFrame], list[dict[str, Any]], list[ExpectedSource]]:
    missing = list(missing)
    if not missing:
        return [], [], []
    print(
        f"  GDEX archive is missing {len(missing):,} required members; "
        "trying NCSS fallback",
        flush=True,
    )
    point_frames: list[pd.DataFrame] = []
    inventory_rows: list[dict[str, Any]] = []
    unavailable: list[ExpectedSource] = []
    with ThreadPoolExecutor(max_workers=min(workers, len(missing))) as executor:
        futures = {
            executor.submit(
                _fallback_one,
                source,
                attempts=attempts,
                timeout_seconds=timeout_seconds,
            ): source
            for source in missing
        }
        for future in as_completed(futures):
            source = futures[future]
            try:
                points, inventory = future.result()
            except Exception as exc:
                print(
                    f"  fallback unavailable {source.source_file}: "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )
                unavailable.append(source)
            else:
                point_frames.append(points)
                inventory_rows.append(inventory)
    return point_frames, inventory_rows, unavailable


def missing_inventory(
    source: ExpectedSource,
    *,
    source_error: str | None = None,
) -> dict[str, Any]:
    return {
        "dataset_id": DATASET_ID,
        "model": MODEL,
        "forecast_reference_time_utc": source.initialization_time_utc,
        "forecast_cycle_hour_utc": source.cycle_hour_utc,
        "forecast_lead_hours": source.forecast_lead_hours,
        "valid_time_utc": source.valid_time_utc,
        "target_date": source.target_date,
        "lead_days": source.lead_days,
        "source_file": source.source_file,
        "source_member": source.member_name,
        "source_status": "missing",
        "source_error": source_error or "source absent from GDEX and NCSS",
        "source_access_method": "GDEX batch plus NCSS fallback",
        "source_url": None,
        "source_response_bytes": 0,
        "source_response_sha256": None,
        "retrieved_at_utc": utc_now_iso(),
    }


def finalize_partition_frames(
    point_frames: list[pd.DataFrame],
    inventory_frames: list[pd.DataFrame],
    *,
    expected: dict[str, ExpectedSource],
    parsed: set[str],
    archive_url_values: tuple[str, ...],
    reported_total_bytes: int,
    fallback_workers: int,
    attempts: int,
    timeout_seconds: float,
    minimum_daily_samples: int,
    valid_hours: tuple[int, ...],
    lead_days: tuple[int, ...],
    maximum_missing_source_files: int | None = 0,
    invalid_archive_member_errors: dict[str, str] | None = None,
) -> tuple[
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    dict[str, Any],
]:
    """Finish one monthly partition from parsed or staged GDEX rows."""

    initially_missing = sorted(
        (expected[name] for name in set(expected).difference(parsed)),
        key=lambda item: (
            item.initialization_time_utc,
            item.forecast_lead_hours,
        ),
    )
    fallback_frames, fallback_inventory, unavailable = fill_missing_sources(
        initially_missing,
        workers=fallback_workers,
        attempts=attempts,
        timeout_seconds=timeout_seconds,
    )
    invalid_archive_member_errors = invalid_archive_member_errors or {}
    if (
        maximum_missing_source_files is not None
        and len(unavailable) > maximum_missing_source_files
    ):
        raise BackfillError(
            f"missing {len(unavailable):,} source files after fallback; "
            f"configured maximum is {maximum_missing_source_files:,}"
        )
    point_frames = [
        frame
        for frame in [*point_frames, *fallback_frames]
        if not frame.empty
    ]
    if not point_frames:
        raise BackfillError("GDEX request produced no required point data")
    points = pd.concat(point_frames, ignore_index=True)
    points = points.sort_values(
        [
            "forecast_reference_time_utc",
            "forecast_lead_hours",
            "location_id",
        ]
    ).reset_index(drop=True)

    for row in fallback_inventory:
        member_name = str(row.get("source_member") or "")
        if member_name in invalid_archive_member_errors:
            row["gdex_source_error"] = invalid_archive_member_errors[
                member_name
            ]
    additional_inventory = [
        *fallback_inventory,
        *(
            missing_inventory(
                item,
                source_error=invalid_archive_member_errors.get(
                    item.member_name
                ),
            )
            for item in unavailable
        ),
    ]
    inventory_frames = [
        frame for frame in inventory_frames if not frame.empty
    ]
    if additional_inventory:
        inventory_frames.append(pd.DataFrame(additional_inventory))
    if not inventory_frames:
        raise BackfillError("GDEX request produced no source inventory")
    inventory = pd.concat(inventory_frames, ignore_index=True)
    inventory = inventory.sort_values(
        ["forecast_reference_time_utc", "forecast_lead_hours"]
    ).reset_index(drop=True)

    source_key = ["forecast_reference_time_utc", "forecast_lead_hours"]
    point_key = [*source_key, "location_id"]
    if points.duplicated(point_key).any():
        raise BackfillError("duplicate point wind rows")
    if inventory.duplicated(source_key).any():
        raise BackfillError("duplicate source inventory rows")
    if len(inventory) != len(expected):
        raise BackfillError(
            f"expected {len(expected):,} inventory rows, "
            f"got {len(inventory):,}"
        )

    daily = make_daily(
        points,
        expected_samples=len(valid_hours),
        minimum_samples=minimum_daily_samples,
    )
    daily["forecast_cycle_hour_utc"] = (
        pd.to_datetime(
            daily["forecast_reference_time_utc"],
            utc=True,
        ).dt.hour.astype("int8")
    )
    city_leads, features = make_features(
        daily,
        expected_lead_days=len(lead_days),
        require_complete=not unavailable,
    )
    city_leads["forecast_cycle_hour_utc"] = (
        pd.to_datetime(
            city_leads["forecast_reference_time_utc"],
            utc=True,
        ).dt.hour.astype("int8")
    )
    features["forecast_cycle_hour_utc"] = (
        pd.to_datetime(
            features["forecast_reference_time_utc"],
            utc=True,
        ).dt.hour.astype("int8")
    )
    diagnostics = {
        "expected_source_files": len(expected),
        "gdex_batch_source_files": len(parsed),
        "ncss_fallback_source_files": len(fallback_inventory),
        "missing_source_files": len(unavailable),
        "missing_source_file_names": [
            item.source_file for item in unavailable
        ],
        "maximum_accepted_missing_source_files": (
            int(maximum_missing_source_files)
            if maximum_missing_source_files is not None
            else None
        ),
        "invalid_archive_member_count": len(
            invalid_archive_member_errors
        ),
        "invalid_archive_members": [
            {"source_member": name, "error": error}
            for name, error in sorted(
                invalid_archive_member_errors.items()
            )
        ],
        "invalid_archive_members_recovered_by_fallback": sum(
            str(row.get("source_member") or "")
            in invalid_archive_member_errors
            for row in fallback_inventory
        ),
        "invalid_archive_members_still_missing": sum(
            item.member_name in invalid_archive_member_errors
            for item in unavailable
        ),
        "daily_complete_groups": int(
            daily["wind_sample_complete"].sum()
        ),
        "daily_incomplete_groups": int(
            (~daily["wind_sample_complete"]).sum()
        ),
        "incomplete_feature_rows": int(
            (
                ~features["gfs_lead_count"].eq(len(lead_days))
                | ~features["gfs_min_locations"].eq(len(LOCATIONS))
            ).sum()
        ),
        "archive_urls": list(archive_url_values),
        "gdex_reported_total_bytes": int(reported_total_bytes),
    }
    return points, inventory, daily, city_leads, features, diagnostics


def build_partition_frames(
    files_payload: dict[str, Any],
    *,
    start: date,
    end: date,
    cycles: tuple[int, ...],
    lead_days: tuple[int, ...],
    valid_hours: tuple[int, ...],
    attempts: int,
    timeout_seconds: float,
    fallback_workers: int,
    minimum_daily_samples: int,
    maximum_missing_source_files: int | None = 0,
) -> tuple[
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    dict[str, Any],
]:
    expected = expected_sources(
        start,
        end,
        cycles=cycles,
        lead_days=lead_days,
        valid_hours=valid_hours,
    )
    urls = archive_urls(files_payload)
    rows, inventories, parsed, invalid_member_errors = parse_archives(
        urls,
        expected,
        attempts=attempts,
        timeout_seconds=timeout_seconds,
    )
    return finalize_partition_frames(
        [pd.DataFrame(rows)],
        [pd.DataFrame(inventories)],
        expected=expected,
        parsed=parsed,
        archive_url_values=urls,
        reported_total_bytes=int(files_payload.get("total_size") or 0),
        fallback_workers=fallback_workers,
        attempts=attempts,
        timeout_seconds=timeout_seconds,
        minimum_daily_samples=minimum_daily_samples,
        valid_hours=valid_hours,
        lead_days=lead_days,
        maximum_missing_source_files=maximum_missing_source_files,
        invalid_archive_member_errors=invalid_member_errors,
    )


def write_partition_outputs(
    fs: gcsfs.GCSFileSystem,
    *,
    request_id: str,
    request_start: date,
    request_end: date,
    start: date,
    end: date,
    cycles: tuple[int, ...],
    lead_days: tuple[int, ...],
    valid_hours: tuple[int, ...],
    minimum_daily_samples: int,
    bucket: str,
    raw_prefix: str,
    processed_prefix: str,
    points: pd.DataFrame,
    inventory: pd.DataFrame,
    daily: pd.DataFrame,
    city_leads: pd.DataFrame,
    features: pd.DataFrame,
    diagnostics: dict[str, Any],
) -> dict[str, Any]:
    """Write one monthly output partition from any GDEX request layout."""

    keys = partition_keys(
        bucket=bucket,
        raw_prefix=raw_prefix,
        processed_prefix=processed_prefix,
        year=start.year,
        month=start.month,
    )
    byte_counts = {
        "raw_points": write_parquet(fs, keys["raw_points"], points),
        "source_inventory": write_parquet(
            fs,
            keys["source_inventory"],
            inventory,
        ),
        "daily": write_parquet(fs, keys["daily"], daily),
        "city_leads": write_parquet(
            fs,
            keys["city_leads"],
            city_leads,
        ),
        "features": write_parquet(fs, keys["features"], features),
    }
    missing_source_files = int(diagnostics["missing_source_files"])
    manifest = {
        "status": (
            "complete_with_missing_sources"
            if missing_source_files
            else "complete"
        ),
        "dataset_id": DATASET_ID,
        "model": MODEL,
        "source_service": GDEX_SOURCE_SERVICE,
        "source_dataset_url": (
            f"https://gdex.ucar.edu/datasets/{DATASET_ID}/"
        ),
        "gdex_request_id": request_id,
        "gdex_request_scope_start": request_start.isoformat(),
        "gdex_request_scope_end": request_end.isoformat(),
        "coverage_start_issue_date": start.isoformat(),
        "coverage_end_issue_date": end.isoformat(),
        "cycle_hours_utc": list(cycles),
        "lead_days": list(lead_days),
        "valid_hours_utc": list(valid_hours),
        "requested_product_lead_hours": list(
            requested_product_leads(cycles, lead_days, valid_hours)
        ),
        "height_m": 80,
        "locations": len(LOCATIONS),
        "bbox": asdict(location_bbox()),
        "minimum_daily_samples": minimum_daily_samples,
        "rows": {
            "raw_points": len(points),
            "source_inventory": len(inventory),
            "daily": len(daily),
            "city_leads": len(city_leads),
            "features": len(features),
        },
        "diagnostics": diagnostics,
        "missing_data_policy": (
            "Missing source files are retained in source_inventory with "
            "source_status=missing. Feature rows may use fewer than five "
            "lead days and expose the actual count in gfs_lead_count."
            if missing_source_files
            else "Strict source completeness"
        ),
        "gcs_keys": {
            name: f"gs://{key}" for name, key in keys.items()
        },
        "gcs_object_bytes": byte_counts,
        "created_at_utc": utc_now_iso(),
        "feature_definition": (
            "For each GFS initialization, equal-weight mean 80 m wind "
            "speed across the configured locations and calendar target "
            "days 1-5; target days use valid hours 00/06/12/18 UTC."
        ),
        "historical_availability_verified": True,
    }
    byte_counts["manifest"] = write_json(
        fs,
        keys["manifest"],
        manifest,
    )
    print(
        f"uploaded {start:%Y-%m}: raw={len(points):,}, "
        f"daily={len(daily):,}, features={len(features):,}, "
        f"missing_sources={diagnostics['missing_source_files']}",
        flush=True,
    )
    return {
        "status": "uploaded",
        "keys": keys,
        "rows": manifest["rows"],
        "diagnostics": diagnostics,
    }


def upload_completed_partition(
    fs: gcsfs.GCSFileSystem,
    *,
    request_id: str,
    files_payload: dict[str, Any],
    start: date,
    end: date,
    cycles: tuple[int, ...],
    lead_days: tuple[int, ...],
    valid_hours: tuple[int, ...],
    attempts: int,
    timeout_seconds: float,
    fallback_workers: int,
    minimum_daily_samples: int,
    bucket: str,
    raw_prefix: str,
    processed_prefix: str,
    force: bool,
    maximum_missing_source_files: int | None = 0,
) -> dict[str, Any]:
    keys = partition_keys(
        bucket=bucket,
        raw_prefix=raw_prefix,
        processed_prefix=processed_prefix,
        year=start.year,
        month=start.month,
    )
    if all(fs.exists(key) for key in keys.values()) and not force:
        return {"status": "skipped", "keys": keys}
    (
        points,
        inventory,
        daily,
        city_leads,
        features,
        diagnostics,
    ) = build_partition_frames(
        files_payload,
        start=start,
        end=end,
        cycles=cycles,
        lead_days=lead_days,
        valid_hours=valid_hours,
        attempts=attempts,
        timeout_seconds=timeout_seconds,
        fallback_workers=fallback_workers,
        minimum_daily_samples=minimum_daily_samples,
        maximum_missing_source_files=maximum_missing_source_files,
    )
    return write_partition_outputs(
        fs,
        request_id=request_id,
        request_start=start,
        request_end=end,
        start=start,
        end=end,
        cycles=cycles,
        lead_days=lead_days,
        valid_hours=valid_hours,
        minimum_daily_samples=minimum_daily_samples,
        bucket=bucket,
        raw_prefix=raw_prefix,
        processed_prefix=processed_prefix,
        points=points,
        inventory=inventory,
        daily=daily,
        city_leads=city_leads,
        features=features,
        diagnostics=diagnostics,
    )


def _stage_archive_once(
    url: str,
    expected: dict[str, ExpectedSource],
    *,
    stage_root: Path,
    timeout_seconds: float,
    maximum_buffered_point_rows: int = 200_000,
) -> tuple[
    dict[tuple[int, int], dict[str, list[Path]]],
    set[str],
]:
    """Stream one range archive into small local month-specific Parquets."""

    retrieved_at = utc_now_iso()
    point_buffers: dict[tuple[int, int], list[dict[str, Any]]] = (
        defaultdict(list)
    )
    inventory_buffers: dict[
        tuple[int, int],
        list[dict[str, Any]],
    ] = defaultdict(list)
    staged: dict[tuple[int, int], dict[str, list[Path]]] = defaultdict(
        lambda: {"points": [], "inventory": []}
    )
    chunk_counts: dict[tuple[int, int], int] = defaultdict(int)
    parsed: set[str] = set()
    buffered_point_rows = 0

    def flush_month(month: tuple[int, int]) -> None:
        nonlocal buffered_point_rows
        point_rows = point_buffers.pop(month, [])
        inventory_rows = inventory_buffers.pop(month, [])
        if not point_rows:
            return
        month_dir = (
            stage_root
            / f"year={month[0]:04d}"
            / f"month={month[1]:02d}"
        )
        month_dir.mkdir(parents=True, exist_ok=True)
        chunk = chunk_counts[month]
        chunk_counts[month] += 1
        point_path = month_dir / f"points-{chunk:05d}.parquet"
        inventory_path = (
            month_dir / f"inventory-{chunk:05d}.parquet"
        )
        pd.DataFrame(point_rows).to_parquet(
            point_path,
            index=False,
            compression="zstd",
        )
        pd.DataFrame(inventory_rows).to_parquet(
            inventory_path,
            index=False,
            compression="zstd",
        )
        staged[month]["points"].append(point_path)
        staged[month]["inventory"].append(inventory_path)
        buffered_point_rows -= len(point_rows)

    with requests.get(
        url,
        stream=True,
        timeout=(30, timeout_seconds),
        headers={
            "User-Agent": "braeswood-naturalgas-gdex-batch-wind/1.0"
        },
    ) as response:
        response.raise_for_status()
        response.raw.decode_content = True
        with tarfile.open(fileobj=response.raw, mode="r|*") as archive:
            for member in archive:
                if not member.isfile():
                    continue
                name = Path(member.name).name
                source = expected.get(name)
                if source is None:
                    continue
                if name in parsed:
                    raise BackfillError(
                        f"duplicate member in archive: {name}"
                    )
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise BackfillError(
                        f"unable to read tar member {name}"
                    )
                payload = extracted.read()
                rows, inventory = parse_netcdf_member(
                    payload,
                    source,
                    archive_url=url,
                    retrieved_at_utc=retrieved_at,
                )
                month = (
                    source.initialization_time_utc.year,
                    source.initialization_time_utc.month,
                )
                point_buffers[month].extend(rows)
                inventory_buffers[month].append(inventory)
                buffered_point_rows += len(rows)
                parsed.add(name)
                if (
                    buffered_point_rows
                    >= maximum_buffered_point_rows
                ):
                    largest_month = max(
                        point_buffers,
                        key=lambda item: len(point_buffers[item]),
                    )
                    flush_month(largest_month)
                if len(parsed) % 2_500 == 0:
                    print(
                        f"  staged {len(parsed):,}/{len(expected):,} "
                        "required NetCDF members",
                        flush=True,
                    )
    for month in list(point_buffers):
        flush_month(month)
    return dict(staged), parsed


def parse_archives_to_stage(
    urls: Iterable[str],
    expected: dict[str, ExpectedSource],
    *,
    stage_root: Path,
    attempts: int,
    timeout_seconds: float,
) -> tuple[
    dict[tuple[int, int], dict[str, list[Path]]],
    set[str],
]:
    """Stage all range-request archives once, partitioned by issue month."""

    all_staged: dict[
        tuple[int, int],
        dict[str, list[Path]],
    ] = defaultdict(lambda: {"points": [], "inventory": []})
    all_parsed: set[str] = set()
    for archive_index, url in enumerate(urls):
        for attempt in range(1, attempts + 1):
            attempt_root = Path(
                tempfile.mkdtemp(
                    prefix=(
                        f"archive-{archive_index:04d}-"
                        f"attempt-{attempt}-"
                    ),
                    dir=stage_root,
                )
            )
            try:
                staged, parsed = _stage_archive_once(
                    url,
                    expected,
                    stage_root=attempt_root,
                    timeout_seconds=timeout_seconds,
                )
                overlap = all_parsed.intersection(parsed)
                if overlap:
                    raise BackfillError(
                        "duplicate members across range tar files: "
                        f"{sorted(overlap)[:5]}"
                    )
            except (
                requests.RequestException,
                tarfile.TarError,
                BackfillError,
                OSError,
            ) as exc:
                shutil.rmtree(attempt_root, ignore_errors=True)
                if attempt == attempts:
                    raise BackfillError(
                        f"failed to stage GDEX archive after "
                        f"{attempts} attempts: {url}"
                    ) from exc
                print(
                    f"  retry range archive {attempt}/{attempts}: "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )
                time.sleep(min(2**attempt, 30))
                continue
            for month, paths in staged.items():
                all_staged[month]["points"].extend(paths["points"])
                all_staged[month]["inventory"].extend(
                    paths["inventory"]
                )
            all_parsed.update(parsed)
            print(
                f"  staged archive {archive_index + 1}: "
                f"total members={len(all_parsed):,}",
                flush=True,
            )
            break
    return dict(all_staged), all_parsed


def upload_completed_range(
    fs: gcsfs.GCSFileSystem,
    *,
    request_id: str,
    files_payload: dict[str, Any],
    ranges: list[tuple[date, date]],
    request_start: date,
    request_end: date,
    cycles: tuple[int, ...],
    lead_days: tuple[int, ...],
    valid_hours: tuple[int, ...],
    attempts: int,
    timeout_seconds: float,
    fallback_workers: int,
    minimum_daily_samples: int,
    bucket: str,
    raw_prefix: str,
    processed_prefix: str,
    state_key: str,
    state: dict[str, Any],
) -> dict[str, Any]:
    """Ingest one multi-year request into the existing monthly schema."""

    expected_by_month: dict[
        tuple[int, int],
        dict[str, ExpectedSource],
    ] = {}
    expected: dict[str, ExpectedSource] = {}
    for start, end in ranges:
        monthly = expected_sources(
            start,
            end,
            cycles=cycles,
            lead_days=lead_days,
            valid_hours=valid_hours,
        )
        expected_by_month[(start.year, start.month)] = monthly
        overlap = set(expected).intersection(monthly)
        if overlap:
            raise BackfillError(
                f"overlapping monthly expected sources: "
                f"{sorted(overlap)[:5]}"
            )
        expected.update(monthly)

    urls = archive_urls(files_payload)
    print(
        f"range request {request_id} completed: "
        f"archives={len(urls):,}, required_members={len(expected):,}; "
        "streaming once into monthly staging",
        flush=True,
    )
    uploaded: list[str] = []
    with tempfile.TemporaryDirectory(
        prefix="ncar-gdex-wind-range-"
    ) as temporary:
        staged, parsed = parse_archives_to_stage(
            urls,
            expected,
            stage_root=Path(temporary),
            attempts=attempts,
            timeout_seconds=timeout_seconds,
        )
        for start, end in ranges:
            month = (start.year, start.month)
            paths = staged.get(
                month,
                {"points": [], "inventory": []},
            )
            point_frames = [
                pd.read_parquet(path) for path in paths["points"]
            ]
            inventory_frames = [
                pd.read_parquet(path)
                for path in paths["inventory"]
            ]
            monthly_expected = expected_by_month[month]
            monthly_parsed = set(monthly_expected).intersection(parsed)
            (
                points,
                inventory,
                daily,
                city_leads,
                features,
                diagnostics,
            ) = finalize_partition_frames(
                point_frames,
                inventory_frames,
                expected=monthly_expected,
                parsed=monthly_parsed,
                archive_url_values=urls,
                reported_total_bytes=int(
                    files_payload.get("total_size") or 0
                ),
                fallback_workers=fallback_workers,
                attempts=attempts,
                timeout_seconds=timeout_seconds,
                minimum_daily_samples=minimum_daily_samples,
                valid_hours=valid_hours,
                lead_days=lead_days,
            )
            write_partition_outputs(
                fs,
                request_id=request_id,
                request_start=request_start,
                request_end=request_end,
                start=start,
                end=end,
                cycles=cycles,
                lead_days=lead_days,
                valid_hours=valid_hours,
                minimum_daily_samples=minimum_daily_samples,
                bucket=bucket,
                raw_prefix=raw_prefix,
                processed_prefix=processed_prefix,
                points=points,
                inventory=inventory,
                daily=daily,
                city_leads=city_leads,
                features=features,
                diagnostics=diagnostics,
            )
            month_label = f"{start:%Y-%m}"
            uploaded.append(month_label)
            state["ingested_partitions"] = uploaded
            state["updated_at_utc"] = utc_now_iso()
            write_json(fs, state_key, state)
    return {
        "status": "uploaded",
        "request_scope_start": request_start.isoformat(),
        "request_scope_end": request_end.isoformat(),
        "monthly_partitions": uploaded,
        "monthly_partition_count": len(uploaded),
        "parsed_source_members": len(parsed),
        "archive_count": len(urls),
    }


def purge_request(api: GdexApi, request_id: str) -> None:
    api._json_request("DELETE", f"purge/{request_id}")


def is_open_request_limit_error(error: Exception) -> bool:
    message = str(error).lower()
    return "open request" in message and (
        "more than" in message
        or "maximum" in message
        or "limit" in message
    )


def is_request_not_found_error(error: Exception) -> bool:
    return "request index not found" in str(error).lower()


def recover_failed_range_request(
    *,
    api: GdexApi,
    fs: gcsfs.GCSFileSystem,
    state: dict[str, Any],
    state_key: str,
    failed_request_id: str,
    failed_status: str,
    start: date,
    end: date,
    cycles: tuple[int, ...],
    lead_days: tuple[int, ...],
    valid_hours: tuple[int, ...],
    poll_seconds: float,
) -> dict[str, Any]:
    """Purge a terminal request and resubmit the same range when possible."""

    history = list(state.get("failed_requests", []))
    history.append(
        {
            "request_id": failed_request_id,
            "status": failed_status,
            "detected_at_utc": utc_now_iso(),
        }
    )
    state["failed_requests"] = history
    state["request_id"] = None
    state["status"] = (
        f"Waiting for failed request {failed_request_id} to purge"
    )
    state["updated_at_utc"] = utc_now_iso()
    write_json(fs, state_key, state)
    try:
        purge_request(api, failed_request_id)
    except NcarDataError as exc:
        if not is_request_not_found_error(exc):
            raise

    while True:
        try:
            payload = response_data(api.status(failed_request_id))
        except NcarDataError as exc:
            if is_request_not_found_error(exc):
                break
            raise
        remote_status = str(payload.get("status", "unknown"))
        state["status"] = (
            f"Waiting for failed request {failed_request_id} "
            f"to purge ({remote_status})"
        )
        state["updated_at_utc"] = utc_now_iso()
        write_json(fs, state_key, state)
        print(state["status"], flush=True)
        time.sleep(poll_seconds)

    while True:
        try:
            return submit_range_request(
                api,
                fs,
                start=start,
                end=end,
                cycles=cycles,
                lead_days=lead_days,
                valid_hours=valid_hours,
                state_key=state_key,
            )
        except NcarDataError as exc:
            if not is_open_request_limit_error(exc):
                raise
            state["status"] = "Waiting for a GDEX request slot"
            state["updated_at_utc"] = utc_now_iso()
            write_json(fs, state_key, state)
            print(state["status"], flush=True)
            time.sleep(poll_seconds)


def report_bulk_status(
    fs: gcsfs.GCSFileSystem,
    *,
    bucket: str,
    raw_prefix: str,
    expected_start: date,
    expected_end: date,
) -> int:
    result = report_status(
        fs,
        bucket=bucket,
        raw_prefix=raw_prefix,
        expected_start=expected_start,
        expected_end=expected_end,
    )
    pattern = f"{bucket}/{raw_prefix}/year=*/month=*/gdex_request.json"
    states = []
    for key in sorted(fs.glob(pattern)):
        with fs.open(key, "rb") as handle:
            states.append(json.load(handle))
    range_pattern = (
        f"{bucket}/{raw_prefix}/_range_requests/*.json"
    )
    for key in sorted(fs.glob(range_pattern)):
        with fs.open(key, "rb") as handle:
            states.append(json.load(handle))
    annual_pattern = (
        f"{bucket}/{raw_prefix}/_annual_requests/*.json"
    )
    for key in sorted(fs.glob(annual_pattern)):
        with fs.open(key, "rb") as handle:
            states.append(json.load(handle))
    active = [
        state
        for state in states
        if str(state.get("status", "")).lower()
        not in {"ingested", "purged"}
    ]
    if active:
        print("active GDEX requests:")
        for state in active[:12]:
            print(
                f"  {state['partition_start']}.."
                f"{state['partition_end']} "
                f"request={state.get('request_id') or 'pending'} "
                f"status={state.get('status', 'unknown')}"
            )
    else:
        print("active GDEX requests: none")
    return result


def run_single_request_backfill(
    *,
    fs: gcsfs.GCSFileSystem,
    api: GdexApi,
    ranges: list[tuple[date, date]],
    cycles: tuple[int, ...],
    lead_days: tuple[int, ...],
    valid_hours: tuple[int, ...],
    poll_seconds: float,
    attempts: int,
    timeout_seconds: float,
    fallback_workers: int,
    minimum_daily_samples: int,
    bucket: str,
    raw_prefix: str,
    processed_prefix: str,
    force: bool,
    purge_completed: bool,
    existing_request_id: str | None,
) -> None:
    """Submit and ingest all incomplete months through one remote request."""

    completed_manifest_keys = set(
        fs.glob(f"{bucket}/{raw_prefix}/year=*/month=*/manifest.json")
    )
    incomplete: list[tuple[date, date]] = []
    for start, end in ranges:
        keys = partition_keys(
            bucket=bucket,
            raw_prefix=raw_prefix,
            processed_prefix=processed_prefix,
            year=start.year,
            month=start.month,
        )
        if keys["manifest"] in completed_manifest_keys and not force:
            print(f"skip complete partition {start:%Y-%m}", flush=True)
        else:
            incomplete.append((start, end))
    if not incomplete:
        print("all requested monthly partitions are complete", flush=True)
        return

    request_start = incomplete[0][0]
    request_end = incomplete[-1][1]
    state_key = range_request_state_key(
        bucket=bucket,
        raw_prefix=raw_prefix,
        start=request_start,
        end=request_end,
    )
    state = read_json(fs, state_key)
    if state is None:
        state = {
            "dataset_id": DATASET_ID,
            "request_id": None,
            "request_layout": "single_remaining_range",
            "partition_start": request_start.isoformat(),
            "partition_end": request_end.isoformat(),
            "cycles_utc": list(cycles),
            "lead_days": list(lead_days),
            "valid_hours_utc": list(valid_hours),
            "status": "Waiting for GDEX request slot",
            "submitted_at_utc": None,
            "updated_at_utc": utc_now_iso(),
        }
        write_json(fs, state_key, state)

    if existing_request_id:
        if state.get("request_id") not in {None, existing_request_id}:
            raise ValueError(
                "range state already contains a different request id"
            )
        state["request_id"] = existing_request_id
        state["status"] = "Existing request"
        state["updated_at_utc"] = utc_now_iso()
        write_json(fs, state_key, state)

    while not state.get("request_id"):
        try:
            state = submit_range_request(
                api,
                fs,
                start=request_start,
                end=request_end,
                cycles=cycles,
                lead_days=lead_days,
                valid_hours=valid_hours,
                state_key=state_key,
            )
        except NcarDataError as exc:
            if not is_open_request_limit_error(exc):
                raise
            state["status"] = "Waiting for GDEX request slot"
            state["updated_at_utc"] = utc_now_iso()
            write_json(fs, state_key, state)
            print(
                "GDEX open-request limit reached; retaining the "
                f"single request {request_start.isoformat()}.."
                f"{request_end.isoformat()} and retrying after "
                f"{poll_seconds:g} seconds",
                flush=True,
            )
            time.sleep(poll_seconds)

    request_id = str(state["request_id"])
    while True:
        status_payload = response_data(api.status(request_id))
        status = str(status_payload.get("status", "unknown"))
        if status != state.get("status"):
            state["status"] = status
            state["updated_at_utc"] = utc_now_iso()
            write_json(fs, state_key, state)
            print(
                f"single request {request_id} status={status}",
                flush=True,
            )
        normalized = status.lower()
        if normalized == "completed":
            break
        if normalized in TERMINAL_FAILURE_STATUSES:
            print(
                f"GDEX request {request_id} ended with {status}; "
                "starting automatic purge and resubmission",
                flush=True,
            )
            state = recover_failed_range_request(
                api=api,
                fs=fs,
                state=state,
                state_key=state_key,
                failed_request_id=request_id,
                failed_status=status,
                start=request_start,
                end=request_end,
                cycles=cycles,
                lead_days=lead_days,
                valid_hours=valid_hours,
                poll_seconds=poll_seconds,
            )
            request_id = str(state["request_id"])
            continue
        time.sleep(poll_seconds)

    files_payload = response_data(api.files(request_id))
    result = upload_completed_range(
        fs,
        request_id=request_id,
        files_payload=files_payload,
        ranges=incomplete,
        request_start=request_start,
        request_end=request_end,
        cycles=cycles,
        lead_days=lead_days,
        valid_hours=valid_hours,
        attempts=attempts,
        timeout_seconds=timeout_seconds,
        fallback_workers=fallback_workers,
        minimum_daily_samples=minimum_daily_samples,
        bucket=bucket,
        raw_prefix=raw_prefix,
        processed_prefix=processed_prefix,
        state_key=state_key,
        state=state,
    )
    state["status"] = "Ingested"
    state["ingested_at_utc"] = utc_now_iso()
    state["result"] = result
    if purge_completed:
        purge_request(api, request_id)
        state["remote_request_purged"] = True
    write_json(fs, state_key, state)


def run_backfill(
    *,
    fs: gcsfs.GCSFileSystem,
    api: GdexApi,
    ranges: list[tuple[date, date]],
    cycles: tuple[int, ...],
    lead_days: tuple[int, ...],
    valid_hours: tuple[int, ...],
    max_open_requests: int,
    poll_seconds: float,
    attempts: int,
    timeout_seconds: float,
    fallback_workers: int,
    minimum_daily_samples: int,
    bucket: str,
    raw_prefix: str,
    processed_prefix: str,
    force: bool,
    purge_completed: bool,
    existing_request_id: str | None,
) -> None:
    pending: list[tuple[date, date]] = []
    active: dict[tuple[int, int], dict[str, Any]] = {}
    completed_manifest_keys = set(
        fs.glob(f"{bucket}/{raw_prefix}/year=*/month=*/manifest.json")
    )
    for start, end in ranges:
        keys = partition_keys(
            bucket=bucket,
            raw_prefix=raw_prefix,
            processed_prefix=processed_prefix,
            year=start.year,
            month=start.month,
        )
        if keys["manifest"] in completed_manifest_keys and not force:
            print(f"skip complete partition {start:%Y-%m}", flush=True)
            continue
        state_key = request_state_key(
            bucket=bucket,
            raw_prefix=raw_prefix,
            year=start.year,
            month=start.month,
        )
        state = read_json(fs, state_key)
        if state and state.get("request_id"):
            active[(start.year, start.month)] = {
                "start": start,
                "end": end,
                "state": state,
                "state_key": state_key,
            }
        else:
            pending.append((start, end))

    if existing_request_id:
        if len(ranges) != 1:
            raise ValueError(
                "--existing-request-id requires exactly one partition"
            )
        start, end = ranges[0]
        state_key = request_state_key(
            bucket=bucket,
            raw_prefix=raw_prefix,
            year=start.year,
            month=start.month,
        )
        state = {
            "dataset_id": DATASET_ID,
            "request_id": existing_request_id,
            "partition_start": start.isoformat(),
            "partition_end": end.isoformat(),
            "cycles_utc": list(cycles),
            "lead_days": list(lead_days),
            "valid_hours_utc": list(valid_hours),
            "status": "Existing request",
            "submitted_at_utc": None,
            "updated_at_utc": utc_now_iso(),
        }
        write_json(fs, state_key, state)
        active = {
            (start.year, start.month): {
                "start": start,
                "end": end,
                "state": state,
                "state_key": state_key,
            }
        }
        pending = []

    while pending or active:
        while pending and len(active) < max_open_requests:
            start, end = pending[0]
            try:
                state = submit_partition_request(
                    api,
                    fs,
                    start=start,
                    end=end,
                    cycles=cycles,
                    lead_days=lead_days,
                    valid_hours=valid_hours,
                    bucket=bucket,
                    raw_prefix=raw_prefix,
                )
            except NcarDataError as exc:
                if not is_open_request_limit_error(exc):
                    raise
                print(
                    "GDEX open-request limit reached; retaining "
                    f"{start:%Y-%m} in the pending queue and polling "
                    "existing requests before retrying",
                    flush=True,
                )
                break
            pending.pop(0)
            state_key = request_state_key(
                bucket=bucket,
                raw_prefix=raw_prefix,
                year=start.year,
                month=start.month,
            )
            active[(start.year, start.month)] = {
                "start": start,
                "end": end,
                "state": state,
                "state_key": state_key,
            }

        completed: list[tuple[int, int]] = []
        for partition, job in list(active.items()):
            request_id = str(job["state"]["request_id"])
            status_payload = response_data(api.status(request_id))
            status = str(status_payload.get("status", "unknown"))
            if status != job["state"].get("status"):
                job["state"]["status"] = status
                job["state"]["updated_at_utc"] = utc_now_iso()
                write_json(fs, job["state_key"], job["state"])
                print(
                    f"{job['start']:%Y-%m}: request {request_id} "
                    f"status={status}",
                    flush=True,
                )
            normalized = status.lower()
            if normalized == "completed":
                completed.append(partition)
            elif normalized in TERMINAL_FAILURE_STATUSES:
                raise BackfillError(
                    f"GDEX request {request_id} ended with {status}"
                )

        if not completed:
            time.sleep(poll_seconds)
            continue

        for partition in completed:
            job = active[partition]
            request_id = str(job["state"]["request_id"])
            files_payload = response_data(api.files(request_id))
            result = upload_completed_partition(
                fs,
                request_id=request_id,
                files_payload=files_payload,
                start=job["start"],
                end=job["end"],
                cycles=cycles,
                lead_days=lead_days,
                valid_hours=valid_hours,
                attempts=attempts,
                timeout_seconds=timeout_seconds,
                fallback_workers=fallback_workers,
                minimum_daily_samples=minimum_daily_samples,
                bucket=bucket,
                raw_prefix=raw_prefix,
                processed_prefix=processed_prefix,
                force=force,
            )
            job["state"]["status"] = "Ingested"
            job["state"]["ingested_at_utc"] = utc_now_iso()
            job["state"]["result"] = result
            if purge_completed:
                purge_request(api, request_id)
                job["state"]["remote_request_purged"] = True
            write_json(fs, job["state_key"], job["state"])
            del active[partition]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=parse_date, default=DEFAULT_START)
    parser.add_argument("--end", type=parse_date, default=DEFAULT_END)
    parser.add_argument("--bucket", default=BUCKET)
    parser.add_argument("--raw-prefix", default=RAW_PREFIX)
    parser.add_argument("--processed-prefix", default=PROCESSED_PREFIX)
    parser.add_argument("--cycles", type=parse_cycles, default=DEFAULT_CYCLES)
    parser.add_argument(
        "--lead-days",
        default=",".join(map(str, DEFAULT_LEAD_DAYS)),
    )
    parser.add_argument(
        "--valid-hours",
        default=",".join(map(str, DEFAULT_VALID_HOURS)),
    )
    parser.add_argument("--max-open-requests", type=int, default=4)
    parser.add_argument("--poll-seconds", type=float, default=30.0)
    parser.add_argument("--attempts", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--fallback-workers", type=int, default=4)
    parser.add_argument("--minimum-daily-samples", type=int, default=1)
    parser.add_argument("--max-partitions", type=int)
    parser.add_argument("--existing-request-id")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--execute",
        action="store_true",
        help=(
            "Explicitly authorize GDEX requests, GCS writes, and optional "
            "remote-request cleanup. Without this flag only --status is allowed."
        ),
    )
    parser.add_argument(
        "--single-request",
        action="store_true",
        help=(
            "submit every incomplete month as one GDEX date-range "
            "request, then stream and write the usual monthly GCS "
            "partitions"
        ),
    )
    parser.add_argument(
        "--keep-remote-requests",
        action="store_true",
        help="do not purge successfully ingested GDEX requests",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="report GCS partitions and outstanding batch requests",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        lead_days = parse_int_csv(
            args.lead_days,
            minimum=1,
            maximum=5,
        )
        valid_hours = parse_int_csv(
            args.valid_hours,
            minimum=0,
            maximum=23,
        )
        cycles = (
            args.cycles
            if isinstance(args.cycles, tuple)
            else parse_cycles(args.cycles)
        )
        if args.start > args.end:
            raise ValueError("start must not be after end")
        if not 1 <= args.max_open_requests <= MAX_OPEN_REQUESTS:
            raise ValueError(
                f"--max-open-requests must be between 1 and "
                f"{MAX_OPEN_REQUESTS}"
            )
        if args.poll_seconds < 5:
            raise ValueError("--poll-seconds must be at least 5")
        if args.attempts < 1:
            raise ValueError("--attempts must be at least 1")
        if args.fallback_workers < 1:
            raise ValueError("--fallback-workers must be at least 1")
        if not 1 <= args.minimum_daily_samples <= len(valid_hours):
            raise ValueError(
                "--minimum-daily-samples must be between 1 and the "
                "number of valid hours"
            )
        ranges = month_ranges(args.start, args.end)
        if args.max_partitions is not None:
            if args.max_partitions < 1:
                raise ValueError("--max-partitions must be at least 1")
            ranges = ranges[: args.max_partitions]
        if not args.status and not args.execute:
            parser.error(
                "refusing remote requests/GCS writes without --execute"
            )
        fs = gcsfs.GCSFileSystem()
        if args.status:
            return report_bulk_status(
                fs,
                bucket=args.bucket,
                raw_prefix=args.raw_prefix,
                expected_start=args.start,
                expected_end=args.end,
            )
        print(
            f"GDEX batch wind backfill: {args.start}..{args.end}, "
            f"partitions={len(ranges)}, cycles={cycles}, "
            f"layout={'single-request' if args.single_request else 'monthly'}, "
            f"max_open_requests={args.max_open_requests}",
            flush=True,
        )
        print(
            f"raw=gs://{args.bucket}/{args.raw_prefix}\n"
            f"processed=gs://{args.bucket}/{args.processed_prefix}",
            flush=True,
        )
        api = GdexApi(
            HttpClient(
                timeout_seconds=min(args.timeout, 180.0),
                retries=args.attempts,
            ),
            token_from_environment(),
        )
        if args.single_request:
            run_single_request_backfill(
                fs=fs,
                api=api,
                ranges=ranges,
                cycles=cycles,
                lead_days=lead_days,
                valid_hours=valid_hours,
                poll_seconds=args.poll_seconds,
                attempts=args.attempts,
                timeout_seconds=args.timeout,
                fallback_workers=args.fallback_workers,
                minimum_daily_samples=args.minimum_daily_samples,
                bucket=args.bucket,
                raw_prefix=args.raw_prefix,
                processed_prefix=args.processed_prefix,
                force=args.force,
                purge_completed=not args.keep_remote_requests,
                existing_request_id=args.existing_request_id,
            )
        else:
            run_backfill(
                fs=fs,
                api=api,
                ranges=ranges,
                cycles=cycles,
                lead_days=lead_days,
                valid_hours=valid_hours,
                max_open_requests=args.max_open_requests,
                poll_seconds=args.poll_seconds,
                attempts=args.attempts,
                timeout_seconds=args.timeout,
                fallback_workers=args.fallback_workers,
                minimum_daily_samples=args.minimum_daily_samples,
                bucket=args.bucket,
                raw_prefix=args.raw_prefix,
                processed_prefix=args.processed_prefix,
                force=args.force,
                purge_completed=not args.keep_remote_requests,
                existing_request_id=args.existing_request_id,
            )
        print("GDEX batch wind backfill finished", flush=True)
        return 0
    except (
        BackfillError,
        NcarDataError,
        ValueError,
        requests.RequestException,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
