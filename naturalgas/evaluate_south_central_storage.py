#!/usr/bin/env python3
"""Research-only test of South Central total, salt and nonsalt storage.

The experiment applies the existing point-in-time Lower-48 storage methodology
unchanged to three EIA WNGSR series: South Central total, salt and nonsalt.
It reports (1) standalone level/change/four-week-change performance and (2)
the effect of replacing either the low-storage slot or all three storage slots
in the current fixed strategy.  No candidate is selected and no GCS object is
modified.
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

from naturalgas.evaluate_native_frequency_fundamentals import (  # noqa: E402
    STORAGE_WEEKLY_KEY,
    STRATEGY_START,
    THROUGH_DATE,
    WEEKLY_Z_MIN,
    WEEKLY_Z_WINDOW,
    apply_native_frequency_fundamentals,
    causal_z,
    json_default,
    load_parquet,
)
from naturalgas.evaluate_no_consumption_fundamental_weights import (  # noqa: E402
    DEVELOPMENT_END,
    HOLDOUT_START,
    VALIDATION_END,
    VALIDATION_START,
    candidate_position,
    candidate_weights,
    common_sample_mask,
    metrics_for,
    prepare_base_panel,
    transform_components,
    weighted_available,
)
from naturalgas.evaluate_ncar_gdex_complete_solar_factor import (  # noqa: E402
    SOLAR_LEAD_PATH,
    SOLAR_SIGNAL_PATH,
    TRANSACTION_COST_BPS,
    extended_performance,
    load_capacity_weighted_solar,
)
from naturalgas.evaluate_ncar_gdex_independent_wind_weights import (  # noqa: E402
    LOCAL_PANEL,
    WIND_FEATURES,
)
from naturalgas.eia_storage_release_calendar import (  # noqa: E402
    wngsr_release_calendar,
)


DEFAULT_OUTPUT_DIR = (
    Path(__file__).resolve().parent / "processed/south_central_storage"
)
CURRENT_WEIGHT_CANDIDATE = "reallocate__low_storage__lng_export_mom"
REGIONS = {
    "lower48": "lower48",
    "south_central_total": "south_central",
    "south_central_salt": "salt",
    "south_central_nonsalt": "nonsalt",
}
STORAGE_COMPONENTS = (
    "sig_low_storage",
    "sig_storage_change",
    "sig_storage_4w_change",
)
PERIODS = {
    "development": (STRATEGY_START, DEVELOPMENT_END),
    "validation": (VALIDATION_START, VALIDATION_END),
    "first_look_holdout": (HOLDOUT_START, THROUGH_DATE),
    "full": (STRATEGY_START, THROUGH_DATE),
}


def regional_weekly_signals(
    filesystem: gcsfs.GCSFileSystem,
) -> pd.DataFrame:
    """Build the same causal weekly features for each requested region."""

    storage = load_parquet(filesystem, STORAGE_WEEKLY_KEY).sort_values(
        "week_ending"
    )
    storage["week_ending"] = pd.to_datetime(storage["week_ending"]).astype(
        "datetime64[ns]"
    )
    storage["week_of_year"] = (
        storage["week_ending"].dt.isocalendar().week.astype(int)
    )
    release_calendar = wngsr_release_calendar(storage["week_ending"])
    storage["storage_available_date"] = release_calendar[
        "storage_available_date"
    ]

    output_columns = ["week_ending", "storage_available_date"]
    for region, source in REGIONS.items():
        level = pd.to_numeric(storage[source], errors="coerce").astype(float)
        normal = level.groupby(storage["week_of_year"]).transform(
            lambda values: values.shift(1).rolling(5, min_periods=3).mean()
        )
        deviation = level / normal - 1.0

        weekly_change = level.diff()
        weekly_normal = weekly_change.groupby(storage["week_of_year"]).transform(
            lambda values: values.shift(1).rolling(5, min_periods=3).mean()
        )
        weekly_surprise = weekly_change - weekly_normal

        four_week_change = level.diff(4)
        four_week_normal = four_week_change.groupby(
            storage["week_of_year"]
        ).transform(
            lambda values: values.shift(1).rolling(5, min_periods=3).mean()
        )
        four_week_surprise = four_week_change - four_week_normal

        raw_features = {
            "level": deviation,
            "change": weekly_surprise,
            "change_4w": four_week_surprise,
        }
        for feature, values in raw_features.items():
            raw_name = f"{region}_{feature}_raw"
            signal_name = f"{region}_{feature}_signal"
            storage[raw_name] = values
            storage[signal_name] = -causal_z(
                values,
                window=WEEKLY_Z_WINDOW,
                min_periods=WEEKLY_Z_MIN,
            )
            output_columns.extend([raw_name, signal_name])

    return storage[output_columns]


def attach_regional_signals(
    panel: pd.DataFrame,
    weekly: pd.DataFrame,
) -> pd.DataFrame:
    result = pd.merge_asof(
        panel.sort_values("date"),
        weekly.sort_values("storage_available_date"),
        left_on="date",
        right_on="storage_available_date",
        direction="backward",
    )
    known = result["storage_available_date"].notna()
    assert (
        result.loc[known, "storage_available_date"]
        <= result.loc[known, "date"]
    ).all()
    return result


def standalone_results(
    panel: pd.DataFrame,
    *,
    start: pd.Timestamp,
    through_date: pd.Timestamp,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for period, (period_start, period_end) in PERIODS.items():
        period_start = max(period_start, start)
        period_end = min(period_end, through_date)
        for region in REGIONS:
            for feature in ("level", "change", "change_4w"):
                signal_column = f"{region}_{feature}_signal"
                position = np.tanh(panel[signal_column] / 2.0).shift(1)
                ready = (
                    panel["date"].between(period_start, period_end)
                    & panel["roll_adjusted_return"].notna()
                    & position.notna()
                )
                sample = panel.loc[ready, ["date", "roll_adjusted_return"]].copy()
                sample["position"] = position.loc[ready]
                metrics = extended_performance(
                    sample,
                    position_column="position",
                    cost_bps=TRANSACTION_COST_BPS,
                )
                metrics.update({
                    "period": period,
                    "region": region,
                    "feature": feature,
                })
                rows.append(metrics)
    return pd.DataFrame(rows)


def strategy_fundamentals(
    base: pd.DataFrame,
) -> tuple[dict[str, pd.Series], dict[str, dict[str, float]]]:
    transformed = transform_components(base)
    weights = candidate_weights()[CURRENT_WEIGHT_CANDIDATE]
    candidates = {
        "current_lower48": weighted_available(transformed, weights),
    }

    for region in REGIONS:
        if region == "lower48":
            continue
        level_only = transformed.copy()
        level_only["sig_low_storage"] = np.tanh(
            base[f"{region}_level_signal"] / 2.0
        )
        candidates[f"replace_level__{region}"] = weighted_available(
            level_only, weights
        )

        all_storage = transformed.copy()
        all_storage["sig_low_storage"] = np.tanh(
            base[f"{region}_level_signal"] / 2.0
        )
        all_storage["sig_storage_change"] = np.tanh(
            base[f"{region}_change_signal"] / 2.0
        )
        all_storage["sig_storage_4w_change"] = np.tanh(
            base[f"{region}_change_4w_signal"] / 2.0
        )
        candidates[f"replace_all_storage__{region}"] = weighted_available(
            all_storage, weights
        )
    return candidates, {name: weights.copy() for name in candidates}


def strategy_results(
    base: pd.DataFrame,
    fundamentals: dict[str, pd.Series],
    *,
    start: pd.Timestamp,
    through_date: pd.Timestamp,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    scores_and_positions = {
        name: candidate_position(base, fundamental)
        for name, fundamental in fundamentals.items()
    }
    positions = {
        name: pair[1] for name, pair in scores_and_positions.items()
    }
    common = common_sample_mask(
        base,
        positions,
        start=start,
        end=through_date,
    )

    rows: list[dict[str, Any]] = []
    for period, (period_start, period_end) in PERIODS.items():
        period_start = max(period_start, start)
        period_end = min(period_end, through_date)
        mask = common & base["date"].between(period_start, period_end)
        for name, position in positions.items():
            metrics = metrics_for(base, position, mask)
            metrics.update({"period": period, "candidate": name})
            rows.append(metrics)

    daily = base.loc[common, ["date", "roll_adjusted_return"]].copy()
    for name, position in positions.items():
        daily[f"fundamental__{name}"] = fundamentals[name].loc[common]
        daily[f"score__{name}"] = scores_and_positions[name][0].loc[common]
        daily[f"position__{name}"] = position.loc[common]
    return pd.DataFrame(rows), daily


def signal_correlations(panel: pd.DataFrame) -> pd.DataFrame:
    columns = [
        f"{region}_{feature}_signal"
        for feature in ("level", "change", "change_4w")
        for region in REGIONS
    ]
    correlation = panel[columns].corr()
    return correlation.rename_axis("signal").reset_index()


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
    panel = pd.read_parquet(panel_path)
    panel["date"] = pd.to_datetime(panel["date"]).astype("datetime64[ns]")
    native = apply_native_frequency_fundamentals(panel, filesystem)
    solar = load_capacity_weighted_solar(solar_signal_path, solar_lead_path)
    base = prepare_base_panel(native, wind_path=wind_path, solar=solar)
    weekly = regional_weekly_signals(filesystem)
    base = attach_regional_signals(base, weekly)

    standalone = standalone_results(
        base,
        start=start,
        through_date=through_date,
    )
    fundamentals, _ = strategy_fundamentals(base)
    strategy, daily = strategy_results(
        base,
        fundamentals,
        start=start,
        through_date=through_date,
    )
    correlations = signal_correlations(base)

    output_dir.mkdir(parents=True, exist_ok=True)
    standalone.to_csv(output_dir / "standalone_results.csv", index=False)
    strategy.to_csv(output_dir / "strategy_results.csv", index=False)
    correlations.to_csv(output_dir / "signal_correlations.csv", index=False)
    daily.to_parquet(
        output_dir / "daily_comparison.parquet",
        index=False,
        compression="zstd",
    )

    full_strategy = strategy.loc[strategy["period"].eq("full")].set_index(
        "candidate"
    )
    full_standalone = standalone.loc[standalone["period"].eq("full")]
    summary = {
        "experiment": "south_central_total_salt_nonsalt_storage",
        "status": "research_only",
        "gcs_objects_modified": False,
        "formal_strategy_modified": False,
        "sample_start": daily["date"].min(),
        "sample_end": daily["date"].max(),
        "trading_days": len(daily),
        "source": STORAGE_WEEKLY_KEY,
        "regions": REGIONS,
        "method": {
            "seasonal_normal": "same ISO week, prior five observations, min 3",
            "zscore": (
                f"causal rolling {WEEKLY_Z_WINDOW} weekly values, "
                f"min {WEEKLY_Z_MIN}, current excluded"
            ),
            "release_alignment": (
                "actual EIA WNGSR publication date, including audited holiday "
                "exceptions; normal release Thursday 10:30 a.m. Eastern"
            ),
            "strategy_signal_lag_sessions": 1,
            "transaction_cost_bps": TRANSACTION_COST_BPS,
            "weight_policy": CURRENT_WEIGHT_CANDIDATE,
            "parameter_selection": "none",
        },
        "full_strategy_metrics": {
            candidate: {
                "sharpe_zero_rf": float(row["sharpe_zero_rf"]),
                "cagr": float(row["cagr"]),
                "maximum_drawdown": float(row["maximum_drawdown"]),
                "win_rate": float(row["win_rate"]),
                "total_turnover": float(row["total_turnover"]),
            }
            for candidate, row in full_strategy.iterrows()
        },
        "full_standalone_sharpe": {
            f"{row.region}__{row.feature}": float(row.sharpe_zero_rf)
            for row in full_standalone.itertuples()
        },
        "caution": (
            "The EIA historical file is revised rather than an archive of first "
            "releases. Regional substitutions are diagnostics, not selected models."
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
