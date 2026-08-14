#!/usr/bin/env python3
"""Reproduce the selected D1--3 wind plus storage-amplified guard strategy.

The selected version retains the 40% Central / 60% Florida EIA-930 sleeve and
the BSEE/Sabine pure short veto, but replaces the wind forecast average from
days 1--5 with days 1--3. A wind direction guard activates for a strong new
bullish shock, or for a moderate new bullish shock when South Central storage
is unusually low. The HDD-revision branch is disabled in June--August and is
active in every other month. Low storage can never activate the guard on its
own.

The guard is one-sided: when the score without wind is positive, wind is
bearish, and wind alone would reverse the score below zero, the score is set to
zero. It cannot create or amplify a long and cannot alter a short already
present without wind. Positions retain the one-session lag and 2.5 bp turnover
cost used throughout the strategy.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from naturalgas.shutin_notice_event_controller import (  # noqa: E402
    DEFAULT_EVENT_REPORTS_PATH,
    apply_controller,
)
from naturalgas.nymex_session_calendar import (  # noqa: E402
    CONFIRMED_NON_SESSION_DATES,
    filter_confirmed_nymex_sessions,
)
from naturalgas.eia930_florida_availability import (  # noqa: E402
    validate_score_history,
)


FORMAL_DAILY = (
    PROJECT_ROOT
    / "naturalgas/processed/south_central_storage_strategy/strategy_daily.parquet"
)
SCORE_INPUTS = (
    PROJECT_ROOT
    / "inputs/audit/wind/d1_3_storage_amplifier_inputs.parquet"
)
STORAGE_CALENDAR_CORRECTIONS = (
    PROJECT_ROOT
    / "inputs/audit/storage/wngsr_d1_3_score_corrections.parquet"
)
DEFAULT_OUTPUT_DIR = (
    PROJECT_ROOT / "results/experiments/d1_3_storage_amplified"
)
TRANSACTION_COST_BPS = 2.5
COLD_MONTHS = (11, 12, 1, 2, 3)
HDD_GUARD_MONTHS = (1, 2, 3, 4, 5, 9, 10, 11, 12)
STRONG_SCORE_THRESHOLD = 1.0
MODERATE_SCORE_THRESHOLD = 0.5
FIRM_STRONG_THRESHOLD = float(np.tanh(1.0))
FIRM_MODERATE_THRESHOLD = float(np.tanh(0.5))

SCORE_D1_5 = "score_d1_5_no_guard"
SCORE_D1_3 = "score_d1_3_no_guard"
SCORE_SELECTED = "score_d1_3_storage_amplified"
PRE_D1_5 = "position_pre_veto__d1_5"
PRE_D1_3 = "position_pre_veto__d1_3"
PRE_SELECTED = "position_pre_veto__d1_3_storage_amplified"
POS_D1_5 = "position__d1_5"
POS_D1_3 = "position__d1_3"
POS_SELECTED = "position__d1_3_storage_amplified"
NET_D1_5 = "net_return__d1_5"
NET_D1_3 = "net_return__d1_3"
NET_SELECTED = "net_return__d1_3_storage_amplified"
BLOCK = "wind_flip_blocked_d1_3_storage_amplifier"
STORAGE_SCORE_DELTA = "wngsr_score_delta_before_production_control"

PERIODS = {
    "development_2019_2020": ("1900-01-01", "2020-12-31"),
    "validation_2021_2023": ("2021-01-01", "2023-12-31"),
    "first_look_2024_plus": ("2024-01-01", "2100-01-01"),
    "2025": ("2025-01-01", "2025-12-31"),
    "2026_ytd": ("2026-01-01", "2026-12-31"),
}


def json_default(value: Any) -> Any:
    if isinstance(value, (pd.Timestamp, np.datetime64)):
        return pd.Timestamp(value).isoformat()
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return None if np.isnan(value) else float(value)
    raise TypeError(type(value).__name__)


def net_return(position: pd.Series, futures_return: pd.Series) -> pd.Series:
    turnover = position.diff().abs().fillna(position.abs())
    return position * futures_return - (
        turnover * TRANSACTION_COST_BPS / 10_000.0
    )


def performance(
    net: pd.Series,
    dates: pd.Series,
    position: pd.Series,
) -> dict[str, Any]:
    sample = pd.DataFrame(
        {"date": dates, "net_return": net, "position": position}
    ).dropna().sort_values("date")
    if len(sample) < 2:
        return {"trading_days": len(sample), "sharpe": np.nan}
    log_return = np.log1p(sample["net_return"])
    wealth = (1.0 + sample["net_return"]).cumprod()
    years = max(
        (sample["date"].iloc[-1] - sample["date"].iloc[0]).days
        / 365.2425,
        1.0 / 252.0,
    )
    volatility = float(log_return.std(ddof=1))
    downside = log_return.clip(upper=0.0)
    downside_deviation = float(
        np.sqrt(np.square(downside).mean()) * np.sqrt(252.0)
    )
    turnover = sample["position"].diff().abs().fillna(
        sample["position"].abs()
    )
    return {
        "trading_days": len(sample),
        "start": sample["date"].iloc[0],
        "end": sample["date"].iloc[-1],
        "total_return": float(wealth.iloc[-1] - 1.0),
        "cagr": float(np.exp(log_return.sum() / years) - 1.0),
        "sharpe": float(log_return.mean() / volatility * np.sqrt(252.0)),
        "sortino": (
            float(log_return.mean() * 252.0 / downside_deviation)
            if downside_deviation > 0.0
            else np.nan
        ),
        "annualized_downside_deviation": downside_deviation,
        "maximum_drawdown": float((wealth / wealth.cummax() - 1.0).min()),
        "win_rate": float(sample["net_return"].gt(0.0).mean()),
        "total_turnover": float(turnover.sum()),
        "mean_absolute_position": float(sample["position"].abs().mean()),
    }


def recompute_guard_states(inputs: pd.DataFrame) -> pd.DataFrame:
    """Recompute every selected state from the frozen score-date inputs."""

    states = pd.DataFrame(index=inputs.index)
    cold = inputs["date"].dt.month.isin(COLD_MONTHS)
    hdd_guard_season = inputs["date"].dt.month.isin(HDD_GUARD_MONTHS)
    positive_production_risk = inputs[
        "prod_freeze_local_level_score"
    ].gt(0.0)
    states["production_strong"] = (
        cold
        & positive_production_risk
        & inputs["prod_freeze_local_revision_score"].ge(
            STRONG_SCORE_THRESHOLD
        )
    )
    states["production_moderate"] = (
        cold
        & positive_production_risk
        & inputs["prod_freeze_local_revision_score"].ge(
            MODERATE_SCORE_THRESHOLD
        )
    )
    states["hdd_strong"] = hdd_guard_season & inputs[
        "hdd_revision_5d_z"
    ].ge(STRONG_SCORE_THRESHOLD)
    states["hdd_moderate"] = hdd_guard_season & inputs[
        "hdd_revision_5d_z"
    ].ge(MODERATE_SCORE_THRESHOLD)
    states["firm_nongas_strong"] = inputs[
        "central_firm_nongas_shortfall"
    ].ge(FIRM_STRONG_THRESHOLD) | inputs["signal__firm__florida"].ge(
        FIRM_STRONG_THRESHOLD
    )
    states["firm_nongas_moderate"] = inputs[
        "central_firm_nongas_shortfall"
    ].ge(FIRM_MODERATE_THRESHOLD) | inputs["signal__firm__florida"].ge(
        FIRM_MODERATE_THRESHOLD
    )
    states["fast_strong"] = states[
        ["production_strong", "hdd_strong", "firm_nongas_strong"]
    ].any(axis=1)
    states["fast_moderate"] = states[
        ["production_moderate", "hdd_moderate", "firm_nongas_moderate"]
    ].any(axis=1)
    states["low_storage"] = inputs[
        "south_central_total_level_signal"
    ].ge(STRONG_SCORE_THRESHOLD)
    states["storage_amplifier_only"] = (
        ~states["fast_strong"]
        & states["low_storage"]
        & states["fast_moderate"]
    )
    states["fast_plus_storage_amplifier"] = (
        states["fast_strong"] | states["storage_amplifier_only"]
    )
    return states


def validate_score_inputs(inputs: pd.DataFrame) -> pd.DataFrame:
    required = {
        "date",
        "central_firm_nongas_shortfall",
        "signal__firm__florida",
        "south_central_total_level_signal",
        "hdd_revision_5d_z",
        "prod_freeze_local_level_score",
        "prod_freeze_local_revision_score",
        "wind_signal__d1_3",
        SCORE_D1_5,
        SCORE_D1_3,
        "score_without_wind",
        BLOCK,
        SCORE_SELECTED,
    }
    missing = required.difference(inputs.columns)
    if missing:
        raise ValueError(f"Selected score input is missing: {sorted(missing)}")
    if not inputs["date"].is_unique:
        raise ValueError("Selected score input contains duplicate score dates")

    states = recompute_guard_states(inputs)
    for column in states:
        frozen = inputs[f"fast_guard__{column}"].fillna(False).astype(bool)
        if not frozen.equals(states[column].fillna(False).astype(bool)):
            raise AssertionError(f"Frozen guard state does not reproduce: {column}")

    expected_block = (
        states["fast_plus_storage_amplifier"]
        & inputs["wind_signal__d1_3"].lt(0.0)
        & inputs["score_without_wind"].gt(0.0)
        & inputs[SCORE_D1_3].lt(0.0)
    )
    if not expected_block.equals(inputs[BLOCK].astype(bool)):
        raise AssertionError("Frozen D1--3 guard intervention does not reproduce")
    expected_score = inputs[SCORE_D1_3].mask(expected_block, 0.0)
    if not np.allclose(
        expected_score,
        inputs[SCORE_SELECTED],
        equal_nan=True,
        atol=1e-12,
    ):
        raise AssertionError("Frozen D1--3 guarded score does not reproduce")
    return states


def apply_storage_calendar_corrections(
    inputs: pd.DataFrame,
    corrections: pd.DataFrame,
) -> pd.DataFrame:
    """Apply only the audited WNGSR timing delta and recompute the guard."""

    required = {
        "date",
        STORAGE_SCORE_DELTA,
        "legacy_south_central_total_level_signal",
        "corrected_south_central_total_level_signal",
        "production_short_block_active",
    }
    missing = required.difference(corrections.columns)
    if missing:
        raise ValueError(
            f"Storage calendar correction input is missing: {sorted(missing)}"
        )
    if not corrections["date"].is_unique:
        raise ValueError("Storage calendar correction input has duplicate dates")
    unknown = set(corrections["date"]) - set(inputs["date"])
    if unknown:
        raise ValueError(
            "Storage calendar correction dates are absent from score inputs: "
            f"{sorted(unknown)}"
        )

    corrected = inputs.merge(
        corrections,
        on="date",
        how="left",
        validate="one_to_one",
    )
    applied = corrected[STORAGE_SCORE_DELTA].notna()
    legacy_level_matches = np.isclose(
        corrected.loc[applied, "south_central_total_level_signal"],
        corrected.loc[applied, "legacy_south_central_total_level_signal"],
        atol=1e-12,
        rtol=0.0,
        equal_nan=True,
    )
    if not legacy_level_matches.all():
        raise AssertionError(
            "WNGSR correction does not match the frozen legacy storage state"
        )

    corrected["storage_release_calendar_correction_applied"] = applied
    corrected[STORAGE_SCORE_DELTA] = corrected[STORAGE_SCORE_DELTA].fillna(0.0)
    production_block = corrected[
        "production_short_block_active"
    ].fillna(False).astype(bool)
    for column in ("score_without_wind", SCORE_D1_5, SCORE_D1_3):
        corrected[f"legacy_{column}"] = corrected[column]
        adjusted = corrected[column] + corrected[STORAGE_SCORE_DELTA]
        corrected[column] = adjusted.where(
            ~production_block,
            adjusted.clip(lower=0.0),
        )
    corrected["south_central_total_level_signal"] = corrected[
        "corrected_south_central_total_level_signal"
    ].where(
        applied,
        corrected["south_central_total_level_signal"],
    )

    states = recompute_guard_states(corrected)
    for column in states:
        corrected[f"fast_guard__{column}"] = states[column]
    expected_block = (
        states["fast_plus_storage_amplifier"]
        & corrected["wind_signal__d1_3"].lt(0.0)
        & corrected["score_without_wind"].gt(0.0)
        & corrected[SCORE_D1_3].lt(0.0)
    )
    corrected[BLOCK] = expected_block
    corrected[SCORE_SELECTED] = corrected[SCORE_D1_3].mask(
        expected_block,
        0.0,
    )
    validate_score_inputs(corrected)
    return corrected


def build_daily(
    *,
    formal_daily_path: Path,
    score_inputs_path: Path,
    storage_calendar_corrections_path: Path,
    event_reports_path: Path,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    formal = pd.read_parquet(
        formal_daily_path, columns=["date", "roll_adjusted_return"]
    )
    formal["date"] = pd.to_datetime(formal["date"]).dt.normalize()
    formal = filter_confirmed_nymex_sessions(formal).reset_index(drop=True)
    if formal["date"].isin(CONFIRMED_NON_SESSION_DATES).any():
        raise AssertionError("Formal daily input contains a non-session date")
    inputs = pd.read_parquet(score_inputs_path)
    inputs["date"] = pd.to_datetime(inputs["date"]).dt.normalize()
    validate_score_history(inputs, signal_column="signal__firm__florida")
    validate_score_inputs(inputs)
    inputs["florida_available_ba_fallback_score_date"] = inputs[
        "florida_available_ba_count"
    ].lt(9)
    corrections = pd.read_parquet(storage_calendar_corrections_path)
    corrections["date"] = pd.to_datetime(corrections["date"]).dt.normalize()
    inputs = apply_storage_calendar_corrections(inputs, corrections)
    states = recompute_guard_states(inputs)
    for column in states:
        inputs[f"recomputed_guard__{column}"] = states[column]

    daily = formal.merge(inputs, on="date", how="left", validate="one_to_one")
    daily[PRE_D1_5] = daily[SCORE_D1_5].shift(1).clip(-1.0, 1.0)
    daily[PRE_D1_3] = daily[SCORE_D1_3].shift(1).clip(-1.0, 1.0)
    daily[PRE_SELECTED] = daily[SCORE_SELECTED].shift(1).clip(-1.0, 1.0)

    reports = pd.read_parquet(event_reports_path)
    daily, aligned_reports = apply_controller(
        daily,
        reports,
        core_position_column=PRE_D1_5,
        controlled_position_column=POS_D1_5,
    )
    active = daily["shutin_notice_controller_active"].fillna(False)
    daily[POS_D1_3] = daily[PRE_D1_3].where(
        ~(active & daily[PRE_D1_3].lt(0.0)), 0.0
    )
    daily[POS_SELECTED] = daily[PRE_SELECTED].where(
        ~(active & daily[PRE_SELECTED].lt(0.0)), 0.0
    )
    daily["selected_event_veto_applied"] = (
        active & daily[PRE_SELECTED].lt(0.0) & daily[POS_SELECTED].eq(0.0)
    )

    score_complete = daily[[SCORE_D1_5, SCORE_D1_3, SCORE_SELECTED]].notna().all(
        axis=1
    )
    common = score_complete.shift(1, fill_value=False)
    common &= daily["roll_adjusted_return"].notna()
    common &= daily[[POS_D1_5, POS_D1_3, POS_SELECTED]].notna().all(axis=1)
    selected = daily.loc[common].copy().reset_index(drop=True)
    selected["position_source_date"] = daily["date"].shift(1).loc[common].to_numpy()
    selected["guard_blocked_position_date"] = daily[BLOCK].shift(
        1, fill_value=False
    ).loc[common].to_numpy()
    selected["storage_release_calendar_corrected_position_date"] = daily[
        "storage_release_calendar_correction_applied"
    ].shift(1, fill_value=False).loc[common].to_numpy()
    selected["florida_available_ba_fallback_position_date"] = daily[
        "florida_available_ba_fallback_score_date"
    ].shift(1, fill_value=False).loc[common].to_numpy()
    selected["position_source_gas_day_florida"] = daily[
        "source_gas_day_florida"
    ].shift(1).loc[common].to_numpy()
    selected["position_source_florida_available_ba_count"] = daily[
        "florida_available_ba_count"
    ].shift(1).loc[common].to_numpy()
    selected["position_source_florida_respondents"] = daily[
        "florida_respondents"
    ].shift(1).loc[common].to_numpy()
    selected[NET_D1_5] = net_return(
        selected[POS_D1_5], selected["roll_adjusted_return"]
    )
    selected[NET_D1_3] = net_return(
        selected[POS_D1_3], selected["roll_adjusted_return"]
    )
    selected[NET_SELECTED] = net_return(
        selected[POS_SELECTED], selected["roll_adjusted_return"]
    )
    selected["incremental_net_return_vs_d1_5"] = (
        selected[NET_SELECTED] - selected[NET_D1_5]
    )
    selected["incremental_net_return_vs_d1_3"] = (
        selected[NET_SELECTED] - selected[NET_D1_3]
    )
    return selected, aligned_reports


def metrics_tables(
    daily: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    variants = {
        "d1_5_current": (NET_D1_5, POS_D1_5),
        "d1_3_no_guard": (NET_D1_3, POS_D1_3),
        "d1_3_storage_amplified": (NET_SELECTED, POS_SELECTED),
    }
    full_rows: list[dict[str, Any]] = []
    period_rows: list[dict[str, Any]] = []
    annual_rows: list[dict[str, Any]] = []
    for name, (net_column, position_column) in variants.items():
        result = performance(daily[net_column], daily["date"], daily[position_column])
        result["variant"] = name
        full_rows.append(result)
        for period, (start, end) in PERIODS.items():
            mask = daily["date"].between(start, end)
            result = performance(
                daily.loc[mask, net_column],
                daily.loc[mask, "date"],
                daily.loc[mask, position_column],
            )
            result.update({"variant": name, "period": period})
            period_rows.append(result)
        for year, group in daily.groupby(daily["date"].dt.year):
            result = performance(
                group[net_column], group["date"], group[position_column]
            )
            result.update({"variant": name, "year": int(year)})
            annual_rows.append(result)
    return (
        pd.DataFrame(full_rows),
        pd.DataFrame(period_rows),
        pd.DataFrame(annual_rows),
    )


def intervention_summary(daily: pd.DataFrame) -> dict[str, Any]:
    active = daily["guard_blocked_position_date"].fillna(False)
    incremental = daily["incremental_net_return_vs_d1_3"]
    return {
        "intervention_dates": int(active.sum()),
        "helped_dates": int((active & incremental.gt(0.0)).sum()),
        "hurt_dates": int((active & incremental.lt(0.0)).sum()),
        "losses_avoided_or_reduced": float(
            incremental.loc[active & incremental.gt(0.0)].sum()
        ),
        "profits_sacrificed": float(
            -incremental.loc[active & incremental.lt(0.0)].sum()
        ),
        "incremental_net_return_vs_d1_3": float(incremental.sum()),
        "incremental_net_return_vs_d1_5": float(
            daily["incremental_net_return_vs_d1_5"].sum()
        ),
    }


def plot_dashboard(
    daily: pd.DataFrame,
    metrics: pd.DataFrame,
    annual: pd.DataFrame,
    output_path: Path,
) -> Path:
    definitions = {
        "d1_5_current": (NET_D1_5, "Current D1-5", "#8b95a1"),
        "d1_3_no_guard": (NET_D1_3, "D1-3, no guard", "#e8892f"),
        "d1_3_storage_amplified": (
            NET_SELECTED,
            "Selected D1-3 + storage amplifier",
            "#0077b6",
        ),
    }
    wealth: dict[str, pd.Series] = {}
    drawdown: dict[str, pd.Series] = {}
    for name, (net_column, _, _) in definitions.items():
        wealth[name] = (1.0 + daily[net_column]).cumprod()
        drawdown[name] = wealth[name] / wealth[name].cummax() - 1.0

    metric_index = metrics.set_index("variant")
    annual_pivot = annual.pivot(index="year", columns="variant", values="sharpe")
    years = annual_pivot.index.to_numpy()
    cumulative_incremental = daily["incremental_net_return_vs_d1_5"].cumsum() * 100.0
    interventions = daily["guard_blocked_position_date"].fillna(False)

    figure, axes = plt.subplots(2, 2, figsize=(18, 12), constrained_layout=True)
    figure.patch.set_facecolor("white")
    for name, (_, label, color) in definitions.items():
        axes[0, 0].plot(
            daily["date"], wealth[name], color=color, linewidth=2.0,
            label=f"{label} ({wealth[name].iloc[-1]:.2f}x)",
        )
    axes[0, 0].set_title("Cumulative Net Wealth after 2.5 bp Turnover Cost")
    axes[0, 0].set_ylabel("Growth of $1")
    axes[0, 0].legend(loc="upper left")
    axes[0, 0].grid(alpha=0.25)

    for name, (_, label, color) in definitions.items():
        axes[0, 1].plot(
            daily["date"], drawdown[name] * 100.0,
            color=color, linewidth=1.5, label=label,
        )
    axes[0, 1].set_title("Drawdown")
    axes[0, 1].set_ylabel("Drawdown (%)")
    axes[0, 1].legend(loc="lower left")
    axes[0, 1].grid(alpha=0.25)

    x = np.arange(len(years))
    width = 0.26
    for offset, (name, (_, label, color)) in zip(
        (-width, 0.0, width), definitions.items(), strict=True
    ):
        axes[1, 0].bar(
            x + offset,
            annual_pivot[name].to_numpy(),
            width,
            color=color,
            label=label,
        )
    axes[1, 0].axhline(0.0, color="black", linewidth=0.8)
    axes[1, 0].set_xticks(x, years.astype(str), rotation=35)
    axes[1, 0].set_title("Annual Net Sharpe (2019 and 2026 are partial years)")
    axes[1, 0].set_ylabel("Sharpe (zero risk-free rate)")
    axes[1, 0].legend(loc="upper left")
    axes[1, 0].grid(axis="y", alpha=0.25)

    axes[1, 1].plot(
        daily["date"], cumulative_incremental,
        color="#d95f02", linewidth=2.0,
        label="Selected minus current D1-5 net return",
    )
    axes[1, 1].scatter(
        daily.loc[interventions, "date"],
        cumulative_incremental.loc[interventions],
        s=36,
        color="#0077b6",
        edgecolor="white",
        linewidth=0.6,
        label="Storage-amplified wind-flip block",
    )
    axes[1, 1].axhline(0.0, color="#6b7280", linewidth=0.8)
    axes[1, 1].set_title("Selected Strategy Increment versus Current D1-5")
    axes[1, 1].set_ylabel("Cumulative incremental net return (pp)")
    axes[1, 1].legend(loc="upper left")
    axes[1, 1].grid(alpha=0.25)

    current = metric_index.loc["d1_5_current"]
    selected = metric_index.loc["d1_3_storage_amplified"]
    figure.suptitle(
        "Henry Hub Strategy Dashboard — Selected D1-3 Wind + Storage Amplifier\n"
        f"Net Sharpe {current['sharpe']:.3f} current → "
        f"{selected['sharpe']:.3f} selected  |  "
        f"Sortino {current['sortino']:.3f} → {selected['sortino']:.3f}",
        fontsize=18,
        fontweight="bold",
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output_path, dpi=170, bbox_inches="tight", facecolor="white")
    plt.close(figure)
    return output_path


def run(
    *,
    formal_daily_path: Path = FORMAL_DAILY,
    score_inputs_path: Path = SCORE_INPUTS,
    storage_calendar_corrections_path: Path = STORAGE_CALENDAR_CORRECTIONS,
    event_reports_path: Path = DEFAULT_EVENT_REPORTS_PATH,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> dict[str, Any]:
    daily, aligned_reports = build_daily(
        formal_daily_path=formal_daily_path,
        score_inputs_path=score_inputs_path,
        storage_calendar_corrections_path=storage_calendar_corrections_path,
        event_reports_path=event_reports_path,
    )
    metrics, periods, annual = metrics_tables(daily)
    interventions = intervention_summary(daily)
    output_dir.mkdir(parents=True, exist_ok=True)
    daily.to_parquet(
        output_dir / "selected_strategy_daily.parquet",
        index=False,
        compression="zstd",
    )
    metrics.to_csv(output_dir / "strategy_metrics.csv", index=False)
    periods.to_csv(output_dir / "period_metrics.csv", index=False)
    annual.to_csv(output_dir / "annual_metrics.csv", index=False)
    aligned_reports.to_parquet(
        output_dir / "event_report_registry.parquet",
        index=False,
        compression="zstd",
    )
    dashboard = plot_dashboard(
        daily,
        metrics,
        annual,
        output_dir / "latest_strategy_dashboard.png",
    )
    metric_index = metrics.set_index("variant")
    current = metric_index.loc["d1_5_current"].to_dict()
    d1_3 = metric_index.loc["d1_3_no_guard"].to_dict()
    selected = metric_index.loc["d1_3_storage_amplified"].to_dict()
    summary = {
        "strategy_version": "d1_3_wind_storage_amplified_hdd_guard",
        "selection_status": (
            "HDD month gate selected by user on 2026-08-13; historical "
            "results are retrospective validation"
        ),
        "sample_start": daily["date"].min(),
        "sample_end": daily["date"].max(),
        "trading_days": len(daily),
        "transaction_cost_bps": TRANSACTION_COST_BPS,
        "wind_horizon": "forecast days 1-3, equally weighted",
        "storage_role": "amplifier only; never a standalone trigger",
        "weather_revision_guard": (
            "HDD revision in every month except June-August; no CDD branch"
        ),
        "storage_release_alignment": (
            "actual EIA WNGSR publication date with audited holiday exceptions"
        ),
        "storage_release_calendar_corrected_score_dates": int(
            daily["storage_release_calendar_correction_applied"].sum()
        ),
        "storage_release_calendar_corrected_position_dates": int(
            daily[
                "storage_release_calendar_corrected_position_date"
            ].sum()
        ),
        "florida_available_ba_policy": (
            "aggregate the complete Florida BAs on each source day into one "
            "continuous rolling history; partial-BA observations remain in "
            "the reference history used by later dates"
        ),
        "florida_available_ba_fallback_position_dates": int(
            daily["florida_available_ba_fallback_position_date"].sum()
        ),
        "florida_minimum_available_ba_count": int(
            daily["position_source_florida_available_ba_count"].min()
        ),
        "guard_action": (
            "set a wind-flipped negative score to zero; never create or amplify exposure"
        ),
        "current_d1_5_metrics": current,
        "d1_3_no_guard_metrics": d1_3,
        "selected_metrics": selected,
        "change_vs_current_d1_5": {
            "sharpe": selected["sharpe"] - current["sharpe"],
            "sortino": selected["sortino"] - current["sortino"],
            "cagr": selected["cagr"] - current["cagr"],
            "cumulative_incremental_net_return": float(
                daily["incremental_net_return_vs_d1_5"].sum()
            ),
        },
        "intervention_summary": interventions,
        "selected_event_veto_days": int(
            daily["selected_event_veto_applied"].sum()
        ),
        "dashboard": str(
            dashboard.relative_to(PROJECT_ROOT)
            if dashboard.is_relative_to(PROJECT_ROOT)
            else dashboard
        ),
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True, default=json_default) + "\n",
        encoding="utf-8",
    )
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--formal-daily", type=Path, default=FORMAL_DAILY)
    parser.add_argument("--score-inputs", type=Path, default=SCORE_INPUTS)
    parser.add_argument(
        "--storage-calendar-corrections",
        type=Path,
        default=STORAGE_CALENDAR_CORRECTIONS,
    )
    parser.add_argument(
        "--event-reports", type=Path, default=DEFAULT_EVENT_REPORTS_PATH
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    result = run(
        formal_daily_path=arguments.formal_daily,
        score_inputs_path=arguments.score_inputs,
        storage_calendar_corrections_path=(
            arguments.storage_calendar_corrections
        ),
        event_reports_path=arguments.event_reports,
        output_dir=arguments.output_dir,
    )
    print(json.dumps(result, indent=2, sort_keys=True, default=json_default))
