#!/usr/bin/env python3
"""Evaluate a capacity-weighted GFS solar factor in the current NG strategy.

This is an isolated, research-only experiment.  It uses the completed GDEX
solar backfill, lagged EIA utility-scale solar capacity weights, the current
capacity-weighted wind allocation, the five-trading-day early futures roll,
and the current policy that keeps CPC level and observed weather as neutral
zero slots.  It writes local research artifacts only.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
from typing import Any, Iterable

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from naturalgas.evaluate_ncar_gdex_independent_wind_weights import (
    GROUP_READY_COLUMNS,
    LOCAL_PANEL,
    PERIODS,
    WIND_FEATURES,
    allocation_score,
    build_research_panel,
    candidate_allocations,
    common_sample,
    evaluate_allocations,
    performance,
    select_development_allocation,
)
from naturalgas.execution import apply_early_roll_return


SOLAR_ROOT = (
    Path(__file__).resolve().parent
    / "processed/ncar_gdex_capacity_weighted_solar"
)
SOLAR_SIGNAL_PATH = SOLAR_ROOT / "capacity_weighted_solar_signals.parquet"
SOLAR_LEAD_PATH = SOLAR_ROOT / "capacity_weighted_location_leads.parquet"
DEFAULT_OUTPUT_DIR = (
    Path(__file__).resolve().parent
    / "processed/ncar_gdex_complete_solar_factor"
)

PRIMARY_CYCLE_UTC = 0
ACTIVE_DIRECT_WEATHER_COMPONENTS = ("sig_cpc_seasonal_revision",)
LEGACY_WEATHER_SLOT_COUNT = 3
TRANSACTION_COST_BPS = 2.5
WEIGHT_GRID = tuple(float(x) for x in np.arange(0.0, 0.1501, 0.025))
CONSERVATIVE_WEIGHT = 0.05
INTERMEDIATE_WEIGHT = 0.10
DAYLIGHT_REFERENCE_KWH_M2_DAY = 10.0
DAYLIGHT_SCALE_FLOOR = 0.25
PRIMARY_FACTOR = "pv_daylight"
FACTOR_COLUMNS = {
    "pv_daylight": "sig_solar_pv",
    "pv_fixed": "sig_solar_pv",
    "radiation_daylight": "sig_solar_radiation",
    "cloud_daylight": "sig_solar_cloud",
}


def json_default(value: Any) -> Any:
    if isinstance(value, (pd.Timestamp, np.datetime64)):
        return pd.Timestamp(value).isoformat()
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if np.isnan(value) else float(value)
    if pd.isna(value):
        return None
    raise TypeError(type(value).__name__)


def neutralize_non_directional_weather_slots(
    frame: pd.DataFrame,
    allocations: Iterable[Any],
) -> pd.DataFrame:
    """Match the current notebook's fixed neutral-slot weather policy."""

    result = frame.copy()
    result["legacy_weather"] = (
        np.tanh(result[list(ACTIVE_DIRECT_WEATHER_COMPONENTS)] / 2.0)
        .fillna(0.0)
        .sum(axis=1)
        / LEGACY_WEATHER_SLOT_COUNT
    )
    result["sig_cpc_level_direct_slot"] = 0.0
    result["sig_observed_weather_direct_slot"] = 0.0
    for allocation in allocations:
        score_column = f"score_{allocation.name}"
        position_column = f"position_{allocation.name}"
        result[score_column] = allocation_score(result, allocation)
        result[position_column] = result[score_column].shift(1).clip(-1.0, 1.0)
    return result


def build_current_baseline(
    panel_path: Path,
    wind_path: Path,
    *,
    through_date: pd.Timestamp,
) -> tuple[pd.DataFrame, Any, pd.DataFrame]:
    """Rebuild and development-select the current independent wind strategy."""

    allocations = candidate_allocations()
    scored = build_research_panel(panel_path, wind_path, allocations)
    scored = neutralize_non_directional_weather_slots(scored, allocations)
    scored = apply_early_roll_return(scored)
    daily = common_sample(scored, allocations, through_date=through_date)
    _, wind_periods = evaluate_allocations(daily, allocations)
    selected_wind = select_development_allocation(wind_periods, allocations)
    daily["baseline_score"] = daily[f"score_{selected_wind.name}"]
    daily["position_baseline"] = daily[f"position_{selected_wind.name}"]
    return daily, selected_wind, wind_periods


def load_capacity_weighted_solar(
    signal_path: Path,
    lead_path: Path,
) -> pd.DataFrame:
    """Load the 00Z causal signal and deterministic daylight/capacity context."""

    signals = pd.read_parquet(signal_path)
    signals["forecast_reference_time_utc"] = pd.to_datetime(
        signals["forecast_reference_time_utc"], utc=True
    )
    signals["date"] = pd.to_datetime(signals["date"])
    primary = signals.loc[
        signals["forecast_cycle_hour_utc"].eq(PRIMARY_CYCLE_UTC)
    ].copy()

    leads = pd.read_parquet(
        lead_path,
        columns=[
            "forecast_reference_time_utc",
            "extraterrestrial_kwh_m2_day",
            "total_solar_capacity_mw",
            "capacity_coverage",
        ],
    )
    leads["forecast_reference_time_utc"] = pd.to_datetime(
        leads["forecast_reference_time_utc"], utc=True
    )
    context = (
        leads.groupby("forecast_reference_time_utc", as_index=False, observed=True)
        .agg(
            extraterrestrial_5d_kwh_m2_day=(
                "extraterrestrial_kwh_m2_day",
                "mean",
            ),
            lagged_utility_solar_capacity_mw=(
                "total_solar_capacity_mw",
                "mean",
            ),
            minimum_capacity_coverage=("capacity_coverage", "min"),
        )
    )
    primary = primary.merge(
        context,
        on="forecast_reference_time_utc",
        how="left",
        validate="one_to_one",
    )
    primary["daylight_scale"] = (
        primary["extraterrestrial_5d_kwh_m2_day"]
        .div(DAYLIGHT_REFERENCE_KWH_M2_DAY)
        .clip(DAYLIGHT_SCALE_FLOOR, 1.0)
    )
    return primary


def merge_and_transform_factors(
    baseline: pd.DataFrame,
    solar: pd.DataFrame,
) -> pd.DataFrame:
    columns = [
        "date",
        "forecast_reference_time_utc",
        "input_complete",
        "clearness_index_5d",
        "pv_availability_index_5d",
        "sig_solar_radiation",
        "sig_solar_pv",
        "sig_solar_cloud",
        "extraterrestrial_5d_kwh_m2_day",
        "daylight_scale",
        "lagged_utility_solar_capacity_mw",
        "minimum_capacity_coverage",
    ]
    result = baseline.merge(
        solar[columns], on="date", how="left", validate="one_to_one"
    )
    result["solar_transformed_pv_fixed"] = np.tanh(
        result["sig_solar_pv"] / 2.0
    )
    result["solar_transformed_pv_daylight"] = (
        result["solar_transformed_pv_fixed"] * result["daylight_scale"]
    )
    result["solar_transformed_radiation_daylight"] = (
        np.tanh(result["sig_solar_radiation"] / 2.0)
        * result["daylight_scale"]
    )
    result["solar_transformed_cloud_daylight"] = (
        np.tanh(result["sig_solar_cloud"] / 2.0)
        * result["daylight_scale"]
    )
    return result


def apply_production_control(frame: pd.DataFrame, score: pd.Series) -> pd.Series:
    cold = frame["date"].dt.month.isin((11, 12, 1, 2, 3))
    freeze = (
        cold
        & frame["prod_freeze_local_level_score"].ge(1.0)
        & frame["prod_freeze_local_revision_score"].ge(0.0)
    )
    return score.where(~freeze, score.clip(lower=0.0))


def add_solar_candidates(panel: pd.DataFrame) -> pd.DataFrame:
    """Fund each fixed solar slot from the current fundamental allocation."""

    result = panel.copy()
    generated: dict[str, pd.Series] = {}
    for factor in FACTOR_COLUMNS:
        transformed = result[f"solar_transformed_{factor}"]
        for weight in WEIGHT_GRID:
            effective_weight = weight
            if factor.endswith("_daylight"):
                effective_weight = weight * result["daylight_scale"].fillna(0.0)
                # The transformed daylight factors already include the scale.
                signal_for_slot = transformed.fillna(0.0).div(
                    result["daylight_scale"].replace(0.0, np.nan)
                ).fillna(0.0)
            else:
                signal_for_slot = transformed.fillna(0.0)

            active_score = result["baseline_score"] + effective_weight * (
                signal_for_slot - result["fundamental_rebuilt"]
            )
            neutral_score = result["baseline_score"] - (
                effective_weight * result["fundamental_rebuilt"]
            )
            active_score = apply_production_control(result, active_score)
            neutral_score = apply_production_control(result, neutral_score)
            generated[f"score_active_{factor}_{weight:.3f}"] = active_score
            generated[f"score_neutral_{factor}_{weight:.3f}"] = neutral_score
            generated[f"position_active_{factor}_{weight:.3f}"] = (
                active_score.shift(1).clip(-1.0, 1.0)
            )
            generated[f"position_neutral_{factor}_{weight:.3f}"] = (
                neutral_score.shift(1).clip(-1.0, 1.0)
            )
    return pd.concat([result, pd.DataFrame(generated, index=result.index)], axis=1)


def extended_performance(
    frame: pd.DataFrame,
    *,
    position_column: str,
    cost_bps: float = TRANSACTION_COST_BPS,
) -> dict[str, Any]:
    metrics = performance(
        frame,
        position_column=position_column,
        cost_bps=cost_bps,
    )
    position = frame[position_column]
    turnover = position.diff().abs().fillna(position.abs())
    net_return = (
        position * frame["roll_adjusted_return"]
        - turnover * cost_bps / 10_000.0
    )
    metrics["win_rate"] = float(net_return.gt(0.0).mean())
    metrics["positive_position_day_rate"] = float(position.gt(0.0).mean())
    return metrics


def evaluate_grid(panel: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for period, (start, configured_end) in PERIODS.items():
        end = min(configured_end, panel["date"].max())
        sample = panel.loc[panel["date"].between(start, end)]
        baseline = extended_performance(sample, position_column="position_baseline")
        baseline.update(
            {
                "period": period,
                "factor": "baseline",
                "variant": "baseline",
                "nominal_weight": 0.0,
            }
        )
        rows.append(baseline)
        for factor in FACTOR_COLUMNS:
            for weight in WEIGHT_GRID:
                for variant in ("active", "neutral"):
                    metrics = extended_performance(
                        sample,
                        position_column=(
                            f"position_{variant}_{factor}_{weight:.3f}"
                        ),
                    )
                    metrics.update(
                        {
                            "period": period,
                            "factor": factor,
                            "variant": variant,
                            "nominal_weight": weight,
                        }
                    )
                    rows.append(metrics)
    return pd.DataFrame(rows)


def select_primary_weight(results: pd.DataFrame) -> float:
    development = results.loc[
        results["period"].eq("development")
        & results["factor"].eq(PRIMARY_FACTOR)
        & results["variant"].eq("active")
    ]
    return float(
        development.sort_values(
            ["sharpe_zero_rf", "nominal_weight"],
            ascending=[False, True],
        ).iloc[0]["nominal_weight"]
    )


def annual_results(panel: pd.DataFrame, selected_weight: float) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for year, sample in panel.groupby(panel["date"].dt.year):
        variants = [("baseline", 0.0, "position_baseline")]
        for label, weight in (
            ("conservative", CONSERVATIVE_WEIGHT),
            ("intermediate", INTERMEDIATE_WEIGHT),
            ("development_selected", selected_weight),
        ):
            variants.extend(
                [
                    (
                        f"solar_active_{label}",
                        weight,
                        f"position_active_{PRIMARY_FACTOR}_{weight:.3f}",
                    ),
                    (
                        f"neutral_slot_{label}",
                        weight,
                        f"position_neutral_{PRIMARY_FACTOR}_{weight:.3f}",
                    ),
                ]
            )
        seen: set[tuple[str, float]] = set()
        for variant, weight, column in variants:
            key = (column, weight)
            if key in seen:
                continue
            seen.add(key)
            metrics = extended_performance(sample, position_column=column)
            metrics.update(
                {
                    "year": int(year),
                    "variant": variant,
                    "nominal_weight": weight,
                }
            )
            rows.append(metrics)
    return pd.DataFrame(rows)


def cost_sensitivity(panel: pd.DataFrame, selected_weight: float) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    variants = (
        ("baseline", 0.0, "position_baseline"),
        (
            "solar_active_conservative",
            CONSERVATIVE_WEIGHT,
            f"position_active_{PRIMARY_FACTOR}_{CONSERVATIVE_WEIGHT:.3f}",
        ),
        (
            "solar_active_intermediate",
            INTERMEDIATE_WEIGHT,
            f"position_active_{PRIMARY_FACTOR}_{INTERMEDIATE_WEIGHT:.3f}",
        ),
        (
            "solar_active_development_selected",
            selected_weight,
            f"position_active_{PRIMARY_FACTOR}_{selected_weight:.3f}",
        ),
    )
    for cost_bps in (0.0, 2.5, 5.0):
        for variant, weight, column in variants:
            metrics = extended_performance(
                panel,
                position_column=column,
                cost_bps=cost_bps,
            )
            metrics.update(
                {
                    "variant": variant,
                    "nominal_weight": weight,
                    "cost_bps": cost_bps,
                }
            )
            rows.append(metrics)
    return pd.DataFrame(rows)


def factor_ic(panel: pd.DataFrame) -> pd.DataFrame:
    next_return = panel["roll_adjusted_return"].shift(-1)
    rows: list[dict[str, Any]] = []
    for period, (start, configured_end) in PERIODS.items():
        end = min(configured_end, panel["date"].max())
        period_mask = panel["date"].between(start, end)
        for factor in FACTOR_COLUMNS:
            sample = pd.DataFrame(
                {
                    "factor": panel[f"solar_transformed_{factor}"],
                    "next_return": next_return,
                }
            ).loc[period_mask].dropna()
            rows.append(
                {
                    "period": period,
                    "factor": factor,
                    "observations": len(sample),
                    "pearson_ic": sample["factor"].corr(sample["next_return"]),
                    "spearman_ic": sample["factor"].corr(
                        sample["next_return"], method="spearman"
                    ),
                }
            )
    return pd.DataFrame(rows)


def period_metric_map(
    results: pd.DataFrame,
    *,
    factor: str,
    variant: str,
    weight: float,
) -> dict[str, dict[str, float]]:
    selected = results.loc[
        results["factor"].eq(factor)
        & results["variant"].eq(variant)
        & results["nominal_weight"].eq(weight)
    ]
    return {
        row.period: {
            "trading_days": int(row.trading_days),
            "sharpe": float(row.sharpe_zero_rf),
            "cagr": float(row.cagr),
            "maximum_drawdown": float(row.maximum_drawdown),
            "win_rate": float(row.win_rate),
        }
        for row in selected.itertuples(index=False)
    }


def run(
    *,
    panel_path: Path,
    wind_path: Path,
    signal_path: Path,
    lead_path: Path,
    output_dir: Path,
    through_date: pd.Timestamp,
) -> dict[str, Any]:
    baseline, selected_wind, wind_periods = build_current_baseline(
        panel_path,
        wind_path,
        through_date=through_date,
    )
    solar = load_capacity_weighted_solar(signal_path, lead_path)
    panel = merge_and_transform_factors(baseline, solar)
    panel = add_solar_candidates(panel)

    results = evaluate_grid(panel)
    selected_weight = select_primary_weight(results)
    annual = annual_results(panel, selected_weight)
    ic = factor_ic(panel)
    costs = cost_sensitivity(panel, selected_weight)

    output_dir.mkdir(parents=True, exist_ok=True)
    results.to_csv(output_dir / "solar_weight_grid.csv", index=False)
    annual.to_csv(output_dir / "solar_selected_annual.csv", index=False)
    ic.to_csv(output_dir / "solar_factor_ic.csv", index=False)
    costs.to_csv(output_dir / "solar_cost_sensitivity.csv", index=False)
    wind_periods.to_csv(output_dir / "upstream_wind_periods.csv", index=False)

    selected_columns = [
        "date",
        "roll_adjusted_return",
        "baseline_score",
        "position_baseline",
        "fundamental_rebuilt",
        "forecast_reference_time_utc",
        "input_complete",
        "clearness_index_5d",
        "pv_availability_index_5d",
        "sig_solar_pv",
        "daylight_scale",
        "lagged_utility_solar_capacity_mw",
        f"score_active_{PRIMARY_FACTOR}_{CONSERVATIVE_WEIGHT:.3f}",
        f"position_active_{PRIMARY_FACTOR}_{CONSERVATIVE_WEIGHT:.3f}",
        f"position_neutral_{PRIMARY_FACTOR}_{CONSERVATIVE_WEIGHT:.3f}",
        f"score_active_{PRIMARY_FACTOR}_{INTERMEDIATE_WEIGHT:.3f}",
        f"position_active_{PRIMARY_FACTOR}_{INTERMEDIATE_WEIGHT:.3f}",
        f"position_neutral_{PRIMARY_FACTOR}_{INTERMEDIATE_WEIGHT:.3f}",
        f"score_active_{PRIMARY_FACTOR}_{selected_weight:.3f}",
        f"position_active_{PRIMARY_FACTOR}_{selected_weight:.3f}",
        f"position_neutral_{PRIMARY_FACTOR}_{selected_weight:.3f}",
    ]
    panel[selected_columns].to_parquet(
        output_dir / "solar_selected_daily.parquet",
        index=False,
        compression="zstd",
    )

    baseline_metrics = period_metric_map(
        results,
        factor="baseline",
        variant="baseline",
        weight=0.0,
    )
    conservative_metrics = period_metric_map(
        results,
        factor=PRIMARY_FACTOR,
        variant="active",
        weight=CONSERVATIVE_WEIGHT,
    )
    selected_metrics = period_metric_map(
        results,
        factor=PRIMARY_FACTOR,
        variant="active",
        weight=selected_weight,
    )
    selected_neutral = period_metric_map(
        results,
        factor=PRIMARY_FACTOR,
        variant="neutral",
        weight=selected_weight,
    )
    conservative_neutral = period_metric_map(
        results,
        factor=PRIMARY_FACTOR,
        variant="neutral",
        weight=CONSERVATIVE_WEIGHT,
    )
    intermediate_metrics = period_metric_map(
        results,
        factor=PRIMARY_FACTOR,
        variant="active",
        weight=INTERMEDIATE_WEIGHT,
    )
    intermediate_neutral = period_metric_map(
        results,
        factor=PRIMARY_FACTOR,
        variant="neutral",
        weight=INTERMEDIATE_WEIGHT,
    )
    availability = panel["input_complete"].fillna(False)
    summary = {
        "experiment": "capacity_weighted_gdex_solar_factor",
        "status": "research_only",
        "production_panel_modified": False,
        "gcs_objects_modified": False,
        "sample_start": panel["date"].min(),
        "sample_end": panel["date"].max(),
        "trading_days": len(panel),
        "solar_complete_trading_days": int(availability.sum()),
        "solar_missing_or_incomplete_trading_days": int((~availability).sum()),
        "selected_upstream_wind_allocation": asdict(selected_wind),
        "primary_factor": PRIMARY_FACTOR,
        "factor_definition": (
            "tanh(causal_z60[-capacity-weighted 1-5d PV availability] / 2) "
            "with deterministic extraterrestrial-daylight slot scaling"
        ),
        "capacity_policy": (
            "EIA utility-scale SUN capacity by month and nearest of 28 GFS "
            "locations, lagged two months"
        ),
        "selection_rule": (
            "highest 2.5 bps net development Sharpe (2016-07-06 through "
            "2020-12-31); ties choose smaller nominal weight"
        ),
        "selected_nominal_weight": selected_weight,
        "selected_at_upper_grid_boundary": selected_weight == max(WEIGHT_GRID),
        "conservative_nominal_weight": CONSERVATIVE_WEIGHT,
        "intermediate_nominal_weight": INTERMEDIATE_WEIGHT,
        "execution": {
            "signal_lag": "one trading session",
            "front_month_roll": "five trading days before official LTD",
            "transaction_cost_bps": TRANSACTION_COST_BPS,
            "missing_signal_policy": "fixed solar slot is neutral zero",
        },
        "baseline_metrics": baseline_metrics,
        "conservative_metrics": conservative_metrics,
        "conservative_neutral_slot_control": conservative_neutral,
        "intermediate_metrics": intermediate_metrics,
        "intermediate_neutral_slot_control": intermediate_neutral,
        "development_selected_metrics": selected_metrics,
        "selected_neutral_slot_control": selected_neutral,
        "limitations": [
            "EIA capacity excludes distributed solar below the utility-scale threshold",
            "plants are mapped to the nearest of only 28 weather locations",
            "capacity history is revised data rather than archived publication vintages",
            "GFS shortwave radiation is not a full plane-of-array PV model",
            "curtailment, snow, tracking geometry, and plant outages are not modeled",
        ],
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
    parser.add_argument("--through-date", type=pd.Timestamp, default="2026-07-31")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    result = run(
        panel_path=args.panel,
        wind_path=args.wind_features,
        signal_path=args.solar_signals,
        lead_path=args.solar_leads,
        output_dir=args.output_dir,
        through_date=args.through_date,
    )
    print(json.dumps(result, default=json_default, indent=2, sort_keys=True))
