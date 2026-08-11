#!/usr/bin/env python3
"""Compare legacy and native-frequency fundamental standardization.

This is a local, research-only comparison of the current fixed natural-gas
strategy.  It changes only six fundamental z-scores that were previously
calculated after weekly/monthly values had been forward-filled to trading
days.  Weekly factors are standardized on weekly releases and monthly factors
on monthly releases before they are aligned to trading dates.

Everything else is held fixed: the selected independent wind allocation,
10% capacity-weighted solar slot, neutral CPC-level/observed-weather slots,
one-session signal lag, five-trading-day early roll, and 2.5 bps cost.
No GCS objects are modified.
"""

from __future__ import annotations

import argparse
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

from naturalgas.evaluate_ncar_gdex_complete_solar_factor import (  # noqa: E402
    INTERMEDIATE_WEIGHT,
    PRIMARY_FACTOR,
    SOLAR_LEAD_PATH,
    SOLAR_SIGNAL_PATH,
    TRANSACTION_COST_BPS,
    add_solar_candidates,
    extended_performance,
    load_capacity_weighted_solar,
    merge_and_transform_factors,
    neutralize_non_directional_weather_slots,
)
from naturalgas.evaluate_ncar_gdex_independent_wind_weights import (  # noqa: E402
    FUNDAMENTAL_FAST_COMPONENTS,
    GROUP_READY_COLUMNS,
    LOCAL_PANEL,
    WIND_FEATURES,
    build_base_components,
    candidate_allocations,
)
from naturalgas.execution import (  # noqa: E402
    apply_early_roll_return,
)


BUCKET = "bcli-natgas-data-497807"
STORAGE_WEEKLY_KEY = f"{BUCKET}/processed/storage_weekly.parquet"
FUNDAMENTALS_MONTHLY_KEY = f"{BUCKET}/processed/fundamentals_monthly.parquet"
LNG_TRADE_KEY = f"{BUCKET}/raw/eia/trade_detail/country_monthly.parquet"

DEFAULT_OUTPUT_DIR = (
    Path(__file__).resolve().parent
    / "processed/native_frequency_fundamentals"
)
STRATEGY_START = pd.Timestamp("2017-07-01")
THROUGH_DATE = pd.Timestamp("2026-07-13")
SELECTED_WIND_ALLOCATION = "independent_shoulder_22p5pct"
SOLAR_NOMINAL_WEIGHT = INTERMEDIATE_WEIGHT

WEEKLY_Z_WINDOW = 104
WEEKLY_Z_MIN = 52
MONTHLY_Z_WINDOW = 60
MONTHLY_Z_MIN = 36

REPLACED_COMPONENTS = (
    "sig_low_storage",
    "sig_storage_change",
    "sig_low_production_growth",
    "sig_lng_export_growth",
    "sig_consumption_growth",
    "sig_net_import_supply",
)

PERIODS = {
    "development": (STRATEGY_START, pd.Timestamp("2020-12-31")),
    "validation": (pd.Timestamp("2021-01-01"), pd.Timestamp("2023-12-31")),
    "first_look_holdout": (pd.Timestamp("2024-01-01"), THROUGH_DATE),
    "full": (STRATEGY_START, THROUGH_DATE),
}


def json_default(value: Any) -> Any:
    if isinstance(value, (pd.Timestamp, np.datetime64)):
        return pd.Timestamp(value).isoformat()
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return None if np.isnan(value) else float(value)
    if pd.isna(value):
        return None
    raise TypeError(type(value).__name__)


def causal_z(
    series: pd.Series,
    *,
    window: int,
    min_periods: int,
) -> pd.Series:
    """Point-in-time z-score whose reference distribution excludes current."""

    values = pd.to_numeric(series, errors="coerce").astype(float)
    history = values.shift(1)
    mean = history.rolling(window, min_periods=min_periods).mean()
    std = history.rolling(window, min_periods=min_periods).std()
    return (values - mean) / std.replace(0.0, np.nan)


def load_parquet(filesystem: gcsfs.GCSFileSystem, key: str) -> pd.DataFrame:
    with filesystem.open(key, "rb") as handle:
        return pd.read_parquet(handle)


def weekly_native_signals(
    filesystem: gcsfs.GCSFileSystem,
) -> pd.DataFrame:
    """Build storage level/change z-scores on unique weekly observations."""

    storage = load_parquet(filesystem, STORAGE_WEEKLY_KEY).sort_values(
        "week_ending"
    )
    storage["week_ending"] = pd.to_datetime(storage["week_ending"]).astype(
        "datetime64[ns]"
    )
    storage["week_of_year"] = (
        storage["week_ending"].dt.isocalendar().week.astype(int)
    )
    storage["storage_change_bcf"] = storage["lower48"].diff()
    storage["storage_normal_bcf"] = storage.groupby("week_of_year")[
        "lower48"
    ].transform(lambda values: values.shift(1).rolling(5, min_periods=3).mean())
    storage["storage_dev"] = (
        storage["lower48"] / storage["storage_normal_bcf"] - 1.0
    )
    storage["storage_change_normal_bcf"] = storage.groupby("week_of_year")[
        "storage_change_bcf"
    ].transform(lambda values: values.shift(1).rolling(5, min_periods=3).mean())
    storage["storage_change_surprise_bcf"] = (
        storage["storage_change_bcf"]
        - storage["storage_change_normal_bcf"]
    )
    storage["native_sig_low_storage"] = -causal_z(
        storage["storage_dev"],
        window=WEEKLY_Z_WINDOW,
        min_periods=WEEKLY_Z_MIN,
    )
    storage["native_sig_storage_change"] = -causal_z(
        storage["storage_change_surprise_bcf"],
        window=WEEKLY_Z_WINDOW,
        min_periods=WEEKLY_Z_MIN,
    )
    storage["native_storage_available_date"] = (
        storage["week_ending"] + pd.Timedelta(days=6)
    ).astype("datetime64[ns]")
    return storage[[
        "week_ending",
        "native_storage_available_date",
        "storage_dev",
        "storage_change_surprise_bcf",
        "native_sig_low_storage",
        "native_sig_storage_change",
    ]]


def monthly_native_signals(
    filesystem: gcsfs.GCSFileSystem,
) -> pd.DataFrame:
    """Build four year-over-year/level z-scores on unique monthly releases."""

    monthly = load_parquet(filesystem, FUNDAMENTALS_MONTHLY_KEY).sort_values(
        "month"
    )
    monthly["month"] = pd.to_datetime(monthly["month"]).astype(
        "datetime64[ns]"
    )
    monthly["dry_prod_yoy"] = monthly["dry_prod"] / monthly["dry_prod"].shift(12) - 1.0
    monthly["total_cons_yoy_native_raw"] = (
        monthly["total_cons"] / monthly["total_cons"].shift(12) - 1.0
    )
    monthly["net_import_ratio_native_raw"] = (
        monthly["imports"] - monthly["exports"]
    ) / monthly["total_cons"]

    lng = load_parquet(filesystem, LNG_TRADE_KEY)
    lng = lng.loc[
        lng["dataset"].eq("country_exports")
        & lng["is_us_aggregate"].fillna(False)
        & lng["process-name"].eq("Liquefied Natural Gas Exports")
        & lng["metric"].eq("volume"),
        ["month", "value"],
    ].rename(columns={"value": "lng_exports"}).sort_values("month")
    lng["month"] = pd.to_datetime(lng["month"]).astype("datetime64[ns]")
    lng["lng_exports_yoy"] = (
        lng["lng_exports"] / lng["lng_exports"].shift(12) - 1.0
    )
    monthly = monthly.merge(
        lng[["month", "lng_exports_yoy"]],
        on="month",
        how="left",
        validate="one_to_one",
    )

    definitions = {
        "native_sig_low_production_growth": ("dry_prod_yoy", -1.0),
        "native_sig_lng_export_growth": ("lng_exports_yoy", 1.0),
        "native_sig_consumption_growth": (
            "total_cons_yoy_native_raw",
            1.0,
        ),
        "native_sig_net_import_supply": (
            "net_import_ratio_native_raw",
            -1.0,
        ),
    }
    for output, (raw, sign) in definitions.items():
        monthly[output] = sign * causal_z(
            monthly[raw],
            window=MONTHLY_Z_WINDOW,
            min_periods=MONTHLY_Z_MIN,
        )

    # Preserve the current conservative timing convention: reference month M
    # becomes usable on calendar day one of M+3, then merge_asof waits for the
    # next futures session when that date is a weekend or holiday.
    monthly["native_fundamentals_available_date"] = (
        monthly["month"] + pd.DateOffset(months=3)
    ).astype("datetime64[ns]")
    return monthly[[
        "month",
        "native_fundamentals_available_date",
        "dry_prod_yoy",
        "lng_exports_yoy",
        "total_cons_yoy_native_raw",
        "net_import_ratio_native_raw",
        *definitions,
    ]]


def apply_native_frequency_fundamentals(
    panel: pd.DataFrame,
    filesystem: gcsfs.GCSFileSystem,
) -> pd.DataFrame:
    """Replace only the six daily-standardized fundamental components."""

    result = panel.copy().sort_values("date").reset_index(drop=True)
    result["date"] = pd.to_datetime(result["date"]).astype("datetime64[ns]")
    weekly = weekly_native_signals(filesystem)
    monthly = monthly_native_signals(filesystem)
    result = pd.merge_asof(
        result,
        weekly.sort_values("native_storage_available_date"),
        left_on="date",
        right_on="native_storage_available_date",
        direction="backward",
    )
    result = pd.merge_asof(
        result.sort_values("date"),
        monthly.sort_values("native_fundamentals_available_date"),
        left_on="date",
        right_on="native_fundamentals_available_date",
        direction="backward",
    )

    replacements = {
        "sig_low_storage": "native_sig_low_storage",
        "sig_storage_change": "native_sig_storage_change",
        "sig_low_production_growth": "native_sig_low_production_growth",
        "sig_lng_export_growth": "native_sig_lng_export_growth",
        "sig_consumption_growth": "native_sig_consumption_growth",
        "sig_net_import_supply": "native_sig_net_import_supply",
    }
    for legacy, native in replacements.items():
        result[f"legacy_{legacy}"] = result[legacy]
        result[legacy] = result[native]

    weekly_rows = result["native_storage_available_date"].notna()
    monthly_rows = result["native_fundamentals_available_date"].notna()
    assert (
        result.loc[weekly_rows, "native_storage_available_date"]
        <= result.loc[weekly_rows, "date"]
    ).all()
    assert (
        result.loc[monthly_rows, "native_fundamentals_available_date"]
        <= result.loc[monthly_rows, "date"]
    ).all()
    return result


def build_strategy_panel(
    panel: pd.DataFrame,
    *,
    wind_path: Path,
    solar: pd.DataFrame,
) -> pd.DataFrame:
    """Apply the current fixed wind/solar/execution policy to a panel."""

    allocations = candidate_allocations()
    selected = next(
        allocation
        for allocation in allocations
        if allocation.name == SELECTED_WIND_ALLOCATION
    )
    wind = pd.read_parquet(
        wind_path,
        columns=["date", "forecast_cycle_hour_utc", "sig_capacity_cf"],
    )
    wind["date"] = pd.to_datetime(wind["date"]).astype("datetime64[ns]")
    wind = wind.loc[
        wind["forecast_cycle_hour_utc"].eq(0),
        ["date", "sig_capacity_cf"],
    ]
    scored = panel.merge(wind, on="date", how="left", validate="one_to_one")
    scored = build_base_components(scored)
    scored = neutralize_non_directional_weather_slots(scored, allocations)
    scored = apply_early_roll_return(scored)
    scored["baseline_score"] = scored[f"score_{selected.name}"]
    scored["position_baseline"] = scored[f"position_{selected.name}"]
    scored = merge_and_transform_factors(scored, solar)
    return add_solar_candidates(scored)


def comparable_sample(
    legacy: pd.DataFrame,
    native: pd.DataFrame,
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    position_column = (
        f"position_active_{PRIMARY_FACTOR}_{SOLAR_NOMINAL_WEIGHT:.3f}"
    )
    legacy_ready = (
        legacy["date"].between(start, end)
        & legacy["required_input_complete"].fillna(False)
        & legacy[list(GROUP_READY_COLUMNS)].notna().all(axis=1)
        & legacy["roll_adjusted_return"].notna()
        & legacy[position_column].notna()
    )
    native_ready = (
        native["date"].between(start, end)
        & native["required_input_complete"].fillna(False)
        & native[list(GROUP_READY_COLUMNS)].notna().all(axis=1)
        & native["roll_adjusted_return"].notna()
        & native[position_column].notna()
    )
    common_dates = set(legacy.loc[legacy_ready, "date"]).intersection(
        native.loc[native_ready, "date"]
    )
    left = legacy.loc[legacy["date"].isin(common_dates)].copy()
    right = native.loc[native["date"].isin(common_dates)].copy()
    left = left.sort_values("date").reset_index(drop=True)
    right = right.sort_values("date").reset_index(drop=True)
    assert left["date"].equals(right["date"])
    assert np.allclose(
        left["roll_adjusted_return"],
        right["roll_adjusted_return"],
        equal_nan=True,
    )
    return left, right


def metric_row(
    frame: pd.DataFrame,
    *,
    version: str,
    period: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> dict[str, Any]:
    position_column = (
        f"position_active_{PRIMARY_FACTOR}_{SOLAR_NOMINAL_WEIGHT:.3f}"
    )
    sample = frame.loc[frame["date"].between(start, end)]
    metrics = extended_performance(
        sample,
        position_column=position_column,
        cost_bps=TRANSACTION_COST_BPS,
    )
    metrics.update({"version": version, "period": period})
    return metrics


def evaluate_periods(
    legacy: pd.DataFrame,
    native: pd.DataFrame,
    *,
    periods: dict[str, tuple[pd.Timestamp, pd.Timestamp]] = PERIODS,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for period, (start, end) in periods.items():
        for version, frame in (("legacy_daily_z", legacy), ("native_frequency_z", native)):
            rows.append(
                metric_row(
                    frame,
                    version=version,
                    period=period,
                    start=start,
                    end=end,
                )
            )
    return pd.DataFrame(rows)


def evaluate_annual(
    legacy: pd.DataFrame,
    native: pd.DataFrame,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for version, frame in (("legacy_daily_z", legacy), ("native_frequency_z", native)):
        for year, sample in frame.groupby(frame["date"].dt.year):
            row = metric_row(
                sample,
                version=version,
                period=str(year),
                start=sample["date"].min(),
                end=sample["date"].max(),
            )
            row["year"] = int(year)
            rows.append(row)
    return pd.DataFrame(rows)


def factor_diagnostics(
    legacy: pd.DataFrame,
    native: pd.DataFrame,
) -> pd.DataFrame:
    """Standalone next-session diagnostics for the six changed components."""

    rows: list[dict[str, Any]] = []
    next_return = legacy["roll_adjusted_return"].shift(-1)
    for component in REPLACED_COMPONENTS:
        for version, frame in (("legacy_daily_z", legacy), ("native_frequency_z", native)):
            transformed = np.tanh(frame[component] / 2.0)
            contribution = transformed * next_return
            valid = transformed.notna() & next_return.notna()
            sample_return = contribution.loc[valid]
            volatility = sample_return.std()
            rows.append({
                "component": component,
                "version": version,
                "observations": int(valid.sum()),
                "pearson_next_return": transformed.loc[valid].corr(
                    next_return.loc[valid]
                ),
                "standalone_sharpe_zero_rf": (
                    float(sample_return.mean() / volatility * np.sqrt(252))
                    if volatility > 0.0
                    else np.nan
                ),
            })
    return pd.DataFrame(rows)


def daily_comparison(
    legacy: pd.DataFrame,
    native: pd.DataFrame,
) -> pd.DataFrame:
    position_column = (
        f"position_active_{PRIMARY_FACTOR}_{SOLAR_NOMINAL_WEIGHT:.3f}"
    )
    output = pd.DataFrame({
        "date": legacy["date"],
        "roll_adjusted_return": legacy["roll_adjusted_return"],
        "fundamental_legacy": legacy["fundamental_rebuilt"],
        "fundamental_native": native["fundamental_rebuilt"],
        "score_legacy": legacy[
            f"score_active_{PRIMARY_FACTOR}_{SOLAR_NOMINAL_WEIGHT:.3f}"
        ],
        "score_native": native[
            f"score_active_{PRIMARY_FACTOR}_{SOLAR_NOMINAL_WEIGHT:.3f}"
        ],
        "position_legacy": legacy[position_column],
        "position_native": native[position_column],
    })
    for version in ("legacy", "native"):
        position = output[f"position_{version}"]
        turnover = position.diff().abs().fillna(position.abs())
        output[f"turnover_{version}"] = turnover
        output[f"net_return_{version}"] = (
            position * output["roll_adjusted_return"]
            - turnover * TRANSACTION_COST_BPS / 10_000.0
        )
        output[f"net_index_{version}"] = (
            1.0 + output[f"net_return_{version}"]
        ).cumprod()
    for component in REPLACED_COMPONENTS:
        output[f"{component}_legacy"] = legacy[component]
        output[f"{component}_native"] = native[component]
    return output


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
    if filesystem is None:
        filesystem = gcsfs.GCSFileSystem()
    base = pd.read_parquet(panel_path)
    base["date"] = pd.to_datetime(base["date"]).astype("datetime64[ns]")
    native_base = apply_native_frequency_fundamentals(base, filesystem)
    solar = load_capacity_weighted_solar(solar_signal_path, solar_lead_path)

    legacy_panel = build_strategy_panel(base, wind_path=wind_path, solar=solar)
    native_panel = build_strategy_panel(
        native_base,
        wind_path=wind_path,
        solar=solar,
    )
    legacy, native = comparable_sample(
        legacy_panel,
        native_panel,
        start=start,
        end=through_date,
    )
    if legacy.empty:
        raise RuntimeError("No common strategy observations after readiness filters")

    periods = {
        **PERIODS,
        "full": (start, through_date),
        "first_look_holdout": (pd.Timestamp("2024-01-01"), through_date),
    }
    period_results = evaluate_periods(legacy, native, periods=periods)
    annual_results = evaluate_annual(legacy, native)
    factors = factor_diagnostics(legacy, native)
    daily = daily_comparison(legacy, native)

    output_dir.mkdir(parents=True, exist_ok=True)
    period_results.to_csv(output_dir / "period_comparison.csv", index=False)
    annual_results.to_csv(output_dir / "annual_comparison.csv", index=False)
    factors.to_csv(output_dir / "factor_comparison.csv", index=False)
    daily.to_parquet(
        output_dir / "daily_comparison.parquet",
        index=False,
        compression="zstd",
    )

    full = period_results.loc[period_results["period"].eq("full")].set_index(
        "version"
    )
    summary = {
        "experiment": "native_frequency_fundamental_standardization",
        "status": "research_only",
        "production_panel_modified": False,
        "gcs_objects_modified": False,
        "sample_start": legacy["date"].min(),
        "sample_end": legacy["date"].max(),
        "trading_days": len(legacy),
        "changed_components": REPLACED_COMPONENTS,
        "unchanged_components": tuple(
            component
            for component in (
                *FUNDAMENTAL_FAST_COMPONENTS,
                "sig_production_mom",
                "sig_lng_export_mom",
                "sig_consumption_mom",
                "sig_net_import_change",
            )
            if component not in REPLACED_COMPONENTS
        ),
        "standardization": {
            "weekly_window": WEEKLY_Z_WINDOW,
            "weekly_min_periods": WEEKLY_Z_MIN,
            "monthly_window": MONTHLY_Z_WINDOW,
            "monthly_min_periods": MONTHLY_Z_MIN,
            "causal_reference": "current release excluded by shift(1)",
            "alignment": "z-score first, then merge_asof/forward-fill",
        },
        "fixed_strategy_policy": {
            "wind_allocation": SELECTED_WIND_ALLOCATION,
            "solar_factor": PRIMARY_FACTOR,
            "solar_nominal_weight": SOLAR_NOMINAL_WEIGHT,
            "cpc_level_direct_slot": 0.0,
            "observed_weather_direct_slot": 0.0,
            "signal_lag_sessions": 1,
            "front_month_roll": "five trading days before official LTD",
            "transaction_cost_bps": TRANSACTION_COST_BPS,
        },
        "full_metrics": {
            version: {
                "sharpe_zero_rf": float(full.loc[version, "sharpe_zero_rf"]),
                "cagr": float(full.loc[version, "cagr"]),
                "maximum_drawdown": float(
                    full.loc[version, "maximum_drawdown"]
                ),
                "win_rate": float(full.loc[version, "win_rate"]),
                "mean_absolute_position": float(
                    full.loc[version, "mean_absolute_position"]
                ),
                "total_turnover": float(full.loc[version, "total_turnover"]),
            }
            for version in ("legacy_daily_z", "native_frequency_z")
        },
        "sharpe_change": float(
            full.loc["native_frequency_z", "sharpe_zero_rf"]
            - full.loc["legacy_daily_z", "sharpe_zero_rf"]
        ),
        "caution": (
            "This holds strategy weights fixed and is not a parameter search, "
            "but it still uses revised historical EIA data rather than archived "
            "first-release vintages."
        ),
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, default=json_default, indent=2, sort_keys=True)
        + "\n",
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
