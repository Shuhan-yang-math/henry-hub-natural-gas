"""Backfill fixed-lead weather forecasts for U.S. natural-gas production regions.

This dataset is deliberately separate from the existing demand-city weather
dataset.  Every row is tagged as a production-region sample point.  The output
contains weather-based freeze-off risk proxies; it does *not* contain measured
production losses, pipeline nominations, or verified freeze-off volumes.

The representative points were selected within major EIA shale-play regions:
https://www.eia.gov/maps/oil-naturalgas.php

Open-Meteo Previous Runs provides fixed lead-time offsets.  For example,
``temperature_2m_previous_day3`` is aligned to the target hour and represents a
forecast made approximately 72 hours earlier.  It is not a complete model-run
archive, and exact historical API availability timestamps are not verified.

GCS layout
----------
Raw hourly point forecasts:
  raw/weather/open_meteo/production_regions/previous_runs/
    model=ncep_gfs_seamless/hourly/year=YYYY/month=MM/data.parquet

Processed daily point proxies:
  processed/weather/open_meteo/production_regions/previous_runs/
    model=ncep_gfs_seamless/point_daily/year=YYYY/month=MM/data.parquet

Processed daily region summaries:
  processed/weather/open_meteo/production_regions/previous_runs/
    model=ncep_gfs_seamless/region_daily/year=YYYY/month=MM/data.parquet
"""

from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import gcsfs
import numpy as np
import pandas as pd
import requests


ENDPOINT = "https://previous-runs-api.open-meteo.com/v1/forecast"
MODEL = "ncep_gfs_seamless"
from naturalgas.storage_config import PERSONAL_GCS_ROOT


BUCKET = PERSONAL_GCS_ROOT
RAW_PREFIX = (
    "raw/weather/open_meteo/production_regions/previous_runs/"
    "model=ncep_gfs_seamless"
)
PROCESSED_PREFIX = (
    "processed/weather/open_meteo/production_regions/previous_runs/"
    "model=ncep_gfs_seamless"
)
LEAD_DAYS = (1, 2, 3, 4, 5)
TEMPERATURE_START = date(2021, 3, 24)
DEFAULT_END = date.today() + timedelta(days=max(LEAD_DAYS))
EIA_MAP_SOURCE = "https://www.eia.gov/maps/oil-naturalgas.php"
LOCATION_TYPE = "production_region_sample_point"


class HourlyApiLimitExceeded(RuntimeError):
    """Raised when Open-Meteo asks the client to wait for the next hour."""


@dataclass(frozen=True)
class ProductionLocation:
    location_id: str
    production_region: str
    play_or_subregion: str
    state: str
    latitude: float
    longitude: float
    location_type: str = LOCATION_TYPE
    coordinate_method: str = (
        "representative sample point within EIA shale-play region; "
        "not well-count or production weighted"
    )


# Multiple points per production region reduce dependence on a single model
# grid cell. Coordinates are representative research sample points within the
# broad EIA play boundaries, not precise well or facility locations.
PRODUCTION_LOCATIONS = (
    # Appalachia: Marcellus and Utica/Point Pleasant.
    ProductionLocation(
        "appalachia_ne_pa", "Appalachia", "Marcellus - northeast Pennsylvania",
        "PA", 41.55, -76.05,
    ),
    ProductionLocation(
        "appalachia_sw_pa", "Appalachia", "Marcellus - southwest Pennsylvania",
        "PA", 40.25, -80.00,
    ),
    ProductionLocation(
        "appalachia_n_wv", "Appalachia", "Marcellus - northern West Virginia",
        "WV", 39.40, -80.40,
    ),
    ProductionLocation(
        "appalachia_se_oh", "Appalachia", "Utica - southeast Ohio",
        "OH", 40.00, -81.20,
    ),
    # Permian: Delaware and Midland sub-basins.
    ProductionLocation(
        "permian_delaware_nm", "Permian", "Delaware Basin - New Mexico",
        "NM", 32.30, -104.10,
    ),
    ProductionLocation(
        "permian_delaware_tx", "Permian", "Delaware Basin - west Texas",
        "TX", 31.70, -103.60,
    ),
    ProductionLocation(
        "permian_midland_n", "Permian", "Midland Basin - north",
        "TX", 32.20, -101.80,
    ),
    ProductionLocation(
        "permian_midland_s", "Permian", "Midland Basin - south",
        "TX", 31.30, -101.80,
    ),
    # Haynesville-Bossier.
    ProductionLocation(
        "haynesville_nw_la", "Haynesville", "Haynesville-Bossier - northwest Louisiana",
        "LA", 32.20, -93.70,
    ),
    ProductionLocation(
        "haynesville_n_la", "Haynesville", "Haynesville-Bossier - north Louisiana",
        "LA", 32.70, -93.40,
    ),
    ProductionLocation(
        "haynesville_e_tx", "Haynesville", "Haynesville-Bossier - east Texas",
        "TX", 31.80, -94.20,
    ),
    # Williston/Bakken.
    ProductionLocation(
        "bakken_williston", "Bakken", "Bakken/Three Forks - Williston area",
        "ND", 48.15, -103.62,
    ),
    ProductionLocation(
        "bakken_watford_city", "Bakken", "Bakken/Three Forks - Watford City area",
        "ND", 47.80, -103.28,
    ),
    ProductionLocation(
        "bakken_dickinson", "Bakken", "Bakken/Three Forks - southern area",
        "ND", 46.88, -102.79,
    ),
    # Eagle Ford.
    ProductionLocation(
        "eagle_ford_west", "Eagle Ford", "Eagle Ford - west",
        "TX", 28.60, -99.80,
    ),
    ProductionLocation(
        "eagle_ford_central", "Eagle Ford", "Eagle Ford - central",
        "TX", 28.80, -98.60,
    ),
    ProductionLocation(
        "eagle_ford_east", "Eagle Ford", "Eagle Ford - east",
        "TX", 29.50, -97.40,
    ),
    # Anadarko/Woodford and Granite Wash.
    ProductionLocation(
        "anadarko_central_ok", "Anadarko", "Woodford/STACK-SCOOP - central Oklahoma",
        "OK", 35.30, -98.40,
    ),
    ProductionLocation(
        "anadarko_nw_ok", "Anadarko", "Woodford - northwest Oklahoma",
        "OK", 36.20, -98.50,
    ),
    ProductionLocation(
        "anadarko_granite_wash", "Anadarko", "Granite Wash - Texas/Oklahoma Panhandle",
        "TX", 35.70, -100.40,
    ),
    # Rocky Mountain production areas.
    ProductionLocation(
        "rockies_piceance", "Rockies", "Piceance Basin",
        "CO", 39.60, -108.20,
    ),
    ProductionLocation(
        "rockies_green_river", "Rockies", "Greater Green River Basin",
        "WY", 41.50, -109.50,
    ),
    ProductionLocation(
        "rockies_denver_julesburg", "Rockies", "Denver-Julesburg/Niobrara",
        "CO", 40.50, -104.60,
    ),
    ProductionLocation(
        "rockies_san_juan", "Rockies", "San Juan Basin",
        "NM", 36.70, -107.60,
    ),
)


def chunks(
    items: tuple[ProductionLocation, ...], size: int
) -> Iterable[tuple[ProductionLocation, ...]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def month_ranges(start: date, end: date) -> list[tuple[date, date]]:
    starts = pd.date_range(start=start.replace(day=1), end=end, freq="MS").date
    return [
        (
            max(start, month_start),
            min(
                end,
                (pd.Timestamp(month_start) + pd.offsets.MonthEnd(0)).date(),
            ),
        )
        for month_start in starts
    ]


def temperature_fields() -> list[str]:
    return [
        f"temperature_2m_previous_day{lead}" for lead in LEAD_DAYS
    ]


def request_batch(
    locations: tuple[ProductionLocation, ...],
    start: date,
    end: date,
    attempts: int = 6,
) -> list[dict]:
    params = {
        "latitude": ",".join(str(location.latitude) for location in locations),
        "longitude": ",".join(str(location.longitude) for location in locations),
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "hourly": ",".join(temperature_fields()),
        "timezone": "GMT",
        "models": MODEL,
    }
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(ENDPOINT, params=params, timeout=180)
            if (
                response.status_code == 429
                and "Hourly API request limit exceeded" in response.text
            ):
                raise HourlyApiLimitExceeded(response.text)
            if response.status_code in {429, 500, 502, 503, 504}:
                raise RuntimeError(
                    f"retryable HTTP {response.status_code}: "
                    f"{response.text[:200]}"
                )
            response.raise_for_status()
            payload = response.json()
            payloads = payload if isinstance(payload, list) else [payload]
            if len(payloads) != len(locations):
                raise RuntimeError(
                    f"expected {len(locations)} locations, "
                    f"received {len(payloads)}"
                )
            return payloads
        except HourlyApiLimitExceeded:
            raise
        except Exception as exc:
            last_error = exc
            if attempt == attempts:
                break
            time.sleep(min(2**attempt, 30))
    raise RuntimeError(
        f"request failed for {start}..{end}, "
        f"{locations[0].location_id}..{locations[-1].location_id}"
    ) from last_error


def payload_to_long(
    payload: dict, location: ProductionLocation
) -> pd.DataFrame:
    hourly = payload["hourly"]
    target_time = pd.to_datetime(hourly["time"], utc=True)
    frames = []
    for lead in LEAD_DAYS:
        temperatures = pd.to_numeric(
            pd.Series(hourly[f"temperature_2m_previous_day{lead}"]),
            errors="coerce",
        ).astype("float32")
        frame = pd.DataFrame(
            {
                "model": MODEL,
                "location_type": location.location_type,
                "location_id": location.location_id,
                "production_region": location.production_region,
                "play_or_subregion": location.play_or_subregion,
                "state": location.state,
                "requested_latitude": np.float32(location.latitude),
                "requested_longitude": np.float32(location.longitude),
                "grid_latitude": np.float32(payload["latitude"]),
                "grid_longitude": np.float32(payload["longitude"]),
                "elevation_m": np.float32(payload.get("elevation", np.nan)),
                "target_time_utc": target_time,
                "target_date": target_time.date,
                "lead_days": np.int8(lead),
                "lead_hours": np.int16(lead * 24),
                "nominal_forecast_time_utc": (
                    target_time - pd.Timedelta(days=lead)
                ),
                "temperature_2m_c": temperatures,
                "historical_availability_verified": False,
                "is_measured_production": False,
                "is_measured_freezeoff": False,
            }
        )
        frames.append(frame)
    return pd.concat(frames, ignore_index=True)


def fetch_month(
    start: date,
    end: date,
    batch_size: int,
    workers: int,
) -> pd.DataFrame:
    batches = list(chunks(PRODUCTION_LOCATIONS, batch_size))
    collected: list[pd.DataFrame] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(request_batch, batch, start, end): batch
            for batch in batches
        }
        for future in as_completed(futures):
            batch = futures[future]
            payloads = future.result()
            for payload, location in zip(payloads, batch, strict=True):
                collected.append(payload_to_long(payload, location))
    hourly = pd.concat(collected, ignore_index=True)
    hourly = hourly.dropna(subset=["temperature_2m_c"])
    # Requests extend through today + max lead so today's full forward horizon
    # can be assembled. Do not retain rows whose nominal issue day is still in
    # the future, even if the API happens to populate them.
    hourly = hourly.loc[
        hourly["nominal_forecast_time_utc"].dt.date.le(date.today())
    ].copy()
    hourly = hourly.sort_values(
        ["target_time_utc", "production_region", "location_id", "lead_days"]
    ).reset_index(drop=True)
    key = ["location_id", "target_time_utc", "lead_days"]
    if hourly.duplicated(key).any():
        raise RuntimeError("duplicate production location/target/lead rows")
    return hourly


def longest_true_run(values: pd.Series) -> int:
    array = values.fillna(False).to_numpy(dtype=bool)
    longest = current = 0
    for value in array:
        current = current + 1 if value else 0
        longest = max(longest, current)
    return int(longest)


def point_daily_proxies(hourly: pd.DataFrame) -> pd.DataFrame:
    data = hourly.copy()
    temperature = data["temperature_2m_c"]
    data["below_0c"] = temperature.lt(0)
    data["below_minus5c"] = temperature.lt(-5)
    data["below_minus10c"] = temperature.lt(-10)
    data["freeze_degree_c"] = (-temperature).clip(lower=0)
    data["severe_freeze_degree_c"] = (-5 - temperature).clip(lower=0)
    data["extreme_freeze_degree_c"] = (-10 - temperature).clip(lower=0)
    # Nonlinear weather-only severity. It is intentionally unitless and must
    # not be interpreted as Bcf/d of lost production.
    data["heuristic_hourly_freezeoff_risk"] = (
        data["freeze_degree_c"] ** np.float32(1.5)
    )

    group_columns = [
        "model", "location_type", "location_id", "production_region",
        "play_or_subregion", "state", "requested_latitude",
        "requested_longitude", "grid_latitude", "grid_longitude",
        "target_date", "lead_days", "lead_hours",
    ]
    result = (
        data.groupby(group_columns, observed=True, dropna=False, as_index=False)
        .agg(
            temperature_mean_c=("temperature_2m_c", "mean"),
            temperature_min_c=("temperature_2m_c", "min"),
            temperature_max_c=("temperature_2m_c", "max"),
            hours_with_temperature=("temperature_2m_c", "count"),
            hours_below_0c=("below_0c", "sum"),
            hours_below_minus5c=("below_minus5c", "sum"),
            hours_below_minus10c=("below_minus10c", "sum"),
            max_consecutive_hours_below_0c=("below_0c", longest_true_run),
            max_consecutive_hours_below_minus5c=(
                "below_minus5c", longest_true_run
            ),
            freeze_degree_hours_0c=("freeze_degree_c", "sum"),
            freeze_degree_hours_minus5c=(
                "severe_freeze_degree_c", "sum"
            ),
            freeze_degree_hours_minus10c=(
                "extreme_freeze_degree_c", "sum"
            ),
            heuristic_freezeoff_weather_risk=(
                "heuristic_hourly_freezeoff_risk", "sum"
            ),
        )
    )
    result["nominal_issue_date"] = (
        pd.to_datetime(result["target_date"])
        - pd.to_timedelta(result["lead_days"], unit="D")
    )
    result["historical_availability_verified"] = False
    result["is_measured_production"] = False
    result["is_measured_freezeoff"] = False
    return result.sort_values(
        ["target_date", "production_region", "location_id", "lead_days"]
    ).reset_index(drop=True)


def region_daily_proxies(point_daily: pd.DataFrame) -> pd.DataFrame:
    group_columns = [
        "model", "location_type", "production_region", "target_date",
        "nominal_issue_date", "lead_days", "lead_hours",
    ]
    result = (
        point_daily.groupby(
            group_columns, observed=True, dropna=False, as_index=False
        )
        .agg(
            point_count=("location_id", "nunique"),
            region_temperature_mean_c=("temperature_mean_c", "mean"),
            region_coldest_point_min_c=("temperature_min_c", "min"),
            region_mean_hours_below_0c=("hours_below_0c", "mean"),
            region_mean_hours_below_minus5c=(
                "hours_below_minus5c", "mean"
            ),
            region_mean_hours_below_minus10c=(
                "hours_below_minus10c", "mean"
            ),
            region_mean_freeze_degree_hours_0c=(
                "freeze_degree_hours_0c", "mean"
            ),
            region_mean_freeze_degree_hours_minus5c=(
                "freeze_degree_hours_minus5c", "mean"
            ),
            region_mean_freeze_degree_hours_minus10c=(
                "freeze_degree_hours_minus10c", "mean"
            ),
            region_mean_heuristic_freezeoff_weather_risk=(
                "heuristic_freezeoff_weather_risk", "mean"
            ),
        )
    )
    result["location_type"] = "production_region_equal_weight_summary"
    result["aggregation_method"] = (
        "equal weight across representative production-region sample points"
    )
    result["production_weighted"] = False
    result["historical_availability_verified"] = False
    result["is_measured_production"] = False
    result["is_measured_freezeoff"] = False
    return result.sort_values(
        ["target_date", "production_region", "lead_days"]
    ).reset_index(drop=True)


def gcs_partition(prefix: str, year: int, month: int) -> str:
    return (
        f"{BUCKET}/{prefix}/year={year:04d}/month={month:02d}/data.parquet"
    )


def upload_frame(
    fs: gcsfs.GCSFileSystem,
    frame: pd.DataFrame,
    local_path: Path,
    remote_path: str,
) -> int:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(local_path, index=False, compression="zstd")
    fs.put_file(str(local_path), remote_path)
    return local_path.stat().st_size


def write_metadata(
    fs: gcsfs.GCSFileSystem,
    work_dir: Path,
    completed: list[dict],
    errors: list[dict],
    started_at: datetime,
    start_requested: date,
    end_requested: date,
) -> None:
    metadata_dir = work_dir / "metadata"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    locations_path = metadata_dir / "production_locations.csv"
    pd.DataFrame(
        [asdict(location) for location in PRODUCTION_LOCATIONS]
    ).to_csv(locations_path, index=False)
    manifest = {
        "dataset": (
            "Open-Meteo Previous Runs weather for U.S. natural-gas "
            "production regions"
        ),
        "dataset_role": "production-region freeze-off weather proxy",
        "model": MODEL,
        "endpoint": ENDPOINT,
        "location_type": LOCATION_TYPE,
        "location_count": len(PRODUCTION_LOCATIONS),
        "production_regions": sorted(
            {location.production_region for location in PRODUCTION_LOCATIONS}
        ),
        "lead_days": list(LEAD_DAYS),
        "variables": ["temperature_2m"],
        "temperature_thresholds_c": [0, -5, -10],
        "heuristic_freezeoff_weather_risk_formula": (
            "daily sum over hourly max(0, -temperature_c)^1.5"
        ),
        "production_weighted": False,
        "is_measured_production": False,
        "is_measured_freezeoff": False,
        "historical_availability_verified": False,
        "important_limitations": [
            "Weather proxy only; not measured production loss or freeze-off.",
            "Representative coordinates are not well-count or production weighted.",
            "Region summaries equally weight sample points.",
            "Previous Runs fixed leads are not a complete model-run archive.",
            "Exact historical API availability timestamps are not verified.",
        ],
        "coordinate_source": EIA_MAP_SOURCE,
        "temperature_start_requested": start_requested.isoformat(),
        "end_requested": end_requested.isoformat(),
        "started_at_utc": started_at.isoformat(),
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "completed_partitions": completed,
        "errors": errors,
    }
    manifest_path = metadata_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    fs.put_file(
        str(locations_path),
        f"{BUCKET}/{RAW_PREFIX}/metadata/production_locations.csv",
    )
    fs.put_file(
        str(manifest_path),
        f"{BUCKET}/{RAW_PREFIX}/metadata/manifest.json",
    )


def run_backfill(
    start: date = TEMPERATURE_START,
    end: date = DEFAULT_END,
    work_dir: Path | None = None,
    batch_size: int = 8,
    workers: int = 3,
    force: bool = False,
) -> tuple[list[dict], list[dict]]:
    if start < TEMPERATURE_START:
        raise ValueError(f"start must be on or after {TEMPERATURE_START}")
    if end < start:
        raise ValueError("end must be on or after start")
    if work_dir is None:
        work_dir = (
            Path(__file__).resolve().parent
            / "processed"
            / "production_region_freezeoff_backfill"
        )
    work_dir.mkdir(parents=True, exist_ok=True)
    fs = gcsfs.GCSFileSystem()
    started_at = datetime.now(timezone.utc)
    completed: list[dict] = []
    errors: list[dict] = []
    ranges = month_ranges(start, end)

    for index, (month_start, month_end) in enumerate(ranges, start=1):
        year, month = month_start.year, month_start.month
        raw_remote = gcs_partition(
            RAW_PREFIX + "/hourly", year, month
        )
        point_remote = gcs_partition(
            PROCESSED_PREFIX + "/point_daily", year, month
        )
        region_remote = gcs_partition(
            PROCESSED_PREFIX + "/region_daily", year, month
        )
        all_exist = all(
            fs.exists(path)
            for path in [raw_remote, point_remote, region_remote]
        )
        if all_exist and not force:
            print(
                f"[{index:02d}/{len(ranges):02d}] {year}-{month:02d} "
                "production-region partitions already exist; skipped",
                flush=True,
            )
            completed.append(
                {"year": year, "month": month, "status": "skipped_existing"}
            )
            continue

        print(
            f"[{index:02d}/{len(ranges):02d}] production-region weather "
            f"{month_start}..{month_end}; "
            f"{len(PRODUCTION_LOCATIONS)} points",
            flush=True,
        )
        try:
            hourly = fetch_month(
                month_start, month_end, batch_size=batch_size, workers=workers
            )
            point_daily = point_daily_proxies(hourly)
            region_daily = region_daily_proxies(point_daily)
            local_month = (
                work_dir / f"year={year:04d}" / f"month={month:02d}"
            )
            raw_bytes = upload_frame(
                fs, hourly, local_month / "hourly.parquet", raw_remote
            )
            point_bytes = upload_frame(
                fs,
                point_daily,
                local_month / "point_daily.parquet",
                point_remote,
            )
            region_bytes = upload_frame(
                fs,
                region_daily,
                local_month / "region_daily.parquet",
                region_remote,
            )
            completed.append(
                {
                    "year": year,
                    "month": month,
                    "status": "uploaded",
                    "start": month_start.isoformat(),
                    "end": month_end.isoformat(),
                    "hourly_rows": len(hourly),
                    "point_daily_rows": len(point_daily),
                    "region_daily_rows": len(region_daily),
                    "raw_bytes": raw_bytes,
                    "point_daily_bytes": point_bytes,
                    "region_daily_bytes": region_bytes,
                    "raw_gcs_uri": f"gs://{raw_remote}",
                    "point_daily_gcs_uri": f"gs://{point_remote}",
                    "region_daily_gcs_uri": f"gs://{region_remote}",
                }
            )
        except Exception as exc:
            error = {
                "year": year,
                "month": month,
                "status": "error",
                "error_type": type(exc).__name__,
                "message": str(exc),
            }
            errors.append(error)
            print(f"ERROR {year}-{month:02d}: {error}", flush=True)
            write_metadata(
                fs, work_dir, completed, errors, started_at, start, end
            )
            if isinstance(exc, HourlyApiLimitExceeded):
                break

    write_metadata(
        fs, work_dir, completed, errors, started_at, start, end
    )
    return completed, errors


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill production-region temperature forecasts and "
            "weather-based freeze-off proxies to GCS."
        )
    )
    parser.add_argument(
        "--start", type=parse_date, default=TEMPERATURE_START
    )
    parser.add_argument("--end", type=parse_date, default=DEFAULT_END)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument(
        "--upload",
        action="store_true",
        help="Explicitly authorize writes to the configured GCS bucket.",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.upload:
        parser.error("refusing GCS writes without --upload")

    print("DATASET ROLE: production-region freeze-off weather proxy")
    print("NOT measured production and NOT measured freeze-off volume")
    print(f"model={MODEL} | locations={len(PRODUCTION_LOCATIONS)}")
    print(f"raw prefix=gs://{BUCKET}/{RAW_PREFIX}")
    print(f"processed prefix=gs://{BUCKET}/{PROCESSED_PREFIX}")
    completed, errors = run_backfill(
        start=args.start,
        end=args.end,
        work_dir=args.work_dir,
        batch_size=args.batch_size,
        workers=args.workers,
        force=args.force,
    )
    uploaded = sum(row.get("status") == "uploaded" for row in completed)
    skipped = sum(
        row.get("status") == "skipped_existing" for row in completed
    )
    print(
        f"finished: uploaded={uploaded}, skipped={skipped}, "
        f"errors={len(errors)}"
    )
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
