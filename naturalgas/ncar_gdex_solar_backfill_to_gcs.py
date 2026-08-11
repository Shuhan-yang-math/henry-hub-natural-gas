#!/usr/bin/env python3
"""Solar-specific GDEX batch parsing and monthly Parquet generation.

This module is an adapter for the durable monthly GDEX scheduler.  It requests
surface downward short-wave radiation, total-atmosphere cloud cover, and 2 m
temperature.  Four non-overlapping six-hour radiation intervals are retained
for each target day, so energy can be derived without double counting.
"""

from __future__ import annotations

import hashlib
import io
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode

import gcsfs
import numpy as np
import pandas as pd
import requests
from scipy.io import netcdf_file

from naturalgas import ncar_gdex_bulk_wind_backfill_to_gcs as _wind
from naturalgas.ncar_gdex_gfs_wind import ncss_url
from naturalgas.ncar_gdex_wind_backfill_to_gcs import (
    DATASET_ID,
    LOCATIONS,
    MODEL,
    BackfillError,
    location_bbox,
    report_status,
    utc_now_iso,
    write_json,
    write_parquet,
)


RAW_PREFIX = (
    "raw/weather/ncar_gdex/d084001/solar_points/"
    "model=ncep_gfs_0p25/cycle=all"
)
PROCESSED_PREFIX = (
    "processed/weather/ncar_gdex/d084001/"
    "model=ncep_gfs_0p25/cycle=all/solar"
)
DEFAULT_VALID_HOURS = (0, 6, 12, 18)
SIX_HOUR_INTERVAL_HOURS = 6
NCSS_VARIABLES = (
    "Downward_Short-Wave_Radiation_Flux_surface_6_Hour_Average",
    "Total_cloud_cover_entire_atmosphere_6_Hour_Average",
    "Temperature_height_above_ground",
)


def _target_end_time(target: date, valid_hour: int) -> datetime:
    """Return the UTC end of one target-day six-hour energy interval."""

    if valid_hour == 0:
        target = target + timedelta(days=1)
    return datetime.combine(
        target,
        datetime_time(hour=valid_hour),
        tzinfo=timezone.utc,
    )


def expected_sources(
    start: date,
    end: date,
    *,
    cycles: Iterable[int],
    lead_days: Iterable[int],
    valid_hours: Iterable[int],
) -> dict[str, _wind.ExpectedSource]:
    valid_hours = tuple(valid_hours)
    if tuple(sorted(valid_hours)) != DEFAULT_VALID_HOURS:
        raise ValueError(
            "solar valid hours must be 0,6,12,18; hour 00 is interpreted "
            "as the interval ending at 00 UTC on the following day"
        )
    expected: dict[str, _wind.ExpectedSource] = {}
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
                    valid_time = _target_end_time(target, valid_hour)
                    lead_hour = int(
                        (valid_time - initialization).total_seconds() // 3600
                    )
                    if lead_hour <= 0 or lead_hour % 6:
                        raise ValueError(
                            f"invalid solar interval lead hour {lead_hour}"
                        )
                    source_file = (
                        f"gfs.0p25.{initialization:%Y%m%d%H}."
                        f"f{lead_hour:03d}.grib2"
                    )
                    member_name = f"{source_file}.nc"
                    expected[member_name] = _wind.ExpectedSource(
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


def requested_product_leads(
    cycles: Iterable[int],
    lead_days: Iterable[int],
    valid_hours: Iterable[int],
) -> tuple[int, ...]:
    # Use a dummy issue date because the lead-hour geometry is date invariant.
    expected = expected_sources(
        date(2020, 1, 1),
        date(2020, 1, 1),
        cycles=cycles,
        lead_days=lead_days,
        valid_hours=valid_hours,
    )
    return tuple(sorted({item.forecast_lead_hours for item in expected.values()}))


def _average_product(lead_hour: int) -> str:
    return (
        f"6-hour Average (initial+{lead_hour - 6} "
        f"to initial+{lead_hour})"
    )


def build_batch_control(
    start: date,
    end: date,
    *,
    cycles: tuple[int, ...],
    lead_days: tuple[int, ...],
    valid_hours: tuple[int, ...],
) -> dict[str, str]:
    bbox = location_bbox()
    leads = requested_product_leads(cycles, lead_days, valid_hours)
    products = [f"{lead}-hour Forecast" for lead in leads]
    products.extend(_average_product(lead) for lead in leads)
    start_stamp = f"{start:%Y%m%d}{min(cycles):02d}00"
    end_stamp = f"{end:%Y%m%d}{max(cycles):02d}00"
    return {
        "dataset": _wind.GDEX_SUBSET_DATASET_ID,
        "date": f"{start_stamp}/to/{end_stamp}",
        "datetype": "init",
        "param": "DSWRF/T CDC/TMP",
        "level": "SFC:0;EATM:0;HTGL:2",
        "nlat": str(bbox.north),
        "slat": str(bbox.south),
        "wlon": str(bbox.west),
        "elon": str(bbox.east),
        "product": "/".join(products),
        "oformat": "netCDF",
    }


def _decode_char_variable(values: np.ndarray) -> str:
    flat = np.asarray(values).astype("S1").reshape(-1)
    return b"".join(flat.tolist()).decode("ascii").strip("\x00 ")


def _attribute_text(variable: Any) -> str:
    pieces: list[str] = []
    for value in getattr(variable, "_attributes", {}).values():
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        pieces.append(str(value))
    return " ".join(pieces).upper()


def _find_variable(
    variables: dict[str, Any],
    *,
    prefixes: tuple[str, ...],
    descriptions: tuple[str, ...],
    preferred_prefixes: tuple[str, ...] = (),
) -> str:
    candidates: list[str] = []
    for name, variable in variables.items():
        normalized = name.upper().replace("-", "_").replace(" ", "_")
        attributes = _attribute_text(variable)
        if any(normalized.startswith(prefix) for prefix in prefixes) or any(
            description in attributes for description in descriptions
        ):
            candidates.append(name)
    if len(candidates) > 1 and preferred_prefixes:
        preferred = [
            name
            for name in candidates
            if any(
                name.upper().replace("-", "_").replace(" ", "_").startswith(
                    prefix
                )
                for prefix in preferred_prefixes
            )
        ]
        if len(preferred) == 1:
            return preferred[0]
    if len(candidates) != 1:
        raise BackfillError(
            f"expected one solar variable {prefixes[0]}, found {candidates}"
        )
    return candidates[0]


def _lat_lon_names(variables: dict[str, Any]) -> tuple[str, str]:
    lowered = {name.lower(): name for name in variables}
    latitude = lowered.get("lat") or lowered.get("latitude")
    longitude = lowered.get("lon") or lowered.get("longitude")
    if latitude is None or longitude is None:
        raise BackfillError("NetCDF response has no latitude/longitude axes")
    return latitude, longitude


def _grid(variable: Any, shape: tuple[int, int], name: str) -> np.ndarray:
    values = np.asarray(variable[:], dtype=np.float32).squeeze()
    if values.shape != shape:
        raise BackfillError(f"{name} grid has shape {values.shape}, expected {shape}")
    return np.where(np.abs(values) < 1e20, values, np.nan)


def parse_netcdf_member(
    payload: bytes,
    source: _wind.ExpectedSource,
    *,
    archive_url: str,
    retrieved_at_utc: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    with netcdf_file(io.BytesIO(payload), "r", mmap=False) as dataset:
        variables = dataset.variables
        lat_name, lon_name = _lat_lon_names(variables)
        latitude = np.asarray(variables[lat_name][:], dtype=float)
        longitude = np.asarray(variables[lon_name][:], dtype=float)
        longitude = np.where(longitude > 180, longitude - 360, longitude)
        dswrf_name = _find_variable(
            variables,
            prefixes=("DSWRF", "DOWNWARD_SHORT_WAVE_RADIATION_FLUX"),
            descriptions=("DOWNWARD SHORTWAVE RADIATION FLUX", "DOWNWARD SHORT-WAVE RADIATION FLUX"),
        )
        cloud_name = _find_variable(
            variables,
            prefixes=("T_CDC", "TCDC", "TOTAL_CLOUD_COVER"),
            descriptions=("TOTAL CLOUD COVER",),
            # GFS v16 files beginning 2021-03-22 12Z can contain both an
            # instantaneous total-cloud-cover field (T_CDC_L10) and the
            # requested interval-average field (T_CDC_L10_Avg_1).  The solar
            # daily-energy product is built from non-overlapping six-hour
            # intervals, so choose the average explicitly when both exist.
            preferred_prefixes=(
                "T_CDC_L10_AVG",
                "TCDC_L10_AVG",
                "TOTAL_CLOUD_COVER_ENTIRE_ATMOSPHERE_6_HOUR_AVERAGE",
            ),
        )
        temperature_name = _find_variable(
            variables,
            prefixes=("TMP", "TEMPERATURE_HEIGHT_ABOVE_GROUND"),
            descriptions=("TEMPERATURE HEIGHT ABOVE GROUND",),
            preferred_prefixes=(
                "TMP_L103",
                "TEMPERATURE_HEIGHT_ABOVE_GROUND",
            ),
        )
        shape = (len(latitude), len(longitude))
        dswrf = _grid(variables[dswrf_name], shape, dswrf_name)
        cloud = _grid(variables[cloud_name], shape, cloud_name)
        temperature = _grid(
            variables[temperature_name], shape, temperature_name
        )
        if "forecast_hour" in variables:
            actual = int(np.asarray(variables["forecast_hour"][:]).reshape(-1)[0])
            if actual != source.forecast_lead_hours:
                raise BackfillError(
                    f"{source.member_name} forecast hour is {actual}"
                )
        if "ref_date_time" in variables:
            actual = _decode_char_variable(variables["ref_date_time"][:])
            expected = source.initialization_time_utc.strftime("%Y%m%d%H")
            if actual != expected:
                raise BackfillError(
                    f"{source.member_name} reference time is {actual}"
                )
        if "valid_date_time" in variables:
            actual = _decode_char_variable(variables["valid_date_time"][:])
            expected = source.valid_time_utc.strftime("%Y%m%d%H")
            if actual != expected:
                raise BackfillError(
                    f"{source.member_name} valid time is {actual}"
                )

    if np.nanmin(dswrf) < -5 or np.nanmax(dswrf) > 1_600:
        raise BackfillError(f"{source.member_name} DSWRF values are implausible")
    if np.nanmin(cloud) < -1 or np.nanmax(cloud) > 101:
        raise BackfillError(f"{source.member_name} cloud values are implausible")
    if np.nanmin(temperature) < 150 or np.nanmax(temperature) > 360:
        raise BackfillError(f"{source.member_name} temperature values are implausible")
    dswrf = np.maximum(dswrf, 0)
    cloud = np.clip(cloud, 0, 100)

    rows: list[dict[str, Any]] = []
    for location in LOCATIONS:
        lat_index = int(np.abs(latitude - location.latitude).argmin())
        lon_index = int(np.abs(longitude - location.longitude).argmin())
        radiation = float(dswrf[lat_index, lon_index])
        cloud_value = float(cloud[lat_index, lon_index])
        temperature_k = float(temperature[lat_index, lon_index])
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
                "grid_latitude": np.float32(latitude[lat_index]),
                "grid_longitude": np.float32(longitude[lon_index]),
                "forecast_reference_time_utc": source.initialization_time_utc,
                "forecast_cycle_hour_utc": np.int8(source.cycle_hour_utc),
                "forecast_lead_hours": np.int16(source.forecast_lead_hours),
                "valid_time_utc": source.valid_time_utc,
                "target_date": source.target_date,
                "lead_days": np.int8(source.lead_days),
                "downward_shortwave_radiation_wm2": np.float32(radiation),
                "shortwave_interval_energy_kwh_m2": np.float32(
                    radiation * SIX_HOUR_INTERVAL_HOURS / 1_000
                ),
                "total_cloud_cover_pct": np.float32(cloud_value),
                "temperature_2m_k": np.float32(temperature_k),
                "temperature_2m_c": np.float32(temperature_k - 273.15),
                "radiation_interval_hours": np.int8(SIX_HOUR_INTERVAL_HOURS),
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


def _parse_archives(
    urls: Iterable[str],
    expected: dict[str, _wind.ExpectedSource],
    *,
    attempts: int,
    timeout_seconds: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[str], dict[str, str]]:
    # The archive reader is generic apart from resolving this global parser.
    previous = _wind.parse_netcdf_member
    _wind.parse_netcdf_member = parse_netcdf_member
    try:
        return _wind.parse_archives(
            urls,
            expected,
            attempts=attempts,
            timeout_seconds=timeout_seconds,
        )
    finally:
        _wind.parse_netcdf_member = previous


def _ncss_params() -> list[tuple[str, str]]:
    bbox = location_bbox()
    return [
        *(("var", variable) for variable in NCSS_VARIABLES),
        ("north", str(bbox.north)),
        ("south", str(bbox.south)),
        ("west", str(bbox.west)),
        ("east", str(bbox.east)),
        ("vertCoord", "2"),
        ("horizStride", "1"),
        ("addLatLon", "true"),
        ("accept", "netcdf"),
    ]


def _fallback_one(
    source: _wind.ExpectedSource,
    *,
    attempts: int,
    timeout_seconds: float,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    url = ncss_url(
        source.initialization_time_utc,
        source.forecast_lead_hours,
    )
    params = _ncss_params()
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(
                url,
                params=params,
                timeout=(30, timeout_seconds),
                headers={
                    "User-Agent": "braeswood-naturalgas-ncar-solar-repair/1.0"
                },
            )
            if response.status_code in {429, 500, 502, 503, 504}:
                raise BackfillError(f"retryable NCSS HTTP {response.status_code}")
            if response.status_code != 200:
                raise BackfillError(
                    f"NCSS HTTP {response.status_code}: {response.text[:300]}"
                )
            rows, inventory = parse_netcdf_member(
                response.content,
                source,
                archive_url=response.url,
                retrieved_at_utc=utc_now_iso(),
            )
            inventory["source_status"] = "ncss_fallback"
            inventory["source_access_method"] = "THREDDS NCSS fallback"
            return pd.DataFrame(rows), inventory
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(min(2**attempt, 30))
    raise BackfillError(
        f"solar NCSS fallback failed after {attempts} attempts"
    ) from last_error


def _missing_inventory(
    source: _wind.ExpectedSource,
    error: str | None,
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
        "source_error": error or "source absent from GDEX and NCSS",
        "source_access_method": "GDEX batch plus NCSS fallback",
        "source_url": None,
        "source_response_bytes": 0,
        "source_response_sha256": None,
        "retrieved_at_utc": utc_now_iso(),
    }


def _repair_missing(
    missing: list[_wind.ExpectedSource],
    *,
    workers: int,
    attempts: int,
    timeout_seconds: float,
) -> tuple[list[pd.DataFrame], list[dict[str, Any]], list[_wind.ExpectedSource]]:
    if not missing:
        return [], [], []
    print(
        f"  GDEX archive is missing {len(missing):,} solar members; "
        "trying NCSS repair",
        flush=True,
    )
    frames: list[pd.DataFrame] = []
    inventory: list[dict[str, Any]] = []
    unavailable: list[_wind.ExpectedSource] = []
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
                frame, row = future.result()
            except Exception as exc:
                print(
                    f"  solar repair unavailable {source.source_file}: "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )
                unavailable.append(source)
            else:
                frames.append(frame)
                inventory.append(row)
    return frames, inventory, unavailable


def make_daily(
    points: pd.DataFrame,
    *,
    expected_samples: int,
    minimum_samples: int,
) -> pd.DataFrame:
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
        points.groupby(group_columns, observed=True, dropna=False, as_index=False)
        .agg(
            solar_sample_count=("downward_shortwave_radiation_wm2", "count"),
            downward_shortwave_mean_wm2=(
                "downward_shortwave_radiation_wm2",
                "mean",
            ),
            downward_shortwave_max_wm2=(
                "downward_shortwave_radiation_wm2",
                "max",
            ),
            downward_shortwave_energy_kwh_m2=(
                "shortwave_interval_energy_kwh_m2",
                "sum",
            ),
            total_cloud_cover_mean_pct=("total_cloud_cover_pct", "mean"),
            temperature_2m_mean_c=("temperature_2m_c", "mean"),
            temperature_2m_max_c=("temperature_2m_c", "max"),
        )
        .sort_values(["forecast_reference_time_utc", "lead_days", "location_id"])
        .reset_index(drop=True)
    )
    invalid = daily["solar_sample_count"].lt(minimum_samples) | daily[
        "solar_sample_count"
    ].gt(expected_samples)
    if invalid.any():
        raise BackfillError(
            "daily solar groups have fewer samples than the configured minimum"
        )
    daily["solar_sample_complete"] = daily["solar_sample_count"].eq(
        expected_samples
    )
    daily["nominal_issue_date"] = pd.to_datetime(
        daily["forecast_reference_time_utc"], utc=True
    ).dt.date
    return daily


def make_features(
    daily: pd.DataFrame,
    *,
    expected_lead_days: int,
    require_complete: bool,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    location_leads = (
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
            gfs_dswrf_wm2=("downward_shortwave_mean_wm2", "mean"),
            gfs_shortwave_energy_kwh_m2_day=(
                "downward_shortwave_energy_kwh_m2",
                "mean",
            ),
            gfs_total_cloud_cover_pct=("total_cloud_cover_mean_pct", "mean"),
            gfs_temperature_2m_c=("temperature_2m_mean_c", "mean"),
            location_count=("location_id", "nunique"),
            min_interval_count=("solar_sample_count", "min"),
        )
        .sort_values(["forecast_reference_time_utc", "lead_days"])
        .reset_index(drop=True)
    )
    features = (
        location_leads.groupby(
            ["forecast_reference_time_utc", "nominal_issue_date"],
            observed=True,
            as_index=False,
        )
        .agg(
            gfs_dswrf_5d_mean_wm2=("gfs_dswrf_wm2", "mean"),
            gfs_shortwave_energy_5d_mean_kwh_m2_day=(
                "gfs_shortwave_energy_kwh_m2_day",
                "mean",
            ),
            gfs_total_cloud_cover_5d_mean_pct=(
                "gfs_total_cloud_cover_pct",
                "mean",
            ),
            gfs_temperature_2m_5d_mean_c=("gfs_temperature_2m_c", "mean"),
            gfs_lead_count=("lead_days", "nunique"),
            gfs_min_locations=("location_count", "min"),
            gfs_min_solar_intervals=("min_interval_count", "min"),
        )
        .sort_values("forecast_reference_time_utc")
        .reset_index(drop=True)
    )
    if require_complete:
        if not features["gfs_lead_count"].eq(expected_lead_days).all():
            raise BackfillError("not every solar issue has all requested lead days")
        if not features["gfs_min_locations"].eq(len(LOCATIONS)).all():
            raise BackfillError("not every solar issue has all configured locations")
    return location_leads, features


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
            f"{bucket}/{processed_prefix}/solar_daily/{partition}/data.parquet"
        ),
        "city_leads": (
            f"{bucket}/{processed_prefix}/solar_location_leads/"
            f"{partition}/data.parquet"
        ),
        "features": (
            f"{bucket}/{processed_prefix}/solar_features/{partition}/data.parquet"
        ),
        "manifest": f"{bucket}/{raw_prefix}/{partition}/manifest.json",
    }


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
    maximum_missing_source_files: int | None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    expected = expected_sources(
        start,
        end,
        cycles=cycles,
        lead_days=lead_days,
        valid_hours=valid_hours,
    )
    urls = _wind.archive_urls(files_payload)
    rows, inventories, parsed, invalid_errors = _parse_archives(
        urls,
        expected,
        attempts=attempts,
        timeout_seconds=timeout_seconds,
    )
    initially_missing = sorted(
        (expected[name] for name in set(expected).difference(parsed)),
        key=lambda item: (
            item.initialization_time_utc,
            item.forecast_lead_hours,
        ),
    )
    fallback_frames, fallback_inventory, unavailable = _repair_missing(
        initially_missing,
        workers=fallback_workers,
        attempts=attempts,
        timeout_seconds=timeout_seconds,
    )
    if maximum_missing_source_files is not None and len(unavailable) > maximum_missing_source_files:
        raise BackfillError(
            f"missing {len(unavailable):,} solar sources after repair; "
            f"configured maximum is {maximum_missing_source_files:,}"
        )
    point_frames = [pd.DataFrame(rows), *fallback_frames]
    point_frames = [frame for frame in point_frames if not frame.empty]
    if not point_frames:
        raise BackfillError("GDEX solar request produced no usable point data")
    points = pd.concat(point_frames, ignore_index=True).sort_values(
        ["forecast_reference_time_utc", "forecast_lead_hours", "location_id"]
    ).reset_index(drop=True)
    inventory_rows = [*inventories, *fallback_inventory]
    inventory_rows.extend(
        _missing_inventory(source, invalid_errors.get(source.member_name))
        for source in unavailable
    )
    inventory = pd.DataFrame(inventory_rows).sort_values(
        ["forecast_reference_time_utc", "forecast_lead_hours"]
    ).reset_index(drop=True)
    source_key = ["forecast_reference_time_utc", "forecast_lead_hours"]
    if inventory.duplicated(source_key).any() or len(inventory) != len(expected):
        raise BackfillError("solar source inventory is incomplete or duplicated")
    if points.duplicated([*source_key, "location_id"]).any():
        raise BackfillError("duplicate solar point rows")
    daily = make_daily(
        points,
        expected_samples=len(valid_hours),
        minimum_samples=minimum_daily_samples,
    )
    location_leads, features = make_features(
        daily,
        expected_lead_days=len(lead_days),
        require_complete=not unavailable,
    )
    for frame in (daily, location_leads, features):
        frame["forecast_cycle_hour_utc"] = pd.to_datetime(
            frame["forecast_reference_time_utc"], utc=True
        ).dt.hour.astype("int8")
    diagnostics = {
        "expected_source_files": len(expected),
        "gdex_batch_source_files": len(parsed),
        "ncss_fallback_source_files": len(fallback_inventory),
        "missing_source_files": len(unavailable),
        "missing_source_file_names": [item.source_file for item in unavailable],
        "maximum_accepted_missing_source_files": maximum_missing_source_files,
        "invalid_archive_member_count": len(invalid_errors),
        "invalid_archive_members": [
            {"source_member": name, "error": error}
            for name, error in sorted(invalid_errors.items())
        ],
        "daily_complete_groups": int(daily["solar_sample_complete"].sum()),
        "daily_incomplete_groups": int((~daily["solar_sample_complete"]).sum()),
        "incomplete_feature_rows": int(
            (
                ~features["gfs_lead_count"].eq(len(lead_days))
                | ~features["gfs_min_locations"].eq(len(LOCATIONS))
                | ~features["gfs_min_solar_intervals"].eq(len(valid_hours))
            ).sum()
        ),
        "archive_urls": list(urls),
        "gdex_reported_total_bytes": int(files_payload.get("total_size") or 0),
    }
    return points, inventory, daily, location_leads, features, diagnostics


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
    points, inventory, daily, location_leads, features, diagnostics = (
        build_partition_frames(
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
    )
    byte_counts = {
        "raw_points": write_parquet(fs, keys["raw_points"], points),
        "source_inventory": write_parquet(
            fs, keys["source_inventory"], inventory
        ),
        "daily": write_parquet(fs, keys["daily"], daily),
        "city_leads": write_parquet(fs, keys["city_leads"], location_leads),
        "features": write_parquet(fs, keys["features"], features),
    }
    missing = int(diagnostics["missing_source_files"])
    manifest = {
        "status": "complete_with_missing_sources" if missing else "complete",
        "dataset_id": DATASET_ID,
        "model": MODEL,
        "source_service": _wind.GDEX_SOURCE_SERVICE,
        "source_dataset_url": f"https://gdex.ucar.edu/datasets/{DATASET_ID}/",
        "gdex_request_id": request_id,
        "coverage_start_issue_date": start.isoformat(),
        "coverage_end_issue_date": end.isoformat(),
        "cycle_hours_utc": list(cycles),
        "lead_days": list(lead_days),
        "valid_hours_utc": list(valid_hours),
        "valid_hour_00_semantics": (
            "interval ending 00 UTC on the day after target_date"
        ),
        "requested_product_lead_hours": list(
            requested_product_leads(cycles, lead_days, valid_hours)
        ),
        "parameters": {
            "DSWRF": "surface downward short-wave radiation, 6-hour average",
            "T CDC": "total-atmosphere cloud cover, 6-hour average",
            "TMP": "2 m air temperature, instantaneous at interval end",
        },
        "locations": len(LOCATIONS),
        "spatial_weighting": "equal weight across configured U.S. locations",
        "bbox": asdict(location_bbox()),
        "minimum_daily_samples": minimum_daily_samples,
        "rows": {
            "raw_points": len(points),
            "source_inventory": len(inventory),
            "daily": len(daily),
            "city_leads": len(location_leads),
            "features": len(features),
        },
        "diagnostics": diagnostics,
        "missing_data_policy": (
            "Each missing/corrupt source is retried through NCSS; remaining "
            "failures are marked missing and usable data are retained."
        ),
        "gcs_keys": {name: f"gs://{key}" for name, key in keys.items()},
        "gcs_object_bytes": byte_counts,
        "created_at_utc": utc_now_iso(),
        "feature_definition": (
            "Four non-overlapping 6-hour DSWRF intervals form each target "
            "day. Daily energy is sum(DSWRF*6h)/1000 kWh/m2; location and "
            "five-day features are preliminary equal-weight means."
        ),
    }
    byte_counts["manifest"] = write_json(fs, keys["manifest"], manifest)
    print(
        f"uploaded solar {start:%Y-%m}: raw={len(points):,}, "
        f"daily={len(daily):,}, features={len(features):,}, "
        f"missing_sources={missing}",
        flush=True,
    )
    return {
        "status": "uploaded",
        "keys": keys,
        "rows": manifest["rows"],
        "diagnostics": diagnostics,
    }


__all__ = [
    "DEFAULT_VALID_HOURS",
    "PROCESSED_PREFIX",
    "RAW_PREFIX",
    "build_batch_control",
    "expected_sources",
    "parse_netcdf_member",
    "partition_keys",
    "report_status",
    "requested_product_leads",
    "upload_completed_partition",
]
