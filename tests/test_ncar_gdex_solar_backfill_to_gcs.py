from __future__ import annotations

import io
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
from scipy.io import netcdf_file

from naturalgas.ncar_gdex_bulk_wind_backfill_to_gcs import ExpectedSource
from naturalgas.ncar_gdex_solar_backfill_to_gcs import (
    DEFAULT_VALID_HOURS,
    build_batch_control,
    expected_sources,
    make_daily,
    make_features,
    parse_netcdf_member,
    requested_product_leads,
)
from naturalgas.ncar_gdex_wind_backfill_to_gcs import LOCATIONS


def _netcdf_payload(*, include_gfsv16_cloud_pair: bool = False) -> bytes:
    buffer = io.BytesIO()
    dataset = netcdf_file(buffer, "w", version=1)
    dataset.createDimension("lat", 2)
    dataset.createDimension("lon", 2)
    latitude = dataset.createVariable("lat", "f", ("lat",))
    longitude = dataset.createVariable("lon", "f", ("lon",))
    latitude[:] = [25.0, 50.0]
    longitude[:] = [-125.0, -65.0]
    radiation = dataset.createVariable("DSWRF_L1", "f", ("lat", "lon"))
    cloud = dataset.createVariable("T_CDC_L10", "f", ("lat", "lon"))
    averaged_cloud = None
    if include_gfsv16_cloud_pair:
        averaged_cloud = dataset.createVariable(
            "T_CDC_L10_Avg_1", "f", ("lat", "lon")
        )
    temperature = dataset.createVariable("TMP_L103", "f", ("lat", "lon"))
    surface_temperature = dataset.createVariable(
        "TMP_L1", "f", ("lat", "lon")
    )
    radiation[:] = [[0.0, 100.0], [200.0, 300.0]]
    cloud[:] = [[10.0, 20.0], [30.0, 40.0]]
    if averaged_cloud is not None:
        averaged_cloud[:] = [[55.0, 55.0], [55.0, 55.0]]
    temperature[:] = [[280.0, 281.0], [282.0, 283.0]]
    surface_temperature[:] = [[330.0, 330.0], [330.0, 330.0]]
    dataset.flush()
    payload = buffer.getvalue()
    dataset.close()
    return payload


def test_expected_sources_use_non_overlapping_calendar_day_intervals() -> None:
    sources = expected_sources(
        date(2020, 1, 1),
        date(2020, 1, 1),
        cycles=(0,),
        lead_days=(1,),
        valid_hours=DEFAULT_VALID_HOURS,
    )
    assert sorted(item.forecast_lead_hours for item in sources.values()) == [
        30,
        36,
        42,
        48,
    ]
    midnight = next(
        item for item in sources.values() if item.valid_time_utc.hour == 0
    )
    assert midnight.target_date == date(2020, 1, 2)
    assert midnight.valid_time_utc == datetime(2020, 1, 3, tzinfo=timezone.utc)


def test_batch_control_requests_solar_fields_and_both_product_types() -> None:
    control = build_batch_control(
        date(2020, 1, 1),
        date(2020, 1, 31),
        cycles=(0, 6, 12, 18),
        lead_days=(1, 2, 3, 4, 5),
        valid_hours=DEFAULT_VALID_HOURS,
    )
    assert control["param"] == "DSWRF/T CDC/TMP"
    assert control["level"] == "SFC:0;EATM:0;HTGL:2"
    assert "12-hour Forecast" in control["product"]
    assert "6-hour Average (initial+6 to initial+12)" in control["product"]
    assert requested_product_leads(
        (0, 6, 12, 18), (1, 2, 3, 4, 5), DEFAULT_VALID_HOURS
    ) == tuple(range(12, 145, 6))


def test_parse_and_aggregate_shortwave_energy() -> None:
    initialization = datetime(2020, 1, 1, tzinfo=timezone.utc)
    source = ExpectedSource(
        member_name="gfs.0p25.2020010100.f030.grib2.nc",
        source_file="gfs.0p25.2020010100.f030.grib2",
        initialization_time_utc=initialization,
        forecast_lead_hours=30,
        valid_time_utc=initialization + timedelta(hours=30),
        target_date=date(2020, 1, 2),
        lead_days=1,
        cycle_hour_utc=0,
    )
    rows, inventory = parse_netcdf_member(
        _netcdf_payload(),
        source,
        archive_url="https://example.test/archive.tar",
        retrieved_at_utc="2020-01-01T00:00:00Z",
    )
    assert len(rows) == len(LOCATIONS)
    assert inventory["source_status"] == "gdex_batch"
    first = rows[0]
    assert np.isclose(
        first["shortwave_interval_energy_kwh_m2"],
        first["downward_shortwave_radiation_wm2"] * 6 / 1_000,
    )

    four_intervals = pd.concat(
        [pd.DataFrame(rows).assign(forecast_lead_hours=30 + 6 * offset)
         for offset in range(4)],
        ignore_index=True,
    )
    daily = make_daily(
        four_intervals,
        expected_samples=4,
        minimum_samples=1,
    )
    assert daily["solar_sample_count"].eq(4).all()
    location_leads, features = make_features(
        daily,
        expected_lead_days=1,
        require_complete=True,
    )
    assert len(location_leads) == 1
    assert len(features) == 1
    assert features.iloc[0]["gfs_min_locations"] == len(LOCATIONS)


def test_parse_gfsv16_prefers_averaged_cloud_over_instantaneous() -> None:
    initialization = datetime(2021, 3, 22, 12, tzinfo=timezone.utc)
    source = ExpectedSource(
        member_name="gfs.0p25.2021032212.f018.grib2.nc",
        source_file="gfs.0p25.2021032212.f018.grib2",
        initialization_time_utc=initialization,
        forecast_lead_hours=18,
        valid_time_utc=initialization + timedelta(hours=18),
        target_date=date(2021, 3, 23),
        lead_days=1,
        cycle_hour_utc=12,
    )
    rows, inventory = parse_netcdf_member(
        _netcdf_payload(include_gfsv16_cloud_pair=True),
        source,
        archive_url="https://example.test/archive.tar",
        retrieved_at_utc="2021-03-22T12:00:00Z",
    )

    assert inventory["source_status"] == "gdex_batch"
    assert len(rows) == len(LOCATIONS)
    assert all(np.isclose(row["total_cloud_cover_pct"], 55.0) for row in rows)
