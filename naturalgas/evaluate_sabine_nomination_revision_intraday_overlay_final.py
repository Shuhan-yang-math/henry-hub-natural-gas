#!/usr/bin/env python3
"""Build the final research record for the Sabine nomination-revision overlay.

This experiment is deliberately separate from formal model V03.  It freezes the
previously researched dominant Intraday-3 nomination-revision signal, validates
the archived execution mapping, and writes a new set of final research artifacts
without modifying the earlier exploratory outputs.

The selected overlay combines two physically distinct revisions:

* TransCameron LNG delivery: Intraday 1 to Intraday 3; and
* Jefferson Island storage tightness: Timely to Intraday 3.

On each eligible posting, the revision with the larger absolute causal z-score
sets a temporary sleeve of ``0.10 * tanh(z)``.  The sleeve enters using the held
NG contract's volume-weighted trades from posting +5 through posting +30 minutes
and exits at that contract's mapped settlement-window VWAP.  Both legs are
charged 2.5 basis points per unit of exposure change.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from naturalgas.audit_inputs import (  # noqa: E402
    SABINE_NOMINATION_ALL_CYCLE_ARTIFACT_ID,
    SABINE_NOMINATION_EXECUTION_ARTIFACT_ID,
    SABINE_NOMINATION_PANEL_ARTIFACT_ID,
    audit_input_path,
    resolve_audit_inputs,
)

DEFAULT_RESEARCH_PANEL = (
    audit_input_path(SABINE_NOMINATION_PANEL_ARTIFACT_ID)
)
DEFAULT_EXECUTION_WINDOWS = (
    audit_input_path(SABINE_NOMINATION_EXECUTION_ARTIFACT_ID)
)
DEFAULT_ALL_CYCLE_SOURCE = (
    audit_input_path(SABINE_NOMINATION_ALL_CYCLE_ARTIFACT_ID)
)
DEFAULT_V03_DAILY = (
    PROJECT_ROOT
    / "results/models/v03_d1_3_storage_guard/strategy_daily.parquet"
)
DEFAULT_OUTPUT_DIR = (
    PROJECT_ROOT
    / "results/experiments/sabine_nomination_revision_intraday_overlay_final"
)

BASE_POSITION = "position__d1_3_storage_amplified"
BASE_NET_RETURN = "net_return__d1_3_storage_amplified"
FUTURES_RETURN = "roll_adjusted_return"
LNG_Z = "lng_revision_z_60"
STORAGE_Z = "storage_revision_z_60"
OVERLAY_WEIGHT = 0.10
COST_PER_LEG = 0.00025
ENTRY_DELAY_MINUTES = 5
ENTRY_WINDOW_MINUTES = 25
BOOTSTRAP_REPLICATIONS = 20_000
BOOTSTRAP_BLOCK_LENGTH = 20
BOOTSTRAP_SEED = 20_260_819


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def logical_path(path: Path) -> str:
    """Use a repository-relative label when the input is inside the checkout."""

    resolved = path.expanduser().resolve()
    if resolved.is_relative_to(PROJECT_ROOT):
        return str(resolved.relative_to(PROJECT_ROOT))
    return str(resolved)


def json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (pd.Timestamp, np.datetime64)):
        return pd.Timestamp(value).isoformat()
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return None if np.isnan(value) else float(value)
    if pd.isna(value):
        return None
    raise TypeError(type(value).__name__)


def metric_row(
    returns: pd.Series,
    dates: pd.Series,
    *,
    position: pd.Series | None = None,
) -> dict[str, Any]:
    sample = pd.DataFrame(
        {
            "date": pd.to_datetime(dates),
            "return": pd.to_numeric(returns, errors="coerce"),
        }
    )
    if position is not None:
        sample["position"] = pd.to_numeric(position, errors="coerce")
    sample = sample.dropna(subset=["date", "return"]).sort_values("date")
    if len(sample) < 2:
        raise ValueError("At least two return observations are required")

    log_return = np.log1p(sample["return"])
    wealth = (1.0 + sample["return"]).cumprod()
    running_peak = wealth.cummax().clip(lower=1.0)
    years = max(
        (sample["date"].iloc[-1] - sample["date"].iloc[0]).days / 365.2425,
        1.0 / 252.0,
    )
    downside = log_return.clip(upper=0.0)
    downside_deviation = float(
        np.sqrt(np.square(downside).mean()) * np.sqrt(252.0)
    )
    result: dict[str, Any] = {
        "trading_days": len(sample),
        "start": sample["date"].iloc[0],
        "end": sample["date"].iloc[-1],
        "total_return": float(wealth.iloc[-1] - 1.0),
        "cagr": float(np.exp(log_return.sum() / years) - 1.0),
        "sharpe": float(
            log_return.mean() / log_return.std(ddof=1) * np.sqrt(252.0)
        ),
        "sortino": (
            float(log_return.mean() * 252.0 / downside_deviation)
            if downside_deviation > 0.0
            else np.nan
        ),
        "maximum_drawdown": float((wealth / running_peak - 1.0).min()),
        "annualized_downside_deviation": downside_deviation,
        "win_rate": float(sample["return"].gt(0.0).mean()),
    }
    if position is not None:
        result["mean_absolute_position"] = float(
            sample["position"].abs().mean()
        )
    return result


def standard_position_net_return(
    position: pd.Series,
    futures_return: pd.Series,
) -> tuple[pd.Series, pd.Series]:
    position = pd.to_numeric(position, errors="coerce")
    turnover = position.diff().abs()
    turnover.iloc[0] = abs(position.iloc[0])
    net = position * futures_return - COST_PER_LEG * turnover
    return net, turnover


def selected_revision(
    panel: pd.DataFrame,
    *,
    minimum_history: int = 60,
    formulation: str = "dominant",
) -> tuple[pd.Series, pd.Series, pd.Series]:
    lng = panel[f"lng_revision_z_{minimum_history}"]
    storage = panel[f"storage_revision_z_{minimum_history}"]
    valid = (
        panel["execution_aligned"].fillna(False)
        & panel["entry_vwap"].gt(0.0)
        & panel["settlement_vwap"].gt(0.0)
        & lng.notna()
        & storage.notna()
    )
    if formulation == "dominant":
        choose_lng = lng.abs().ge(storage.abs())
        z_score = lng.where(choose_lng, storage)
        source = pd.Series(
            np.where(choose_lng, "LNG", "Storage"), index=panel.index
        )
    elif formulation == "lng":
        z_score = lng
        source = pd.Series("LNG", index=panel.index)
    elif formulation == "storage":
        z_score = storage
        source = pd.Series("Storage", index=panel.index)
    elif formulation == "equal":
        average = 0.5 * (
            np.tanh(lng.clip(-5.0, 5.0))
            + np.tanh(storage.clip(-5.0, 5.0))
        )
        z_score = np.arctanh(average.clip(-0.999999, 0.999999))
        source = pd.Series("Equal", index=panel.index)
    else:
        raise ValueError(f"Unknown formulation: {formulation}")
    return z_score.where(valid), source.where(valid), valid


def intraday_candidate(
    panel: pd.DataFrame,
    *,
    minimum_history: int = 60,
    formulation: str = "dominant",
    cost_per_leg: float = COST_PER_LEG,
) -> pd.DataFrame:
    result = panel.copy()
    z_score, source, valid = selected_revision(
        result,
        minimum_history=minimum_history,
        formulation=formulation,
    )
    factor_position = np.tanh(z_score.clip(-5.0, 5.0)).fillna(0.0)
    target = (
        result[BASE_POSITION] + OVERLAY_WEIGHT * factor_position
    ).clip(-1.0, 1.0)
    incremental_position = target - result[BASE_POSITION]
    incremental_gross = (
        incremental_position
        * result["posting_to_settlement_return"].fillna(0.0)
    )
    incremental_cost = 2.0 * cost_per_leg * incremental_position.abs()
    result["factor_valid_final"] = valid
    result["selected_source_final"] = source
    result["selected_revision_z_final"] = z_score
    result["factor_position_final"] = factor_position
    result["intraday_incremental_position"] = incremental_position
    result["intraday_incremental_gross_return"] = incremental_gross
    result["intraday_incremental_cost"] = incremental_cost
    result["intraday_incremental_net_return"] = (
        incremental_gross - incremental_cost
    )
    result["intraday_hybrid_net_return"] = (
        result[BASE_NET_RETURN]
        + result["intraday_incremental_net_return"]
    )
    return result


def build_final_panel(
    research_panel_path: Path,
    execution_windows_path: Path,
    v03_daily_path: Path,
) -> pd.DataFrame:
    panel = pd.read_parquet(research_panel_path)
    panel["date"] = pd.to_datetime(panel["date"]).dt.tz_localize(None)
    formal = pd.read_parquet(
        v03_daily_path,
        columns=[
            "date",
            FUTURES_RETURN,
            BASE_POSITION,
            BASE_NET_RETURN,
        ],
    )
    formal["date"] = pd.to_datetime(formal["date"]).dt.tz_localize(None)
    if not panel["date"].equals(formal["date"]):
        raise AssertionError("Research panel and formal V03 dates do not match")
    # The nomination panel is a factor/execution contract, not an independent
    # frozen copy of the base strategy.  Retain the difference for audit, then
    # always use the current formal V03 path so the overlay remains connected
    # to the selected model's reproducible output.
    panel["archived_base_position_difference"] = (
        panel[BASE_POSITION] - formal[BASE_POSITION]
    )
    panel["archived_base_net_return_difference"] = (
        panel[BASE_NET_RETURN] - formal[BASE_NET_RETURN]
    )
    panel[BASE_POSITION] = formal[BASE_POSITION]
    panel[BASE_NET_RETURN] = formal[BASE_NET_RETURN]
    panel[FUTURES_RETURN] = formal[FUTURES_RETURN]

    execution = pd.read_parquet(execution_windows_path)[
        [
            "date",
            "posting_time_utc",
            "entry_start_utc",
            "entry_end_utc",
            "entry_vwap",
            "settlement_vwap",
            "contract_symbol",
            "settlement_method",
            "entry_trade_count",
            "entry_volume",
            "settlement_trade_count",
            "settlement_volume",
        ]
    ].copy()
    execution["date"] = pd.to_datetime(execution["date"]).dt.tz_localize(None)
    execution = execution.rename(columns={
        "posting_time_utc": "execution_source__posting_time_utc",
        "entry_vwap": "execution_source__entry_vwap",
        "settlement_vwap": "execution_source__settlement_vwap",
        "contract_symbol": "execution_source__contract_symbol",
    })
    panel = panel.merge(execution, on="date", how="left", validate="one_to_one")

    panel_posting = pd.to_datetime(
        panel["posting_time_utc_execution"], utc=True
    )
    source_posting = pd.to_datetime(
        panel["execution_source__posting_time_utc"], utc=True
    )
    if not panel_posting.equals(source_posting):
        raise AssertionError(
            "Assembled panel posting timestamps do not match execution input"
        )
    for panel_column, source_column in (
        ("entry_vwap", "execution_source__entry_vwap"),
        ("settlement_vwap", "execution_source__settlement_vwap"),
    ):
        if not np.allclose(
            panel[panel_column],
            panel[source_column],
            atol=0.0,
            rtol=0.0,
            equal_nan=True,
        ):
            raise AssertionError(
                f"Assembled panel {panel_column} does not match execution input"
            )
    if not panel["contract_symbol"].fillna("").equals(
        panel["execution_source__contract_symbol"].fillna("")
    ):
        raise AssertionError(
            "Assembled panel contract symbols do not match execution input"
        )
    panel = intraday_candidate(panel)

    parity_columns = {
        "factor_valid": "factor_valid_final",
        "selected_revision_z": "selected_revision_z_final",
        "factor_position": "factor_position_final",
        "incremental_position": "intraday_incremental_position",
        "incremental_net_return": "intraday_incremental_net_return",
    }
    for frozen, rebuilt in parity_columns.items():
        left = panel[frozen]
        right = panel[rebuilt]
        if left.dtype == bool:
            matches = left.fillna(False).equals(right.fillna(False))
        else:
            matches = np.allclose(
                left,
                right,
                atol=1e-14,
                rtol=0.0,
                equal_nan=True,
            )
        if not matches:
            raise AssertionError(f"Candidate parity failed: {frozen}")

    base_rebuilt, base_turnover = standard_position_net_return(
        panel[BASE_POSITION], panel[FUTURES_RETURN]
    )
    if not np.allclose(
        base_rebuilt, panel[BASE_NET_RETURN], atol=1e-14, rtol=0.0
    ):
        raise AssertionError("Formal V03 net-return convention does not reproduce")
    panel["base_turnover"] = base_turnover

    # Timing comparator: the same I3 signal changes only the next confirmed
    # session's normal position.  It does not create an I3-to-settlement trade.
    next_increment = panel["intraday_incremental_position"].shift(
        1, fill_value=0.0
    )
    panel["next_session_incremental_position"] = next_increment
    panel["next_session_position"] = (
        panel[BASE_POSITION] + next_increment
    ).clip(-1.0, 1.0)
    next_net, next_turnover = standard_position_net_return(
        panel["next_session_position"], panel[FUTURES_RETURN]
    )
    panel["next_session_net_return"] = next_net
    panel["next_session_turnover"] = next_turnover
    return panel


def metric_tables(
    panel: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    valid = panel["factor_valid_final"]
    active_start = panel.loc[valid, "date"].min()
    masks = {
        "active_overlap": panel["date"].ge(active_start),
        "full_v03_sample": pd.Series(True, index=panel.index),
    }
    variants = {
        "base_v03": (BASE_NET_RETURN, BASE_POSITION),
        "selected_intraday_overlay": (
            "intraday_hybrid_net_return",
            BASE_POSITION,
        ),
        "next_session_only_comparator": (
            "next_session_net_return",
            "next_session_position",
        ),
    }
    rows: list[dict[str, Any]] = []
    for sample_name, mask in masks.items():
        for variant, (return_column, position_column) in variants.items():
            metric = metric_row(
                panel.loc[mask, return_column],
                panel.loc[mask, "date"],
                position=panel.loc[mask, position_column],
            )
            metric.update({"sample": sample_name, "variant": variant})
            rows.append(metric)
    headline = pd.DataFrame(rows)

    annual_rows: list[dict[str, Any]] = []
    active = panel.loc[masks["active_overlap"]].copy()
    for year, group in active.groupby(active["date"].dt.year):
        base = metric_row(group[BASE_NET_RETURN], group["date"])
        intraday = metric_row(
            group["intraday_hybrid_net_return"], group["date"]
        )
        next_session = metric_row(
            group["next_session_net_return"], group["date"]
        )
        annual_rows.append(
            {
                "year": int(year),
                "events": int(group["factor_valid_final"].sum()),
                "base_sharpe": base["sharpe"],
                "intraday_sharpe": intraday["sharpe"],
                "next_session_sharpe": next_session["sharpe"],
                "intraday_delta_sharpe": (
                    intraday["sharpe"] - base["sharpe"]
                ),
                "next_session_delta_sharpe": (
                    next_session["sharpe"] - base["sharpe"]
                ),
                "intraday_incremental_net_sum_bps": float(
                    group["intraday_incremental_net_return"].sum() * 10_000.0
                ),
                "next_session_incremental_net_sum_bps": float(
                    (
                        group["next_session_net_return"]
                        - group[BASE_NET_RETURN]
                    ).sum()
                    * 10_000.0
                ),
            }
        )
    annual = pd.DataFrame(annual_rows)

    source_rows: list[dict[str, Any]] = []
    for source, group in panel.loc[valid].groupby("selected_source_final"):
        source_rows.append(
            {
                "selected_source": source,
                "events": len(group),
                "mean_absolute_incremental_position": float(
                    group["intraday_incremental_position"].abs().mean()
                ),
                "incremental_net_sum_bps": float(
                    group["intraday_incremental_net_return"].sum() * 10_000.0
                ),
                "positive_incremental_return_share": float(
                    group["intraday_incremental_net_return"].gt(0.0).mean()
                ),
            }
        )
    source = pd.DataFrame(source_rows)
    return headline, annual, source


def robustness_tables(
    panel: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    formulations: list[dict[str, Any]] = []
    for formulation in ("lng", "storage", "equal", "dominant"):
        candidate = intraday_candidate(panel, formulation=formulation)
        start = candidate.loc[candidate["factor_valid_final"], "date"].min()
        mask = candidate["date"].ge(start)
        base = metric_row(candidate.loc[mask, BASE_NET_RETURN], candidate.loc[mask, "date"])
        hybrid = metric_row(
            candidate.loc[mask, "intraday_hybrid_net_return"],
            candidate.loc[mask, "date"],
        )
        formulations.append(
            {
                "formulation": formulation,
                "events": int(candidate.loc[mask, "factor_valid_final"].sum()),
                "delta_sharpe": hybrid["sharpe"] - base["sharpe"],
                "delta_total_return": (
                    hybrid["total_return"] - base["total_return"]
                ),
                "incremental_net_sum_bps": float(
                    candidate.loc[mask, "intraday_incremental_net_return"].sum()
                    * 10_000.0
                ),
            }
        )
    formulation_table = pd.DataFrame(formulations)

    costs: list[dict[str, Any]] = []
    for bps in (2.5, 5.0, 10.0, 20.0):
        candidate = intraday_candidate(panel, cost_per_leg=bps / 10_000.0)
        start = candidate.loc[candidate["factor_valid_final"], "date"].min()
        mask = candidate["date"].ge(start)
        base = metric_row(candidate.loc[mask, BASE_NET_RETURN], candidate.loc[mask, "date"])
        hybrid = metric_row(
            candidate.loc[mask, "intraday_hybrid_net_return"],
            candidate.loc[mask, "date"],
        )
        costs.append(
            {
                "cost_bps_per_leg_per_unit": bps,
                "delta_sharpe": hybrid["sharpe"] - base["sharpe"],
                "delta_total_return": (
                    hybrid["total_return"] - base["total_return"]
                ),
                "incremental_net_sum_bps": float(
                    candidate.loc[mask, "intraday_incremental_net_return"].sum()
                    * 10_000.0
                ),
            }
        )
    cost_table = pd.DataFrame(costs)

    histories: list[dict[str, Any]] = []
    for minimum_history in (20, 60, 120):
        candidate = intraday_candidate(
            panel, minimum_history=minimum_history
        )
        start = candidate.loc[candidate["factor_valid_final"], "date"].min()
        mask = candidate["date"].ge(start)
        base = metric_row(candidate.loc[mask, BASE_NET_RETURN], candidate.loc[mask, "date"])
        hybrid = metric_row(
            candidate.loc[mask, "intraday_hybrid_net_return"],
            candidate.loc[mask, "date"],
        )
        histories.append(
            {
                "minimum_prior_gas_days": minimum_history,
                "start": start,
                "events": int(candidate.loc[mask, "factor_valid_final"].sum()),
                "delta_sharpe": hybrid["sharpe"] - base["sharpe"],
                "delta_total_return": (
                    hybrid["total_return"] - base["total_return"]
                ),
                "incremental_net_sum_bps": float(
                    candidate.loc[mask, "intraday_incremental_net_return"].sum()
                    * 10_000.0
                ),
            }
        )
    history_table = pd.DataFrame(histories)
    return formulation_table, cost_table, history_table


def circular_block_bootstrap(
    base_returns: pd.Series,
    candidate_returns: pd.Series,
) -> dict[str, Any]:
    base = base_returns.to_numpy(float)
    candidate = candidate_returns.to_numpy(float)
    count = len(base)
    blocks = int(np.ceil(count / BOOTSTRAP_BLOCK_LENGTH))
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    differences = np.empty(BOOTSTRAP_REPLICATIONS)
    for repetition in range(BOOTSTRAP_REPLICATIONS):
        starts = rng.integers(0, count, size=blocks)
        indices = np.concatenate(
            [
                (start + np.arange(BOOTSTRAP_BLOCK_LENGTH)) % count
                for start in starts
            ]
        )[:count]
        base_log = np.log1p(base[indices])
        candidate_log = np.log1p(candidate[indices])
        base_sharpe = (
            base_log.mean() / base_log.std(ddof=1) * np.sqrt(252.0)
        )
        candidate_sharpe = (
            candidate_log.mean()
            / candidate_log.std(ddof=1)
            * np.sqrt(252.0)
        )
        differences[repetition] = candidate_sharpe - base_sharpe
    lower, median, upper = np.quantile(differences, [0.025, 0.5, 0.975])
    return {
        "replications": BOOTSTRAP_REPLICATIONS,
        "block_length": BOOTSTRAP_BLOCK_LENGTH,
        "seed": BOOTSTRAP_SEED,
        "delta_sharpe_percentile_interval_95": [lower, upper],
        "delta_sharpe_bootstrap_median": median,
        "share_nonpositive": float((differences <= 0.0).mean()),
    }


def make_plots(
    panel: pd.DataFrame,
    annual: pd.DataFrame,
    formulation: pd.DataFrame,
    costs: pd.DataFrame,
    output_dir: Path,
) -> None:
    active_start = panel.loc[panel["factor_valid_final"], "date"].min()
    active = panel.loc[panel["date"].ge(active_start)].copy()
    series = {
        "Base V03": active[BASE_NET_RETURN],
        "Selected intraday overlay": active["intraday_hybrid_net_return"],
        "Next-session-only comparator": active["next_session_net_return"],
    }
    colors = {
        "Base V03": "#5b6770",
        "Selected intraday overlay": "#0072b2",
        "Next-session-only comparator": "#d55e00",
    }

    fig, ax = plt.subplots(figsize=(11, 5.8))
    for label, returns in series.items():
        wealth = (1.0 + returns).cumprod()
        ax.plot(active["date"], wealth, label=label, color=colors[label], linewidth=2.0)
    ax.set_title("Sabine nomination-revision strategy: cumulative net wealth")
    ax.set_ylabel("Growth of $1")
    ax.grid(alpha=0.25)
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(output_dir / "cumulative_net_wealth.png", dpi=180)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(11, 5.8))
    for label, returns in series.items():
        wealth = (1.0 + returns).cumprod()
        drawdown = wealth / wealth.cummax().clip(lower=1.0) - 1.0
        ax.plot(
            active["date"],
            drawdown * 100.0,
            label=label,
            color=colors[label],
            linewidth=1.8,
        )
    ax.set_title("Drawdown comparison on the common active window")
    ax.set_ylabel("Drawdown (%)")
    ax.grid(alpha=0.25)
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(output_dir / "drawdown_comparison.png", dpi=180)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(9.5, 5.4))
    x = np.arange(len(annual))
    width = 0.36
    ax.bar(
        x - width / 2,
        annual["intraday_incremental_net_sum_bps"],
        width,
        label="Intraday overlay",
        color="#0072b2",
    )
    ax.bar(
        x + width / 2,
        annual["next_session_incremental_net_sum_bps"],
        width,
        label="Next-session only",
        color="#d55e00",
    )
    ax.axhline(0.0, color="black", linewidth=0.8)
    ax.set_xticks(x, annual["year"].astype(str))
    ax.set_ylabel("Incremental net return (bps, simple sum)")
    ax.set_title("Annual contribution depends on execution timing")
    ax.legend(frameon=False)
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    fig.savefig(output_dir / "annual_incremental_contribution.png", dpi=180)
    plt.close(fig)

    fig, axes = plt.subplots(1, 2, figsize=(12, 5.2))
    axes[0].bar(
        formulation["formulation"].str.title(),
        formulation["delta_sharpe"],
        color=["#56b4e9", "#009e73", "#cc79a7", "#0072b2"],
    )
    axes[0].axhline(0.0, color="black", linewidth=0.8)
    axes[0].set_title("Signal formulation comparison")
    axes[0].set_ylabel("Sharpe change vs V03")
    axes[0].grid(axis="y", alpha=0.25)

    axes[1].plot(
        costs["cost_bps_per_leg_per_unit"],
        costs["delta_sharpe"],
        marker="o",
        color="#0072b2",
        linewidth=2.0,
    )
    axes[1].axhline(0.0, color="black", linewidth=0.8)
    axes[1].set_title("Per-leg cost sensitivity")
    axes[1].set_xlabel("Cost (bps per unit per leg)")
    axes[1].set_ylabel("Sharpe change vs V03")
    axes[1].grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(output_dir / "signal_and_cost_comparison.png", dpi=180)
    plt.close(fig)


def run(
    *,
    research_panel_path: Path = DEFAULT_RESEARCH_PANEL,
    execution_windows_path: Path = DEFAULT_EXECUTION_WINDOWS,
    all_cycle_source_path: Path = DEFAULT_ALL_CYCLE_SOURCE,
    v03_daily_path: Path = DEFAULT_V03_DAILY,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> dict[str, Any]:
    audit_paths = resolve_audit_inputs({
        SABINE_NOMINATION_PANEL_ARTIFACT_ID: research_panel_path,
        SABINE_NOMINATION_EXECUTION_ARTIFACT_ID: execution_windows_path,
        SABINE_NOMINATION_ALL_CYCLE_ARTIFACT_ID: all_cycle_source_path,
    })
    research_panel_path = audit_paths[SABINE_NOMINATION_PANEL_ARTIFACT_ID]
    execution_windows_path = audit_paths[
        SABINE_NOMINATION_EXECUTION_ARTIFACT_ID
    ]
    all_cycle_source_path = audit_paths[
        SABINE_NOMINATION_ALL_CYCLE_ARTIFACT_ID
    ]
    required_paths = (
        research_panel_path,
        execution_windows_path,
        all_cycle_source_path,
        v03_daily_path,
    )
    missing = [str(path) for path in required_paths if not path.exists()]
    if missing:
        raise FileNotFoundError(f"Required final-research inputs are missing: {missing}")
    output_dir.mkdir(parents=True, exist_ok=True)

    panel = build_final_panel(
        research_panel_path,
        execution_windows_path,
        v03_daily_path,
    )
    headline, annual, source = metric_tables(panel)
    formulation, costs, history = robustness_tables(panel)
    active_start = panel.loc[panel["factor_valid_final"], "date"].min()
    active = panel["date"].ge(active_start)
    bootstrap = circular_block_bootstrap(
        panel.loc[active, BASE_NET_RETURN],
        panel.loc[active, "intraday_hybrid_net_return"],
    )

    output_columns = [
        "date",
        FUTURES_RETURN,
        BASE_POSITION,
        BASE_NET_RETURN,
        "gas_date",
        "posting_time_utc_factor",
        "posting_time_utc_execution",
        "entry_start_utc",
        "entry_end_utc",
        "entry_vwap",
        "settlement_vwap",
        "settlement_method",
        "contract_symbol",
        LNG_Z,
        STORAGE_Z,
        "factor_valid_final",
        "selected_source_final",
        "selected_revision_z_final",
        "factor_position_final",
        "intraday_incremental_position",
        "intraday_incremental_gross_return",
        "intraday_incremental_cost",
        "intraday_incremental_net_return",
        "intraday_hybrid_net_return",
        "next_session_incremental_position",
        "next_session_position",
        "next_session_turnover",
        "next_session_net_return",
        "archived_base_position_difference",
        "archived_base_net_return_difference",
    ]
    panel[output_columns].to_parquet(
        output_dir / "daily_strategy_path.parquet",
        index=False,
        compression="zstd",
    )
    headline.to_csv(output_dir / "headline_metrics.csv", index=False)
    annual.to_csv(output_dir / "annual_attribution.csv", index=False)
    source.to_csv(output_dir / "source_attribution.csv", index=False)
    formulation.to_csv(output_dir / "formulation_comparison.csv", index=False)
    costs.to_csv(output_dir / "cost_sensitivity.csv", index=False)
    history.to_csv(output_dir / "history_sensitivity.csv", index=False)
    make_plots(panel, annual, formulation, costs, output_dir)

    active_metrics = headline.loc[
        headline["sample"].eq("active_overlap")
    ].set_index("variant")
    full_metrics = headline.loc[
        headline["sample"].eq("full_v03_sample")
    ].set_index("variant")
    selected = active_metrics.loc["selected_intraday_overlay"]
    base = active_metrics.loc["base_v03"]
    next_session = active_metrics.loc["next_session_only_comparator"]
    valid = panel["factor_valid_final"]
    summary = {
        "experiment": "sabine_nomination_revision_intraday_overlay_final",
        "status": "final_research_specification_not_part_of_v03",
        "generated_utc": datetime.now(timezone.utc),
        "base_strategy_modified": False,
        "base_v03_refresh": {
            "position_mismatch_dates": int(
                panel["archived_base_position_difference"].abs().gt(1e-14).sum()
            ),
            "net_return_mismatch_dates": int(
                panel["archived_base_net_return_difference"].abs().gt(1e-14).sum()
            ),
            "maximum_absolute_position_difference": float(
                panel["archived_base_position_difference"].abs().max()
            ),
            "maximum_absolute_net_return_difference": float(
                panel["archived_base_net_return_difference"].abs().max()
            ),
            "policy": (
                "nomination panel supplies factor data; base position and net "
                "return are refreshed from the current formal V03 daily path"
            ),
        },
        "strategy_definition": {
            "lng_revision": "TransCameron delivery, Intraday 1 to Intraday 3",
            "storage_revision": (
                "Jefferson Island injection minus withdrawal, Timely to Intraday 3"
            ),
            "standardization": (
                "causal expanding z-score with 60 prior gas days"
            ),
            "selection": "larger absolute z-score, economic sign preserved",
            "temporary_position": "0.10 * tanh(selected revision z)",
            "entry": (
                "held NG contract trade VWAP from I3 posting +5 through +30 minutes"
            ),
            "exit": "same contract settlement-window VWAP",
            "contract": (
                "V03 held contract: C2 during the five-session early-roll window, otherwise C1"
            ),
            "cost": "2.5 bps per unit on entry and 2.5 bps per unit on exit",
        },
        "active_evaluation": {
            "start": active_start,
            "end": panel["date"].max(),
            "trading_days": int(active.sum()),
            "events": int(valid.sum()),
            "exact_execution_matches": int(panel["execution_aligned"].sum()),
            "base_sharpe": base["sharpe"],
            "selected_intraday_sharpe": selected["sharpe"],
            "delta_sharpe": selected["sharpe"] - base["sharpe"],
            "base_total_return": base["total_return"],
            "selected_intraday_total_return": selected["total_return"],
            "delta_total_return": (
                selected["total_return"] - base["total_return"]
            ),
            "base_maximum_drawdown": base["maximum_drawdown"],
            "selected_intraday_maximum_drawdown": selected[
                "maximum_drawdown"
            ],
            "incremental_net_sum_bps": float(
                panel.loc[active, "intraday_incremental_net_return"].sum()
                * 10_000.0
            ),
            "next_session_only_sharpe": next_session["sharpe"],
            "next_session_only_delta_sharpe": (
                next_session["sharpe"] - base["sharpe"]
            ),
            "next_session_only_incremental_net_sum_bps": float(
                (
                    panel.loc[active, "next_session_net_return"]
                    - panel.loc[active, BASE_NET_RETURN]
                ).sum()
                * 10_000.0
            ),
        },
        "full_v03_sample": {
            variant: {
                key: value
                for key, value in row.items()
                if key
                in {
                    "trading_days",
                    "start",
                    "end",
                    "sharpe",
                    "sortino",
                    "cagr",
                    "total_return",
                    "maximum_drawdown",
                }
            }
            for variant, row in full_metrics.to_dict(orient="index").items()
        },
        "bootstrap": bootstrap,
        "input_lineage": {
            "assembled_research_panel": {
                "path": logical_path(research_panel_path),
                "sha256": sha256(research_panel_path),
            },
            "execution_windows": {
                "path": logical_path(execution_windows_path),
                "sha256": sha256(execution_windows_path),
            },
            "formal_v03_daily": {
                "path": logical_path(v03_daily_path),
                "sha256": sha256(v03_daily_path),
            },
            "all_cycle_source": (
                {
                    "path": logical_path(all_cycle_source_path),
                    "sha256": sha256(all_cycle_source_path),
                }
                if all_cycle_source_path.exists()
                else {
                    "path": logical_path(all_cycle_source_path),
                    "sha256": None,
                }
            ),
        },
        "interpretation": [
            "The return improvement is specific to the post-I3 intraday window.",
            "Using the same signal only at the next normal rebalance is a rejected timing comparator.",
            "The all-cycle history was assembled retrospectively and is not an untouched prospective sample.",
            "Scheduled quantity is a nomination rather than metered physical flow.",
        ],
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, default=json_default, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--research-panel", type=Path, default=DEFAULT_RESEARCH_PANEL
    )
    parser.add_argument(
        "--execution-windows", type=Path, default=DEFAULT_EXECUTION_WINDOWS
    )
    parser.add_argument(
        "--all-cycle-source", type=Path, default=DEFAULT_ALL_CYCLE_SOURCE
    )
    parser.add_argument("--v03-daily", type=Path, default=DEFAULT_V03_DAILY)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    result = run(
        research_panel_path=arguments.research_panel,
        execution_windows_path=arguments.execution_windows,
        all_cycle_source_path=arguments.all_cycle_source,
        v03_daily_path=arguments.v03_daily,
        output_dir=arguments.output_dir,
    )
    print(json.dumps(result, default=json_default, indent=2, sort_keys=True))
