#!/usr/bin/env python3
"""Remove consumption fundamentals and reallocate their two fixed slots.

The current native-frequency strategy has eleven equally weighted internal
fundamental slots.  This research experiment removes total-consumption YoY and
MoM, then assigns each freed 1/11 slot to one of the nine remaining factors.
All 45 unordered assignments are selected using the development period only.

Top-level weather, wind, solar and fundamental allocations are unchanged, as
are the one-session signal lag, five-trading-day early roll and 2.5 bps cost.
The validation and first-look holdout periods do not participate in selection.
Only local research artifacts are written; no GCS object is modified.
"""

from __future__ import annotations

import argparse
from itertools import combinations_with_replacement
import json
from pathlib import Path
import sys
from typing import Any

import gcsfs
import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from naturalgas.evaluate_native_frequency_fundamentals import (  # noqa: E402
    STRATEGY_START,
    THROUGH_DATE,
    SELECTED_WIND_ALLOCATION,
    apply_native_frequency_fundamentals,
    json_default,
)
from naturalgas.evaluate_ncar_gdex_complete_solar_factor import (  # noqa: E402
    ACTIVE_DIRECT_WEATHER_COMPONENTS,
    LEGACY_WEATHER_SLOT_COUNT,
    PRIMARY_FACTOR,
    SOLAR_LEAD_PATH,
    SOLAR_SIGNAL_PATH,
    TRANSACTION_COST_BPS,
    apply_production_control,
    extended_performance,
    load_capacity_weighted_solar,
    merge_and_transform_factors,
)
from naturalgas.evaluate_ncar_gdex_independent_wind_weights import (  # noqa: E402
    FUNDAMENTAL_FAST_COMPONENTS,
    FUNDAMENTAL_MONTHLY_COMPONENTS,
    GROUP_READY_COLUMNS,
    LOCAL_PANEL,
    WIND_FEATURES,
    allocation_score,
    build_base_components,
    candidate_allocations,
)
from naturalgas.execution import (  # noqa: E402
    apply_early_roll_return,
)


DEFAULT_OUTPUT_DIR = (
    Path(__file__).resolve().parent
    / "processed/no_consumption_fundamental_weights"
)
DEVELOPMENT_END = pd.Timestamp("2020-12-31")
VALIDATION_START = pd.Timestamp("2021-01-01")
VALIDATION_END = pd.Timestamp("2023-12-31")
HOLDOUT_START = pd.Timestamp("2024-01-01")
SOLAR_NOMINAL_WEIGHT = 0.10
SIGNAL_LAG_TRADING_SESSIONS = 1
POSITION_BOUNDS = (-1.0, 1.0)
ORIGINAL_SLOT_COUNT = 11
DEVELOPMENT_SHARPE_TOLERANCE = 0.01

REMOVED_COMPONENTS = (
    "sig_consumption_growth",
    "sig_consumption_mom",
)
ALL_COMPONENTS = (
    *FUNDAMENTAL_FAST_COMPONENTS,
    *FUNDAMENTAL_MONTHLY_COMPONENTS,
)
REMAINING_COMPONENTS = tuple(
    component for component in ALL_COMPONENTS if component not in REMOVED_COMPONENTS
)


def transform_components(panel: pd.DataFrame) -> pd.DataFrame:
    """Match the current composite's fast/monthly transformations exactly."""

    transformed = pd.DataFrame(index=panel.index)
    for component in ALL_COMPONENTS:
        values = pd.to_numeric(panel[component], errors="coerce")
        transformed[component] = (
            np.tanh(values / 2.0)
            if component in FUNDAMENTAL_FAST_COMPONENTS
            else values
        )
    return transformed


def weighted_available(
    transformed: pd.DataFrame,
    weights: dict[str, float],
) -> pd.Series:
    """Weighted mean that preserves the existing skip-missing convention."""

    columns = list(weights)
    weight_series = pd.Series(weights, dtype=float)
    values = transformed[columns]
    numerator = values.mul(weight_series, axis=1).sum(axis=1, min_count=1)
    available_weight = values.notna().mul(weight_series, axis=1).sum(axis=1)
    return numerator.div(available_weight.replace(0.0, np.nan))


def candidate_weights() -> dict[str, dict[str, float]]:
    """Benchmarks plus the 45 ways to reassign the two removed 1/11 slots."""

    original = {component: 1.0 / ORIGINAL_SLOT_COUNT for component in ALL_COMPONENTS}
    candidates: dict[str, dict[str, float]] = {
        "current_all_11_equal": original,
        "no_consumption_equal_9": {
            component: 1.0 / len(REMAINING_COMPONENTS)
            for component in REMAINING_COMPONENTS
        },
    }
    for first, second in combinations_with_replacement(REMAINING_COMPONENTS, 2):
        weights = {
            component: 1.0 / ORIGINAL_SLOT_COUNT
            for component in REMAINING_COMPONENTS
        }
        weights[first] += 1.0 / ORIGINAL_SLOT_COUNT
        weights[second] += 1.0 / ORIGINAL_SLOT_COUNT
        name = f"reallocate__{first.removeprefix('sig_')}__{second.removeprefix('sig_')}"
        candidates[name] = weights

    for name, weights in candidates.items():
        if not np.isclose(sum(weights.values()), 1.0):
            raise AssertionError(f"{name} weights sum to {sum(weights.values())}")
        if name != "current_all_11_equal" and any(
            component in weights for component in REMOVED_COMPONENTS
        ):
            raise AssertionError(f"{name} retained a removed consumption factor")
    return candidates


def prepare_base_panel(
    native_panel: pd.DataFrame,
    *,
    wind_path: Path,
    solar: pd.DataFrame,
) -> pd.DataFrame:
    """Load unchanged wind, weather, execution and solar context once."""

    wind = pd.read_parquet(
        wind_path,
        columns=["date", "forecast_cycle_hour_utc", "sig_capacity_cf"],
    )
    wind["date"] = pd.to_datetime(wind["date"]).astype("datetime64[ns]")
    wind = wind.loc[
        wind["forecast_cycle_hour_utc"].eq(0),
        ["date", "sig_capacity_cf"],
    ]
    base = native_panel.merge(wind, on="date", how="left", validate="one_to_one")
    base = build_base_components(base)
    base["legacy_weather"] = (
        np.tanh(base[list(ACTIVE_DIRECT_WEATHER_COMPONENTS)] / 2.0)
        .fillna(0.0)
        .sum(axis=1)
        / LEGACY_WEATHER_SLOT_COUNT
    )
    base["sig_cpc_level_direct_slot"] = 0.0
    base["sig_observed_weather_direct_slot"] = 0.0
    base = apply_early_roll_return(base)
    return merge_and_transform_factors(base, solar)


def candidate_position(
    base: pd.DataFrame,
    fundamental: pd.Series,
) -> tuple[pd.Series, pd.Series]:
    """Build the unchanged fixed-wind, 10% daylight-solar strategy."""

    allocation = next(
        item
        for item in candidate_allocations()
        if item.name == SELECTED_WIND_ALLOCATION
    )
    working = base.copy()
    working["fundamental_rebuilt"] = fundamental
    baseline_score = allocation_score(working, allocation)

    daylight_scale = working["daylight_scale"].fillna(0.0)
    effective_solar_weight = SOLAR_NOMINAL_WEIGHT * daylight_scale
    solar_signal = np.tanh(working["sig_solar_pv"] / 2.0).fillna(0.0)
    active_score = baseline_score + effective_solar_weight * (
        solar_signal - fundamental
    )
    active_score = apply_production_control(working, active_score)
    position = active_score.shift(SIGNAL_LAG_TRADING_SESSIONS).clip(
        *POSITION_BOUNDS
    )
    return active_score, position


def common_sample_mask(
    panel: pd.DataFrame,
    positions: dict[str, pd.Series],
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.Series:
    ready = (
        panel["date"].between(start, end)
        & panel["required_input_complete"].fillna(False)
        & panel[list(GROUP_READY_COLUMNS)].notna().all(axis=1)
        & panel["roll_adjusted_return"].notna()
    )
    for position in positions.values():
        ready &= position.notna()
    return ready


def metrics_for(
    panel: pd.DataFrame,
    position: pd.Series,
    mask: pd.Series,
) -> dict[str, Any]:
    sample = panel.loc[mask].copy()
    sample["candidate_position"] = position.loc[mask]
    return extended_performance(
        sample,
        position_column="candidate_position",
        cost_bps=TRANSACTION_COST_BPS,
    )


def run(
    *,
    panel_path: Path,
    wind_path: Path,
    solar_signal_path: Path,
    solar_lead_path: Path,
    output_dir: Path,
    start: pd.Timestamp,
    through_date: pd.Timestamp,
) -> dict[str, Any]:
    filesystem = gcsfs.GCSFileSystem()
    panel = pd.read_parquet(panel_path)
    panel["date"] = pd.to_datetime(panel["date"]).astype("datetime64[ns]")
    native = apply_native_frequency_fundamentals(panel, filesystem)
    solar = load_capacity_weighted_solar(solar_signal_path, solar_lead_path)
    base = prepare_base_panel(native, wind_path=wind_path, solar=solar)
    transformed = transform_components(base)

    # Verify that the explicit all-eleven weights reproduce the current code.
    original_weights = {
        component: 1.0 / ORIGINAL_SLOT_COUNT for component in ALL_COMPONENTS
    }
    explicit_original = weighted_available(transformed, original_weights)
    if not np.allclose(
        explicit_original,
        base["fundamental_rebuilt"],
        equal_nan=True,
        atol=1e-12,
    ):
        raise AssertionError("Explicit original weights do not match current composite")

    weights_by_candidate = candidate_weights()
    fundamentals = {
        name: weighted_available(transformed, weights)
        for name, weights in weights_by_candidate.items()
    }
    scores_and_positions = {
        name: candidate_position(base, fundamental)
        for name, fundamental in fundamentals.items()
    }
    positions = {
        name: score_and_position[1]
        for name, score_and_position in scores_and_positions.items()
    }
    common = common_sample_mask(
        base,
        positions,
        start=start,
        end=through_date,
    )
    development = common & base["date"].le(DEVELOPMENT_END)
    if not development.any():
        raise RuntimeError("No development observations after readiness filters")

    candidate_rows: list[dict[str, Any]] = []
    for name, weights in weights_by_candidate.items():
        metrics = metrics_for(base, positions[name], development)
        recipients = [
            component
            for component, weight in weights.items()
            if component in REMAINING_COMPONENTS
            and weight > 1.0 / ORIGINAL_SLOT_COUNT + 1e-12
        ]
        metrics.update({
            "candidate": name,
            "eligible_for_selection": name.startswith("reallocate__"),
            "extra_slot_recipients": "|".join(recipients),
        })
        for component in ALL_COMPONENTS:
            metrics[f"weight__{component}"] = weights.get(component, 0.0)
        candidate_rows.append(metrics)
    candidate_results = pd.DataFrame(candidate_rows)
    eligible = candidate_results.loc[
        candidate_results["eligible_for_selection"]
    ].copy()
    best_development_sharpe = float(eligible["sharpe_zero_rf"].max())
    candidate_results["within_selection_tolerance"] = (
        candidate_results["eligible_for_selection"]
        & candidate_results["sharpe_zero_rf"].ge(
            best_development_sharpe - DEVELOPMENT_SHARPE_TOLERANCE
        )
    )
    shortlist = candidate_results.loc[
        candidate_results["within_selection_tolerance"]
    ]
    selected_row = shortlist.sort_values(
        ["total_turnover", "sharpe_zero_rf", "candidate"],
        ascending=[True, False, True],
    ).iloc[0]
    selected = str(selected_row["candidate"])

    reported = (
        "current_all_11_equal",
        "no_consumption_equal_9",
        selected,
    )
    periods = {
        "development": (start, DEVELOPMENT_END),
        "validation": (VALIDATION_START, VALIDATION_END),
        "first_look_holdout": (HOLDOUT_START, through_date),
        "full": (start, through_date),
    }
    period_rows: list[dict[str, Any]] = []
    for period, (period_start, period_end) in periods.items():
        mask = common & base["date"].between(period_start, period_end)
        for name in reported:
            metrics = metrics_for(base, positions[name], mask)
            metrics.update({"period": period, "candidate": name})
            period_rows.append(metrics)
    period_results = pd.DataFrame(period_rows)

    annual_rows: list[dict[str, Any]] = []
    for year in sorted(base.loc[common, "date"].dt.year.unique()):
        mask = common & base["date"].dt.year.eq(year)
        for name in reported:
            metrics = metrics_for(base, positions[name], mask)
            metrics.update({"year": int(year), "candidate": name})
            annual_rows.append(metrics)
    annual_results = pd.DataFrame(annual_rows)

    selected_weights = weights_by_candidate[selected]
    weight_rows = []
    for component in ALL_COMPONENTS:
        weight_rows.append({
            "component": component,
            "removed": component in REMOVED_COMPONENTS,
            "original_weight": 1.0 / ORIGINAL_SLOT_COUNT,
            "selected_weight": selected_weights.get(component, 0.0),
            "weight_change": (
                selected_weights.get(component, 0.0)
                - 1.0 / ORIGINAL_SLOT_COUNT
            ),
        })
    selected_weight_table = pd.DataFrame(weight_rows)

    selected_score, selected_position = scores_and_positions[selected]
    daily = base.loc[common, ["date", "roll_adjusted_return"]].copy()
    daily["fundamental_current_all_11"] = fundamentals[
        "current_all_11_equal"
    ].loc[common]
    daily["fundamental_no_consumption_equal_9"] = fundamentals[
        "no_consumption_equal_9"
    ].loc[common]
    daily["fundamental_selected"] = fundamentals[selected].loc[common]
    daily["score_selected"] = selected_score.loc[common]
    daily["position_selected"] = selected_position.loc[common]
    daily_labels = {
        "current": "current_all_11_equal",
        "equal_9": "no_consumption_equal_9",
        "selected": selected,
    }
    for label, name in daily_labels.items():
        position = positions[name].loc[common]
        daily[f"position_{label}"] = position
        daily[f"turnover_{label}"] = position.diff().abs().fillna(position.abs())
        daily[f"net_return_{label}"] = (
            position * daily["roll_adjusted_return"]
            - daily[f"turnover_{label}"] * TRANSACTION_COST_BPS / 10_000.0
        )
        daily[f"net_index_{label}"] = (
            1.0 + daily[f"net_return_{label}"]
        ).cumprod()

    output_dir.mkdir(parents=True, exist_ok=True)
    candidate_results.to_csv(output_dir / "development_candidates.csv", index=False)
    period_results.to_csv(output_dir / "period_comparison.csv", index=False)
    annual_results.to_csv(output_dir / "annual_comparison.csv", index=False)
    selected_weight_table.to_csv(output_dir / "selected_weights.csv", index=False)
    daily.to_parquet(
        output_dir / "selected_daily.parquet",
        index=False,
        compression="zstd",
    )

    full = period_results.loc[period_results["period"].eq("full")].set_index(
        "candidate"
    )
    period_sharpes = period_results.pivot(
        index="candidate", columns="period", values="sharpe_zero_rf"
    ).to_dict(orient="index")
    summary = {
        "experiment": "remove_consumption_and_reallocate_two_fixed_slots",
        "status": "research_only",
        "gcs_objects_modified": False,
        "sample_start": base.loc[common, "date"].min(),
        "sample_end": base.loc[common, "date"].max(),
        "trading_days": int(common.sum()),
        "removed_components": REMOVED_COMPONENTS,
        "remaining_components": REMAINING_COMPONENTS,
        "selection_period": {
            "start": base.loc[development, "date"].min(),
            "end": base.loc[development, "date"].max(),
            "trading_days": int(development.sum()),
        },
        "selection_rule": (
            "among 45 unordered assignments of two fixed 1/11 slots, retain "
            f"candidates within {DEVELOPMENT_SHARPE_TOLERANCE:.2f} of the best "
            "2.5-bps development Sharpe, then select lowest development turnover; "
            "validation and holdout excluded"
        ),
        "best_development_sharpe": best_development_sharpe,
        "development_sharpe_tolerance": DEVELOPMENT_SHARPE_TOLERANCE,
        "selected_candidate": selected,
        "selected_weights": selected_weights,
        "period_sharpes": period_sharpes,
        "full_metrics": {
            name: {
                "sharpe_zero_rf": float(full.loc[name, "sharpe_zero_rf"]),
                "cagr": float(full.loc[name, "cagr"]),
                "maximum_drawdown": float(full.loc[name, "maximum_drawdown"]),
                "win_rate": float(full.loc[name, "win_rate"]),
                "total_turnover": float(full.loc[name, "total_turnover"]),
            }
            for name in reported
        },
        "unchanged_policy": {
            "top_level_fundamental_allocation": "unchanged seasonal allocation",
            "wind_allocation": SELECTED_WIND_ALLOCATION,
            "solar_factor": PRIMARY_FACTOR,
            "solar_nominal_weight": SOLAR_NOMINAL_WEIGHT,
            "front_month_roll": "five trading days before official LTD",
            "signal_lag_sessions": 1,
            "transaction_cost_bps": TRANSACTION_COST_BPS,
        },
        "caution": (
            "Selection uses revised historical fundamentals rather than archived "
            "first-release vintages. The selected weights must be judged mainly "
            "on validation and first-look holdout, not full-sample Sharpe."
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
