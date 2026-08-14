from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd
import pytest

from naturalgas.pipelines.rebuild_d1_3_strategy import (
    DEFAULT_WEATHER_MANIFEST,
    rebuild_d1_3_strategy,
    verify_score_input_wind_lineage,
    verify_selected_summary,
)


def _lineage_inputs(tmp_path: Path) -> tuple[Path, Path]:
    dates = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"])
    horizons = pd.DataFrame({
        "date": dates,
        "forecast_reference_time_utc": pd.to_datetime(dates, utc=True),
        "wind_z__d1_3": [0.2, 0.3, float("nan")],
        "wind_signal__d1_3": [0.1, 0.2, float("nan")],
        "wind_signal__d1_5": [0.05, 0.15, float("nan")],
    })
    scores = horizons[["date", "wind_signal__d1_3", "wind_signal__d1_5"]]
    horizon_path = tmp_path / "horizons.parquet"
    score_path = tmp_path / "scores.parquet"
    horizons.to_parquet(horizon_path, index=False)
    scores.to_parquet(score_path, index=False)
    return horizon_path, score_path


def test_score_input_wind_lineage_requires_exact_raw_parity(
    tmp_path: Path,
) -> None:
    horizon_path, score_path = _lineage_inputs(tmp_path)
    result = verify_score_input_wind_lineage(
        horizon_path=horizon_path,
        score_inputs_path=score_path,
    )
    assert result["status"] == "exact"
    assert result["matched_non_null_d1_3_dates"] == 2
    assert result["missing_initialization_dates"] == ["2024-01-04"]
    assert result["maximum_absolute_difference"] == {
        "wind_signal__d1_3": 0.0,
        "wind_signal__d1_5": 0.0,
    }


def test_score_input_wind_lineage_rejects_value_or_missing_date_changes(
    tmp_path: Path,
) -> None:
    horizon_path, score_path = _lineage_inputs(tmp_path)
    scores = pd.read_parquet(score_path)
    scores.loc[0, "wind_signal__d1_3"] += 1e-12
    scores.to_parquet(score_path, index=False)
    with pytest.raises(AssertionError, match="raw-rebuilt wind_signal__d1_3"):
        verify_score_input_wind_lineage(
            horizon_path=horizon_path,
            score_inputs_path=score_path,
        )

    _, score_path = _lineage_inputs(tmp_path)
    scores = pd.read_parquet(score_path)
    scores.loc[2, "wind_signal__d1_3"] = 0.0
    scores.to_parquet(score_path, index=False)
    with pytest.raises(AssertionError, match="missing-date mismatch"):
        verify_score_input_wind_lineage(
            horizon_path=horizon_path,
            score_inputs_path=score_path,
        )


def test_selected_summary_verification_ignores_only_dashboard_location(
    tmp_path: Path,
) -> None:
    expected = tmp_path / "expected.json"
    actual = tmp_path / "actual.json"
    expected.write_text(
        json.dumps({"dashboard": "results/a.png", "trading_days": 10}) + "\n",
        encoding="utf-8",
    )
    actual.write_text(
        json.dumps({"dashboard": "/tmp/a.png", "trading_days": 10}) + "\n",
        encoding="utf-8",
    )
    verify_selected_summary(actual, expected)
    actual.write_text(
        json.dumps({"dashboard": "/tmp/a.png", "trading_days": 11}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="does not reproduce"):
        verify_selected_summary(actual, expected)


@pytest.mark.skipif(
    os.environ.get("RUN_HENRY_HUB_D1_3_CHAIN") != "1",
    reason="set RUN_HENRY_HUB_D1_3_CHAIN=1 for the pinned GCS D1-3 rebuild",
)
def test_pinned_gcs_wind_through_selected_d1_3_strategy(
    tmp_path: Path,
) -> None:
    receipt = rebuild_d1_3_strategy(
        weather_manifest=DEFAULT_WEATHER_MANIFEST,
        output_dir=tmp_path / "d1_3",
        overwrite=False,
    )
    wind = receipt["wind_horizon_rebuild"]
    assert wind["rows"] == 3857
    assert wind["output_integrity"]["sha256"] == (
        "34fb31802a41144e5ed842d2433a1b67db8d93810cf900835c875913f62db94c"
    )
    selected = receipt["selected_strategy_rebuild"]
    assert selected["wind_lineage"]["matched_non_null_d1_3_dates"] == 1750
    assert selected["wind_lineage"]["missing_initialization_dates"] == [
        "2019-10-10",
        "2022-12-01",
    ]
    assert selected["summary"]["trading_days"] == 1748
    assert selected["summary"]["selected_metrics"]["sharpe"] == (
        2.2280397376832175
    )
