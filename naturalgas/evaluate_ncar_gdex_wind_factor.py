#!/usr/bin/env python3
"""Evaluate the GDEX nonlinear wind factor on the complete common sample.

This is an isolated research experiment.  It reads the existing GCS wind
partitions and the local multisignal panel, writes local diagnostics, and does
not modify the production panel or the running GDEX downloader.

The wind factor applies the generic turbine power curve at every location and
valid hour before aggregation.  Low wind and high-wind derating/cut-out both
increase the gas-supporting wind-power shortfall.  Only GFS initializations
with all 560 expected point/hour samples are eligible.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import gcsfs
import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from naturalgas.ncar_gdex_nonlinear_wind import (  # noqa: E402
    DEFAULT_POWER_CURVE,
    add_nonlinear_signals,
    aggregate_nonlinear_features,
    causal_zscore,
)
from naturalgas.weather_feature_policy import (  # noqa: E402
    PRIMARY_WEATHER_COMPONENTS,
    fixed_weight_mean,
)


from naturalgas.storage_config import PERSONAL_GCS_ROOT


PROJECT = None
RAW_GLOB = (
    f"{PERSONAL_GCS_ROOT}/raw/weather/ncar_gdex/d084001/"
    "wind_points/model=ncep_gfs_0p25/cycle=all/"
    "year=*/month=*/data.parquet"
)
LOCAL_PANEL = (
    Path(__file__).resolve().parent
    / "processed"
    / "ng_multisignal_score"
    / "ng_multisignal_panel.parquet"
)
DEFAULT_OUTPUT_DIR = (
    Path(__file__).resolve().parent
    / "processed"
    / "ncar_gdex_wind_factor_experiment"
)
RAW_START_MONTH = pd.Period("2016-01", freq="M")
DEFAULT_THROUGH_MONTH = pd.Period("2023-07", freq="M")
BACKTEST_START = pd.Timestamp("2016-07-06")
EXPECTED_SAMPLES_PER_INITIALIZATION = 560
PRIMARY_CYCLE_UTC = 0
PEAK_DEMAND_MONTHS = (11, 12, 1, 2, 6, 7, 8)
COLD_SEASON_MONTHS = (11, 12, 1, 2, 3)
PEAK_WEATHER_WEIGHT = 0.60
SHOULDER_WEATHER_WEIGHT = 0.30
PRODUCTION_LOCAL_LEVEL_THRESHOLD = 1.0
FUNDAMENTAL_FAST_COMPONENTS = (
    "sig_low_storage",
    "sig_storage_change",
    "sig_storage_4w_change",
    "sig_low_production_growth",
    "sig_lng_export_growth",
    "sig_consumption_growth",
    "sig_net_import_supply",
)
FUNDAMENTAL_MONTHLY_COMPONENTS = (
    "sig_production_mom",
    "sig_lng_export_mom",
    "sig_consumption_mom",
    "sig_net_import_change",
)
GROUP_READY_COLUMNS = (
    "weather_score",
    "fundamental_score",
    "market_score",
    "macro_risk_score",
)


class WindFactorExperimentError(RuntimeError):
    """Raised when the common-sample experiment is not reproducible."""


def parse_month(value: str) -> pd.Period:
    try:
        result = pd.Period(value, freq="M")
    except ValueError as exc:
        raise argparse.ArgumentTypeError("month must be YYYY-MM") from exc
    return result


def month_from_key(key: str) -> pd.Period:
    try:
        year = key.split("year=", 1)[1][:4]
        month = key.split("month=", 1)[1][:2]
    except IndexError as exc:
        raise WindFactorExperimentError(
            f"cannot parse year/month from GCS key: {key}"
        ) from exc
    return pd.Period(f"{year}-{month}", freq="M")


def select_continuous_keys(
    keys: list[str],
    *,
    through_month: pd.Period,
) -> list[str]:
    selected = {
        month_from_key(key): key
        for key in keys
        if RAW_START_MONTH <= month_from_key(key) <= through_month
    }
    expected = pd.period_range(
        RAW_START_MONTH,
        through_month,
        freq="M",
    )
    missing = [str(month) for month in expected if month not in selected]
    if missing:
        raise WindFactorExperimentError(
            "wind partitions are not continuous; missing "
            + ", ".join(missing)
        )
    return [selected[month] for month in expected]


def read_wind_points(
    filesystem: gcsfs.GCSFileSystem,
    keys: list[str],
) -> pd.DataFrame:
    columns = [
        "forecast_reference_time_utc",
        "forecast_cycle_hour_utc",
        "location_id",
        "valid_time_utc",
        "wind_speed_80m_mps",
    ]
    frames: list[pd.DataFrame] = []
    for number, key in enumerate(keys, start=1):
        with filesystem.open(key, "rb") as handle:
            frames.append(pd.read_parquet(handle, columns=columns))
        if number % 12 == 0 or number == len(keys):
            print(
                f"read {number}/{len(keys)} monthly wind partitions | "
                f"{sum(len(frame) for frame in frames):,} rows",
                flush=True,
            )
    points = pd.concat(frames, ignore_index=True)
    if points.empty:
        raise WindFactorExperimentError("wind point data are empty")
    return points


def build_wind_features(points: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    all_features = aggregate_nonlinear_features(points)
    full_sample = all_features["gfs_nonlinear_sample_count"].eq(
        EXPECTED_SAMPLES_PER_INITIALIZATION
    )
    features = add_nonlinear_signals(
        all_features.loc[full_sample].copy()
    )
    features = features.sort_values(
        ["forecast_cycle_hour_utc", "forecast_reference_time_utc"]
    )
    features["sig_gdex_wind_linear"] = features.groupby(
        "forecast_cycle_hour_utc",
        group_keys=False,
    )["gfs_wind80_mean_5d_mps"].transform(
        lambda values: -causal_zscore(values)
    )
    features = features.sort_values(
        "forecast_reference_time_utc"
    ).reset_index(drop=True)
    quality = {
        "raw_point_rows": int(len(points)),
        "all_initializations": int(len(all_features)),
        "complete_initializations": int(full_sample.sum()),
        "excluded_incomplete_initializations": int((~full_sample).sum()),
        "expected_samples_per_initialization": (
            EXPECTED_SAMPLES_PER_INITIALIZATION
        ),
        "minimum_incomplete_sample_count": (
            int(
                all_features.loc[
                    ~full_sample, "gfs_nonlinear_sample_count"
                ].min()
            )
            if (~full_sample).any()
            else None
        ),
    }
    return features, quality


def add_strategy_scores(panel: pd.DataFrame) -> pd.DataFrame:
    result = panel.copy()
    weather_components = list(PRIMARY_WEATHER_COMPONENTS)
    result["weather_base"] = fixed_weight_mean(
        np.tanh(result[weather_components] / 2.0),
        weather_components,
    )
    result["fundamental_rebuilt"] = pd.concat(
        [
            np.tanh(result[list(FUNDAMENTAL_FAST_COMPONENTS)] / 2.0),
            result[list(FUNDAMENTAL_MONTHLY_COMPONENTS)],
        ],
        axis=1,
    ).mean(axis=1, skipna=True)

    # Adding wind as a fourth equal-weight weather component mechanically
    # reduces each legacy component's weight from 1/3 to 1/4.  This neutral
    # slot is the required dilution control: it makes that same weight change
    # while contributing no wind information.
    result["weather_neutral"] = result["weather_base"] * (
        len(weather_components) / (len(weather_components) + 1)
    )

    wind_specs = {
        "linear": "sig_gdex_wind_linear",
        "nonlinear": "sig_gdex_wind_nonlinear",
    }
    for version, wind_column in wind_specs.items():
        components = weather_components + [wind_column]
        result[f"weather_{version}"] = fixed_weight_mean(
            np.tanh(result[components] / 2.0),
            components,
        )

    result["weather_weight_experiment"] = np.where(
        result["date"].dt.month.isin(PEAK_DEMAND_MONTHS),
        PEAK_WEATHER_WEIGHT,
        SHOULDER_WEATHER_WEIGHT,
    )
    result["fundamental_weight_experiment"] = (
        1.0 - result["weather_weight_experiment"]
    )
    production_control_active = (
        result["date"].dt.month.isin(COLD_SEASON_MONTHS)
        & result["prod_freeze_local_level_score"].ge(
            PRODUCTION_LOCAL_LEVEL_THRESHOLD
        )
        & result["prod_freeze_local_revision_score"].ge(0.0)
    )
    result["production_control_active_experiment"] = (
        production_control_active
    )

    for version in ("base", "neutral", "linear", "nonlinear"):
        seasonal_pre = (
            result["weather_weight_experiment"]
            * result[f"weather_{version}"]
            + result["fundamental_weight_experiment"]
            * result["fundamental_rebuilt"]
        )
        result[f"seasonal_{version}_before_control"] = seasonal_pre
        result[f"seasonal_{version}"] = np.where(
            production_control_active,
            np.maximum(seasonal_pre, 0.0),
            seasonal_pre,
        )
        result[f"fixed_{version}"] = (
            0.35 * result[f"weather_{version}"]
            + 0.30 * result["fundamental_rebuilt"]
        ) / 0.65
    return result


def performance_metrics(
    frame: pd.DataFrame,
    *,
    position_column: str,
) -> dict[str, Any]:
    position = frame[position_column]
    returns = position * frame["roll_adjusted_return"]
    log_returns = np.log1p(returns)
    wealth = (1.0 + returns).cumprod()
    drawdown = wealth / wealth.cummax() - 1.0
    years = (frame["date"].max() - frame["date"].min()).days / 365.2425
    volatility = log_returns.std()
    return {
        "start": frame["date"].min(),
        "end": frame["date"].max(),
        "trading_days": int(len(frame)),
        "total_return": float(wealth.iloc[-1] - 1.0),
        "cagr": float(np.exp(log_returns.sum() / years) - 1.0),
        "annualized_volatility": float(volatility * np.sqrt(252)),
        "sharpe_zero_rf": float(
            log_returns.mean() / volatility * np.sqrt(252)
        ),
        "maximum_drawdown": float(drawdown.min()),
        "mean_absolute_position": float(position.abs().mean()),
        "total_turnover": float(
            position.diff().abs().fillna(position.abs()).sum()
        ),
    }


def evaluate_panel(
    panel: pd.DataFrame,
    *,
    backtest_end: pd.Timestamp,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    result = panel.copy()
    versions = ("base", "neutral", "linear", "nonlinear")
    families = ("seasonal", "fixed")
    for family in families:
        for version in versions:
            result[f"position_{family}_{version}"] = result[
                f"{family}_{version}"
            ].shift(1).clip(-1.0, 1.0)

    group_ready = result[list(GROUP_READY_COLUMNS)].notna().all(axis=1)
    common = (
        result["date"].between(BACKTEST_START, backtest_end)
        & result["required_input_complete"].fillna(False)
        & group_ready
        & result["roll_adjusted_return"].notna()
    )
    for family in families:
        for version in versions:
            common &= result[f"position_{family}_{version}"].notna()
    common_panel = result.loc[common].copy().reset_index(drop=True)
    if common_panel.empty:
        raise WindFactorExperimentError("common backtest sample is empty")

    comparison_rows: list[dict[str, Any]] = []
    for family in families:
        for version in versions:
            row = performance_metrics(
                common_panel,
                position_column=f"position_{family}_{version}",
            )
            row.update({"family": family, "version": version})
            comparison_rows.append(row)
    comparison = pd.DataFrame(comparison_rows)

    annual_rows: list[dict[str, Any]] = []
    for year, year_frame in common_panel.groupby(
        common_panel["date"].dt.year
    ):
        for family in families:
            for version in versions:
                position_column = f"position_{family}_{version}"
                returns = (
                    year_frame[position_column]
                    * year_frame["roll_adjusted_return"]
                )
                log_returns = np.log1p(returns)
                log_volatility = log_returns.std()
                annual_rows.append(
                    {
                        "year": int(year),
                        "family": family,
                        "version": version,
                        "trading_days": int(len(year_frame)),
                        "total_return": float((1.0 + returns).prod() - 1.0),
                        "sharpe_zero_rf": float(
                            log_returns.mean()
                            / log_volatility
                            * np.sqrt(252)
                        ),
                    }
                )
    annual = pd.DataFrame(annual_rows)

    result["target_next_trading_day_return"] = result[
        "roll_adjusted_return"
    ].shift(-1)
    ic_rows: list[dict[str, Any]] = []
    for version, signal_column in {
        "linear": "sig_gdex_wind_linear",
        "nonlinear": "sig_gdex_wind_nonlinear",
    }.items():
        ready = (
            result["date"].between(BACKTEST_START, backtest_end)
            & result[signal_column].notna()
            & result["target_next_trading_day_return"].notna()
        )
        ic_rows.append(
            {
                "version": version,
                "observations": int(ready.sum()),
                "pearson_ic": float(
                    result.loc[ready, signal_column].corr(
                        result.loc[ready, "target_next_trading_day_return"]
                    )
                ),
                "spearman_ic": float(
                    result.loc[ready, signal_column].corr(
                        result.loc[ready, "target_next_trading_day_return"],
                        method="spearman",
                    )
                ),
            }
        )
    return common_panel, comparison, annual, pd.DataFrame(ic_rows)


def json_value(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, pd.Period):
        return str(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=PROJECT)
    parser.add_argument(
        "--through-month",
        type=parse_month,
        default=DEFAULT_THROUGH_MONTH,
        help="last continuous wind month, formatted YYYY-MM",
    )
    parser.add_argument(
        "--panel",
        type=Path,
        default=LOCAL_PANEL,
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.through_month < RAW_START_MONTH:
        raise WindFactorExperimentError(
            f"through month precedes {RAW_START_MONTH}"
        )
    if not args.panel.exists():
        raise WindFactorExperimentError(f"panel not found: {args.panel}")

    filesystem = gcsfs.GCSFileSystem(project=args.project)
    keys = select_continuous_keys(
        sorted(filesystem.glob(RAW_GLOB)),
        through_month=args.through_month,
    )
    points = read_wind_points(filesystem, keys)
    wind_features, quality = build_wind_features(points)
    primary = wind_features.loc[
        wind_features["forecast_cycle_hour_utc"].eq(PRIMARY_CYCLE_UTC)
    ].copy()
    primary = primary.rename(columns={"nominal_issue_date": "date"})
    primary["date"] = pd.to_datetime(primary["date"])

    panel = pd.read_parquet(args.panel)
    panel["date"] = pd.to_datetime(panel["date"])
    required_columns = {
        "date",
        "roll_adjusted_return",
        "required_input_complete",
        "prod_freeze_local_level_score",
        "prod_freeze_local_revision_score",
        *PRIMARY_WEATHER_COMPONENTS,
        *FUNDAMENTAL_FAST_COMPONENTS,
        *FUNDAMENTAL_MONTHLY_COMPONENTS,
        *GROUP_READY_COLUMNS,
    }
    missing_columns = sorted(required_columns.difference(panel.columns))
    if missing_columns:
        raise WindFactorExperimentError(
            "panel is missing columns: " + ", ".join(missing_columns)
        )
    wind_columns = [
        "date",
        "forecast_reference_time_utc",
        "forecast_cycle_hour_utc",
        "gfs_nonlinear_sample_count",
        "gfs_nonlinear_location_count",
        "gfs_nonlinear_valid_time_count",
        "gfs_wind80_mean_5d_mps",
        "gfs_effective_power_cf_5d",
        "gfs_low_wind_shortfall_cf_5d",
        "gfs_high_wind_cutout_loss_cf_5d",
        "gfs_total_wind_shortfall_cf_5d",
        "sig_gdex_wind_linear",
        "sig_gdex_wind_nonlinear",
        "sig_gdex_wind_low",
        "sig_gdex_wind_cutout",
    ]
    merged = panel.merge(
        primary[wind_columns],
        on="date",
        how="left",
        validate="one_to_one",
    ).sort_values("date").reset_index(drop=True)
    scored = add_strategy_scores(merged)
    backtest_end = args.through_month.end_time.normalize()
    daily, comparison, annual, ic = evaluate_panel(
        scored,
        backtest_end=backtest_end,
    )

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    primary.to_parquet(output_dir / "wind_features_daily.parquet", index=False)
    daily.to_parquet(output_dir / "wind_factor_backtest_daily.parquet", index=False)
    comparison.to_csv(output_dir / "wind_factor_comparison.csv", index=False)
    annual.to_csv(output_dir / "wind_factor_by_year.csv", index=False)
    ic.to_csv(output_dir / "wind_factor_ic.csv", index=False)

    nonlinear = comparison.loc[
        comparison["version"].eq("nonlinear")
    ].set_index("family")
    baseline = comparison.loc[
        comparison["version"].eq("base")
    ].set_index("family")
    manifest = {
        "experiment": "ncar_gdex_nonlinear_wind_factor_common_sample",
        "status": "research_only",
        "production_panel_modified": False,
        "gcs_objects_modified": False,
        "raw_start_month": RAW_START_MONTH,
        "through_month": args.through_month,
        "monthly_partition_count": len(keys),
        "primary_cycle_utc": PRIMARY_CYCLE_UTC,
        "backtest_start": daily["date"].min(),
        "backtest_end": daily["date"].max(),
        "common_trading_days": len(daily),
        "quality": quality,
        "power_curve": asdict(DEFAULT_POWER_CURVE),
        "causality": {
            "wind_standardization": (
                "60 prior initializations within cycle; 30 minimum"
            ),
            "position_lag": "one trading day",
            "incomplete_initializations_excluded_before_standardization": True,
        },
        "integration": {
            "weather_components_before": list(PRIMARY_WEATHER_COMPONENTS),
            "wind_component": "sig_gdex_wind_nonlinear",
            "weather_component_weighting": "equal fixed weights",
            "seasonal_weather_weights": {
                "peak_demand_months": PEAK_WEATHER_WEIGHT,
                "shoulder_months": SHOULDER_WEATHER_WEIGHT,
            },
            "transaction_cost_bps_per_unit_turnover": 0.0,
        },
        "headline_delta": {
            family: {
                "baseline_sharpe": baseline.loc[family, "sharpe_zero_rf"],
                "nonlinear_sharpe": nonlinear.loc[
                    family, "sharpe_zero_rf"
                ],
                "sharpe_change": (
                    nonlinear.loc[family, "sharpe_zero_rf"]
                    - baseline.loc[family, "sharpe_zero_rf"]
                ),
                "baseline_cagr": baseline.loc[family, "cagr"],
                "nonlinear_cagr": nonlinear.loc[family, "cagr"],
            }
            for family in ("seasonal", "fixed")
        },
        "outputs": [
            "wind_features_daily.parquet",
            "wind_factor_backtest_daily.parquet",
            "wind_factor_comparison.csv",
            "wind_factor_by_year.csv",
            "wind_factor_ic.csv",
        ],
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, default=json_value) + "\n",
        encoding="utf-8",
    )

    print("\nCommon-sample comparison")
    print(
        comparison[
            [
                "family",
                "version",
                "trading_days",
                "total_return",
                "cagr",
                "sharpe_zero_rf",
                "maximum_drawdown",
                "total_turnover",
            ]
        ].round(4).to_string(index=False)
    )
    print("\nWind IC")
    print(ic.round(4).to_string(index=False))
    print(f"\nWrote research outputs to {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
