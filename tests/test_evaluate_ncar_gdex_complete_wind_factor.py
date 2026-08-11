from __future__ import annotations

import pandas as pd

from naturalgas.evaluate_ncar_gdex_complete_wind_factor import (
    _weighted_group_features,
    build_annual_location_weights,
)


def test_annual_weights_use_only_prior_year_commissioning() -> None:
    turbines = pd.DataFrame(
        {
            "case_id": [1, 2],
            "p_year": [2015, 2016],
            "t_cap": [1_000.0, 2_000.0],
            "t_hh": [80.0, 100.0],
            "xlong": [-96.8, -96.9],
            "ylat": [32.8, 32.9],
        }
    )
    weights, diagnostics = build_annual_location_weights(
        turbines,
        first_year=2016,
        last_year=2017,
    )
    by_year = diagnostics.set_index("issue_year")
    assert by_year.loc[2016, "fleet_cutoff_year"] == 2015
    assert by_year.loc[2016, "capacity_mw"] == 1.0
    assert by_year.loc[2017, "capacity_mw"] == 3.0
    assert weights.groupby("issue_year")["capacity_share"].sum().eq(1.0).all()


def test_point_power_is_capacity_weighted_before_aggregation() -> None:
    rows = []
    for lead_day in range(1, 6):
        for _ in range(4):
            for location, capacity, effective_power in (
                ("a", 1.0, 0.0),
                ("b", 3.0, 1.0),
            ):
                rows.append(
                    {
                        "forecast_reference_time_utc": pd.Timestamp(
                            "2020-01-01", tz="UTC"
                        ),
                        "forecast_cycle_hour_utc": 0,
                        "issue_year": 2020,
                        "fleet_cutoff_year": 2019,
                        "location_id": location,
                        "lead_days": lead_day,
                        "capacity_mw": capacity,
                        "wind_speed_80m_mps": 8.0,
                        "wind_speed_hub_mps": 8.0,
                        "power_cf_no_cutout": effective_power,
                        "effective_power_cf": effective_power,
                        "low_wind_shortfall_cf": 1.0 - effective_power,
                        "high_wind_cutout_loss_cf": 0.0,
                        "total_shortfall_cf": 1.0 - effective_power,
                    }
                )
    result = _weighted_group_features(pd.DataFrame(rows)).iloc[0]
    assert result["sample_count"] == 40
    assert result["fleet_capacity_mw"] == 4.0
    assert result["effective_power_cf_equal"] == 0.75
    assert result["total_shortfall_cf_front"] == 0.25
