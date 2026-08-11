#!/usr/bin/env python3
"""Build time-varying EIA-capacity-weighted GDEX solar lead features.

The EIA operating-generator-capacity API supplies monthly generator-level
utility-scale solar capacity and plant coordinates.  Each operating generator
is mapped to the nearest of the 28 weather sampling locations.  Forecasts use
capacity from two months earlier by default, avoiding contemporaneous capacity
information while retaining the rapid historical growth of the solar fleet.

This module is research-only.  It reads GCS solar_daily partitions and writes
only local experimental outputs when invoked by an evaluator.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import math
from typing import Any, Iterable

import gcsfs
import numpy as np
import pandas as pd
import requests

from naturalgas.ncar_gdex_solar_backfill_to_gcs import PROCESSED_PREFIX
from naturalgas.ncar_gdex_wind_backfill_to_gcs import BUCKET, LOCATIONS


EIA_CAPACITY_URL = (
    "https://api.eia.gov/v2/electricity/operating-generator-capacity/data/"
)
PAGE_SIZE = 5_000
NON_CONUS_STATE_IDS = {"AK", "HI", "PR", "VI", "GU", "AS", "MP"}
DAILY_COLUMNS = (
    "forecast_reference_time_utc",
    "nominal_issue_date",
    "target_date",
    "lead_days",
    "location_id",
    "requested_latitude",
    "requested_longitude",
    "solar_sample_count",
    "downward_shortwave_mean_wm2",
    "downward_shortwave_energy_kwh_m2",
    "total_cloud_cover_mean_pct",
    "temperature_2m_mean_c",
    "solar_sample_complete",
)


class SolarCapacityWeightingError(RuntimeError):
    """Raised when capacity or weather coverage cannot support weighting."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def solar_daily_pattern() -> str:
    return (
        f"{BUCKET}/{PROCESSED_PREFIX}/solar_daily/"
        "year=*/month=*/data.parquet"
    )


def load_current_solar_daily(
    filesystem: gcsfs.GCSFileSystem,
) -> tuple[pd.DataFrame, tuple[str, ...]]:
    keys = tuple(sorted(filesystem.glob(solar_daily_pattern())))
    if not keys:
        raise FileNotFoundError("no uploaded GDEX solar_daily partitions")
    frames: list[pd.DataFrame] = []
    for key in keys:
        with filesystem.open(key, "rb") as source:
            frames.append(pd.read_parquet(source, columns=list(DAILY_COLUMNS)))
    daily = pd.concat(frames, ignore_index=True)
    daily["forecast_reference_time_utc"] = pd.to_datetime(
        daily["forecast_reference_time_utc"], utc=True
    )
    daily["target_date"] = pd.to_datetime(daily["target_date"])
    return daily, keys


def _eia_params(
    *,
    api_key: str,
    start: str,
    end: str,
    offset: int,
    length: int,
) -> list[tuple[str, Any]]:
    return [
        ("api_key", api_key),
        ("frequency", "monthly"),
        ("data[0]", "nameplate-capacity-mw"),
        ("data[1]", "operating-year-month"),
        ("data[2]", "latitude"),
        ("data[3]", "longitude"),
        ("facets[energy_source_code][]", "SUN"),
        ("facets[status][]", "OP"),
        ("start", start),
        ("end", end),
        ("sort[0][column]", "period"),
        ("sort[0][direction]", "asc"),
        ("sort[1][column]", "plantid"),
        ("sort[1][direction]", "asc"),
        ("sort[2][column]", "generatorid"),
        ("sort[2][direction]", "asc"),
        ("offset", offset),
        ("length", length),
    ]


def _fetch_eia_page(
    *,
    api_key: str,
    start: str,
    end: str,
    offset: int,
    length: int,
    timeout_seconds: float,
    attempts: int = 4,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(
                EIA_CAPACITY_URL,
                params=_eia_params(
                    api_key=api_key,
                    start=start,
                    end=end,
                    offset=offset,
                    length=length,
                ),
                timeout=(20, timeout_seconds),
            )
            response.raise_for_status()
            payload = response.json()
            if "response" not in payload:
                raise SolarCapacityWeightingError(
                    f"EIA response at offset {offset} has no response object"
                )
            return payload["response"]
        except Exception as exc:  # requests and malformed remote payloads
            last_error = exc
            if attempt == attempts:
                break
    raise SolarCapacityWeightingError(
        f"EIA capacity page failed at offset {offset}"
    ) from last_error


def fetch_eia_solar_generators(
    *,
    api_key: str,
    start: str,
    end: str,
    workers: int = 4,
    timeout_seconds: float = 90.0,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Download monthly operating SUN generator records without logging a key."""

    first = _fetch_eia_page(
        api_key=api_key,
        start=start,
        end=end,
        offset=0,
        length=1,
        timeout_seconds=timeout_seconds,
    )
    total = int(first.get("total", 0))
    if total <= 0:
        raise SolarCapacityWeightingError("EIA returned no operating solar rows")
    offsets = list(range(0, total, PAGE_SIZE))
    pages: dict[int, list[dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        future_offsets = {
            executor.submit(
                _fetch_eia_page,
                api_key=api_key,
                start=start,
                end=end,
                offset=offset,
                length=PAGE_SIZE,
                timeout_seconds=timeout_seconds,
            ): offset
            for offset in offsets
        }
        for future in as_completed(future_offsets):
            offset = future_offsets[future]
            pages[offset] = list(future.result().get("data", []))
    records = [record for offset in offsets for record in pages[offset]]
    if len(records) != total:
        raise SolarCapacityWeightingError(
            f"EIA expected {total:,} rows but returned {len(records):,}"
        )
    generators = pd.DataFrame(records)
    for column in ("nameplate-capacity-mw", "latitude", "longitude"):
        generators[column] = pd.to_numeric(generators[column], errors="coerce")
    metadata = {
        "provider": "U.S. Energy Information Administration",
        "route": "electricity/operating-generator-capacity",
        "energy_source_code": "SUN",
        "status": "OP",
        "start_period": start,
        "end_period": end,
        "retrieved_at_utc": utc_now_iso(),
        "rows": len(generators),
        "pages": len(offsets),
        "api_key_stored": False,
    }
    return generators, metadata


def _nearest_location_ids(plants: pd.DataFrame) -> np.ndarray:
    plant_lat = np.radians(plants["latitude"].to_numpy(dtype=float))[:, None]
    plant_lon = np.radians(plants["longitude"].to_numpy(dtype=float))[:, None]
    location_lat = np.radians(
        np.array([location.latitude for location in LOCATIONS], dtype=float)
    )[None, :]
    location_lon = np.radians(
        np.array([location.longitude for location in LOCATIONS], dtype=float)
    )[None, :]
    delta_lat = location_lat - plant_lat
    delta_lon = location_lon - plant_lon
    haversine = (
        np.sin(delta_lat / 2.0) ** 2
        + np.cos(plant_lat)
        * np.cos(location_lat)
        * np.sin(delta_lon / 2.0) ** 2
    )
    nearest = np.argmin(haversine, axis=1)
    ids = np.array([location.location_id for location in LOCATIONS], dtype=object)
    return ids[nearest]


def build_monthly_location_weights(
    generators: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Map generator capacity to weather locations and normalize each month."""

    required = {
        "period",
        "stateid",
        "plantid",
        "nameplate-capacity-mw",
        "latitude",
        "longitude",
    }
    missing = required.difference(generators.columns)
    if missing:
        raise SolarCapacityWeightingError(
            f"EIA generator frame is missing {sorted(missing)}"
        )
    frame = generators.copy()
    frame["period"] = frame["period"].astype(str)
    positive = frame["nameplate-capacity-mw"].gt(0.0)
    conus_state = ~frame["stateid"].isin(NON_CONUS_STATE_IDS)
    conus_coordinates = (
        frame["latitude"].between(24.0, 50.0)
        & frame["longitude"].between(-125.0, -66.0)
    )
    eligible = frame.loc[positive & conus_state & conus_coordinates].copy()
    if eligible.empty:
        raise SolarCapacityWeightingError("no coordinate-complete CONUS solar rows")

    plants = eligible[
        ["stateid", "plantid", "latitude", "longitude"]
    ].drop_duplicates(["stateid", "plantid"])
    plants["location_id"] = _nearest_location_ids(plants)
    eligible = eligible.merge(
        plants[["stateid", "plantid", "location_id"]],
        on=["stateid", "plantid"],
        how="left",
        validate="many_to_one",
    )
    weights = (
        eligible.groupby(["period", "location_id"], as_index=False, observed=True)
        .agg(
            capacity_mw=("nameplate-capacity-mw", "sum"),
            generator_count=("generatorid", "nunique"),
            plant_count=("plantid", "nunique"),
        )
        .sort_values(["period", "location_id"])
        .reset_index(drop=True)
    )
    weights["total_capacity_mw"] = weights.groupby("period")[
        "capacity_mw"
    ].transform("sum")
    weights["capacity_share"] = (
        weights["capacity_mw"] / weights["total_capacity_mw"]
    )
    all_positive_capacity = float(
        frame.loc[positive & conus_state, "nameplate-capacity-mw"].sum()
    )
    mapped_capacity = float(eligible["nameplate-capacity-mw"].sum())
    diagnostics = {
        "input_generator_month_rows": len(frame),
        "mapped_generator_month_rows": len(eligible),
        "unique_mapped_plants": int(plants["plantid"].nunique()),
        "weather_locations_with_capacity": int(weights["location_id"].nunique()),
        "capacity_periods": int(weights["period"].nunique()),
        "mapped_capacity_mw_month_sum": mapped_capacity,
        "coordinate_complete_capacity_share": (
            mapped_capacity / all_positive_capacity
            if all_positive_capacity
            else None
        ),
    }
    return weights, diagnostics


def extraterrestrial_energy_for_latitude(
    target_dates: pd.Series,
    latitudes: pd.Series,
) -> np.ndarray:
    day = target_dates.dt.dayofyear.to_numpy(dtype=float)
    latitude = np.radians(latitudes.to_numpy(dtype=float))
    inverse_distance = 1.0 + 0.033 * np.cos(2.0 * np.pi * day / 365.0)
    declination = 0.409 * np.sin(2.0 * np.pi * day / 365.0 - 1.39)
    sunset = np.arccos(
        np.clip(-np.tan(latitude) * np.tan(declination), -1.0, 1.0)
    )
    radiation_mj = (
        (24.0 * 60.0 / np.pi)
        * 0.0820
        * inverse_distance
        * (
            sunset * np.sin(latitude) * np.sin(declination)
            + np.cos(latitude) * np.cos(declination) * np.sin(sunset)
        )
    )
    return radiation_mj / 3.6


def _weighted_mean(
    values: pd.Series,
    capacity: pd.Series,
) -> float:
    valid = values.notna() & capacity.gt(0.0)
    denominator = capacity.loc[valid].sum()
    if not denominator:
        return math.nan
    return float((values.loc[valid] * capacity.loc[valid]).sum() / denominator)


def build_capacity_weighted_location_leads(
    daily: pd.DataFrame,
    weights: pd.DataFrame,
    *,
    capacity_lag_months: int = 2,
    minimum_capacity_coverage: float = 0.995,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Aggregate location-level forecasts using lagged monthly solar capacity."""

    frame = daily.copy()
    frame["issue_period"] = (
        frame["forecast_reference_time_utc"]
        .dt.tz_localize(None)
        .dt.to_period("M")
    )
    frame["capacity_period"] = (
        frame["issue_period"] - capacity_lag_months
    ).astype(str)
    weight_frame = weights.rename(columns={"period": "capacity_period"})
    frame = frame.merge(
        weight_frame[
            ["capacity_period", "location_id", "capacity_mw", "capacity_share"]
        ],
        on=["capacity_period", "location_id"],
        how="left",
        validate="many_to_one",
    )
    frame["capacity_mw"] = frame["capacity_mw"].fillna(0.0)
    frame["capacity_share"] = frame["capacity_share"].fillna(0.0)
    frame["extraterrestrial_kwh_m2_day_location"] = (
        extraterrestrial_energy_for_latitude(
            frame["target_date"], frame["requested_latitude"]
        )
    )
    complete = (
        frame["solar_sample_complete"].fillna(False)
        & frame["solar_sample_count"].eq(4)
    )
    frame["available_capacity_mw"] = frame["capacity_mw"].where(complete, 0.0)

    group_columns = [
        "forecast_reference_time_utc",
        "nominal_issue_date",
        "target_date",
        "lead_days",
        "capacity_period",
    ]
    rows: list[dict[str, Any]] = []
    for key, group in frame.groupby(group_columns, observed=True, sort=False):
        total_capacity = float(group["capacity_mw"].sum())
        available_capacity = float(group["available_capacity_mw"].sum())
        coverage = (
            available_capacity / total_capacity if total_capacity else math.nan
        )
        usable_capacity = group["available_capacity_mw"]
        rows.append(
            {
                **dict(zip(group_columns, key, strict=True)),
                "gfs_dswrf_wm2": _weighted_mean(
                    group["downward_shortwave_mean_wm2"], usable_capacity
                ),
                "gfs_shortwave_energy_kwh_m2_day": _weighted_mean(
                    group["downward_shortwave_energy_kwh_m2"], usable_capacity
                ),
                "gfs_total_cloud_cover_pct": _weighted_mean(
                    group["total_cloud_cover_mean_pct"], usable_capacity
                ),
                "gfs_temperature_2m_c": _weighted_mean(
                    group["temperature_2m_mean_c"], usable_capacity
                ),
                "extraterrestrial_kwh_m2_day": _weighted_mean(
                    group["extraterrestrial_kwh_m2_day_location"],
                    usable_capacity,
                ),
                "location_count": int(group["location_id"].nunique()),
                "min_interval_count": int(group.loc[group["capacity_mw"].gt(0), "solar_sample_count"].min()),
                "total_solar_capacity_mw": total_capacity,
                "capacity_coverage": coverage,
            }
        )
    result = pd.DataFrame(rows).sort_values(
        ["forecast_reference_time_utc", "lead_days"]
    )
    insufficient = result["capacity_coverage"].lt(minimum_capacity_coverage)
    value_columns = [
        "gfs_dswrf_wm2",
        "gfs_shortwave_energy_kwh_m2_day",
        "gfs_total_cloud_cover_pct",
        "gfs_temperature_2m_c",
        "extraterrestrial_kwh_m2_day",
    ]
    result.loc[insufficient, value_columns] = np.nan
    diagnostics = {
        "capacity_lag_months": capacity_lag_months,
        "minimum_capacity_coverage": minimum_capacity_coverage,
        "lead_rows": len(result),
        "insufficient_capacity_coverage_rows": int(insufficient.sum()),
        "minimum_observed_capacity_coverage": float(
            result["capacity_coverage"].min()
        ),
        "first_capacity_period": result["capacity_period"].min(),
        "last_capacity_period": result["capacity_period"].max(),
    }
    return result.reset_index(drop=True), diagnostics


__all__ = [
    "build_capacity_weighted_location_leads",
    "build_monthly_location_weights",
    "fetch_eia_solar_generators",
    "load_current_solar_daily",
]
