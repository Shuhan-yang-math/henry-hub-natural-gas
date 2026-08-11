from __future__ import annotations

import pandas as pd

from naturalgas.ncar_gdex_capacity_weighted_solar import (
    build_capacity_weighted_location_leads,
    build_monthly_location_weights,
)


def test_build_monthly_weights_maps_capacity_to_nearest_location() -> None:
    generators = pd.DataFrame(
        {
            "period": ["2020-01", "2020-01", "2020-01"],
            "stateid": ["AZ", "CA", "HI"],
            "plantid": [1, 2, 3],
            "generatorid": ["A", "B", "C"],
            "nameplate-capacity-mw": [75.0, 25.0, 100.0],
            "latitude": [33.45, 34.05, 21.3],
            "longitude": [-112.07, -118.24, -157.8],
        }
    )

    weights, diagnostics = build_monthly_location_weights(generators)

    assert set(weights["location_id"]) == {"phoenix_az", "los_angeles_ca"}
    shares = weights.set_index("location_id")["capacity_share"]
    assert shares["phoenix_az"] == 0.75
    assert shares["los_angeles_ca"] == 0.25
    assert diagnostics["weather_locations_with_capacity"] == 2


def test_weighted_lead_uses_two_month_lag_and_capacity_shares() -> None:
    reference = pd.Timestamp("2020-03-01T00:00:00Z")
    daily = pd.DataFrame(
        {
            "forecast_reference_time_utc": [reference, reference],
            "nominal_issue_date": [pd.Timestamp("2020-03-01").date()] * 2,
            "target_date": pd.to_datetime(["2020-03-02", "2020-03-02"]),
            "lead_days": [1, 1],
            "location_id": ["phoenix_az", "los_angeles_ca"],
            "requested_latitude": [33.4484, 34.0522],
            "requested_longitude": [-112.0740, -118.2437],
            "solar_sample_count": [4, 4],
            "downward_shortwave_mean_wm2": [100.0, 300.0],
            "downward_shortwave_energy_kwh_m2": [2.0, 6.0],
            "total_cloud_cover_mean_pct": [20.0, 60.0],
            "temperature_2m_mean_c": [20.0, 30.0],
            "solar_sample_complete": [True, True],
        }
    )
    weights = pd.DataFrame(
        {
            "period": ["2020-01", "2020-01"],
            "location_id": ["phoenix_az", "los_angeles_ca"],
            "capacity_mw": [75.0, 25.0],
            "capacity_share": [0.75, 0.25],
        }
    )

    leads, diagnostics = build_capacity_weighted_location_leads(
        daily,
        weights,
        capacity_lag_months=2,
    )

    assert len(leads) == 1
    row = leads.iloc[0]
    assert row["capacity_period"] == "2020-01"
    assert row["gfs_dswrf_wm2"] == 150.0
    assert row["gfs_shortwave_energy_kwh_m2_day"] == 3.0
    assert row["gfs_total_cloud_cover_pct"] == 30.0
    assert row["gfs_temperature_2m_c"] == 22.5
    assert row["capacity_coverage"] == 1.0
    assert diagnostics["insufficient_capacity_coverage_rows"] == 0
