#!/usr/bin/env python3
"""Evaluate no-delay monthly fundamentals inside the current selected strategy.

This is a deliberately non-tradable perfect-information experiment. Final
reference-month EIA values are aligned to the same calendar month instead of
the strategy's M+3 availability convention. The experiment changes only the
monthly fundamental aggregate. It retains D1--3 wind, the storage-amplified
fast-shock guard, the Central 40% / Florida 60% EIA-930 sleeve, the
BSEE/Sabine pure short veto, the one-session position lag, and 2.5 bp turnover
cost.

The fixed scenarios are reported together so that the result is not selected
from one favorable definition after viewing returns:

* no-delay production growth and momentum;
* no-delay production plus one consumption-YoY slot;
* all six active monthly signals with no delay; and
* all six active monthly signals plus one consumption-YoY slot.

Consumption is not active in the selected strategy. Its fixed 1/11 slot is
funded symmetrically from the two previously doubled slots, matching the prior
perfect-information experiment.
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


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from naturalgas.evaluate_model_v03_d1_3_storage_guard import (  # noqa: E402
    MODEL_V01_DAILY,
    SCORE_INPUTS,
    TRANSACTION_COST_BPS,
    net_return,
    performance,
)
from naturalgas.audit_inputs import (  # noqa: E402
    D1_3_SCORE_INPUTS_ARTIFACT_ID,
    EIA930_OVERLAY_ARTIFACT_ID,
    audit_input_path,
    resolve_audit_inputs,
)
from naturalgas.evaluate_native_frequency_fundamentals import (  # noqa: E402
    FUNDAMENTALS_MONTHLY_KEY,
    LNG_TRADE_KEY,
    MONTHLY_Z_MIN,
    MONTHLY_Z_WINDOW,
    apply_native_frequency_fundamentals,
    causal_z,
    load_parquet,
)
from naturalgas.evaluate_ncar_gdex_complete_solar_factor import (  # noqa: E402
    SOLAR_LEAD_PATH,
    SOLAR_SIGNAL_PATH,
    load_capacity_weighted_solar,
)
from naturalgas.evaluate_ncar_gdex_independent_wind_weights import (  # noqa: E402
    LOCAL_PANEL,
    SHOULDER_MONTHS,
    WIND_FEATURES,
)
from naturalgas.evaluate_no_consumption_fundamental_weights import (  # noqa: E402
    ORIGINAL_SLOT_COUNT,
    candidate_weights,
    prepare_base_panel,
    transform_components,
    weighted_available,
)
from naturalgas.evaluate_south_central_storage import (  # noqa: E402
    CURRENT_WEIGHT_CANDIDATE,
    attach_regional_signals,
    regional_weekly_signals,
)


CURRENT_DAILY = (
    PROJECT_ROOT
    / "results/models/v03_d1_3_storage_guard/strategy_daily.parquet"
)
OVERLAY_INPUTS = audit_input_path(EIA930_OVERLAY_ARTIFACT_ID)
DEFAULT_OUTPUT_DIR = (
    PROJECT_ROOT / "results/experiments/perfect_information_current_strategy"
)

CURRENT = "current_selected"
PI_PRODUCTION = "no_delay_production"
PI_PRODUCTION_CONSUMPTION = "no_delay_production_plus_consumption_yoy"
PI_ALL_ACTIVE = "no_delay_all_six_active_monthly"
PI_ALL_CONSUMPTION = "no_delay_all_six_plus_consumption_yoy"
SCENARIOS = (
    CURRENT,
    PI_PRODUCTION,
    PI_PRODUCTION_CONSUMPTION,
    PI_ALL_ACTIVE,
    PI_ALL_CONSUMPTION,
)

PERIODS = {
    "development_2019_2020": ("1900-01-01", "2020-12-31"),
    "validation_2021_2023": ("2021-01-01", "2023-12-31"),
    "first_look_2024_plus": ("2024-01-01", "2100-01-01"),
}


def json_default(value: Any) -> Any:
    if isinstance(value, (pd.Timestamp, np.datetime64)):
        return pd.Timestamp(value).isoformat()
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return None if np.isnan(value) else float(value)
    raise TypeError(type(value).__name__)


def perfect_information_monthly(
    filesystem: gcsfs.GCSFileSystem,
) -> pd.DataFrame:
    """Build causal monthly signals and retain their true reference month."""

    monthly = load_parquet(filesystem, FUNDAMENTALS_MONTHLY_KEY).sort_values(
        "month"
    )
    monthly["month"] = pd.to_datetime(monthly["month"]).astype("datetime64[ns]")
    days = monthly["month"].dt.days_in_month.astype(float)
    production_rate = monthly["dry_prod"] / days
    consumption_rate = monthly["total_cons"] / days
    monthly["production_yoy_raw"] = (
        monthly["dry_prod"] / monthly["dry_prod"].shift(12) - 1.0
    )
    monthly["production_mom_raw"] = production_rate.pct_change()
    monthly["consumption_yoy_raw"] = (
        monthly["total_cons"] / monthly["total_cons"].shift(12) - 1.0
    )
    monthly["consumption_mom_raw"] = consumption_rate.pct_change()
    monthly["net_import_ratio_raw"] = (
        monthly["imports"] - monthly["exports"]
    ) / monthly["total_cons"]
    monthly["net_import_change_raw"] = monthly["net_import_ratio_raw"].diff()

    lng = load_parquet(filesystem, LNG_TRADE_KEY)
    lng = (
        lng.loc[
            lng["dataset"].eq("country_exports")
            & lng["is_us_aggregate"].fillna(False)
            & lng["process-name"].eq("Liquefied Natural Gas Exports")
            & lng["metric"].eq("volume"),
            ["month", "value"],
        ]
        .rename(columns={"value": "lng_exports"})
        .sort_values("month")
    )
    lng["month"] = pd.to_datetime(lng["month"]).astype("datetime64[ns]")
    lng_rate = lng["lng_exports"] / lng["month"].dt.days_in_month.astype(float)
    lng["lng_yoy_raw"] = lng["lng_exports"] / lng["lng_exports"].shift(12) - 1.0
    lng["lng_mom_raw"] = lng_rate.pct_change()
    monthly = monthly.merge(
        lng[["month", "lng_yoy_raw", "lng_mom_raw"]],
        on="month",
        how="left",
        validate="one_to_one",
    )

    definitions = {
        "pi_low_production_growth": ("production_yoy_raw", -1.0),
        "pi_production_mom": ("production_mom_raw", -1.0),
        "pi_lng_export_growth": ("lng_yoy_raw", 1.0),
        "pi_lng_export_mom": ("lng_mom_raw", 1.0),
        "pi_net_import_supply": ("net_import_ratio_raw", -1.0),
        "pi_net_import_change": ("net_import_change_raw", -1.0),
        "pi_consumption_growth": ("consumption_yoy_raw", 1.0),
        "pi_consumption_mom": ("consumption_mom_raw", 1.0),
    }
    for output, (source, sign) in definitions.items():
        monthly[output] = sign * causal_z(
            monthly[source], window=MONTHLY_Z_WINDOW, min_periods=MONTHLY_Z_MIN
        )
    monthly["period"] = monthly["month"].dt.to_period("M")
    return monthly[["month", "period", *definitions]]


def build_fundamentals(
    filesystem: gcsfs.GCSFileSystem,
) -> pd.DataFrame:
    """Build current and fixed no-delay fundamental composites by score date."""

    panel = pd.read_parquet(LOCAL_PANEL)
    panel["date"] = pd.to_datetime(panel["date"]).astype("datetime64[ns]")
    native = apply_native_frequency_fundamentals(panel, filesystem)
    solar = load_capacity_weighted_solar(SOLAR_SIGNAL_PATH, SOLAR_LEAD_PATH)
    base = prepare_base_panel(native, wind_path=WIND_FEATURES, solar=solar)
    base = attach_regional_signals(base, regional_weekly_signals(filesystem))

    transformed = transform_components(base)
    transformed["sig_low_storage"] = np.tanh(
        base["south_central_total_level_signal"] / 2.0
    )
    transformed["sig_storage_change"] = np.tanh(
        base["south_central_total_change_signal"] / 2.0
    )
    transformed["sig_storage_4w_change"] = np.tanh(
        base["south_central_total_change_4w_signal"] / 2.0
    )
    current_weights = candidate_weights()[CURRENT_WEIGHT_CANDIDATE]
    current_fundamental = weighted_available(transformed, current_weights)

    monthly = perfect_information_monthly(filesystem)
    aligned = pd.DataFrame(
        {"period": base["date"].dt.to_period("M")}, index=base.index
    ).merge(monthly.drop(columns="month"), on="period", how="left", validate="many_to_one")
    aligned.index = base.index

    production = transformed.copy()
    production["sig_low_production_growth"] = np.tanh(
        aligned["pi_low_production_growth"] / 2.0
    )
    production["sig_production_mom"] = aligned["pi_production_mom"]

    all_active = production.copy()
    all_active["sig_lng_export_growth"] = np.tanh(
        aligned["pi_lng_export_growth"] / 2.0
    )
    all_active["sig_lng_export_mom"] = aligned["pi_lng_export_mom"]
    all_active["sig_net_import_supply"] = np.tanh(
        aligned["pi_net_import_supply"] / 2.0
    )
    all_active["sig_net_import_change"] = aligned["pi_net_import_change"]

    consumption_weights = current_weights.copy()
    half_slot = 0.5 / ORIGINAL_SLOT_COUNT
    consumption_weights["sig_low_storage"] -= half_slot
    consumption_weights["sig_lng_export_mom"] -= half_slot
    consumption_weights["sig_consumption_growth"] = 1.0 / ORIGINAL_SLOT_COUNT
    if not np.isclose(sum(consumption_weights.values()), 1.0):
        raise AssertionError("Consumption scenario weights do not sum to one")

    production_consumption = production.copy()
    production_consumption["sig_consumption_growth"] = np.tanh(
        aligned["pi_consumption_growth"] / 2.0
    )
    all_consumption = all_active.copy()
    all_consumption["sig_consumption_growth"] = np.tanh(
        aligned["pi_consumption_growth"] / 2.0
    )

    output = base[["date", "daylight_scale"]].copy()
    output[CURRENT] = current_fundamental
    output[PI_PRODUCTION] = weighted_available(production, current_weights)
    output[PI_PRODUCTION_CONSUMPTION] = weighted_available(
        production_consumption, consumption_weights
    )
    output[PI_ALL_ACTIVE] = weighted_available(all_active, current_weights)
    output[PI_ALL_CONSUMPTION] = weighted_available(
        all_consumption, consumption_weights
    )
    production_ready = aligned[
        ["pi_low_production_growth", "pi_production_mom"]
    ].notna().all(axis=1)
    consumption_ready = aligned["pi_consumption_growth"].notna()
    all_active_ready = aligned[
        [
            "pi_low_production_growth",
            "pi_production_mom",
            "pi_lng_export_growth",
            "pi_lng_export_mom",
            "pi_net_import_supply",
            "pi_net_import_change",
        ]
    ].notna().all(axis=1)
    output[PI_PRODUCTION] = output[PI_PRODUCTION].where(production_ready)
    output[PI_PRODUCTION_CONSUMPTION] = output[
        PI_PRODUCTION_CONSUMPTION
    ].where(production_ready & consumption_ready)
    output[PI_ALL_ACTIVE] = output[PI_ALL_ACTIVE].where(all_active_ready)
    output[PI_ALL_CONSUMPTION] = output[PI_ALL_CONSUMPTION].where(
        all_active_ready & consumption_ready
    )
    output["perfect_information_reference_month"] = aligned["period"].astype(str)

    shoulder = output["date"].dt.month.isin(SHOULDER_MONTHS)
    seasonal_fundamental_weight = pd.Series(
        np.where(shoulder, 0.55, 0.40), index=output.index
    )
    solar_funding = 0.10 * output["daylight_scale"].fillna(0.0)
    output["effective_fundamental_score_weight"] = (
        seasonal_fundamental_weight - solar_funding - 0.10
    )
    return output


def build_daily(
    *,
    model_v01_daily_path: Path,
    score_inputs_path: Path,
    current_daily_path: Path,
    overlay_inputs_path: Path,
    filesystem: gcsfs.GCSFileSystem,
) -> pd.DataFrame:
    fundamentals = build_fundamentals(filesystem)
    scores = pd.read_parquet(score_inputs_path)
    scores["date"] = pd.to_datetime(scores["date"]).dt.normalize()
    overlay = pd.read_parquet(
        overlay_inputs_path, columns=["date", "production_short_block_active"]
    )
    overlay["date"] = pd.to_datetime(overlay["date"]).dt.normalize()
    frame = scores.merge(fundamentals, on="date", how="left", validate="one_to_one")
    frame = frame.merge(overlay, on="date", how="left", validate="one_to_one")
    production_block = (
        frame["production_short_block_active"].fillna(False).astype(bool)
    )
    fast_guard = (
        frame["fast_guard__fast_plus_storage_amplifier"]
        .fillna(False)
        .astype(bool)
    )

    frame[f"score__{CURRENT}"] = frame["score_d1_3_storage_amplified"]
    for scenario in SCENARIOS[1:]:
        delta = frame["effective_fundamental_score_weight"] * (
            frame[scenario] - frame[CURRENT]
        )
        no_wind = frame["score_without_wind"] + delta
        no_guard = frame["score_d1_3_no_guard"] + delta
        no_wind = no_wind.where(~production_block, no_wind.clip(lower=0.0))
        no_guard = no_guard.where(~production_block, no_guard.clip(lower=0.0))
        wind_block = (
            fast_guard
            & frame["wind_signal__d1_3"].lt(0.0)
            & no_wind.gt(0.0)
            & no_guard.lt(0.0)
        )
        frame[f"wind_block__{scenario}"] = wind_block
        frame[f"score__{scenario}"] = no_guard.mask(wind_block, 0.0)

    formal = pd.read_parquet(
        model_v01_daily_path, columns=["date", "roll_adjusted_return"]
    )
    formal["date"] = pd.to_datetime(formal["date"]).dt.normalize()
    current_daily = pd.read_parquet(
        current_daily_path,
        columns=["date", "shutin_notice_controller_active", "position__d1_3_storage_amplified"],
    )
    current_daily["date"] = pd.to_datetime(current_daily["date"]).dt.normalize()
    frame = formal.merge(frame, on="date", how="inner", validate="one_to_one")
    frame = frame.merge(current_daily, on="date", how="left", validate="one_to_one")
    event_active = frame["shutin_notice_controller_active"].fillna(False)
    for scenario in SCENARIOS:
        pre_veto = frame[f"score__{scenario}"].shift(1).clip(-1.0, 1.0)
        frame[f"position_pre_veto__{scenario}"] = pre_veto
        frame[f"position__{scenario}"] = pre_veto.where(
            ~(event_active & pre_veto.lt(0.0)), 0.0
        )

    ready = frame["roll_adjusted_return"].notna()
    ready &= frame[[f"position__{name}" for name in SCENARIOS]].notna().all(axis=1)
    ready &= frame[list(SCENARIOS)].notna().all(axis=1)
    ready &= frame["position__d1_3_storage_amplified"].notna()
    daily = frame.loc[ready].copy().reset_index(drop=True)
    baseline_audit = daily["position__d1_3_storage_amplified"]
    if not np.allclose(
        daily[f"position__{CURRENT}"], baseline_audit, atol=1e-12, equal_nan=True
    ):
        difference = (daily[f"position__{CURRENT}"] - baseline_audit).abs()
        raise AssertionError(
            "Current selected position does not reproduce: "
            f"{int(difference.gt(1e-12).sum())} mismatches, "
            f"maximum absolute difference {float(difference.max()):.12g}"
        )
    for scenario in SCENARIOS:
        daily[f"net_return__{scenario}"] = net_return(
            daily[f"position__{scenario}"], daily["roll_adjusted_return"]
        )
    return daily


def metrics_tables(daily: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    full_rows: list[dict[str, Any]] = []
    period_rows: list[dict[str, Any]] = []
    for scenario in SCENARIOS:
        full = performance(
            daily[f"net_return__{scenario}"],
            daily["date"],
            daily[f"position__{scenario}"],
        )
        full["scenario"] = scenario
        full_rows.append(full)
        for period, (start, end) in PERIODS.items():
            mask = daily["date"].between(start, end)
            result = performance(
                daily.loc[mask, f"net_return__{scenario}"],
                daily.loc[mask, "date"],
                daily.loc[mask, f"position__{scenario}"],
            )
            result.update({"scenario": scenario, "period": period})
            period_rows.append(result)
    return pd.DataFrame(full_rows), pd.DataFrame(period_rows)


def run(
    *,
    model_v01_daily_path: Path = MODEL_V01_DAILY,
    score_inputs_path: Path = SCORE_INPUTS,
    current_daily_path: Path = CURRENT_DAILY,
    overlay_inputs_path: Path = OVERLAY_INPUTS,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> dict[str, Any]:
    audit_paths = resolve_audit_inputs({
        D1_3_SCORE_INPUTS_ARTIFACT_ID: score_inputs_path,
        EIA930_OVERLAY_ARTIFACT_ID: overlay_inputs_path,
    })
    filesystem = gcsfs.GCSFileSystem()
    daily = build_daily(
        model_v01_daily_path=model_v01_daily_path,
        score_inputs_path=audit_paths[D1_3_SCORE_INPUTS_ARTIFACT_ID],
        current_daily_path=current_daily_path,
        overlay_inputs_path=audit_paths[EIA930_OVERLAY_ARTIFACT_ID],
        filesystem=filesystem,
    )
    metrics, periods = metrics_tables(daily)
    output_dir.mkdir(parents=True, exist_ok=True)
    daily.to_parquet(
        output_dir / "perfect_information_daily.parquet",
        index=False,
        compression="zstd",
    )
    metrics.to_csv(output_dir / "strategy_metrics.csv", index=False)
    periods.to_csv(output_dir / "period_metrics.csv", index=False)

    indexed = metrics.set_index("scenario")
    baseline = indexed.loc[CURRENT]
    scenario_summary: dict[str, Any] = {}
    for scenario in SCENARIOS:
        row = indexed.loc[scenario]
        scenario_summary[scenario] = {
            "sharpe": row["sharpe"],
            "sortino": row["sortino"],
            "cagr": row["cagr"],
            "maximum_drawdown": row["maximum_drawdown"],
            "sharpe_delta_vs_current": row["sharpe"] - baseline["sharpe"],
        }
    summary = {
        "experiment": "perfect_information_monthly_fundamentals_in_current_selected_strategy",
        "tradable": False,
        "warning": (
            "Final month-M values are aligned to month M; the experiment contains "
            "deliberate look-ahead and is only an explanatory upper bound."
        ),
        "sample_start": daily["date"].min(),
        "sample_end": daily["date"].max(),
        "trading_days": len(daily),
        "transaction_cost_bps": TRANSACTION_COST_BPS,
        "retained_strategy_layers": [
            "D1-3 wind",
            "storage-amplified fast-shock wind guard",
            "Central 40% / Florida 60% EIA-930 sleeve",
            "BSEE/Sabine pure short veto",
            "one-session position lag",
        ],
        "scenarios": scenario_summary,
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True, default=json_default) + "\n",
        encoding="utf-8",
    )
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model-v01-daily", type=Path, default=MODEL_V01_DAILY
    )
    parser.add_argument("--score-inputs", type=Path, default=SCORE_INPUTS)
    parser.add_argument("--current-daily", type=Path, default=CURRENT_DAILY)
    parser.add_argument("--overlay-inputs", type=Path, default=OVERLAY_INPUTS)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    result = run(
        model_v01_daily_path=arguments.model_v01_daily,
        score_inputs_path=arguments.score_inputs,
        current_daily_path=arguments.current_daily,
        overlay_inputs_path=arguments.overlay_inputs,
        output_dir=arguments.output_dir,
    )
    print(json.dumps(result, indent=2, sort_keys=True, default=json_default))
