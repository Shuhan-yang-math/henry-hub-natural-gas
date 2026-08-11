#!/usr/bin/env python3
"""Compare equal-location and EIA-capacity-weighted GDEX solar factors.

The script is a local research experiment.  It snapshots currently uploaded
solar_daily partitions, obtains monthly operating utility-scale solar capacity
from EIA, lags that capacity by two months, and maps plant coordinates to the
nearest configured weather point.  It then runs the same causal signal and
portfolio evaluation for equal and capacity weighting.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

import gcsfs
import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from naturalgas.evaluate_ncar_gdex_independent_wind_weights import (  # noqa: E402
    LOCAL_PANEL,
    WIND_FEATURES,
    build_research_panel,
    candidate_allocations,
)
from naturalgas.evaluate_ncar_gdex_solar_factor import (  # noqa: E402
    CONSERVATIVE_WEIGHT,
    PRIMARY_CYCLE_UTC,
    SIGNALS,
    TRANSACTION_COST_BPS,
    add_candidate_positions,
    apply_early_roll_return,
    build_solar_signals,
    evaluate_candidates,
    ic_summary,
)
from naturalgas.ncar_gdex_capacity_weighted_solar import (  # noqa: E402
    build_capacity_weighted_location_leads,
    build_monthly_location_weights,
    fetch_eia_solar_generators,
    load_current_solar_daily,
)


DEFAULT_OUTPUT_DIR = (
    Path(__file__).resolve().parent
    / "processed/ncar_gdex_capacity_weighted_solar"
)
CAPACITY_LAG_MONTHS = 2


def equal_location_leads(daily: pd.DataFrame) -> pd.DataFrame:
    """Reproduce the existing equal-location regional lead aggregation."""

    return (
        daily.groupby(
            [
                "forecast_reference_time_utc",
                "nominal_issue_date",
                "target_date",
                "lead_days",
            ],
            observed=True,
            as_index=False,
        )
        .agg(
            gfs_dswrf_wm2=("downward_shortwave_mean_wm2", "mean"),
            gfs_shortwave_energy_kwh_m2_day=(
                "downward_shortwave_energy_kwh_m2",
                "mean",
            ),
            gfs_total_cloud_cover_pct=("total_cloud_cover_mean_pct", "mean"),
            gfs_temperature_2m_c=("temperature_2m_mean_c", "mean"),
            location_count=("location_id", "nunique"),
            min_interval_count=("solar_sample_count", "min"),
        )
        .sort_values(["forecast_reference_time_utc", "lead_days"])
        .reset_index(drop=True)
    )


def signal_panel(signals: pd.DataFrame) -> pd.DataFrame:
    allocations = candidate_allocations()
    panel = build_research_panel(LOCAL_PANEL, WIND_FEATURES, allocations)
    panel = apply_early_roll_return(panel)
    primary = signals.loc[
        signals["forecast_cycle_hour_utc"].eq(PRIMARY_CYCLE_UTC)
    ].copy()
    columns = ["date", "input_complete"] + [
        f"sig_solar_{name}" for name in SIGNALS
    ]
    panel = panel.merge(
        primary[columns], on="date", how="left", validate="one_to_one"
    )
    return add_candidate_positions(panel)


def evaluate_weighting(
    signals: pd.DataFrame,
    *,
    weighting: str,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    panel = signal_panel(signals)
    ic = ic_summary(panel)
    allocation, standalone = evaluate_candidates(panel)
    for frame in (ic, allocation, standalone):
        frame.insert(0, "weighting", weighting)
    return ic, allocation, standalone


def metric_rows(
    allocation: pd.DataFrame,
    *,
    weighting: str,
    weight: float,
) -> dict[str, dict[str, float]]:
    selected = allocation.loc[
        allocation["weighting"].eq(weighting)
        & allocation["signal"].eq("radiation")
        & allocation["variant"].eq("active")
        & allocation["warm_weight"].eq(weight)
    ]
    return {
        row.period: {
            "trading_days": int(row.trading_days),
            "sharpe": float(row.sharpe_zero_rf),
            "cagr": float(row.cagr),
            "maximum_drawdown": float(row.maximum_drawdown),
        }
        for row in selected.itertuples(index=False)
    }


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


def capacity_query_bounds(
    daily: pd.DataFrame,
    *,
    lag_months: int,
) -> tuple[str, str]:
    issue_periods = (
        daily["forecast_reference_time_utc"].dt.tz_localize(None).dt.to_period("M")
    )
    return str(issue_periods.min() - lag_months), str(issue_periods.max() - lag_months)


def load_or_fetch_capacity(
    *,
    output_dir: Path,
    api_key: str,
    start: str,
    end: str,
    workers: int,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    cache = output_dir / "eia860m_operating_solar_generators.parquet"
    metadata_path = output_dir / "eia860m_metadata.json"
    if cache.exists() and metadata_path.exists():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if (
            metadata.get("start_period") <= start
            and metadata.get("end_period") >= end
        ):
            return pd.read_parquet(cache), metadata
    generators, metadata = fetch_eia_solar_generators(
        api_key=api_key,
        start=start,
        end=end,
        workers=workers,
    )
    generators.to_parquet(cache, index=False)
    metadata_path.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return generators, metadata


def run(
    *,
    output_dir: Path,
    api_key: str,
    workers: int,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    filesystem = gcsfs.GCSFileSystem()
    daily, source_keys = load_current_solar_daily(filesystem)
    query_start, query_end = capacity_query_bounds(
        daily, lag_months=CAPACITY_LAG_MONTHS
    )
    generators, eia_metadata = load_or_fetch_capacity(
        output_dir=output_dir,
        api_key=api_key,
        start=query_start,
        end=query_end,
        workers=workers,
    )
    weights, weight_diagnostics = build_monthly_location_weights(generators)
    capacity_leads, aggregation_diagnostics = (
        build_capacity_weighted_location_leads(
            daily,
            weights,
            capacity_lag_months=CAPACITY_LAG_MONTHS,
        )
    )
    equal_leads = equal_location_leads(daily)
    capacity_signals = build_solar_signals(capacity_leads)
    equal_signals = build_solar_signals(equal_leads)

    result_sets = []
    for weighting, signals in (
        ("equal_location", equal_signals),
        ("eia_capacity_lag2m", capacity_signals),
    ):
        ic, allocation, standalone = evaluate_weighting(
            signals, weighting=weighting
        )
        result_sets.append((ic, allocation, standalone))
    ic_all = pd.concat([item[0] for item in result_sets], ignore_index=True)
    allocation_all = pd.concat(
        [item[1] for item in result_sets], ignore_index=True
    )
    standalone_all = pd.concat(
        [item[2] for item in result_sets], ignore_index=True
    )

    weights.to_parquet(output_dir / "monthly_location_weights.parquet", index=False)
    capacity_leads.to_parquet(
        output_dir / "capacity_weighted_location_leads.parquet", index=False
    )
    capacity_signals.to_parquet(
        output_dir / "capacity_weighted_solar_signals.parquet", index=False
    )
    equal_signals.to_parquet(
        output_dir / "equal_location_solar_signals.parquet", index=False
    )
    ic_all.to_csv(output_dir / "ic_comparison.csv", index=False)
    allocation_all.to_csv(
        output_dir / "allocation_comparison.csv", index=False
    )
    standalone_all.to_csv(
        output_dir / "standalone_comparison.csv", index=False
    )
    pd.DataFrame({"gcs_key": source_keys}).to_csv(
        output_dir / "source_partitions_snapshot.csv", index=False
    )

    latest_period = weights["period"].max()
    first_period = weights["period"].min()
    capacity_totals = (
        weights[["period", "total_capacity_mw"]]
        .drop_duplicates()
        .set_index("period")["total_capacity_mw"]
    )
    summary = {
        "research_only": True,
        "weather_partition_count": len(source_keys),
        "first_weather_partition": source_keys[0],
        "last_weather_partition": source_keys[-1],
        "capacity_source": eia_metadata,
        "capacity_lag_months": CAPACITY_LAG_MONTHS,
        "capacity_weight_diagnostics": weight_diagnostics,
        "weather_aggregation_diagnostics": aggregation_diagnostics,
        "first_capacity_period": first_period,
        "last_capacity_period": latest_period,
        "first_capacity_mw": float(capacity_totals.loc[first_period]),
        "last_capacity_mw": float(capacity_totals.loc[latest_period]),
        "portfolio_warm_weight": CONSERVATIVE_WEIGHT,
        "portfolio_cool_weight": CONSERVATIVE_WEIGHT / 2.0,
        "transaction_cost_bps": TRANSACTION_COST_BPS,
        "equal_location_baseline": metric_rows(
            allocation_all, weighting="equal_location", weight=0.0
        ),
        "equal_location_5pct": metric_rows(
            allocation_all,
            weighting="equal_location",
            weight=CONSERVATIVE_WEIGHT,
        ),
        "capacity_weighted_baseline": metric_rows(
            allocation_all, weighting="eia_capacity_lag2m", weight=0.0
        ),
        "capacity_weighted_5pct": metric_rows(
            allocation_all,
            weighting="eia_capacity_lag2m",
            weight=CONSERVATIVE_WEIGHT,
        ),
        "limitations": [
            "EIA-860M/API includes utility-scale plants, not distributed solar below 1 MW",
            "plant capacity is approximated by the nearest of 28 weather locations",
            "historical API values are revised data, not archived publication vintages",
            "the solar weather backfill remains incomplete and non-contiguous",
        ],
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, default=_json_value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--eia-api-key-env", default="EIA_API_KEY")
    parser.add_argument("--workers", type=int, default=4)
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    key = os.environ.get(arguments.eia_api_key_env)
    if not key:
        raise SystemExit(
            f"missing EIA API key in environment variable {arguments.eia_api_key_env}"
        )
    result = run(
        output_dir=arguments.output_dir,
        api_key=key,
        workers=arguments.workers,
    )
    print(json.dumps(result, default=_json_value, indent=2, sort_keys=True))
