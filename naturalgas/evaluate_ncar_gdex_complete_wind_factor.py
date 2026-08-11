#!/usr/bin/env python3
"""Build and evaluate a complete physical GDEX wind-power factor.

The experiment is local and research-only.  It combines:

* annual point-in-time USWTDB capacity weights (commissioned by prior year);
* capacity-weighted hub-height adjustment from the GFS 80 m wind field;
* a fixed nonlinear fleet power curve, including high-wind derating/cut-out;
* front-loaded forecast-horizon weights for power-sector relevance;
* a causal level anomaly and a successive-run revision component;
* one-trading-day execution lag and transaction-cost sensitivity.

No production panel or cloud object is modified.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import gcsfs
import numpy as np
import pandas as pd
import requests


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from naturalgas.evaluate_ncar_gdex_wind_factor import (  # noqa: E402
    BACKTEST_START,
    COLD_SEASON_MONTHS,
    FUNDAMENTAL_FAST_COMPONENTS,
    FUNDAMENTAL_MONTHLY_COMPONENTS,
    GROUP_READY_COLUMNS,
    LOCAL_PANEL,
    PEAK_DEMAND_MONTHS,
    PEAK_WEATHER_WEIGHT,
    PRODUCTION_LOCAL_LEVEL_THRESHOLD,
    PROJECT,
    RAW_GLOB,
    RAW_START_MONTH,
    SHOULDER_WEATHER_WEIGHT,
    month_from_key,
    parse_month,
    select_continuous_keys,
)
from naturalgas.ncar_gdex_capacity_weighted_wind import (  # noqa: E402
    USWTDB_API,
    USWTDB_COLUMNS,
)
from naturalgas.ncar_gdex_nonlinear_wind import (  # noqa: E402
    DEFAULT_POWER_CURVE,
    causal_zscore,
    nonlinear_power_components,
)
from naturalgas.ncar_gdex_wind_backfill_to_gcs import (  # noqa: E402
    LOCATIONS,
    location_bbox,
)
from naturalgas.weather_feature_policy import (  # noqa: E402
    PRIMARY_WEATHER_COMPONENTS,
    fixed_weight_mean,
)


DEFAULT_THROUGH_MONTH = pd.Period("2026-07", freq="M")
DEFAULT_OUTPUT_DIR = (
    Path(__file__).resolve().parent
    / "processed"
    / "ncar_gdex_complete_wind_factor"
)
EXPECTED_SAMPLES_PER_INITIALIZATION = len(LOCATIONS) * 5 * 4
PRIMARY_CYCLE_UTC = 0
SHEAR_EXPONENT = 0.14
LEVEL_WEIGHT = 0.75
REVISION_WEIGHT = 0.25
HORIZON_WEIGHTS = {1: 5.0, 2: 4.0, 3: 3.0, 4: 2.0, 5: 1.0}
TRANSACTION_COSTS_BPS = (0.0, 2.5, 5.0)
WIND_SPECS = {
    "equal_nonlinear": "sig_equal_nonlinear",
    "capacity_cf": "sig_capacity_cf",
    "capacity_mw": "sig_capacity_mw",
    "level_plus_revision": "sig_complete_wind",
}
SELECTED_VERSION = "capacity_cf"
EVALUATION_PERIODS = {
    "full": (pd.Timestamp("2016-07-06"), pd.Timestamp("2026-07-31")),
    "development": (pd.Timestamp("2016-07-06"), pd.Timestamp("2020-12-31")),
    "validation": (pd.Timestamp("2021-01-01"), pd.Timestamp("2023-12-31")),
    "first_look_holdout": (pd.Timestamp("2024-01-01"), pd.Timestamp("2026-07-31")),
}


class CompleteWindFactorError(RuntimeError):
    """Raised when the complete wind experiment cannot be reproduced."""


def fetch_turbines(
    *,
    timeout_seconds: float,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    response = requests.get(
        USWTDB_API,
        params={
            "select": USWTDB_COLUMNS,
            "limit": 100_000,
            "order": "case_id.asc",
        },
        headers={
            "Prefer": "count=exact",
            "User-Agent": "braeswood-complete-wind-factor/1.0",
        },
        timeout=(30, timeout_seconds),
    )
    response.raise_for_status()
    turbines = pd.DataFrame(response.json())
    if turbines.empty:
        raise CompleteWindFactorError("USWTDB returned no turbines")
    for column in ("p_year", "t_cap", "t_hh", "xlong", "ylat"):
        turbines[column] = pd.to_numeric(turbines[column], errors="coerce")
    metadata = {
        "source_url": response.url,
        "response_rows": len(turbines),
        "content_range": response.headers.get("Content-Range"),
    }
    return turbines, metadata


def assign_nearest_location(turbines: pd.DataFrame) -> pd.DataFrame:
    bbox = location_bbox()
    eligible = turbines.loc[
        turbines["p_year"].notna()
        & turbines["t_cap"].gt(0)
        & turbines["xlong"].between(bbox.west, bbox.east)
        & turbines["ylat"].between(bbox.south, bbox.north)
    ].copy()
    if eligible.empty:
        raise CompleteWindFactorError("no eligible turbines inside GDEX bbox")
    location_ids = np.array([item.location_id for item in LOCATIONS])
    location_latitude = np.array([item.latitude for item in LOCATIONS])
    location_longitude = np.array([item.longitude for item in LOCATIONS])
    turbine_latitude = eligible["ylat"].to_numpy(dtype=float)
    turbine_longitude = eligible["xlong"].to_numpy(dtype=float)
    latitude_delta = turbine_latitude[:, None] - location_latitude[None, :]
    longitude_scale = np.cos(
        np.radians((turbine_latitude[:, None] + location_latitude[None, :]) / 2)
    )
    longitude_delta = (
        turbine_longitude[:, None] - location_longitude[None, :]
    ) * longitude_scale
    nearest = np.argmin(latitude_delta**2 + longitude_delta**2, axis=1)
    eligible["location_id"] = location_ids[nearest]
    eligible["capacity_mw"] = eligible["t_cap"] / 1_000.0
    return eligible


def build_annual_location_weights(
    turbines: pd.DataFrame,
    *,
    first_year: int,
    last_year: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    assigned = assign_nearest_location(turbines)
    frames: list[pd.DataFrame] = []
    diagnostics: list[dict[str, Any]] = []
    all_locations = pd.DataFrame(
        {"location_id": [item.location_id for item in LOCATIONS]}
    )
    for issue_year in range(first_year, last_year + 1):
        cutoff = issue_year - 1
        fleet = assigned.loc[assigned["p_year"].le(cutoff)].copy()
        if fleet.empty:
            raise CompleteWindFactorError(f"no wind fleet for {issue_year}")
        fleet["known_hub_capacity_mw"] = np.where(
            fleet["t_hh"].gt(0), fleet["capacity_mw"], 0.0
        )
        fleet["hub_capacity_product"] = np.where(
            fleet["t_hh"].gt(0),
            fleet["t_hh"] * fleet["capacity_mw"],
            0.0,
        )
        grouped = (
            fleet.groupby("location_id", as_index=False)
            .agg(
                capacity_mw=("capacity_mw", "sum"),
                turbine_count=("case_id", "nunique"),
                known_hub_capacity_mw=("known_hub_capacity_mw", "sum"),
                hub_capacity_product=("hub_capacity_product", "sum"),
            )
        )
        grouped = all_locations.merge(grouped, on="location_id", how="left")
        for column in (
            "capacity_mw",
            "turbine_count",
            "known_hub_capacity_mw",
            "hub_capacity_product",
        ):
            grouped[column] = grouped[column].fillna(0.0)
        total_capacity = float(grouped["capacity_mw"].sum())
        known_capacity = float(grouped["known_hub_capacity_mw"].sum())
        fleet_hub_height = (
            float(grouped["hub_capacity_product"].sum()) / known_capacity
            if known_capacity
            else 80.0
        )
        grouped["hub_height_m"] = np.where(
            grouped["known_hub_capacity_mw"].gt(0),
            grouped["hub_capacity_product"]
            / grouped["known_hub_capacity_mw"].replace(0, np.nan),
            fleet_hub_height,
        )
        grouped["capacity_share"] = grouped["capacity_mw"] / total_capacity
        grouped["issue_year"] = issue_year
        grouped["fleet_cutoff_year"] = cutoff
        frames.append(grouped)
        diagnostics.append(
            {
                "issue_year": issue_year,
                "fleet_cutoff_year": cutoff,
                "capacity_mw": total_capacity,
                "turbine_count": int(grouped["turbine_count"].sum()),
                "active_proxy_locations": int(grouped["capacity_mw"].gt(0).sum()),
                "capacity_weighted_hub_height_m": fleet_hub_height,
                "largest_location_share": float(grouped["capacity_share"].max()),
            }
        )
    weights = pd.concat(frames, ignore_index=True)
    return weights, pd.DataFrame(diagnostics)


def _weighted_group_features(month: pd.DataFrame) -> pd.DataFrame:
    group_columns = [
        "forecast_reference_time_utc",
        "forecast_cycle_hour_utc",
        "issue_year",
        "fleet_cutoff_year",
    ]
    month["equal_weight"] = month["capacity_mw"]
    month["front_weight"] = (
        month["capacity_mw"] * month["lead_days"].map(HORIZON_WEIGHTS)
    )
    value_columns = (
        "wind_speed_hub_mps",
        "power_cf_no_cutout",
        "effective_power_cf",
        "low_wind_shortfall_cf",
        "high_wind_cutout_loss_cf",
        "total_shortfall_cf",
    )
    aggregations: dict[str, tuple[str, str]] = {
        "sample_count": ("wind_speed_80m_mps", "count"),
        "capacity_mw_sum_repeated": ("capacity_mw", "sum"),
    }
    for weight_name in ("equal_weight", "front_weight"):
        aggregations[f"{weight_name}_denominator"] = (weight_name, "sum")
        for value in value_columns:
            product = f"_{value}_{weight_name}_product"
            month[product] = month[value] * month[weight_name]
            aggregations[f"{value}_{weight_name}_numerator"] = (product, "sum")
    grouped = month.groupby(group_columns, as_index=False).agg(**aggregations)
    samples_per_capacity = 20.0
    grouped["fleet_capacity_mw"] = (
        grouped["capacity_mw_sum_repeated"] / samples_per_capacity
    )
    for weight_name in ("equal_weight", "front_weight"):
        denominator = grouped[f"{weight_name}_denominator"]
        suffix = "equal" if weight_name == "equal_weight" else "front"
        for value in value_columns:
            grouped[f"{value}_{suffix}"] = (
                grouped[f"{value}_{weight_name}_numerator"] / denominator
            )
    return grouped


def build_capacity_features(
    filesystem: gcsfs.GCSFileSystem,
    keys: list[str],
    annual_weights: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    columns = [
        "forecast_reference_time_utc",
        "forecast_cycle_hour_utc",
        "location_id",
        "lead_days",
        "wind_speed_80m_mps",
    ]
    frames: list[pd.DataFrame] = []
    raw_rows = 0
    for number, key in enumerate(keys, start=1):
        with filesystem.open(key, "rb") as handle:
            month = pd.read_parquet(handle, columns=columns)
        raw_rows += len(month)
        month["issue_year"] = pd.to_datetime(
            month["forecast_reference_time_utc"], utc=True
        ).dt.year
        month = month.merge(
            annual_weights[
                [
                    "issue_year",
                    "fleet_cutoff_year",
                    "location_id",
                    "capacity_mw",
                    "hub_height_m",
                ]
            ],
            on=["issue_year", "location_id"],
            how="left",
            validate="many_to_one",
        )
        if month[["capacity_mw", "hub_height_m"]].isna().any().any():
            raise CompleteWindFactorError(f"missing annual weights for {key}")
        month["wind_speed_hub_mps"] = month["wind_speed_80m_mps"] * (
            month["hub_height_m"] / 80.0
        ) ** SHEAR_EXPONENT
        components = nonlinear_power_components(month["wind_speed_hub_mps"])
        for name, values in components.items():
            if name in {
                "power_cf_no_cutout",
                "effective_power_cf",
                "low_wind_shortfall_cf",
                "high_wind_cutout_loss_cf",
                "total_shortfall_cf",
            }:
                month[name] = values
        frames.append(_weighted_group_features(month))
        if number % 12 == 0 or number == len(keys):
            print(
                f"capacity aggregation {number}/{len(keys)} months | "
                f"raw rows={raw_rows:,}",
                flush=True,
            )
    features = pd.concat(frames, ignore_index=True).sort_values(
        "forecast_reference_time_utc"
    ).reset_index(drop=True)
    complete = features["sample_count"].eq(EXPECTED_SAMPLES_PER_INITIALIZATION)
    quality = {
        "raw_point_rows": raw_rows,
        "all_initializations": len(features),
        "complete_initializations": int(complete.sum()),
        "excluded_incomplete_initializations": int((~complete).sum()),
        "expected_samples_per_initialization": EXPECTED_SAMPLES_PER_INITIALIZATION,
    }
    features = features.loc[complete].copy()
    features["shortfall_mw_equal"] = (
        features["fleet_capacity_mw"]
        * features["total_shortfall_cf_equal"]
    )
    features["shortfall_mw_front"] = (
        features["fleet_capacity_mw"]
        * features["total_shortfall_cf_front"]
    )
    features["previous_reference_time"] = features[
        "forecast_reference_time_utc"
    ].shift(1)
    six_hour_gap = (
        features["forecast_reference_time_utc"]
        - features["previous_reference_time"]
    ).eq(pd.Timedelta(hours=6))
    features["shortfall_mw_front_revision"] = (
        features["shortfall_mw_front"]
        - features["shortfall_mw_front"].shift(1)
    ).where(six_hour_gap)
    signal_sources = {
        "sig_capacity_cf": "total_shortfall_cf_equal",
        "sig_capacity_mw": "shortfall_mw_equal",
        "sig_capacity_mw_front": "shortfall_mw_front",
        "sig_capacity_revision": "shortfall_mw_front_revision",
    }
    for signal, source in signal_sources.items():
        features[signal] = features.groupby(
            "forecast_cycle_hour_utc", group_keys=False
        )[source].transform(causal_zscore)
    features["sig_complete_wind"] = (
        LEVEL_WEIGHT * features["sig_capacity_mw_front"]
        + REVISION_WEIGHT * features["sig_capacity_revision"]
    ) / math.sqrt(LEVEL_WEIGHT**2 + REVISION_WEIGHT**2)
    features["date"] = pd.to_datetime(
        features["forecast_reference_time_utc"], utc=True
    ).dt.tz_localize(None).dt.normalize()
    return features, quality


def build_strategy_scores(panel: pd.DataFrame) -> pd.DataFrame:
    result = panel.copy()
    weather_components = list(PRIMARY_WEATHER_COMPONENTS)
    result["weather_base"] = fixed_weight_mean(
        np.tanh(result[weather_components] / 2.0), weather_components
    )
    result["fundamental_rebuilt"] = pd.concat(
        [
            np.tanh(result[list(FUNDAMENTAL_FAST_COMPONENTS)] / 2.0),
            result[list(FUNDAMENTAL_MONTHLY_COMPONENTS)],
        ],
        axis=1,
    ).mean(axis=1, skipna=True)
    result["weather_neutral"] = result["weather_base"] * (
        len(weather_components) / (len(weather_components) + 1)
    )
    for name, wind_column in WIND_SPECS.items():
        components = [*weather_components, wind_column]
        result[f"weather_{name}"] = fixed_weight_mean(
            np.tanh(result[components] / 2.0), components
        )
    result["weather_weight"] = np.where(
        result["date"].dt.month.isin(PEAK_DEMAND_MONTHS),
        PEAK_WEATHER_WEIGHT,
        SHOULDER_WEATHER_WEIGHT,
    )
    result["fundamental_weight"] = 1.0 - result["weather_weight"]
    production_control = (
        result["date"].dt.month.isin(COLD_SEASON_MONTHS)
        & result["prod_freeze_local_level_score"].ge(
            PRODUCTION_LOCAL_LEVEL_THRESHOLD
        )
        & result["prod_freeze_local_revision_score"].ge(0.0)
    )
    versions = ["base", "neutral", *WIND_SPECS]
    for version in versions:
        pre_control = (
            result["weather_weight"] * result[f"weather_{version}"]
            + result["fundamental_weight"] * result["fundamental_rebuilt"]
        )
        result[f"seasonal_{version}"] = np.where(
            production_control, np.maximum(pre_control, 0.0), pre_control
        )
        result[f"fixed_{version}"] = (
            0.35 * result[f"weather_{version}"]
            + 0.30 * result["fundamental_rebuilt"]
        ) / 0.65
    return result


def performance(
    frame: pd.DataFrame,
    *,
    position_column: str,
    cost_bps: float,
) -> dict[str, Any]:
    position = frame[position_column]
    turnover = position.diff().abs().fillna(position.abs())
    net_return = (
        position * frame["roll_adjusted_return"]
        - turnover * cost_bps / 10_000.0
    )
    log_return = np.log1p(net_return)
    wealth = (1.0 + net_return).cumprod()
    drawdown = wealth / wealth.cummax() - 1.0
    years = (frame["date"].max() - frame["date"].min()).days / 365.2425
    volatility = log_return.std()
    return {
        "start": frame["date"].min(),
        "end": frame["date"].max(),
        "trading_days": len(frame),
        "total_return": float(wealth.iloc[-1] - 1.0),
        "cagr": float(np.exp(log_return.sum() / years) - 1.0),
        "annualized_volatility": float(volatility * np.sqrt(252)),
        "sharpe_zero_rf": float(
            log_return.mean() / volatility * np.sqrt(252)
        ),
        "maximum_drawdown": float(drawdown.min()),
        "mean_absolute_position": float(position.abs().mean()),
        "total_turnover": float(turnover.sum()),
    }


def hac_regression(
    signal: pd.Series,
    target: pd.Series,
    *,
    max_lag: int = 5,
) -> dict[str, float]:
    ready = signal.notna() & target.notna()
    x = signal.loc[ready].to_numpy(dtype=float)
    y = target.loc[ready].to_numpy(dtype=float)
    design = np.column_stack([np.ones(len(x)), x])
    inverse = np.linalg.inv(design.T @ design)
    beta = inverse @ design.T @ y
    residual = y - design @ beta
    score = design * residual[:, None]
    meat = score.T @ score
    for lag in range(1, min(max_lag, len(x) - 1) + 1):
        weight = 1.0 - lag / (max_lag + 1.0)
        covariance = score[lag:].T @ score[:-lag]
        meat += weight * (covariance + covariance.T)
    standard_error = np.sqrt(np.diag(inverse @ meat @ inverse))
    return {
        "observations": int(len(x)),
        "pearson_ic": float(np.corrcoef(x, y)[0, 1]),
        "spearman_ic": float(pd.Series(x).corr(pd.Series(y), method="spearman")),
        "regression_beta": float(beta[1]),
        "hac_t": float(beta[1] / standard_error[1]),
    }


def evaluate(
    panel: pd.DataFrame,
    *,
    through_month: pd.Period,
) -> tuple[pd.DataFrame, ...]:
    result = panel.copy()
    versions = ["base", "neutral", *WIND_SPECS]
    for family in ("seasonal", "fixed"):
        for version in versions:
            result[f"position_{family}_{version}"] = result[
                f"{family}_{version}"
            ].shift(1).clip(-1.0, 1.0)
    for name, signal in WIND_SPECS.items():
        result[f"position_standalone_{name}"] = np.tanh(
            result[signal].shift(1) / 2.0
        )
    group_ready = result[list(GROUP_READY_COLUMNS)].notna().all(axis=1)
    common = (
        result["date"].between(BACKTEST_START, through_month.end_time)
        & result["required_input_complete"].fillna(False)
        & group_ready
        & result["roll_adjusted_return"].notna()
    )
    for family in ("seasonal", "fixed"):
        for version in versions:
            common &= result[f"position_{family}_{version}"].notna()
    common_panel = result.loc[common].copy().reset_index(drop=True)
    comparison_rows: list[dict[str, Any]] = []
    period_rows: list[dict[str, Any]] = []
    for family in ("seasonal", "fixed"):
        for version in versions:
            for cost in TRANSACTION_COSTS_BPS:
                row = performance(
                    common_panel,
                    position_column=f"position_{family}_{version}",
                    cost_bps=cost,
                )
                row.update(
                    {"family": family, "version": version, "cost_bps": cost}
                )
                comparison_rows.append(row)
            for period, (start, end) in EVALUATION_PERIODS.items():
                period_frame = common_panel.loc[
                    common_panel["date"].between(start, end)
                ]
                if period_frame.empty:
                    continue
                row = performance(
                    period_frame,
                    position_column=f"position_{family}_{version}",
                    cost_bps=2.5,
                )
                row.update(
                    {"period": period, "family": family, "version": version}
                )
                period_rows.append(row)
    standalone_rows = []
    for name in WIND_SPECS:
        row = performance(
            common_panel,
            position_column=f"position_standalone_{name}",
            cost_bps=2.5,
        )
        row["version"] = name
        standalone_rows.append(row)
    result["target_next_trading_day_return"] = result[
        "roll_adjusted_return"
    ].shift(-1)
    ic_rows = []
    for name, signal in WIND_SPECS.items():
        row = hac_regression(
            result[signal], result["target_next_trading_day_return"]
        )
        row["version"] = name
        ic_rows.append(row)
    annual_rows = []
    for year, year_frame in common_panel.groupby(common_panel["date"].dt.year):
        for family in ("seasonal", "fixed"):
            for version in versions:
                row = performance(
                    year_frame,
                    position_column=f"position_{family}_{version}",
                    cost_bps=2.5,
                )
                row.update(
                    {"year": int(year), "family": family, "version": version}
                )
                annual_rows.append(row)
    return (
        common_panel,
        pd.DataFrame(comparison_rows),
        pd.DataFrame(period_rows),
        pd.DataFrame(standalone_rows),
        pd.DataFrame(ic_rows),
        pd.DataFrame(annual_rows),
    )


def json_default(value: Any) -> Any:
    if isinstance(value, (pd.Timestamp, pd.Period)):
        return str(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    raise TypeError(type(value).__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=PROJECT)
    parser.add_argument(
        "--through-month", type=parse_month, default=DEFAULT_THROUGH_MONTH
    )
    parser.add_argument("--panel", type=Path, default=LOCAL_PANEL)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--timeout", type=float, default=180.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    filesystem = gcsfs.GCSFileSystem(project=args.project)
    keys = select_continuous_keys(
        sorted(filesystem.glob(RAW_GLOB)), through_month=args.through_month
    )
    turbines, uswtdb_metadata = fetch_turbines(timeout_seconds=args.timeout)
    annual_weights, fleet_diagnostics = build_annual_location_weights(
        turbines,
        first_year=RAW_START_MONTH.year,
        last_year=args.through_month.year,
    )
    features, quality = build_capacity_features(
        filesystem, keys, annual_weights
    )
    primary = features.loc[
        features["forecast_cycle_hour_utc"].eq(PRIMARY_CYCLE_UTC)
    ].copy()
    equal_features = pd.read_parquet(
        Path(__file__).resolve().parent
        / "processed/ncar_gdex_wind_factor_experiment/wind_features_daily.parquet",
        columns=["date", "forecast_cycle_hour_utc", "sig_gdex_wind_nonlinear"],
    )
    equal_features = equal_features.loc[
        equal_features["forecast_cycle_hour_utc"].eq(PRIMARY_CYCLE_UTC)
    ].rename(columns={"sig_gdex_wind_nonlinear": "sig_equal_nonlinear"})
    panel = pd.read_parquet(args.panel)
    panel["date"] = pd.to_datetime(panel["date"])
    merged = panel.merge(
        primary[
            [
                "date",
                "forecast_reference_time_utc",
                "fleet_capacity_mw",
                "issue_year",
                "effective_power_cf_equal",
                "effective_power_cf_front",
                "high_wind_cutout_loss_cf_front",
                "shortfall_mw_front",
                "shortfall_mw_front_revision",
                "sig_capacity_cf",
                "sig_capacity_mw",
                "sig_capacity_mw_front",
                "sig_capacity_revision",
                "sig_complete_wind",
            ]
        ],
        on="date",
        how="left",
        validate="one_to_one",
    ).merge(
        equal_features[["date", "sig_equal_nonlinear"]],
        on="date",
        how="left",
        validate="one_to_one",
    )
    scored = build_strategy_scores(merged)
    daily, comparison, periods, standalone, ic, annual = evaluate(
        scored,
        through_month=args.through_month,
    )

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    primary.to_parquet(
        output_dir / "capacity_weighted_wind_features_daily.parquet",
        index=False,
        compression="zstd",
    )
    annual_weights.to_parquet(
        output_dir / "annual_location_weights.parquet",
        index=False,
        compression="zstd",
    )
    fleet_diagnostics.to_csv(output_dir / "annual_fleet_diagnostics.csv", index=False)
    daily.to_parquet(
        output_dir / "complete_wind_factor_backtest_daily.parquet",
        index=False,
        compression="zstd",
    )
    comparison.to_csv(output_dir / "complete_wind_factor_comparison.csv", index=False)
    periods.to_csv(output_dir / "complete_wind_factor_periods.csv", index=False)
    standalone.to_csv(output_dir / "complete_wind_factor_standalone.csv", index=False)
    ic.to_csv(output_dir / "complete_wind_factor_ic.csv", index=False)
    annual.to_csv(output_dir / "complete_wind_factor_by_year.csv", index=False)

    gross = comparison.loc[comparison["cost_bps"].eq(0)].set_index(
        ["family", "version"]
    )
    net = comparison.loc[comparison["cost_bps"].eq(2.5)].set_index(
        ["family", "version"]
    )
    manifest = {
        "experiment": "ncar_gdex_complete_physical_wind_factor",
        "status": "research_only",
        "production_panel_modified": False,
        "gcs_objects_modified": False,
        "through_month": args.through_month,
        "monthly_partition_count": len(keys),
        "quality": quality,
        "uswtdb": uswtdb_metadata,
        "fleet_diagnostics": fleet_diagnostics.to_dict(orient="records"),
        "power_curve": asdict(DEFAULT_POWER_CURVE),
        "hub_height_adjustment": {
            "source_height_m": 80.0,
            "power_law_exponent": SHEAR_EXPONENT,
        },
        "horizon_weights": HORIZON_WEIGHTS,
        "selected_signal": {
            "version": SELECTED_VERSION,
            "definition": (
                "causal z-score of annual USWTDB-capacity-weighted "
                "nonlinear effective-power capacity-factor shortfall"
            ),
            "selection_reason": (
                "best full-sample net Sharpe with improved 2024-2026 "
                "first-look holdout; capacity scaling and run revision "
                "did not improve robustness"
            ),
        },
        "rejected_level_plus_revision_signal": {
            "level_component": "capacity-scaled front-weighted nonlinear shortfall",
            "revision_component": "change from the immediately preceding 6-hour GFS run",
            "level_weight": LEVEL_WEIGHT,
            "revision_weight": REVISION_WEIGHT,
            "causal_standardization": "60 prior same-cycle observations, 30 minimum",
        },
        "execution": {
            "position_lag": "one trading day",
            "transaction_cost_sensitivity_bps": TRANSACTION_COSTS_BPS,
        },
        "headline": {
            family: {
                "baseline_gross_sharpe": gross.loc[(family, "base"), "sharpe_zero_rf"],
                "selected_gross_sharpe": gross.loc[(family, SELECTED_VERSION), "sharpe_zero_rf"],
                "baseline_net_2p5bps_sharpe": net.loc[(family, "base"), "sharpe_zero_rf"],
                "selected_net_2p5bps_sharpe": net.loc[(family, SELECTED_VERSION), "sharpe_zero_rf"],
            }
            for family in ("seasonal", "fixed")
        },
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, default=json_default) + "\n",
        encoding="utf-8",
    )

    print("\nGross comparison")
    print(
        comparison.loc[comparison["cost_bps"].eq(0), [
            "family", "version", "trading_days", "cagr",
            "sharpe_zero_rf", "maximum_drawdown", "total_turnover",
        ]].round(4).to_string(index=False)
    )
    print("\nPeriod comparison at 2.5 bps")
    print(
        periods[["period", "family", "version", "trading_days", "cagr", "sharpe_zero_rf"]]
        .round(4).to_string(index=False)
    )
    print("\nStandalone wind at 2.5 bps")
    print(standalone[["version", "cagr", "sharpe_zero_rf", "maximum_drawdown"]].round(4).to_string(index=False))
    print("\nSignal IC")
    print(ic.round(4).to_string(index=False))
    print(f"\nWrote {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
