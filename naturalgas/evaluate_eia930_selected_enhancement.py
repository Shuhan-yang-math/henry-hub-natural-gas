#!/usr/bin/env python3
"""Build the selected EIA-930 Central 40% / Florida 60% enhancement.

The selected version preserves the existing GFS wind and solar factors and
funds one fixed 10% EIA-930 slot from the fundamental allocation.  Forty
percent of that slot uses the ERCOT/MISO/SPP total non-gas shortfall and 60%
uses Florida firm non-gas generation relative to demand.  The existing
production-risk state and BSEE/Sabine pure short-veto controller are retained.
Only tracked strategy and audit artifacts are read by default.
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

from naturalgas.shutin_notice_event_controller import (
    CONTROLLED_POSITION_COLUMN,
    DEFAULT_EVENT_REPORTS_PATH,
    apply_controller,
)
from naturalgas.audit_inputs import (
    EIA930_OVERLAY_ARTIFACT_ID,
    EVENT_REPORTS_ARTIFACT_ID,
    audit_input_path,
    resolve_audit_inputs,
)
from naturalgas.nymex_session_calendar import filter_confirmed_nymex_sessions
from naturalgas.eia930_florida_availability import validate_score_history
from naturalgas.sync_documentation_metrics import (
    synchronize_after_canonical_result,
)


FORMAL_DAILY = (
    PROJECT_ROOT
    / "naturalgas/processed/south_central_storage_strategy/strategy_daily.parquet"
)
OVERLAY_INPUTS = audit_input_path(EIA930_OVERLAY_ARTIFACT_ID)
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "results/experiments/eia930_selected"

CORE_SCORE = "score__replace_all_storage__south_central_total"
CORE_FUNDAMENTAL = "fundamental__replace_all_storage__south_central_total"
CORE_POSITION = "position__replace_all_storage__south_central_total"
CENTRAL_SIGNAL = "central_total_nongas_shortfall"
FLORIDA_SIGNAL = "florida_firm_nongas_share_shortfall"
SELECTED_SIGNAL = "central40_florida60_shortfall"
BASE_NET_RETURN = "net_return__current_gfs_with_veto"
TRANSACTION_COST_BPS = 2.5
EIA930_SLOT_WEIGHT = 0.10
CENTRAL_SHARE = 0.40
FLORIDA_SHARE = 0.60
FLORIDA_WEIGHT_GRID = tuple(np.round(np.linspace(0.0, 1.0, 11), 2))


def weight_suffix(florida_weight: float) -> str:
    central_pct = int(round(100 * (1.0 - florida_weight)))
    florida_pct = int(round(100 * florida_weight))
    return f"central{central_pct:03d}_florida{florida_pct:03d}"


def score_column(florida_weight: float) -> str:
    return f"score__eia930_{weight_suffix(florida_weight)}_10pct"


def pre_veto_column(florida_weight: float) -> str:
    return f"position_pre_veto__eia930_{weight_suffix(florida_weight)}_10pct"


def position_column(florida_weight: float) -> str:
    return f"position__eia930_{weight_suffix(florida_weight)}_10pct"


def net_return_column(florida_weight: float) -> str:
    return f"net_return__eia930_{weight_suffix(florida_weight)}_10pct"


CURRENT_CENTRAL_POSITION = position_column(0.0)
SELECTED_PRE_VETO = pre_veto_column(FLORIDA_SHARE)
SELECTED_POSITION = position_column(FLORIDA_SHARE)
CURRENT_CENTRAL_NET_RETURN = net_return_column(0.0)
SELECTED_NET_RETURN = net_return_column(FLORIDA_SHARE)


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
    return (
        position * futures_return
        - turnover * TRANSACTION_COST_BPS / 10_000.0
    )


def performance(net: pd.Series, dates: pd.Series, position: pd.Series) -> dict[str, Any]:
    sample = pd.DataFrame(
        {"date": dates, "net_return": net, "position": position}
    ).dropna().sort_values("date")
    if len(sample) < 2:
        raise ValueError("At least two observations are required")
    log_return = np.log1p(sample["net_return"])
    wealth = (1.0 + sample["net_return"]).cumprod()
    years = max(
        (sample["date"].iloc[-1] - sample["date"].iloc[0]).days / 365.2425,
        1.0 / 252.0,
    )
    volatility = float(log_return.std(ddof=1))
    downside = log_return.clip(upper=0.0)
    downside_deviation = float(
        np.sqrt(np.square(downside).mean()) * np.sqrt(252.0)
    )
    annualized_log_return = float(log_return.mean() * 252.0)
    turnover = sample["position"].diff().abs().fillna(sample["position"].abs())
    return {
        "trading_days": len(sample),
        "start": sample["date"].iloc[0],
        "end": sample["date"].iloc[-1],
        "total_return": float(wealth.iloc[-1] - 1.0),
        "cagr": float(np.exp(log_return.sum() / years) - 1.0),
        "sharpe": float(log_return.mean() / volatility * np.sqrt(252.0)),
        "sortino": (
            annualized_log_return / downside_deviation
            if downside_deviation > 0.0
            else np.nan
        ),
        "annualized_downside_deviation": downside_deviation,
        "maximum_drawdown": float((wealth / wealth.cummax() - 1.0).min()),
        "win_rate": float(sample["net_return"].gt(0.0).mean()),
        "total_turnover": float(turnover.sum()),
        "mean_absolute_position": float(sample["position"].abs().mean()),
    }


def build_daily(
    *,
    formal_daily_path: Path,
    overlay_inputs_path: Path,
    event_reports_path: Path,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    audit_paths = resolve_audit_inputs({
        EIA930_OVERLAY_ARTIFACT_ID: overlay_inputs_path,
        EVENT_REPORTS_ARTIFACT_ID: event_reports_path,
    })
    overlay_inputs_path = audit_paths[EIA930_OVERLAY_ARTIFACT_ID]
    event_reports_path = audit_paths[EVENT_REPORTS_ARTIFACT_ID]
    formal = pd.read_parquet(formal_daily_path)
    formal["date"] = pd.to_datetime(formal["date"]).dt.normalize()
    formal = filter_confirmed_nymex_sessions(formal).reset_index(drop=True)
    overlay = pd.read_parquet(overlay_inputs_path)
    overlay["date"] = pd.to_datetime(overlay["date"]).dt.normalize()
    validate_score_history(
        overlay,
        signal_column=FLORIDA_SIGNAL,
    )
    overlay["florida_available_ba_fallback_score_date"] = overlay[
        "florida_available_ba_count"
    ].lt(9)
    for column in ("source_gas_day_central", "source_gas_day_florida"):
        overlay[column] = pd.to_datetime(overlay[column]).dt.normalize()
    reports = pd.read_parquet(event_reports_path)

    required_formal = {
        "date",
        "roll_adjusted_return",
        CORE_SCORE,
        CORE_FUNDAMENTAL,
        CORE_POSITION,
    }
    missing_formal = required_formal.difference(formal.columns)
    if missing_formal:
        raise ValueError(f"Formal strategy input is missing: {sorted(missing_formal)}")
    required_overlay = {
        "date",
        "source_gas_day_central",
        "source_gas_day_florida",
        CENTRAL_SIGNAL,
        FLORIDA_SIGNAL,
        "production_short_block_active",
    }
    missing_overlay = required_overlay.difference(overlay.columns)
    if missing_overlay:
        raise ValueError(f"EIA-930 overlay input is missing: {sorted(missing_overlay)}")

    if not overlay["date"].is_unique:
        raise ValueError("EIA-930 overlay input contains duplicate score dates")
    for column in ("source_gas_day_central", "source_gas_day_florida"):
        if not (overlay[column] < overlay["date"]).all():
            raise ValueError(f"{column} must strictly precede its score date")

    daily = formal.merge(overlay, on="date", how="left", validate="one_to_one")
    daily, aligned_reports = apply_controller(
        daily,
        reports,
        core_position_column=CORE_POSITION,
    )
    daily[SELECTED_SIGNAL] = (
        CENTRAL_SHARE * daily[CENTRAL_SIGNAL]
        + FLORIDA_SHARE * daily[FLORIDA_SIGNAL]
    )
    short_block = daily["production_short_block_active"].fillna(False).astype(bool)
    active = daily["shutin_notice_controller_active"].fillna(False).astype(bool)

    for florida_weight in FLORIDA_WEIGHT_GRID:
        blended_signal = (
            (1.0 - florida_weight) * daily[CENTRAL_SIGNAL]
            + florida_weight * daily[FLORIDA_SIGNAL]
        )
        candidate_score = daily[CORE_SCORE] + EIA930_SLOT_WEIGHT * (
            blended_signal - daily[CORE_FUNDAMENTAL]
        )
        candidate_score = candidate_score.where(
            ~short_block,
            candidate_score.clip(lower=0.0),
        )
        score_name = score_column(florida_weight)
        pre_veto_name = pre_veto_column(florida_weight)
        position_name = position_column(florida_weight)
        daily[score_name] = candidate_score
        daily[pre_veto_name] = candidate_score.shift(1).clip(-1.0, 1.0)
        daily[position_name] = daily[pre_veto_name].where(
            ~(active & daily[pre_veto_name].lt(0.0)),
            0.0,
        )

    daily["selected_veto_applied"] = (
        active
        & daily[SELECTED_PRE_VETO].lt(0.0)
        & daily[SELECTED_POSITION].eq(0.0)
    )
    signal_complete = daily[[CENTRAL_SIGNAL, FLORIDA_SIGNAL]].notna().all(axis=1)
    common = signal_complete.shift(1, fill_value=False)
    common &= daily["roll_adjusted_return"].notna()
    common &= daily[CONTROLLED_POSITION_COLUMN].notna()
    for florida_weight in FLORIDA_WEIGHT_GRID:
        common &= daily[position_column(florida_weight)].notna()
    selected = daily.loc[common].copy().reset_index(drop=True)
    selected["position_source_gas_day_central"] = daily[
        "source_gas_day_central"
    ].shift(1).loc[common].to_numpy()
    selected["position_source_gas_day_florida"] = daily[
        "source_gas_day_florida"
    ].shift(1).loc[common].to_numpy()
    selected["florida_available_ba_fallback_position_date"] = daily[
        "florida_available_ba_fallback_score_date"
    ].shift(1, fill_value=False).loc[common].to_numpy()
    selected["position_source_florida_available_ba_count"] = daily[
        "florida_available_ba_count"
    ].shift(1).loc[common].to_numpy()
    selected["position_source_florida_respondents"] = daily[
        "florida_respondents"
    ].shift(1).loc[common].to_numpy()
    selected[BASE_NET_RETURN] = net_return(
        selected[CONTROLLED_POSITION_COLUMN],
        selected["roll_adjusted_return"],
    )
    for florida_weight in FLORIDA_WEIGHT_GRID:
        selected[net_return_column(florida_weight)] = net_return(
            selected[position_column(florida_weight)],
            selected["roll_adjusted_return"],
        )
    selected["incremental_net_return_vs_baseline"] = (
        selected[SELECTED_NET_RETURN] - selected[BASE_NET_RETURN]
    )
    selected["incremental_net_return_vs_central"] = (
        selected[SELECTED_NET_RETURN] - selected[CURRENT_CENTRAL_NET_RETURN]
    )
    return selected, aligned_reports


def annual_metrics(daily: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    versions = {
        "current_gfs_with_veto": (
            BASE_NET_RETURN,
            CONTROLLED_POSITION_COLUMN,
        ),
        "eia930_central_100pct": (
            CURRENT_CENTRAL_NET_RETURN,
            CURRENT_CENTRAL_POSITION,
        ),
        "eia930_central40_florida60": (
            SELECTED_NET_RETURN,
            SELECTED_POSITION,
        ),
    }
    for year, year_frame in daily.groupby(daily["date"].dt.year):
        for version, (net_column, position_column) in versions.items():
            metrics = performance(
                year_frame[net_column],
                year_frame["date"],
                year_frame[position_column],
            )
            metrics.update({"year": int(year), "version": version})
            rows.append(metrics)
    return pd.DataFrame(rows)


def weight_sweep_metrics(daily: pd.DataFrame) -> pd.DataFrame:
    period_bounds = {
        "development_2019_2020": ("1900-01-01", "2020-12-31"),
        "validation_2021_2023": ("2021-01-01", "2023-12-31"),
        "first_look_2024_plus": ("2024-01-01", "2100-01-01"),
        "2025": ("2025-01-01", "2025-12-31"),
        "2026_ytd": ("2026-01-01", "2026-12-31"),
    }
    current_metrics = performance(
        daily[CURRENT_CENTRAL_NET_RETURN],
        daily["date"],
        daily[CURRENT_CENTRAL_POSITION],
    )
    rows: list[dict[str, Any]] = []
    for florida_weight in FLORIDA_WEIGHT_GRID:
        net_column = net_return_column(florida_weight)
        position_name = position_column(florida_weight)
        full = performance(daily[net_column], daily["date"], daily[position_name])
        row: dict[str, Any] = {
            "central_weight": 1.0 - florida_weight,
            "florida_weight": florida_weight,
            "trading_days": full["trading_days"],
            "start": full["start"],
            "end": full["end"],
            "full_sharpe": full["sharpe"],
            "full_sortino": full["sortino"],
            "full_cagr": full["cagr"],
            "full_maximum_drawdown": full["maximum_drawdown"],
            "sharpe_delta_vs_current_central": (
                full["sharpe"] - current_metrics["sharpe"]
            ),
            "cumulative_incremental_net_return_vs_current_central": float(
                (
                    daily[net_column]
                    - daily[CURRENT_CENTRAL_NET_RETURN]
                ).sum()
            ),
        }
        for period, (start, end) in period_bounds.items():
            mask = daily["date"].between(start, end)
            metrics = performance(
                daily.loc[mask, net_column],
                daily.loc[mask, "date"],
                daily.loc[mask, position_name],
            )
            row[f"{period}__sharpe"] = metrics["sharpe"]
            row[f"{period}__sortino"] = metrics["sortino"]
            row[f"{period}__total_return"] = metrics["total_return"]
        rows.append(row)
    return pd.DataFrame(rows)


def plot_weight_sweep(sweep: pd.DataFrame, output_path: Path) -> Path:
    florida_weight = sweep["florida_weight"] * 100.0
    figure, axes = plt.subplots(1, 2, figsize=(15, 5.5), constrained_layout=True)
    for column, label in [
        ("full_sharpe", "Full sample"),
        ("development_2019_2020__sharpe", "Development 2019-2020"),
        ("validation_2021_2023__sharpe", "Validation 2021-2023"),
        ("first_look_2024_plus__sharpe", "First look 2024+"),
    ]:
        axes[0].plot(florida_weight, sweep[column], marker="o", label=label)
    axes[0].axvline(60.0, color="#0077b6", linestyle="--", linewidth=1.2)
    axes[0].set_title("Central / Florida Weight Stability")
    axes[0].set_xlabel("Florida weight inside the fixed 10% slot (%)")
    axes[0].set_ylabel("Net Sharpe")
    axes[0].grid(alpha=0.25)
    axes[0].legend()

    axes[1].plot(
        florida_weight,
        sweep["full_sortino"],
        marker="o",
        label="Full Sortino",
    )
    axes[1].plot(
        florida_weight,
        sweep["full_maximum_drawdown"].abs() * 100.0,
        marker="o",
        label="Maximum drawdown magnitude (%)",
    )
    axes[1].plot(
        florida_weight,
        sweep["cumulative_incremental_net_return_vs_current_central"] * 100.0,
        marker="o",
        label="Incremental net return vs Central (pp)",
    )
    axes[1].axvline(60.0, color="#0077b6", linestyle="--", linewidth=1.2)
    axes[1].axhline(0.0, color="#6b7280", linewidth=0.8)
    axes[1].set_title("Risk and Return Trade-off")
    axes[1].set_xlabel("Florida weight inside the fixed 10% slot (%)")
    axes[1].grid(alpha=0.25)
    axes[1].legend()

    figure.suptitle(
        "EIA-930 Central / Florida Fixed-Weight Audit — No Added Leverage",
        fontsize=15,
        fontweight="bold",
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output_path, dpi=170, bbox_inches="tight", facecolor="white")
    plt.close(figure)
    return output_path


def loss_day_diagnostics(daily: pd.DataFrame) -> tuple[dict[str, Any], pd.DataFrame]:
    frame = daily[
        [
            "date",
            "roll_adjusted_return",
            CURRENT_CENTRAL_POSITION,
            SELECTED_POSITION,
            CURRENT_CENTRAL_NET_RETURN,
            SELECTED_NET_RETURN,
        ]
    ].copy()
    frame["incremental_net_return"] = (
        frame[SELECTED_NET_RETURN] - frame[CURRENT_CENTRAL_NET_RETURN]
    )
    losses = frame.loc[frame[CURRENT_CENTRAL_NET_RETURN].lt(0.0)].copy()
    losses["gross_increment"] = (
        losses[SELECTED_POSITION] - losses[CURRENT_CENTRAL_POSITION]
    ) * losses["roll_adjusted_return"]
    losses["cost_increment"] = (
        losses["incremental_net_return"] - losses["gross_increment"]
    )
    same_sign = np.sign(losses[CURRENT_CENTRAL_POSITION]).eq(
        np.sign(losses[SELECTED_POSITION])
    )
    reduced = same_sign & losses[SELECTED_POSITION].abs().lt(
        losses[CURRENT_CENTRAL_POSITION].abs()
    )
    increased = same_sign & losses[SELECTED_POSITION].abs().gt(
        losses[CURRENT_CENTRAL_POSITION].abs()
    )
    flipped = (
        ~same_sign
        & losses[CURRENT_CENTRAL_POSITION].ne(0.0)
        & losses[SELECTED_POSITION].ne(0.0)
    )
    losses["position_change_class"] = np.select(
        [reduced, increased, flipped],
        ["same_direction_reduced", "same_direction_increased", "direction_flipped"],
        default="other_or_zero",
    )
    yearly = (
        losses.groupby(losses["date"].dt.year)
        .agg(
            loss_days=("date", "size"),
            central_net_return=(CURRENT_CENTRAL_NET_RETURN, "sum"),
            selected_net_return=(SELECTED_NET_RETURN, "sum"),
            incremental_net_return=("incremental_net_return", "sum"),
            improved_share=(
                "incremental_net_return",
                lambda values: float(values.gt(0.0).mean()),
            ),
        )
        .reset_index(names="year")
    )
    nonloss = frame.loc[frame[CURRENT_CENTRAL_NET_RETURN].ge(0.0)]
    summary = {
        "central_loss_days": len(losses),
        "improved_loss_days": int(losses["incremental_net_return"].gt(0.0).sum()),
        "worsened_loss_days": int(losses["incremental_net_return"].lt(0.0).sum()),
        "loss_days_flipped_nonnegative": int(
            losses[SELECTED_NET_RETURN].ge(0.0).sum()
        ),
        "central_loss_day_net_return": float(
            losses[CURRENT_CENTRAL_NET_RETURN].sum()
        ),
        "selected_on_central_loss_day_net_return": float(
            losses[SELECTED_NET_RETURN].sum()
        ),
        "loss_day_incremental_net_return": float(
            losses["incremental_net_return"].sum()
        ),
        "nonloss_day_incremental_net_return": float(
            (
                nonloss[SELECTED_NET_RETURN]
                - nonloss[CURRENT_CENTRAL_NET_RETURN]
            ).sum()
        ),
        "mean_absolute_position_on_loss_days": {
            "central": float(losses[CURRENT_CENTRAL_POSITION].abs().mean()),
            "selected": float(losses[SELECTED_POSITION].abs().mean()),
        },
        "gross_position_increment_on_loss_days": float(
            losses["gross_increment"].sum()
        ),
        "cost_increment_on_loss_days": float(losses["cost_increment"].sum()),
        "position_change_contribution": {
            label: {
                "days": int(len(group)),
                "incremental_net_return": float(
                    group["incremental_net_return"].sum()
                ),
            }
            for label, group in losses.groupby("position_change_class")
        },
    }
    return summary, yearly


def plot_dashboard(
    daily: pd.DataFrame,
    annual: pd.DataFrame,
    base_metrics: dict[str, Any],
    central_metrics: dict[str, Any],
    selected_metrics: dict[str, Any],
    output_path: Path,
) -> Path:
    base_wealth = (1.0 + daily[BASE_NET_RETURN]).cumprod()
    central_wealth = (1.0 + daily[CURRENT_CENTRAL_NET_RETURN]).cumprod()
    selected_wealth = (1.0 + daily[SELECTED_NET_RETURN]).cumprod()
    base_drawdown = base_wealth / base_wealth.cummax() - 1.0
    central_drawdown = central_wealth / central_wealth.cummax() - 1.0
    selected_drawdown = selected_wealth / selected_wealth.cummax() - 1.0
    cumulative_incremental = (
        daily["incremental_net_return_vs_central"].cumsum() * 100.0
    )

    annual_pivot = annual.pivot(
        index="year", columns="version", values="sharpe"
    ).sort_index()
    years = annual_pivot.index.to_numpy()
    base_annual = annual_pivot["current_gfs_with_veto"].to_numpy()
    central_annual = annual_pivot["eia930_central_100pct"].to_numpy()
    selected_annual = annual_pivot["eia930_central40_florida60"].to_numpy()
    veto = daily["selected_veto_applied"].fillna(False)
    veto_counts = daily.loc[veto, "date"].dt.year.value_counts().sort_index()
    veto_summary = ", ".join(
        f"{count} in {year}" for year, count in veto_counts.items()
    )

    figure, axes = plt.subplots(2, 2, figsize=(18, 12), constrained_layout=True)
    figure.patch.set_facecolor("white")
    figure.suptitle(
        "Henry Hub Strategy Dashboard — EIA-930 Central 40% / Florida 60%\n"
        f"Net Sharpe: {base_metrics['sharpe']:.3f} core → "
        f"{central_metrics['sharpe']:.3f} Central → "
        f"{selected_metrics['sharpe']:.3f} selected  |  "
        f"Selected Sortino: {selected_metrics['sortino']:.3f}",
        fontsize=18,
        fontweight="bold",
    )

    axes[0, 0].plot(
        daily["date"], base_wealth, color="#6b7280", linewidth=1.8,
        label=f"Current GFS + veto ({base_wealth.iloc[-1]:.2f}x)",
    )
    axes[0, 0].plot(
        daily["date"], central_wealth, color="#e8892f", linewidth=1.7,
        label=f"10% Central only ({central_wealth.iloc[-1]:.2f}x)",
    )
    axes[0, 0].plot(
        daily["date"], selected_wealth, color="#0077b6", linewidth=2.2,
        label=(
            "Selected 10% slot: 40% Central / 60% Florida "
            f"({selected_wealth.iloc[-1]:.2f}x)"
        ),
    )
    axes[0, 0].set_title("Cumulative Net Wealth after 2.5 bp Turnover Cost")
    axes[0, 0].set_ylabel("Growth of $1")
    axes[0, 0].legend(loc="upper left")
    axes[0, 0].grid(alpha=0.25)

    axes[0, 1].fill_between(
        daily["date"], base_drawdown * 100.0, 0.0,
        color="#9ca3af", alpha=0.35, label="Current GFS + veto",
    )
    axes[0, 1].plot(
        daily["date"], central_drawdown * 100.0,
        color="#e8892f", linewidth=1.2, label="10% Central only",
    )
    axes[0, 1].plot(
        daily["date"], selected_drawdown * 100.0,
        color="#0077b6", linewidth=1.6,
        label="Selected Central 40% / Florida 60%",
    )
    axes[0, 1].set_title("Drawdown")
    axes[0, 1].set_ylabel("Drawdown (%)")
    axes[0, 1].legend(loc="lower left")
    axes[0, 1].grid(alpha=0.25)

    x = np.arange(len(years))
    width = 0.26
    axes[1, 0].bar(
        x - width, base_annual, width,
        color="#8b95a1", label="Current GFS + veto",
    )
    axes[1, 0].bar(
        x, central_annual, width,
        color="#e8892f", label="10% Central only",
    )
    axes[1, 0].bar(
        x + width, selected_annual, width,
        color="#1683b6", label="Selected Central 40% / Florida 60%",
    )
    annual_min = float(np.nanmin([base_annual, central_annual, selected_annual]))
    annual_max = float(np.nanmax([base_annual, central_annual, selected_annual]))
    axes[1, 0].set_ylim(min(-0.70, annual_min - 0.15), annual_max + 0.35)
    for index, (central_value, selected_value) in enumerate(
        zip(central_annual, selected_annual, strict=True)
    ):
        delta = selected_value - central_value
        anchor = max(central_value, selected_value)
        axes[1, 0].text(
            index,
            anchor + 0.06,
            f"{delta:+.3f}",
            ha="center",
            va="bottom",
            fontsize=9,
            color="#d95f02" if delta >= 0.0 else "#9b2226",
            fontweight="bold",
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
        label="Cumulative selected-minus-Central net return",
    )
    axes[1, 1].scatter(
        daily.loc[veto, "date"], cumulative_incremental.loc[veto],
        color="#d95f02", edgecolor="white", linewidth=0.7,
        s=55, zorder=4, label="Actual selected-strategy short-veto date",
    )
    axes[1, 1].axhline(0.0, color="#6b7280", linewidth=0.8)
    axes[1, 1].set_title("40/60 Contribution versus Central and Event-Veto Dates")
    axes[1, 1].set_ylabel("Cumulative incremental net return (pp)")
    axes[1, 1].legend(loc="upper left")
    axes[1, 1].grid(alpha=0.25)
    axes[1, 1].text(
        0.02,
        0.04,
        f"{int(veto.sum())} veto dates: {veto_summary}",
        transform=axes[1, 1].transAxes,
        fontsize=10,
        bbox={
            "boxstyle": "round,pad=0.25",
            "facecolor": "white",
            "alpha": 0.8,
            "edgecolor": "#d1d5db",
        },
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output_path, dpi=170, bbox_inches="tight", facecolor="white")
    plt.close(figure)
    return output_path


def run(
    *,
    formal_daily_path: Path,
    overlay_inputs_path: Path,
    event_reports_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    daily, aligned_reports = build_daily(
        formal_daily_path=formal_daily_path,
        overlay_inputs_path=overlay_inputs_path,
        event_reports_path=event_reports_path,
    )
    base_metrics = performance(
        daily[BASE_NET_RETURN], daily["date"], daily[CONTROLLED_POSITION_COLUMN]
    )
    central_metrics = performance(
        daily[CURRENT_CENTRAL_NET_RETURN],
        daily["date"],
        daily[CURRENT_CENTRAL_POSITION],
    )
    selected_metrics = performance(
        daily[SELECTED_NET_RETURN], daily["date"], daily[SELECTED_POSITION]
    )
    annual = annual_metrics(daily)
    sweep = weight_sweep_metrics(daily)
    loss_day_summary, loss_day_yearly = loss_day_diagnostics(daily)
    output_dir.mkdir(parents=True, exist_ok=True)
    daily.to_parquet(
        output_dir / "selected_strategy_daily.parquet",
        index=False,
        compression="zstd",
    )
    annual.to_csv(output_dir / "annual_metrics.csv", index=False)
    sweep.to_csv(output_dir / "central_florida_weight_sweep.csv", index=False)
    loss_day_yearly.to_csv(output_dir / "loss_day_yearly.csv", index=False)
    aligned_reports.to_parquet(
        output_dir / "event_report_registry.parquet",
        index=False,
        compression="zstd",
    )
    dashboard = plot_dashboard(
        daily,
        annual,
        base_metrics,
        central_metrics,
        selected_metrics,
        output_dir / "latest_strategy_dashboard.png",
    )
    weight_sweep_chart = plot_weight_sweep(
        sweep,
        output_dir / "central_florida_weight_sweep.png",
    )
    summary = {
        "strategy_version": "eia930_central40_florida60_10pct_with_event_veto",
        "sample_start": daily["date"].min(),
        "sample_end": daily["date"].max(),
        "trading_days": len(daily),
        "transaction_cost_bps": TRANSACTION_COST_BPS,
        "eia930_slot_weight": EIA930_SLOT_WEIGHT,
        "central_share_within_slot": CENTRAL_SHARE,
        "florida_share_within_slot": FLORIDA_SHARE,
        "florida_available_ba_policy": (
            "aggregate the complete Florida BAs on each source day into one "
            "continuous rolling history"
        ),
        "florida_available_ba_fallback_position_dates": int(
            daily["florida_available_ba_fallback_position_date"].sum()
        ),
        "florida_minimum_available_ba_count": int(
            daily["position_source_florida_available_ba_count"].min()
        ),
        "central_respondents": ["ERCO", "MISO", "SWPP"],
        "florida_respondents": [
            "FMPP", "FPC", "FPL", "GVL", "HST", "JEA", "SEC", "TAL", "TEC"
        ],
        "signals": {
            "central": "continuous total non-gas generation shortfall / demand",
            "florida": "continuous coal+nuclear+water shortfall / demand",
        },
        "baseline_metrics": base_metrics,
        "current_central_metrics": central_metrics,
        "selected_metrics": selected_metrics,
        "change_vs_baseline": {
            "sharpe": selected_metrics["sharpe"] - base_metrics["sharpe"],
            "sortino": selected_metrics["sortino"] - base_metrics["sortino"],
            "cagr": selected_metrics["cagr"] - base_metrics["cagr"],
            "cumulative_incremental_net_return": float(
                daily["incremental_net_return_vs_baseline"].sum()
            ),
        },
        "change_vs_current_central": {
            "sharpe": selected_metrics["sharpe"] - central_metrics["sharpe"],
            "sortino": selected_metrics["sortino"] - central_metrics["sortino"],
            "cagr": selected_metrics["cagr"] - central_metrics["cagr"],
            "cumulative_incremental_net_return": float(
                daily["incremental_net_return_vs_central"].sum()
            ),
        },
        "loss_day_diagnostics": loss_day_summary,
        "selected_event_veto_days": int(daily["selected_veto_applied"].sum()),
        "dashboard": str(
            dashboard.relative_to(PROJECT_ROOT)
            if dashboard.is_relative_to(PROJECT_ROOT)
            else dashboard
        ),
        "weight_sweep_chart": str(
            weight_sweep_chart.relative_to(PROJECT_ROOT)
            if weight_sweep_chart.is_relative_to(PROJECT_ROOT)
            else weight_sweep_chart
        ),
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, default=json_default, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    synchronize_after_canonical_result(
        output_dir=output_dir,
        canonical_output_dir=DEFAULT_OUTPUT_DIR,
        root=PROJECT_ROOT,
    )
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--formal-daily", type=Path, default=FORMAL_DAILY)
    parser.add_argument("--overlay-inputs", type=Path, default=OVERLAY_INPUTS)
    parser.add_argument(
        "--event-reports", type=Path, default=DEFAULT_EVENT_REPORTS_PATH
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    result = run(
        formal_daily_path=args.formal_daily,
        overlay_inputs_path=args.overlay_inputs,
        event_reports_path=args.event_reports,
        output_dir=args.output_dir,
    )
    print(json.dumps(result, default=json_default, indent=2, sort_keys=True))
