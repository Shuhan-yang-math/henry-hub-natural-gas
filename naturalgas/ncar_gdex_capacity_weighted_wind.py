#!/usr/bin/env python3
"""Build experimental USWTDB-capacity-weighted wind features from GDEX tar files.

This script does not submit new GDEX requests and does not modify the formal
wind backfill.  It reuses archive URLs recorded in completed monthly manifests,
samples every occupied USWTDB/GFS grid cell, and writes local experimental
Parquet partitions.

For a point-in-time conservative pilot, a 2016 run should use a fleet cutoff of
2015.  USWTDB reports commissioning year but not commissioning month, so this
avoids treating late-2016 additions as if they were online in January.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import re
import sys
import tarfile
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import gcsfs
import numpy as np
import pandas as pd
import requests
from scipy.io import netcdf_file


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from naturalgas.ncar_gdex_bulk_wind_backfill_to_gcs import (  # noqa: E402
    DEFAULT_CYCLES,
    DEFAULT_LEAD_DAYS,
    DEFAULT_VALID_HOURS,
    ExpectedSource,
    _wind_variable_name,
    expected_sources,
)
from naturalgas.ncar_gdex_wind_backfill_to_gcs import (  # noqa: E402
    BUCKET,
    DATASET_ID,
    MODEL,
    location_bbox,
)


USWTDB_API = "https://energy.usgs.gov/api/uswtdb/v1/turbines"
RAW_PREFIX = (
    "raw/weather/ncar_gdex/d084001/wind_points/"
    "model=ncep_gfs_0p25/cycle=all"
)
DEFAULT_OUTPUT_DIR = (
    Path(__file__).resolve().parent
    / "processed"
    / "ncar_gdex_capacity_weighted_wind"
)
USWTDB_COLUMNS = (
    "case_id,eia_id,p_name,p_year,p_cap,t_cap,t_hh,t_rd,t_state,"
    "t_fips,xlong,ylat,t_offshore,t_retrofit,t_retro_yr"
)
GRID_STEP_DEGREES = 0.25
USER_AGENT = "braeswood-naturalgas-capacity-weighted-wind-pilot/1.0"


class CapacityWeightingError(RuntimeError):
    """Raised when an experimental capacity-weighted partition is invalid."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_months(value: str) -> tuple[int, ...]:
    try:
        months = tuple(
            sorted(
                {
                    int(item.strip())
                    for item in value.split(",")
                    if item.strip()
                }
            )
        )
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "months must be comma-separated integers"
        ) from exc
    if not months or months[0] < 1 or months[-1] > 12:
        raise argparse.ArgumentTypeError("months must be between 1 and 12")
    return months


def month_bounds(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    end = (pd.Timestamp(start) + pd.offsets.MonthEnd(0)).date()
    return start, end


def round_to_gfs_grid(values: pd.Series) -> pd.Series:
    return (values / GRID_STEP_DEGREES).round() * GRID_STEP_DEGREES


def fetch_uswtdb(
    session: requests.Session,
    *,
    timeout_seconds: float,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    response = session.get(
        USWTDB_API,
        params={
            "select": USWTDB_COLUMNS,
            "limit": 100_000,
            "order": "case_id.asc",
        },
        headers={"Prefer": "count=exact"},
        timeout=(30, timeout_seconds),
    )
    response.raise_for_status()
    frame = pd.DataFrame(response.json())
    if frame.empty:
        raise CapacityWeightingError("USWTDB API returned no turbines")
    for column in [
        "p_year",
        "t_cap",
        "t_hh",
        "t_rd",
        "xlong",
        "ylat",
        "t_offshore",
    ]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    metadata = {
        "source_url": response.url,
        "retrieved_at_utc": utc_now_iso(),
        "content_range": response.headers.get("Content-Range"),
        "response_rows": len(frame),
    }
    return frame, metadata


def build_grid_weights(
    turbines: pd.DataFrame,
    *,
    fleet_cutoff_year: int,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    bbox = location_bbox()
    eligible = turbines.loc[
        turbines["p_year"].le(fleet_cutoff_year)
        & turbines["t_cap"].gt(0)
        & turbines["xlong"].notna()
        & turbines["ylat"].notna()
    ].copy()
    if eligible.empty:
        raise CapacityWeightingError("no eligible USWTDB turbines")
    eligible["capacity_mw"] = eligible["t_cap"] / 1_000.0
    total_capacity_mw = float(eligible["capacity_mw"].sum())
    inside = eligible.loc[
        eligible["ylat"].between(bbox.south, bbox.north)
        & eligible["xlong"].between(bbox.west, bbox.east)
    ].copy()
    if inside.empty:
        raise CapacityWeightingError("no turbines fall inside GDEX bbox")
    inside["grid_latitude"] = round_to_gfs_grid(inside["ylat"])
    inside["grid_longitude"] = round_to_gfs_grid(inside["xlong"])
    inside["known_hub_capacity_mw"] = np.where(
        inside["t_hh"].notna(),
        inside["capacity_mw"],
        0.0,
    )
    inside["hub_height_capacity_product"] = (
        inside["t_hh"].fillna(0.0) * inside["capacity_mw"]
    )
    weights = (
        inside.groupby(
            ["grid_latitude", "grid_longitude"],
            as_index=False,
        )
        .agg(
            capacity_mw=("capacity_mw", "sum"),
            turbine_count=("case_id", "nunique"),
            project_count=("p_name", "nunique"),
            known_hub_capacity_mw=(
                "known_hub_capacity_mw",
                "sum",
            ),
            hub_height_capacity_product=(
                "hub_height_capacity_product",
                "sum",
            ),
        )
        .sort_values(["grid_latitude", "grid_longitude"])
        .reset_index(drop=True)
    )
    weights["capacity_share"] = (
        weights["capacity_mw"] / weights["capacity_mw"].sum()
    )
    inside_capacity_mw = float(weights["capacity_mw"].sum())
    known_hub_capacity_mw = float(
        weights["known_hub_capacity_mw"].sum()
    )
    weighted_hub_height_m = (
        float(weights["hub_height_capacity_product"].sum())
        / known_hub_capacity_mw
        if known_hub_capacity_mw
        else None
    )
    diagnostics = {
        "fleet_cutoff_year": fleet_cutoff_year,
        "eligible_turbines_total": int(len(eligible)),
        "eligible_turbines_inside_bbox": int(len(inside)),
        "eligible_projects_inside_bbox": int(inside["p_name"].nunique()),
        "gfs_grid_cells": int(len(weights)),
        "total_capacity_mw": total_capacity_mw,
        "inside_bbox_capacity_mw": inside_capacity_mw,
        "inside_bbox_capacity_share": inside_capacity_mw
        / total_capacity_mw,
        "known_hub_capacity_share": known_hub_capacity_mw
        / inside_capacity_mw,
        "capacity_weighted_hub_height_m": weighted_hub_height_m,
        "request_bbox": asdict(bbox),
    }
    return weights, diagnostics


def generic_power_curve(
    wind_speed_mps: np.ndarray,
    *,
    cut_in_mps: float = 3.0,
    rated_mps: float = 12.0,
    cut_out_mps: float = 25.0,
) -> np.ndarray:
    """Return a transparent generic turbine capacity-factor proxy."""

    speed = np.asarray(wind_speed_mps, dtype=float)
    factor = np.zeros_like(speed)
    ramp = (speed >= cut_in_mps) & (speed < rated_mps)
    factor[ramp] = (
        speed[ramp] ** 3 - cut_in_mps**3
    ) / (rated_mps**3 - cut_in_mps**3)
    rated = (speed >= rated_mps) & (speed < cut_out_mps)
    factor[rated] = 1.0
    return np.clip(factor, 0.0, 1.0)


def _grid_mapping(
    latitude: np.ndarray,
    longitude: np.ndarray,
    weights: pd.DataFrame,
) -> tuple[np.ndarray, np.ndarray]:
    latitude_indices = np.abs(
        latitude[:, None]
        - weights["grid_latitude"].to_numpy(dtype=float)[None, :]
    ).argmin(axis=0)
    longitude_indices = np.abs(
        longitude[:, None]
        - weights["grid_longitude"].to_numpy(dtype=float)[None, :]
    ).argmin(axis=0)
    return latitude_indices, longitude_indices


def parse_capacity_member(
    payload: bytes,
    source: ExpectedSource,
    weights: pd.DataFrame,
    mapping_cache: dict[
        tuple[Any, ...],
        tuple[np.ndarray, np.ndarray],
    ],
) -> dict[str, Any]:
    with netcdf_file(io.BytesIO(payload), "r", mmap=False) as dataset:
        variables = dataset.variables
        latitude = np.asarray(variables["lat"][:], dtype=float)
        longitude = np.asarray(variables["lon"][:], dtype=float)
        longitude = np.where(longitude > 180, longitude - 360, longitude)
        u_name = _wind_variable_name(variables, "U_GRD")
        v_name = _wind_variable_name(variables, "V_GRD")
        u_grid = np.asarray(
            variables[u_name][:],
            dtype=np.float32,
        ).squeeze()
        v_grid = np.asarray(
            variables[v_name][:],
            dtype=np.float32,
        ).squeeze()
    if u_grid.shape != (len(latitude), len(longitude)):
        raise CapacityWeightingError(
            f"{source.member_name} unexpected U shape {u_grid.shape}"
        )
    if v_grid.shape != u_grid.shape:
        raise CapacityWeightingError(
            f"{source.member_name} U/V shape mismatch"
        )
    cache_key = (
        len(latitude),
        float(latitude[0]),
        float(latitude[-1]),
        len(longitude),
        float(longitude[0]),
        float(longitude[-1]),
    )
    if cache_key not in mapping_cache:
        mapping_cache[cache_key] = _grid_mapping(
            latitude,
            longitude,
            weights,
        )
    latitude_indices, longitude_indices = mapping_cache[cache_key]
    u_values = u_grid[latitude_indices, longitude_indices].astype(float)
    v_values = v_grid[latitude_indices, longitude_indices].astype(float)
    valid = (
        np.isfinite(u_values)
        & np.isfinite(v_values)
        & (np.abs(u_values) < 1e20)
        & (np.abs(v_values) < 1e20)
    )
    if not valid.all():
        raise CapacityWeightingError(
            f"{source.member_name} has invalid wind at "
            f"{int((~valid).sum())} occupied grid cells"
        )
    capacity = weights["capacity_mw"].to_numpy(dtype=float)
    speed = np.hypot(u_values, v_values)
    capacity_mw = float(capacity.sum())
    weighted_u = float(np.average(u_values, weights=capacity))
    weighted_v = float(np.average(v_values, weights=capacity))
    weighted_speed = float(np.average(speed, weights=capacity))
    capacity_factor = generic_power_curve(speed)
    weighted_capacity_factor = float(
        np.average(capacity_factor, weights=capacity)
    )
    return {
        "dataset_id": DATASET_ID,
        "model": MODEL,
        "weighting_source": "USWTDB turbine rated capacity",
        "fleet_cutoff_year": int(weights.attrs["fleet_cutoff_year"]),
        "forecast_reference_time_utc": source.initialization_time_utc,
        "forecast_cycle_hour_utc": source.cycle_hour_utc,
        "forecast_lead_hours": source.forecast_lead_hours,
        "valid_time_utc": source.valid_time_utc,
        "target_date": source.target_date,
        "lead_days": source.lead_days,
        "source_file": source.source_file,
        "capacity_mw": capacity_mw,
        "turbine_count": int(weights.attrs["turbine_count"]),
        "gfs_grid_cell_count": int(len(weights)),
        "u_wind_80m_capacity_weighted_mps": weighted_u,
        "v_wind_80m_capacity_weighted_mps": weighted_v,
        "wind_speed_80m_capacity_weighted_mps": weighted_speed,
        "wind_speed_80m_capacity_weighted_kmh": weighted_speed * 3.6,
        "wind_direction_80m_capacity_weighted_deg": (
            270.0 - math.degrees(math.atan2(weighted_v, weighted_u))
        )
        % 360.0,
        "generic_power_capacity_factor": weighted_capacity_factor,
        "generic_expected_generation_mw": (
            weighted_capacity_factor * capacity_mw
        ),
    }


def manifest_key(bucket: str, year: int, month: int) -> str:
    return (
        f"{bucket}/{RAW_PREFIX}/year={year:04d}/month={month:02d}/"
        "manifest.json"
    )


def read_month_manifest(
    fs: gcsfs.GCSFileSystem,
    *,
    bucket: str,
    year: int,
    month: int,
) -> dict[str, Any]:
    key = manifest_key(bucket, year, month)
    if not fs.exists(key):
        raise FileNotFoundError(f"gs://{key}")
    with fs.open(key, "rb") as handle:
        return json.load(handle)


def archive_urls(manifest: dict[str, Any]) -> tuple[str, ...]:
    urls = manifest.get("diagnostics", {}).get("archive_urls", [])
    if not urls:
        raise CapacityWeightingError(
            "manifest contains no GDEX archive URLs"
        )
    return tuple(sorted({str(url) for url in urls}))


def process_archive_url(
    session: requests.Session,
    url: str,
    *,
    expected: dict[str, ExpectedSource],
    weights: pd.DataFrame,
    mapping_cache: dict[
        tuple[Any, ...],
        tuple[np.ndarray, np.ndarray],
    ],
    timeout_seconds: float,
) -> tuple[list[dict[str, Any]], set[str]]:
    rows: list[dict[str, Any]] = []
    parsed: set[str] = set()
    with session.get(
        url,
        stream=True,
        timeout=(30, timeout_seconds),
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
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise CapacityWeightingError(
                        f"unable to read tar member {name}"
                    )
                rows.append(
                    parse_capacity_member(
                        extracted.read(),
                        source,
                        weights,
                        mapping_cache,
                    )
                )
                parsed.add(name)
                if len(parsed) % 500 == 0:
                    print(
                        f"  parsed {len(parsed):,}/{len(expected):,}",
                        flush=True,
                    )
    return rows, parsed


def aggregate_features(
    samples: pd.DataFrame,
    *,
    expected_daily_samples: int,
    expected_lead_days: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    daily = (
        samples.groupby(
            [
                "forecast_reference_time_utc",
                "forecast_cycle_hour_utc",
                "target_date",
                "lead_days",
                "fleet_cutoff_year",
            ],
            as_index=False,
        )
        .agg(
            wind_sample_count=(
                "wind_speed_80m_capacity_weighted_mps",
                "count",
            ),
            capacity_mw=("capacity_mw", "first"),
            turbine_count=("turbine_count", "first"),
            gfs_grid_cell_count=("gfs_grid_cell_count", "first"),
            wind_speed_80m_capacity_weighted_mps=(
                "wind_speed_80m_capacity_weighted_mps",
                "mean",
            ),
            wind_speed_80m_capacity_weighted_max_mps=(
                "wind_speed_80m_capacity_weighted_mps",
                "max",
            ),
            generic_power_capacity_factor=(
                "generic_power_capacity_factor",
                "mean",
            ),
            generic_expected_generation_mw=(
                "generic_expected_generation_mw",
                "mean",
            ),
        )
        .sort_values(
            ["forecast_reference_time_utc", "lead_days"]
        )
        .reset_index(drop=True)
    )
    if daily.empty:
        raise CapacityWeightingError("no daily aggregates produced")
    if daily["wind_sample_count"].lt(1).any():
        raise CapacityWeightingError("empty daily wind groups")
    daily["wind_sample_complete"] = daily[
        "wind_sample_count"
    ].eq(expected_daily_samples)
    daily["wind_speed_80m_capacity_weighted_kmh"] = (
        daily["wind_speed_80m_capacity_weighted_mps"] * 3.6
    )
    features = (
        daily.groupby(
            [
                "forecast_reference_time_utc",
                "forecast_cycle_hour_utc",
                "fleet_cutoff_year",
            ],
            as_index=False,
        )
        .agg(
            gfs_capacity_weighted_wind80_5d=(
                "wind_speed_80m_capacity_weighted_kmh",
                "mean",
            ),
            gfs_generic_power_cf_5d=(
                "generic_power_capacity_factor",
                "mean",
            ),
            gfs_generic_expected_generation_mw_5d=(
                "generic_expected_generation_mw",
                "mean",
            ),
            gfs_lead_count=("lead_days", "nunique"),
            gfs_min_daily_samples=("wind_sample_count", "min"),
            gfs_complete_daily_groups=(
                "wind_sample_complete",
                "sum",
            ),
            capacity_mw=("capacity_mw", "first"),
            turbine_count=("turbine_count", "first"),
            gfs_grid_cell_count=("gfs_grid_cell_count", "first"),
        )
        .sort_values("forecast_reference_time_utc")
        .reset_index(drop=True)
    )
    if not features["gfs_lead_count"].eq(
        expected_lead_days
    ).all():
        raise CapacityWeightingError(
            "not every initialization has every lead day"
        )
    features["nominal_issue_date"] = pd.to_datetime(
        features["forecast_reference_time_utc"],
        utc=True,
    ).dt.date
    return daily, features


def process_month(
    fs: gcsfs.GCSFileSystem,
    session: requests.Session,
    *,
    bucket: str,
    year: int,
    month: int,
    weights: pd.DataFrame,
    output_dir: Path,
    timeout_seconds: float,
    force: bool,
) -> dict[str, Any]:
    partition_dir = (
        output_dir / f"year={year:04d}" / f"month={month:02d}"
    )
    feature_path = partition_dir / "features.parquet"
    manifest_path = partition_dir / "manifest.json"
    if feature_path.exists() and manifest_path.exists() and not force:
        print(f"skip complete {year:04d}-{month:02d}", flush=True)
        return json.loads(manifest_path.read_text())
    start, end = month_bounds(year, month)
    expected = expected_sources(
        start,
        end,
        cycles=DEFAULT_CYCLES,
        lead_days=DEFAULT_LEAD_DAYS,
        valid_hours=DEFAULT_VALID_HOURS,
    )
    source_manifest = read_month_manifest(
        fs,
        bucket=bucket,
        year=year,
        month=month,
    )
    rows: list[dict[str, Any]] = []
    parsed: set[str] = set()
    mapping_cache: dict[
        tuple[Any, ...],
        tuple[np.ndarray, np.ndarray],
    ] = {}
    for url in archive_urls(source_manifest):
        archive_rows, archive_parsed = process_archive_url(
            session,
            url,
            expected=expected,
            weights=weights,
            mapping_cache=mapping_cache,
            timeout_seconds=timeout_seconds,
        )
        overlap = parsed.intersection(archive_parsed)
        if overlap:
            raise CapacityWeightingError(
                f"duplicate archive members: {sorted(overlap)[:5]}"
            )
        rows.extend(archive_rows)
        parsed.update(archive_parsed)
    samples = pd.DataFrame(rows).sort_values(
        ["forecast_reference_time_utc", "forecast_lead_hours"]
    ).reset_index(drop=True)
    if samples.empty:
        raise CapacityWeightingError(
            f"{year:04d}-{month:02d} produced no samples"
        )
    daily, features = aggregate_features(
        samples,
        expected_daily_samples=len(DEFAULT_VALID_HOURS),
        expected_lead_days=len(DEFAULT_LEAD_DAYS),
    )
    missing = sorted(set(expected).difference(parsed))
    partition_dir.mkdir(parents=True, exist_ok=True)
    samples.to_parquet(
        partition_dir / "source_samples.parquet",
        index=False,
        compression="zstd",
    )
    daily.to_parquet(
        partition_dir / "daily.parquet",
        index=False,
        compression="zstd",
    )
    features.to_parquet(
        feature_path,
        index=False,
        compression="zstd",
    )
    result = {
        "status": "complete",
        "year": year,
        "month": month,
        "coverage_start_issue_date": start.isoformat(),
        "coverage_end_issue_date": end.isoformat(),
        "expected_source_members": len(expected),
        "parsed_source_members": len(parsed),
        "missing_source_members": len(missing),
        "missing_source_member_names": missing,
        "rows": {
            "source_samples": len(samples),
            "daily": len(daily),
            "features": len(features),
        },
        "source_manifest_created_at_utc": source_manifest.get(
            "created_at_utc"
        ),
        "source_gdex_request_id": source_manifest.get(
            "gdex_request_id"
        ),
        "archive_urls": list(archive_urls(source_manifest)),
        "created_at_utc": utc_now_iso(),
    }
    manifest_path.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n"
    )
    print(
        f"completed {year:04d}-{month:02d}: "
        f"samples={len(samples):,}, features={len(features):,}, "
        f"missing={len(missing):,}",
        flush=True,
    )
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int, default=2016)
    parser.add_argument(
        "--months",
        type=parse_months,
        default=tuple(range(1, 13)),
    )
    parser.add_argument("--fleet-cutoff-year", type=int, default=2015)
    parser.add_argument("--bucket", default=BUCKET)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
    )
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--force", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.year < 2007:
        raise ValueError("GFS 0.25 archive is not available before 2007")
    if args.fleet_cutoff_year > args.year:
        raise ValueError("fleet cutoff cannot be after feature year")
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    turbines, uswtdb_metadata = fetch_uswtdb(
        session,
        timeout_seconds=args.timeout,
    )
    weights, fleet_diagnostics = build_grid_weights(
        turbines,
        fleet_cutoff_year=args.fleet_cutoff_year,
    )
    weights.attrs["fleet_cutoff_year"] = args.fleet_cutoff_year
    weights.attrs["turbine_count"] = fleet_diagnostics[
        "eligible_turbines_inside_bbox"
    ]
    root = args.output_dir / (
        f"weighting=uswtdb_capacity/fleet_cutoff_year="
        f"{args.fleet_cutoff_year:04d}"
    )
    root.mkdir(parents=True, exist_ok=True)
    weights.to_parquet(
        root / "grid_weights.parquet",
        index=False,
        compression="zstd",
    )
    metadata = {
        "dataset": (
            "Experimental NCAR GDEX wind weighted by USWTDB "
            "turbine rated capacity"
        ),
        "source_dataset_id": DATASET_ID,
        "model": MODEL,
        "year": args.year,
        "months": list(args.months),
        "uswtdb": uswtdb_metadata,
        "fleet": fleet_diagnostics,
        "generic_power_curve": {
            "cut_in_mps": 3.0,
            "rated_mps": 12.0,
            "cut_out_mps": 25.0,
            "ramp": "normalized cubic between cut-in and rated",
        },
        "historical_caveat": (
            "Current USWTDB snapshot filtered by commissioning year; "
            "not a historical vintage and may omit subsequently "
            "decommissioned turbines."
        ),
        "created_at_utc": utc_now_iso(),
    }
    (root / "metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(fleet_diagnostics, indent=2), flush=True)
    fs = gcsfs.GCSFileSystem(project=args.bucket)
    manifests = []
    for month in args.months:
        manifests.append(
            process_month(
                fs,
                session,
                bucket=args.bucket,
                year=args.year,
                month=month,
                weights=weights,
                output_dir=root,
                timeout_seconds=args.timeout,
                force=args.force,
            )
        )
    feature_paths = sorted(
        root.glob(f"year={args.year:04d}/month=*/features.parquet")
    )
    combined = pd.concat(
        [pd.read_parquet(path) for path in feature_paths],
        ignore_index=True,
    ).sort_values("forecast_reference_time_utc")
    combined.to_parquet(
        root / f"capacity_weighted_wind_features_{args.year:04d}.parquet",
        index=False,
        compression="zstd",
    )
    print(
        f"finished: months={len(manifests):,}, "
        f"features={len(combined):,}, output={root}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
