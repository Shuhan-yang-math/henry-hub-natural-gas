#!/usr/bin/env python3
"""First-look evaluation of a GDEX GFS solar-availability factor.

This local research script reads the solar partitions that currently exist in
GCS.  It does not write to GCS or modify the production score panel.  The main
candidate removes deterministic solar geometry from the GFS downward
short-wave radiation forecast, then interprets unusually low radiation as
bullish pressure on gas-fired generation.

The experiment uses only information available at each forecast issue time,
standardizes each GFS cycle against its preceding 60 issues, and applies the
result one trading session later.  Solar is tested as a separately transformed
component in the current capacity-weighted wind allocation.  A neutral-slot
control distinguishes signal value from the effect of reducing fundamentals.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Iterable

import gcsfs
import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from naturalgas.evaluate_ncar_gdex_independent_wind_weights import (
    GROUP_READY_COLUMNS,
    LOCAL_PANEL,
    WIND_FEATURES,
    build_research_panel,
    candidate_allocations,
    performance,
)
from naturalgas.ncar_gdex_nonlinear_wind import causal_zscore
from naturalgas.ncar_gdex_solar_backfill_to_gcs import PROCESSED_PREFIX
from naturalgas.ncar_gdex_wind_backfill_to_gcs import BUCKET, LOCATIONS


DEFAULT_OUTPUT_DIR = (
    Path(__file__).resolve().parent
    / "processed/ncar_gdex_solar_factor_first_look"
)
PRIMARY_CYCLE_UTC = 0
EARLY_ROLL_TRADING_DAYS = 5
WARM_SOLAR_MONTHS = (4, 5, 6, 7, 8, 9)
COLD_SEASON_MONTHS = (11, 12, 1, 2, 3)
PRODUCTION_LOCAL_LEVEL_THRESHOLD = 1.0
BASELINE_ALLOCATION = "independent_shoulder_25pct"
SIGNALS = ("radiation", "pv", "cloud", "revision")
WEIGHT_GRID = tuple(float(value) for value in np.arange(0.0, 0.1501, 0.025))
CONSERVATIVE_WEIGHT = 0.05
TRANSACTION_COST_BPS = 2.5
PERIODS = {
    "development": (pd.Timestamp("2016-07-06"), pd.Timestamp("2018-12-31")),
    "validation": (pd.Timestamp("2019-01-01"), pd.Timestamp("2099-12-31")),
    "full": (pd.Timestamp("2016-07-06"), pd.Timestamp("2099-12-31")),
}

SOLAR_COLUMNS = (
    "forecast_reference_time_utc",
    "nominal_issue_date",
    "target_date",
    "lead_days",
    "gfs_dswrf_wm2",
    "gfs_shortwave_energy_kwh_m2_day",
    "gfs_total_cloud_cover_pct",
    "gfs_temperature_2m_c",
    "location_count",
    "min_interval_count",
)


def solar_partition_pattern() -> str:
    return (
        f"{BUCKET}/{PROCESSED_PREFIX}/solar_location_leads/"
        "year=*/month=*/data.parquet"
    )


def load_current_solar_partitions(
    filesystem: gcsfs.GCSFileSystem,
) -> tuple[pd.DataFrame, tuple[str, ...]]:
    """Read a point-in-time snapshot of all uploaded monthly lead files."""

    keys = tuple(sorted(filesystem.glob(solar_partition_pattern())))
    if not keys:
        raise FileNotFoundError("no uploaded GDEX solar_location_leads partitions")
    frames: list[pd.DataFrame] = []
    for key in keys:
        with filesystem.open(key, "rb") as source:
            frames.append(pd.read_parquet(source, columns=list(SOLAR_COLUMNS)))
    result = pd.concat(frames, ignore_index=True)
    result["forecast_reference_time_utc"] = pd.to_datetime(
        result["forecast_reference_time_utc"], utc=True
    )
    result["target_date"] = pd.to_datetime(result["target_date"])
    return result, keys


def extraterrestrial_horizontal_energy_kwh_m2_day(
    target_dates: pd.Series,
) -> np.ndarray:
    """FAO-56 daily top-of-atmosphere horizontal energy, averaged by location."""

    day_of_year = target_dates.dt.dayofyear.to_numpy(dtype=float)
    inverse_distance = 1.0 + 0.033 * np.cos(2.0 * np.pi * day_of_year / 365.0)
    declination = 0.409 * np.sin(
        2.0 * np.pi * day_of_year / 365.0 - 1.39
    )
    location_values: list[np.ndarray] = []
    for location in LOCATIONS:
        latitude = np.radians(float(location.latitude))
        sunset_angle = np.arccos(
            np.clip(-np.tan(latitude) * np.tan(declination), -1.0, 1.0)
        )
        radiation_mj_m2_day = (
            (24.0 * 60.0 / np.pi)
            * 0.0820
            * inverse_distance
            * (
                sunset_angle * np.sin(latitude) * np.sin(declination)
                + np.cos(latitude)
                * np.cos(declination)
                * np.sin(sunset_angle)
            )
        )
        location_values.append(radiation_mj_m2_day / 3.6)
    return np.mean(location_values, axis=0)


def build_solar_signals(location_leads: pd.DataFrame) -> pd.DataFrame:
    """Create causal issue-time radiation, PV, cloud, and revision candidates."""

    leads = location_leads.copy()
    # Capacity-weighted callers may provide a matching weighted solar-geometry
    # denominator.  The original equal-location experiment retains the mean
    # geometry of the 28 configured points.
    if "extraterrestrial_kwh_m2_day" not in leads:
        leads["extraterrestrial_kwh_m2_day"] = (
            extraterrestrial_horizontal_energy_kwh_m2_day(
                leads["target_date"]
            )
        )
    leads["clearness_index"] = (
        leads["gfs_shortwave_energy_kwh_m2_day"]
        / leads["extraterrestrial_kwh_m2_day"]
    ).clip(0.0, 1.2)

    # A deliberately simple PV conversion sensitivity.  It is a comparison
    # candidate, not a full plane-of-array or plant-level PV model.
    leads["cell_temperature_proxy_c"] = (
        leads["gfs_temperature_2m_c"] + 0.025 * leads["gfs_dswrf_wm2"]
    )
    leads["temperature_efficiency"] = (
        1.0 - 0.004 * (leads["cell_temperature_proxy_c"] - 25.0)
    ).clip(0.75, 1.10)
    leads["pv_availability_index"] = (
        leads["clearness_index"] * leads["temperature_efficiency"]
    )
    leads["lead_complete"] = (
        leads["location_count"].eq(len(LOCATIONS))
        & leads["min_interval_count"].eq(4)
    )

    revisions = leads[
        [
            "forecast_reference_time_utc",
            "target_date",
            "pv_availability_index",
        ]
    ].sort_values(["target_date", "forecast_reference_time_utc"])
    revisions["previous_reference_time"] = revisions.groupby(
        "target_date", observed=True
    )["forecast_reference_time_utc"].shift(1)
    revisions["previous_pv_availability"] = revisions.groupby(
        "target_date", observed=True
    )["pv_availability_index"].shift(1)
    consecutive = revisions["forecast_reference_time_utc"].sub(
        revisions["previous_reference_time"]
    ).eq(pd.Timedelta(hours=6))
    revisions["pv_revision"] = revisions["pv_availability_index"].sub(
        revisions["previous_pv_availability"]
    ).where(consecutive)

    leads = leads.merge(
        revisions[
            ["forecast_reference_time_utc", "target_date", "pv_revision"]
        ],
        on=["forecast_reference_time_utc", "target_date"],
        how="left",
        validate="one_to_one",
    )
    issues = (
        leads.groupby("forecast_reference_time_utc", observed=True, as_index=False)
        .agg(
            nominal_issue_date=("nominal_issue_date", "first"),
            clearness_index_5d=("clearness_index", "mean"),
            pv_availability_index_5d=("pv_availability_index", "mean"),
            cloud_cover_5d_pct=("gfs_total_cloud_cover_pct", "mean"),
            pv_revision_5d=("pv_revision", "mean"),
            lead_count=("lead_days", "nunique"),
            min_locations=("location_count", "min"),
            min_intervals=("min_interval_count", "min"),
            all_leads_complete=("lead_complete", "all"),
        )
        .sort_values("forecast_reference_time_utc")
        .reset_index(drop=True)
    )
    issues["forecast_cycle_hour_utc"] = issues[
        "forecast_reference_time_utc"
    ].dt.hour
    issues["input_complete"] = (
        issues["lead_count"].eq(5)
        & issues["min_locations"].eq(len(LOCATIONS))
        & issues["min_intervals"].eq(4)
        & issues["all_leads_complete"]
    )
    raw_candidates = {
        "radiation": -issues["clearness_index_5d"],
        "pv": -issues["pv_availability_index_5d"],
        "cloud": issues["cloud_cover_5d_pct"],
        "revision": -issues["pv_revision_5d"],
    }
    for name, candidate in raw_candidates.items():
        issues[f"sig_solar_{name}"] = candidate.groupby(
            issues["forecast_cycle_hour_utc"]
        ).transform(lambda values: causal_zscore(values, window=60, min_periods=30))
        issues.loc[~issues["input_complete"], f"sig_solar_{name}"] = np.nan
    issues["date"] = pd.to_datetime(issues["nominal_issue_date"])
    return issues


def build_ng_roll_calendar(
    trading_dates: Iterable[pd.Timestamp],
    *,
    roll_advance_days: int = 0,
) -> pd.DataFrame:
    """Reproduce the official NG LTD and an earlier trading-session switch."""

    dates = pd.DatetimeIndex(pd.to_datetime(list(trading_dates))).dropna()
    dates = dates.unique().sort_values()
    rows: list[dict[str, pd.Timestamp]] = []
    first_delivery = dates.min().to_period("M") + 1
    last_delivery = dates.max().to_period("M")
    for delivery in pd.period_range(first_delivery, last_delivery, freq="M"):
        month_start = delivery.to_timestamp()
        preceding = dates[dates < month_start]
        required = 3 + roll_advance_days
        if len(preceding) < required:
            continue
        official_ltd = preceding[-3]
        roll_trade_date = preceding[-required]
        following = dates[dates > roll_trade_date]
        if not len(following):
            continue
        rows.append(
            {
                "delivery_month": month_start,
                "official_ltd": official_ltd,
                "roll_switch_date": following[0],
            }
        )
    return pd.DataFrame(rows)


def apply_early_roll_return(panel: pd.DataFrame) -> pd.DataFrame:
    """Replace the evaluation return with the notebook's five-day-early roll."""

    result = panel.copy()
    official = build_ng_roll_calendar(result["date"]).rename(
        columns={"roll_switch_date": "official_switch_date"}
    )
    early = build_ng_roll_calendar(
        result["date"], roll_advance_days=EARLY_ROLL_TRADING_DAYS
    ).rename(columns={"roll_switch_date": "early_switch_date"})
    schedule = official.merge(
        early[["delivery_month", "early_switch_date"]],
        on="delivery_month",
        validate="one_to_one",
    )
    early_c2_window = pd.Series(False, index=result.index)
    for roll in schedule.itertuples(index=False):
        early_c2_window |= result["date"].ge(roll.early_switch_date) & result[
            "date"
        ].lt(roll.official_switch_date)

    previous_c1 = result["c1"].shift(1)
    previous_c2 = result["c2"].shift(1)
    official_return = np.where(
        result["is_roll_switch"],
        result["c1"] / previous_c2 - 1.0,
        result["c1"] / previous_c1 - 1.0,
    )
    result["official_ltd_roll_return"] = result["roll_adjusted_return"]
    result["early_5d_roll_return"] = np.where(
        early_c2_window,
        result["c2"] / previous_c2 - 1.0,
        official_return,
    )
    result["roll_adjusted_return"] = result["early_5d_roll_return"]
    return result


def add_candidate_positions(
    panel: pd.DataFrame,
    *,
    weights: Iterable[float] = WEIGHT_GRID,
) -> pd.DataFrame:
    """Add active, neutral-slot, scaled, and standalone solar positions."""

    result = panel.copy()
    baseline_score = result[f"score_{BASELINE_ALLOCATION}"]
    warm = result["date"].dt.month.isin(WARM_SOLAR_MONTHS)
    production_control = (
        result["date"].dt.month.isin(COLD_SEASON_MONTHS)
        & result["prod_freeze_local_level_score"].ge(
            PRODUCTION_LOCAL_LEVEL_THRESHOLD
        )
        & result["prod_freeze_local_revision_score"].ge(0.0)
    )
    for signal_name in SIGNALS:
        transformed = np.tanh(result[f"sig_solar_{signal_name}"] / 2.0)
        result[f"position_standalone_{signal_name}"] = transformed.shift(1).clip(
            -1.0, 1.0
        )
        for weight in weights:
            seasonal_weight = np.where(warm, weight, weight / 2.0)
            candidates = {
                "active": baseline_score
                + seasonal_weight
                * (transformed.fillna(0.0) - result["fundamental_rebuilt"]),
                "neutral": baseline_score
                - seasonal_weight * result["fundamental_rebuilt"],
                "scaled": (1.0 - seasonal_weight) * baseline_score
                + seasonal_weight * transformed.fillna(0.0),
            }
            for variant, raw_score in candidates.items():
                controlled = pd.Series(raw_score, index=result.index).where(
                    ~production_control,
                    pd.Series(raw_score, index=result.index).clip(lower=0.0),
                )
                result[
                    f"position_{variant}_{signal_name}_{weight:.3f}"
                ] = controlled.shift(1).clip(-1.0, 1.0)
    return result


def base_ready(panel: pd.DataFrame) -> pd.Series:
    return (
        panel["required_input_complete"].fillna(False)
        & panel[list(GROUP_READY_COLUMNS)].notna().all(axis=1)
        & panel["roll_adjusted_return"].notna()
    )


def ic_summary(panel: pd.DataFrame) -> pd.DataFrame:
    next_return = panel["roll_adjusted_return"].shift(-1)
    rows: list[dict[str, Any]] = []
    ready = base_ready(panel)
    for period, (start, configured_end) in PERIODS.items():
        end = min(configured_end, panel["date"].max())
        for signal_name in SIGNALS:
            signal = panel[f"sig_solar_{signal_name}"]
            sample = pd.DataFrame(
                {"signal": signal, "next_return": next_return}
            ).loc[ready & panel["date"].between(start, end)].dropna()
            rows.append(
                {
                    "period": period,
                    "signal": signal_name,
                    "start": sample.index.to_series().map(panel["date"]).min(),
                    "end": sample.index.to_series().map(panel["date"]).max(),
                    "observations": len(sample),
                    "pearson_ic": sample["signal"].corr(sample["next_return"]),
                    "spearman_ic": sample["signal"].corr(
                        sample["next_return"], method="spearman"
                    ),
                }
            )
    return pd.DataFrame(rows)


def evaluate_candidates(panel: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows: list[dict[str, Any]] = []
    standalone_rows: list[dict[str, Any]] = []
    ready = base_ready(panel)
    for period, (start, configured_end) in PERIODS.items():
        end = min(configured_end, panel["date"].max())
        period_mask = ready & panel["date"].between(start, end)
        for signal_name in SIGNALS:
            signal_available = panel[f"sig_solar_{signal_name}"].shift(1).notna()
            sample = panel.loc[period_mask & signal_available].copy()
            standalone = performance(
                sample,
                position_column=f"position_standalone_{signal_name}",
                cost_bps=TRANSACTION_COST_BPS,
            )
            standalone.update(
                {
                    "period": period,
                    "signal": signal_name,
                    "cost_bps": TRANSACTION_COST_BPS,
                }
            )
            standalone_rows.append(standalone)
            for weight in WEIGHT_GRID:
                for variant in ("active", "neutral", "scaled"):
                    column = (
                        f"position_{variant}_{signal_name}_{weight:.3f}"
                    )
                    metrics = performance(
                        sample,
                        position_column=column,
                        cost_bps=TRANSACTION_COST_BPS,
                    )
                    metrics.update(
                        {
                            "period": period,
                            "signal": signal_name,
                            "variant": variant,
                            "warm_weight": weight,
                            "cool_weight": weight / 2.0,
                            "cost_bps": TRANSACTION_COST_BPS,
                        }
                    )
                    rows.append(metrics)
    return pd.DataFrame(rows), pd.DataFrame(standalone_rows)


def annual_summary(panel: pd.DataFrame, *, weight: float) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    ready = base_ready(panel) & panel["sig_solar_radiation"].shift(1).notna()
    sample = panel.loc[ready & panel["date"].ge(PERIODS["full"][0])]
    for year, year_frame in sample.groupby(sample["date"].dt.year):
        for variant in ("active", "neutral"):
            metrics = performance(
                year_frame,
                position_column=(
                    f"position_{variant}_radiation_{weight:.3f}"
                ),
                cost_bps=TRANSACTION_COST_BPS,
            )
            metrics.update({"year": int(year), "variant": variant})
            rows.append(metrics)
        baseline = performance(
            year_frame,
            position_column="position_active_radiation_0.000",
            cost_bps=TRANSACTION_COST_BPS,
        )
        baseline.update({"year": int(year), "variant": "baseline"})
        rows.append(baseline)
    return pd.DataFrame(rows)


def _json_value(value: Any) -> Any:
    if isinstance(value, (pd.Timestamp, np.datetime64)):
        return pd.Timestamp(value).isoformat()
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if np.isnan(value) else float(value)
    if pd.isna(value):
        return None
    return value


def selected_metrics(
    performance_table: pd.DataFrame,
    *,
    weight: float,
) -> dict[str, dict[str, float]]:
    selected = performance_table.loc[
        performance_table["signal"].eq("radiation")
        & performance_table["variant"].eq("active")
        & performance_table["warm_weight"].eq(weight)
    ]
    return {
        row.period: {
            "observations": int(row.trading_days),
            "sharpe_2p5bps": float(row.sharpe_zero_rf),
            "cagr_2p5bps": float(row.cagr),
            "max_drawdown_2p5bps": float(row.maximum_drawdown),
        }
        for row in selected.itertuples(index=False)
    }


def run(output_dir: Path) -> dict[str, Any]:
    filesystem = gcsfs.GCSFileSystem()
    location_leads, keys = load_current_solar_partitions(filesystem)
    issues = build_solar_signals(location_leads)
    primary = issues.loc[
        issues["forecast_cycle_hour_utc"].eq(PRIMARY_CYCLE_UTC)
    ].copy()

    allocations = candidate_allocations()
    panel = build_research_panel(LOCAL_PANEL, WIND_FEATURES, allocations)
    panel = apply_early_roll_return(panel)
    signal_columns = ["date", "input_complete"] + [
        f"sig_solar_{name}" for name in SIGNALS
    ]
    panel = panel.merge(
        primary[signal_columns], on="date", how="left", validate="one_to_one"
    )
    panel = add_candidate_positions(panel)

    ic = ic_summary(panel)
    allocation_results, standalone = evaluate_candidates(panel)
    development = allocation_results.loc[
        allocation_results["period"].eq("development")
        & allocation_results["signal"].eq("radiation")
        & allocation_results["variant"].eq("active")
    ]
    development_weight = float(
        development.sort_values(
            ["sharpe_zero_rf", "warm_weight"], ascending=[False, True]
        ).iloc[0]["warm_weight"]
    )
    annual = annual_summary(panel, weight=CONSERVATIVE_WEIGHT)

    output_dir.mkdir(parents=True, exist_ok=True)
    primary.to_parquet(output_dir / "solar_signal_daily.parquet", index=False)
    ic.to_csv(output_dir / "ic_summary.csv", index=False)
    allocation_results.to_csv(
        output_dir / "allocation_performance.csv", index=False
    )
    standalone.to_csv(output_dir / "standalone_performance.csv", index=False)
    annual.to_csv(output_dir / "annual_performance_5pct.csv", index=False)
    pd.DataFrame({"gcs_key": keys}).to_csv(
        output_dir / "source_partitions_snapshot.csv", index=False
    )

    summary = {
        "research_only": True,
        "generated_from_current_gcs_snapshot": True,
        "source_partition_count": len(keys),
        "first_source_partition": keys[0],
        "last_source_partition": keys[-1],
        "issue_rows_all_cycles": len(issues),
        "complete_issue_rows_all_cycles": int(issues["input_complete"].sum()),
        "incomplete_issue_rows_all_cycles": int((~issues["input_complete"]).sum()),
        "primary_cycle": PRIMARY_CYCLE_UTC,
        "primary_issue_start": primary["date"].min(),
        "primary_issue_end": primary["date"].max(),
        "factor_definition": (
            "causal_zscore_60(-mean_1_to_5d_gfs_shortwave_energy / "
            "mean_extraterrestrial_horizontal_energy)"
        ),
        "application_lag": "one trading session",
        "roll_policy": "front month switched five trading days before official LTD",
        "transaction_cost_bps": TRANSACTION_COST_BPS,
        "conservative_warm_weight": CONSERVATIVE_WEIGHT,
        "conservative_cool_weight": CONSERVATIVE_WEIGHT / 2.0,
        "development_selected_warm_weight": development_weight,
        "development_selected_cool_weight": development_weight / 2.0,
        "conservative_metrics": selected_metrics(
            allocation_results, weight=CONSERVATIVE_WEIGHT
        ),
        "development_selected_metrics": selected_metrics(
            allocation_results, weight=development_weight
        ),
        "limitations": [
            "28 configured locations are equally weighted, not solar-capacity weighted",
            "GFS downward shortwave radiation is not a full plane-of-array PV model",
            "available sample ends before the current solar backfill is complete",
            "the development optimum is at the upper boundary of the tested grid",
            "mechanical futures roll trading costs are not charged separately",
        ],
    }
    with (output_dir / "summary.json").open("w", encoding="utf-8") as target:
        json.dump(summary, target, default=_json_value, indent=2, sort_keys=True)
        target.write("\n")
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


if __name__ == "__main__":
    result = run(parse_args().output_dir)
    print(json.dumps(result, default=_json_value, indent=2, sort_keys=True))
