import numpy as np
import pandas as pd

from naturalgas.eia930_florida_availability import (
    FLORIDA_RESPONDENTS,
    build_source_history,
)


def synthetic_florida_daily() -> pd.DataFrame:
    rows = []
    for day_number, date in enumerate(pd.date_range("2020-01-01", periods=200)):
        for ba_number, respondent in enumerate(FLORIDA_RESPONDENTS):
            demand = 100.0 + ba_number
            rows.append(
                {
                    "date": date,
                    "respondent": respondent,
                    "demand_mwh": demand,
                    "coal_mwh": 18.0 + 2.0 * np.sin(day_number / 9.0),
                    "nuclear_mwh": 30.0 + ba_number / 10.0,
                    "petroleum_mwh": 0.0,
                    "hydro_mwh": 8.0 + np.cos(day_number / 13.0),
                    "pumped_storage_mwh": 1.0,
                    "solar_mwh": 0.0,
                    "wind_mwh": 0.0,
                    "gas_mwh": 35.0,
                    "geothermal_mwh": 0.0,
                    "other_fuel_mwh": 0.0,
                    "unknown_fuel_mwh": 0.0,
                    "complete_day": True,
                }
            )
    return pd.DataFrame(rows)


def test_partial_ba_day_uses_one_continuous_rolling_history() -> None:
    daily = synthetic_florida_daily()
    partial_date = pd.Timestamp("2020-06-08")
    next_same_weekday = partial_date + pd.Timedelta(days=7)
    daily.loc[
        daily["date"].eq(partial_date) & daily["respondent"].eq("GVL"),
        "demand_mwh",
    ] = np.nan

    history = build_source_history(daily).set_index("date")
    prior_same_weekday = history.loc[
        (history.index < partial_date)
        & (history.index.dayofweek == partial_date.dayofweek)
    ].tail(8)
    assert prior_same_weekday["florida_available_ba_count"].eq(9).all()
    assert history.loc[partial_date, "florida_available_ba_count"] == 8
    assert history.loc[
        partial_date, "florida_share_past_8_same_weekday_mean"
    ] == prior_same_weekday["florida_firm_nongas_share"].mean()

    # The partial eight-BA raw share remains in the ordinary history. It is
    # therefore one of the eight values used by the following Monday.
    future_reference = history.loc[
        (history.index < next_same_weekday)
        & (history.index.dayofweek == next_same_weekday.dayofweek)
    ].tail(8)
    assert partial_date in future_reference.index
    assert history.loc[
        next_same_weekday, "florida_share_past_8_same_weekday_mean"
    ] == future_reference["florida_firm_nongas_share"].mean()

    next_index = history.index.get_loc(next_same_weekday)
    expected_scale = history["florida_share_innovation"].iloc[:next_index].tail(
        252
    ).std()
    assert np.isclose(
        history.loc[
            next_same_weekday, "florida_prior_252_innovation_std"
        ],
        expected_scale,
        rtol=0.0,
        atol=1e-15,
    )
    assert pd.notna(history.loc[partial_date, "signal__firm__florida"])
