"""Backfill fixed-lead NCEP GFS weather forecasts for U.S. gas demand centers.

The Open-Meteo Previous Runs API exposes fixed lead offsets.  A value named
``*_previous_day3`` is aligned to the target time and was predicted 72 hours
earlier.  It is not a full model-run archive and does not prove the historical
API availability timestamp.

Outputs are partitioned by target year/month and are safe to resume: an
existing GCS partition is skipped unless ``--force`` is supplied.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
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
    "raw/weather/open_meteo/previous_runs/"
    "model=ncep_gfs_seamless"
)
PROCESSED_PREFIX = (
    "processed/weather/open_meteo/previous_runs/"
    "model=ncep_gfs_seamless"
)
LEAD_DAYS = (1, 2, 3, 4, 5)
ALL_VARIABLES = (
    "temperature_2m",
    "precipitation",
    "cloud_cover",
    "snowfall",
    "wind_speed_10m",
    "wind_speed_80m",
)
TEMPERATURE_START = date(2021, 3, 24)
FULL_WEATHER_START = date(2024, 1, 19)
DEFAULT_END = date.today()


class HourlyApiLimitExceeded(RuntimeError):
    """Raised when Open-Meteo asks the client to wait for the next hour."""


@dataclass(frozen=True)
class Location:
    location_id: str
    city: str
    state: str
    census_division: str
    latitude: float
    longitude: float


# Representative load centers spanning all nine U.S. Census divisions.
# Raw city-level data are retained so weighting can be changed later.
LOCATIONS = (
    Location("boston_ma", "Boston", "MA", "New England", 42.3601, -71.0589),
    Location("new_york_ny", "New York", "NY", "Middle Atlantic", 40.7128, -74.0060),
    Location("philadelphia_pa", "Philadelphia", "PA", "Middle Atlantic", 39.9526, -75.1652),
    Location("pittsburgh_pa", "Pittsburgh", "PA", "Middle Atlantic", 40.4406, -79.9959),
    Location("buffalo_ny", "Buffalo", "NY", "Middle Atlantic", 42.8864, -78.8784),
    Location("chicago_il", "Chicago", "IL", "East North Central", 41.8781, -87.6298),
    Location("detroit_mi", "Detroit", "MI", "East North Central", 42.3314, -83.0458),
    Location("cleveland_oh", "Cleveland", "OH", "East North Central", 41.4993, -81.6944),
    Location("minneapolis_mn", "Minneapolis", "MN", "West North Central", 44.9778, -93.2650),
    Location("st_louis_mo", "St. Louis", "MO", "West North Central", 38.6270, -90.1994),
    Location("kansas_city_mo", "Kansas City", "MO", "West North Central", 39.0997, -94.5786),
    Location("washington_dc", "Washington", "DC", "South Atlantic", 38.9072, -77.0369),
    Location("charlotte_nc", "Charlotte", "NC", "South Atlantic", 35.2271, -80.8431),
    Location("atlanta_ga", "Atlanta", "GA", "South Atlantic", 33.7490, -84.3880),
    Location("jacksonville_fl", "Jacksonville", "FL", "South Atlantic", 30.3322, -81.6557),
    Location("miami_fl", "Miami", "FL", "South Atlantic", 25.7617, -80.1918),
    Location("nashville_tn", "Nashville", "TN", "East South Central", 36.1627, -86.7816),
    Location("birmingham_al", "Birmingham", "AL", "East South Central", 33.5186, -86.8104),
    Location("dallas_tx", "Dallas", "TX", "West South Central", 32.7767, -96.7970),
    Location("houston_tx", "Houston", "TX", "West South Central", 29.7604, -95.3698),
    Location("new_orleans_la", "New Orleans", "LA", "West South Central", 29.9511, -90.0715),
    Location("oklahoma_city_ok", "Oklahoma City", "OK", "West South Central", 35.4676, -97.5164),
    Location("denver_co", "Denver", "CO", "Mountain", 39.7392, -104.9903),
    Location("salt_lake_city_ut", "Salt Lake City", "UT", "Mountain", 40.7608, -111.8910),
    Location("phoenix_az", "Phoenix", "AZ", "Mountain", 33.4484, -112.0740),
    Location("los_angeles_ca", "Los Angeles", "CA", "Pacific", 34.0522, -118.2437),
    Location("san_francisco_ca", "San Francisco", "CA", "Pacific", 37.7749, -122.4194),
    Location("seattle_wa", "Seattle", "WA", "Pacific", 47.6062, -122.3321),
)


def chunks(items: tuple[Location, ...], size: int) -> Iterable[tuple[Location, ...]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def month_ranges(start: date, end: date) -> list[tuple[date, date]]:
    starts = pd.date_range(start=start.replace(day=1), end=end, freq="MS").date
    ranges = []
    for month_start in starts:
        month_end = (
            pd.Timestamp(month_start) + pd.offsets.MonthEnd(0)
        ).date()
        ranges.append((max(start, month_start), min(end, month_end)))
    return ranges


def fields_for(variables: tuple[str, ...]) -> list[str]:
    return [
        f"{variable}_previous_day{lead}"
        for variable in variables
        for lead in LEAD_DAYS
    ]


def request_batch(
    locations: tuple[Location, ...],
    start: date,
    end: date,
    variables: tuple[str, ...],
    attempts: int = 5,
) -> list[dict]:
    params = {
        "latitude": ",".join(str(location.latitude) for location in locations),
        "longitude": ",".join(str(location.longitude) for location in locations),
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "hourly": ",".join(fields_for(variables)),
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
                    f"retryable HTTP {response.status_code}: {response.text[:200]}"
                )
            response.raise_for_status()
            payload = response.json()
            payloads = payload if isinstance(payload, list) else [payload]
            if len(payloads) != len(locations):
                raise RuntimeError(
                    f"expected {len(locations)} locations, received {len(payloads)}"
                )
            return payloads
        except HourlyApiLimitExceeded:
            raise
        except Exception as exc:
            last_error = exc
            if attempt == attempts:
                break
            time.sleep(min(2**attempt, 20))
    raise RuntimeError(
        f"request failed for {start} to {end}, "
        f"{locations[0].location_id}..{locations[-1].location_id}"
    ) from last_error


def payload_to_long(
    payload: dict,
    location: Location,
    variables_requested: tuple[str, ...],
) -> pd.DataFrame:
    hourly = payload["hourly"]
    target_time = pd.to_datetime(hourly["time"], utc=True)
    frames = []
    for lead in LEAD_DAYS:
        frame = pd.DataFrame(
            {
                "model": MODEL,
                "location_id": location.location_id,
                "city": location.city,
                "state": location.state,
                "census_division": location.census_division,
                "requested_latitude": np.float32(location.latitude),
                "requested_longitude": np.float32(location.longitude),
                "grid_latitude": np.float32(payload["latitude"]),
                "grid_longitude": np.float32(payload["longitude"]),
                "elevation_m": np.float32(payload.get("elevation", np.nan)),
                "target_time_utc": target_time,
                "lead_days": np.int8(lead),
                "lead_hours": np.int16(lead * 24),
                "nominal_forecast_time_utc": (
                    target_time - pd.Timedelta(days=lead)
                ),
                "historical_availability_verified": False,
            }
        )
        for variable in ALL_VARIABLES:
            key = f"{variable}_previous_day{lead}"
            if variable in variables_requested:
                frame[variable] = pd.to_numeric(
                    pd.Series(hourly[key]), errors="coerce"
                ).astype("float32")
            else:
                frame[variable] = np.float32(np.nan)
        frames.append(frame)
    return pd.concat(frames, ignore_index=True)


def fetch_month(
    start: date,
    end: date,
    variables: tuple[str, ...],
    batch_size: int,
    workers: int,
) -> pd.DataFrame:
    location_batches = list(chunks(LOCATIONS, batch_size))
    collected: list[pd.DataFrame] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(request_batch, batch, start, end, variables): batch
            for batch in location_batches
        }
        for future in as_completed(future_map):
            batch = future_map[future]
            payloads = future.result()
            for payload, location in zip(payloads, batch, strict=True):
                collected.append(
                    payload_to_long(payload, location, variables)
                )

    result = pd.concat(collected, ignore_index=True)
    result = result.dropna(subset=list(ALL_VARIABLES), how="all")
    result["target_date"] = result["target_time_utc"].dt.date
    result = result.sort_values(
        ["target_time_utc", "location_id", "lead_days"]
    ).reset_index(drop=True)
    key = ["location_id", "target_time_utc", "lead_days"]
    if result.duplicated(key).any():
        raise RuntimeError("duplicate location/target/lead rows")
    return result


def make_daily(hourly: pd.DataFrame) -> pd.DataFrame:
    grouped = (
        hourly.groupby(
            [
                "model",
                "location_id",
                "city",
                "state",
                "census_division",
                "requested_latitude",
                "requested_longitude",
                "target_date",
                "lead_days",
                "lead_hours",
            ],
            observed=True,
            dropna=False,
            as_index=False,
        )
        .agg(
            temperature_mean_c=("temperature_2m", "mean"),
            temperature_min_c=("temperature_2m", "min"),
            temperature_max_c=("temperature_2m", "max"),
            precipitation_sum_mm=("precipitation", lambda x: x.sum(min_count=1)),
            cloud_cover_mean_pct=("cloud_cover", "mean"),
            snowfall_sum_cm=("snowfall", lambda x: x.sum(min_count=1)),
            wind_speed_10m_mean_kmh=("wind_speed_10m", "mean"),
            wind_speed_10m_max_kmh=("wind_speed_10m", "max"),
            wind_speed_80m_mean_kmh=("wind_speed_80m", "mean"),
            wind_speed_80m_max_kmh=("wind_speed_80m", "max"),
            hours_with_temperature=("temperature_2m", "count"),
        )
    )
    # Standard U.S. 65°F = 18.333°C degree-day base, using forecast daily mean.
    base_c = np.float32(18.333333)
    grouped["hdd18c"] = (base_c - grouped["temperature_mean_c"]).clip(
        lower=0
    )
    grouped["cdd18c"] = (grouped["temperature_mean_c"] - base_c).clip(
        lower=0
    )
    # Fahrenheit degree-day differences are 9/5 times Celsius differences.
    grouped["hdd65_f"] = grouped["hdd18c"] * np.float32(1.8)
    grouped["cdd65_f"] = grouped["cdd18c"] * np.float32(1.8)
    return grouped.sort_values(
        ["target_date", "location_id", "lead_days"]
    ).reset_index(drop=True)


def gcs_path(prefix: str, year: int, month: int) -> str:
    return f"{BUCKET}/{prefix}/year={year:04d}/month={month:02d}/data.parquet"


def upload_file(
    fs: gcsfs.GCSFileSystem,
    local_path: Path,
    remote_path: str,
) -> None:
    fs.put_file(str(local_path), remote_path)


def write_metadata(
    fs: gcsfs.GCSFileSystem,
    work_dir: Path,
    completed: list[dict],
    errors: list[dict],
    started_at: datetime,
) -> None:
    metadata_dir = work_dir / "metadata"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    locations_path = metadata_dir / "locations.csv"
    pd.DataFrame([asdict(location) for location in LOCATIONS]).to_csv(
        locations_path, index=False
    )
    manifest = {
        "dataset": "Open-Meteo Previous Runs fixed-lead forecasts",
        "model": MODEL,
        "endpoint": ENDPOINT,
        "lead_days": list(LEAD_DAYS),
        "variables": list(ALL_VARIABLES),
        "temperature_start_requested": TEMPERATURE_START.isoformat(),
        "full_weather_start_requested": FULL_WEATHER_START.isoformat(),
        "end_requested": DEFAULT_END.isoformat(),
        "location_count": len(LOCATIONS),
        "semantics": (
            "Fixed lead offsets; historical API availability is not verified. "
            "Not a complete model-run archive."
        ),
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
    upload_file(fs, locations_path, f"{BUCKET}/{RAW_PREFIX}/metadata/locations.csv")
    upload_file(fs, manifest_path, f"{BUCKET}/{RAW_PREFIX}/metadata/manifest.json")


def repair_daily_partitions(work_dir: Path | None = None) -> int:
    """Rebuild every daily partition from the normalized hourly source."""
    if work_dir is None:
        work_dir = (
            Path(__file__).resolve().parent
            / "processed"
            / "us_ng_weather_backfill"
            / "daily_repair"
        )
    work_dir.mkdir(parents=True, exist_ok=True)
    fs = gcsfs.GCSFileSystem()
    pattern = f"{BUCKET}/{RAW_PREFIX}/hourly/year=*/month=*/data.parquet"
    raw_paths = sorted(fs.glob(pattern))
    repaired = 0
    for index, raw_path in enumerate(raw_paths, start=1):
        match = re.search(r"/year=(\d{4})/month=(\d{2})/", raw_path)
        if not match:
            raise RuntimeError(f"cannot parse partition from {raw_path}")
        year, month = map(int, match.groups())
        with fs.open(raw_path, "rb") as handle:
            hourly = pd.read_parquet(handle)
        daily = make_daily(hourly)
        local_path = work_dir / f"{year:04d}-{month:02d}.parquet"
        daily.to_parquet(local_path, index=False, compression="zstd")
        remote_path = gcs_path(PROCESSED_PREFIX + "/daily", year, month)
        upload_file(fs, local_path, remote_path)
        repaired += 1
        print(
            f"[{index:02d}/{len(raw_paths):02d}] rebuilt "
            f"{year:04d}-{month:02d}: {len(daily):,} rows",
            flush=True,
        )
    return repaired


def run_backfill(
    end: date = DEFAULT_END,
    work_dir: Path | None = None,
    batch_size: int = 5,
    workers: int = 3,
    force: bool = False,
) -> tuple[list[dict], list[dict]]:
    if end < TEMPERATURE_START:
        raise ValueError(f"end must be on or after {TEMPERATURE_START}")
    if work_dir is None:
        work_dir = Path(__file__).resolve().parent / "processed" / "us_ng_weather_backfill"
    work_dir.mkdir(parents=True, exist_ok=True)
    fs = gcsfs.GCSFileSystem()
    started_at = datetime.now(timezone.utc)
    completed: list[dict] = []
    errors: list[dict] = []

    ranges = month_ranges(TEMPERATURE_START, end)
    for index, (start, month_end) in enumerate(ranges, start=1):
        variables = (
            ("temperature_2m",)
            if month_end < FULL_WEATHER_START
            else ALL_VARIABLES
        )
        year, month = start.year, start.month
        raw_remote = gcs_path(RAW_PREFIX + "/hourly", year, month)
        daily_remote = gcs_path(PROCESSED_PREFIX + "/daily", year, month)

        if not force and fs.exists(raw_remote) and fs.exists(daily_remote):
            print(
                f"[{index:02d}/{len(ranges):02d}] {year}-{month:02d} "
                "already exists; skipped",
                flush=True,
            )
            completed.append(
                {"year": year, "month": month, "status": "skipped_existing"}
            )
            continue

        print(
            f"[{index:02d}/{len(ranges):02d}] fetching {start}..{month_end} "
            f"({len(variables)} variables, {len(LOCATIONS)} locations)",
            flush=True,
        )
        try:
            hourly = fetch_month(
                start=start,
                end=month_end,
                variables=variables,
                batch_size=batch_size,
                workers=workers,
            )
            if hourly.empty:
                raise RuntimeError("API returned no non-null rows")
            daily = make_daily(hourly)

            local_partition = work_dir / f"year={year:04d}" / f"month={month:02d}"
            local_partition.mkdir(parents=True, exist_ok=True)
            hourly_path = local_partition / "hourly.parquet"
            daily_path = local_partition / "daily.parquet"
            hourly.to_parquet(hourly_path, index=False, compression="zstd")
            daily.to_parquet(daily_path, index=False, compression="zstd")
            upload_file(fs, hourly_path, raw_remote)
            upload_file(fs, daily_path, daily_remote)

            record = {
                "year": year,
                "month": month,
                "status": "uploaded",
                "start": start.isoformat(),
                "end": month_end.isoformat(),
                "variables_requested": list(variables),
                "hourly_rows": len(hourly),
                "daily_rows": len(daily),
                "hourly_bytes": hourly_path.stat().st_size,
                "daily_bytes": daily_path.stat().st_size,
                "raw_gcs_uri": f"gs://{raw_remote}",
                "daily_gcs_uri": f"gs://{daily_remote}",
            }
            completed.append(record)
            print(
                f"    uploaded {len(hourly):,} hourly rows and "
                f"{len(daily):,} daily rows",
                flush=True,
            )
        except HourlyApiLimitExceeded as exc:
            error = {
                "year": year,
                "month": month,
                "start": start.isoformat(),
                "end": month_end.isoformat(),
                "error": repr(exc),
            }
            errors.append(error)
            print(
                "    HOURLY LIMIT REACHED: stop now and resume next hour",
                flush=True,
            )
            write_metadata(fs, work_dir, completed, errors, started_at)
            break
        except Exception as exc:
            error = {
                "year": year,
                "month": month,
                "start": start.isoformat(),
                "end": month_end.isoformat(),
                "error": repr(exc),
            }
            errors.append(error)
            print(f"    ERROR: {exc!r}", flush=True)

        write_metadata(fs, work_dir, completed, errors, started_at)

    write_metadata(fs, work_dir, completed, errors, started_at)
    return completed, errors


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--end", type=date.fromisoformat, default=DEFAULT_END)
    parser.add_argument("--batch-size", type=int, default=6)
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--repair-daily-only", action="store_true")
    parser.add_argument(
        "--upload",
        action="store_true",
        help="Explicitly authorize writes to the configured GCS bucket.",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=Path(__file__).resolve().parent
        / "processed"
        / "us_ng_weather_backfill",
    )
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if not args.upload:
        build_parser().error("refusing GCS writes without --upload")
    if args.repair_daily_only:
        repaired = repair_daily_partitions(args.work_dir / "daily_repair")
        print(f"Finished: repaired_daily_partitions={repaired}")
        return
    completed, errors = run_backfill(
        end=args.end,
        work_dir=args.work_dir,
        batch_size=args.batch_size,
        workers=args.workers,
        force=args.force,
    )
    uploaded = sum(record["status"] == "uploaded" for record in completed)
    skipped = sum(record["status"] == "skipped_existing" for record in completed)
    print(
        f"Finished: uploaded={uploaded}, skipped={skipped}, errors={len(errors)}"
    )
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
