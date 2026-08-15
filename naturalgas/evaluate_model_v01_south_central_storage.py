#!/usr/bin/env python3
"""Build model V01, the frozen South Central storage formal baseline.

This promotes the pre-specified ``replace_all_storage__south_central_total``
candidate from the regional storage comparison into a clean, reproducible
strategy artifact.  The three storage components keep their existing weights;
only their source changes from Lower 48 to South Central Total.  No other
factor, timing rule or parameter is changed, and no GCS object is modified.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from naturalgas.evaluate_native_frequency_fundamentals import (  # noqa: E402
    STRATEGY_START,
    THROUGH_DATE,
    json_default,
)
from naturalgas.evaluate_no_consumption_fundamental_weights import (  # noqa: E402
    ALL_COMPONENTS,
    candidate_weights,
)
from naturalgas.evaluate_ncar_gdex_complete_solar_factor import (  # noqa: E402
    SOLAR_LEAD_PATH,
    SOLAR_SIGNAL_PATH,
    TRANSACTION_COST_BPS,
    extended_performance,
)
from naturalgas.evaluate_ncar_gdex_independent_wind_weights import (  # noqa: E402
    LOCAL_PANEL,
    WIND_FEATURES,
)
from naturalgas.evaluate_south_central_storage import (  # noqa: E402
    run as run_comparison,
)


MODEL_ID = "hh_v01_south_central_storage"
MODEL_SEQUENCE = 1
LIFECYCLE_STATE = "frozen_formal_baseline"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "results/models/v01_south_central_storage"
SELECTED_CANDIDATE = "replace_all_storage__south_central_total"
BASELINE_CANDIDATE = "current_lower48"
CURRENT_WEIGHT_CANDIDATE = "reallocate__low_storage__lng_export_mom"
STORAGE_COMPONENTS = {
    "sig_low_storage": "South Central Total level vs same-week 5-year normal",
    "sig_storage_change": "South Central Total 1-week change vs seasonal normal",
    "sig_storage_4w_change": "South Central Total 4-week change vs seasonal normal",
}


def add_net_returns(
    daily: pd.DataFrame,
    *,
    label: str,
    candidate: str,
) -> None:
    position = daily[f"position__{candidate}"]
    turnover = position.diff().abs().fillna(position.abs())
    daily[f"turnover_{label}"] = turnover
    daily[f"net_return_{label}"] = (
        position * daily["roll_adjusted_return"]
        - turnover * TRANSACTION_COST_BPS / 10_000.0
    )
    daily[f"net_index_{label}"] = (
        1.0 + daily[f"net_return_{label}"]
    ).cumprod()


def annual_metrics(daily: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for year, year_frame in daily.groupby(daily["date"].dt.year):
        for label, candidate in (
            ("lower48_baseline", BASELINE_CANDIDATE),
            ("south_central_total", SELECTED_CANDIDATE),
        ):
            metrics = extended_performance(
                year_frame,
                position_column=f"position__{candidate}",
                cost_bps=TRANSACTION_COST_BPS,
            )
            metrics.update({"year": int(year), "version": label})
            rows.append(metrics)
    return pd.DataFrame(rows)


def weight_table() -> pd.DataFrame:
    weights = candidate_weights()[CURRENT_WEIGHT_CANDIDATE]
    rows = []
    for component in ALL_COMPONENTS:
        weight = float(weights.get(component, 0.0))
        rows.append({
            "component": component,
            "internal_fundamental_weight": weight,
            "internal_fundamental_weight_pct": 100.0 * weight,
            "source": STORAGE_COMPONENTS.get(component, "unchanged"),
            "removed": np.isclose(weight, 0.0),
        })
    return pd.DataFrame(rows)


def run(
    *,
    panel_path: Path,
    wind_path: Path,
    solar_signal_path: Path,
    solar_lead_path: Path,
    output_dir: Path,
    start: pd.Timestamp,
    through_date: pd.Timestamp,
    filesystem: Any | None = None,
) -> dict[str, Any]:
    comparison_dir = output_dir / "comparison"
    comparison_summary = run_comparison(
        panel_path=panel_path,
        wind_path=wind_path,
        solar_signal_path=solar_signal_path,
        solar_lead_path=solar_lead_path,
        output_dir=comparison_dir,
        start=start,
        through_date=through_date,
        filesystem=filesystem,
    )

    periods = pd.read_csv(comparison_dir / "strategy_results.csv")
    periods = periods.loc[
        periods["candidate"].isin([BASELINE_CANDIDATE, SELECTED_CANDIDATE])
    ].copy()
    periods["version"] = periods["candidate"].map({
        BASELINE_CANDIDATE: "lower48_baseline",
        SELECTED_CANDIDATE: "south_central_total",
    })

    source_daily = pd.read_parquet(comparison_dir / "daily_comparison.parquet")
    keep = ["date", "roll_adjusted_return"]
    for candidate in (BASELINE_CANDIDATE, SELECTED_CANDIDATE):
        keep.extend([
            f"fundamental__{candidate}",
            f"score__{candidate}",
            f"position__{candidate}",
        ])
    daily = source_daily[keep].copy()
    daily["date"] = pd.to_datetime(daily["date"]).astype("datetime64[ns]")
    add_net_returns(daily, label="lower48", candidate=BASELINE_CANDIDATE)
    add_net_returns(
        daily,
        label="south_central_total",
        candidate=SELECTED_CANDIDATE,
    )
    annual = annual_metrics(daily)
    weights = weight_table()

    output_dir.mkdir(parents=True, exist_ok=True)
    periods.to_csv(output_dir / "period_comparison.csv", index=False)
    annual.to_csv(output_dir / "annual_comparison.csv", index=False)
    weights.to_csv(output_dir / "strategy_weights.csv", index=False)
    daily.to_parquet(
        output_dir / "strategy_daily.parquet",
        index=False,
        compression="zstd",
    )

    full = periods.loc[periods["period"].eq("full")].set_index("version")
    selected = full.loc["south_central_total"]
    baseline = full.loc["lower48_baseline"]
    summary = {
        "model_id": MODEL_ID,
        "model_sequence": MODEL_SEQUENCE,
        "lifecycle_state": LIFECYCLE_STATE,
        "selected_candidate": SELECTED_CANDIDATE,
        "baseline_candidate": BASELINE_CANDIDATE,
        "sample_start": daily["date"].min(),
        "sample_end": daily["date"].max(),
        "trading_days": len(daily),
        "storage_replacement": {
            component: description
            for component, description in STORAGE_COMPONENTS.items()
        },
        "selected_full_metrics": {
            "sharpe_zero_rf": float(selected["sharpe_zero_rf"]),
            "cagr": float(selected["cagr"]),
            "maximum_drawdown": float(selected["maximum_drawdown"]),
            "win_rate": float(selected["win_rate"]),
            "total_turnover": float(selected["total_turnover"]),
        },
        "change_vs_lower48": {
            "sharpe_zero_rf": float(
                selected["sharpe_zero_rf"] - baseline["sharpe_zero_rf"]
            ),
            "cagr": float(selected["cagr"] - baseline["cagr"]),
            "maximum_drawdown": float(
                selected["maximum_drawdown"] - baseline["maximum_drawdown"]
            ),
        },
        "fixed_policy": comparison_summary["method"],
        "other_factors": "unchanged",
        "gcs_objects_modified": False,
        "caution": (
            "South Central was promoted at the user's direction after a research "
            "comparison. The improvement is small and not statistically decisive; "
            "the EIA history is revised rather than first-release vintage data."
        ),
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, default=json_default, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--panel", type=Path, default=LOCAL_PANEL)
    parser.add_argument("--wind-features", type=Path, default=WIND_FEATURES)
    parser.add_argument("--solar-signals", type=Path, default=SOLAR_SIGNAL_PATH)
    parser.add_argument("--solar-leads", type=Path, default=SOLAR_LEAD_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--start", type=pd.Timestamp, default=STRATEGY_START)
    parser.add_argument("--through-date", type=pd.Timestamp, default=THROUGH_DATE)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    result = run(
        panel_path=args.panel,
        wind_path=args.wind_features,
        solar_signal_path=args.solar_signals,
        solar_lead_path=args.solar_leads,
        output_dir=args.output_dir,
        start=pd.Timestamp(args.start),
        through_date=pd.Timestamp(args.through_date),
    )
    print(json.dumps(result, default=json_default, indent=2, sort_keys=True))
