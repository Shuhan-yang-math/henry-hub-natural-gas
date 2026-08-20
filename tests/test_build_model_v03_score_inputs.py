from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from naturalgas.build_model_v03_score_inputs import (
    _comparison,
    _require_zero_mismatches,
    build_central_source_history,
    causal_anomaly_signal,
    map_central_to_score_dates,
)


def test_causal_anomaly_uses_only_prior_same_weekdays() -> None:
    dates = pd.Series(pd.date_range("2020-01-06", periods=150, freq="7D"))
    values = pd.Series(np.arange(150, dtype=float))
    original = causal_anomaly_signal(values, dates)
    changed = values.copy()
    changed.iloc[-1] = 1_000_000.0
    revised = causal_anomaly_signal(changed, dates)
    pd.testing.assert_series_equal(original.iloc[:-1], revised.iloc[:-1])


def test_central_history_requires_unique_respondent_days() -> None:
    rows = []
    for respondent in ("ERCO", "MISO", "SWPP"):
        rows.append({
            "date": pd.Timestamp("2024-01-01"),
            "respondent": respondent,
            "complete_day": True,
            "demand_mwh": 100.0,
            "coal_mwh": 10.0,
            "gas_mwh": 20.0,
            "nuclear_mwh": 10.0,
            "petroleum_mwh": 0.0,
            "hydro_mwh": 5.0,
            "pumped_storage_mwh": 0.0,
            "solar_mwh": 5.0,
            "wind_mwh": 10.0,
            "battery_mwh": 0.0,
            "other_storage_mwh": 0.0,
            "unknown_storage_mwh": 0.0,
            "geothermal_mwh": 0.0,
            "other_fuel_mwh": 0.0,
            "unknown_fuel_mwh": 0.0,
        })
    duplicated = pd.DataFrame([*rows, rows[0]])
    with pytest.raises(ValueError, match="duplicate respondent-days"):
        build_central_source_history(duplicated)


def test_central_mapping_uses_first_strictly_later_score_date() -> None:
    source = pd.DataFrame({
        "date": pd.to_datetime(["2024-01-05", "2024-01-06"]),
        "selected_nongas_signal": [0.1, 0.2],
    })
    mapped = map_central_to_score_dates(
        source,
        pd.to_datetime(["2024-01-05", "2024-01-08"]),
    )
    assert mapped["date"].tolist() == [pd.Timestamp("2024-01-08")]
    assert mapped["source_gas_day_central"].tolist() == [
        pd.Timestamp("2024-01-06")
    ]
    assert mapped["selected_nongas_signal"].tolist() == [0.2]


def test_parity_reports_noncommon_dates_and_rejects_value_mismatch() -> None:
    rebuilt = pd.DataFrame({
        "date": pd.to_datetime(["2024-01-02", "2024-01-03"]),
        "score": [2.0, 3.0],
    })
    frozen = pd.DataFrame({
        "date": pd.to_datetime(["2024-01-01", "2024-01-02"]),
        "score": [1.0, 200.0],
    })
    result = _comparison(rebuilt, frozen, columns=["score"])
    assert result["frozen_only_dates"] == ["2024-01-01"]
    assert result["rebuilt_only_dates"] == ["2024-01-03"]
    assert result["columns"]["score"]["mismatch_dates"] == 1
    with pytest.raises(AssertionError, match="test parity mismatch"):
        _require_zero_mismatches(result, label="test")
