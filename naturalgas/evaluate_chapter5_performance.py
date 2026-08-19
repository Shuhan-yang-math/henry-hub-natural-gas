"""Build Chapter 5 inference, intervention, and comparison-chart artifacts."""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.ticker as mtick  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = ROOT / "results" / "models" / "v03_d1_3_storage_guard"
DAILY_PATH = MODEL_DIR / "strategy_daily.parquet"
INFERENCE_PATH = MODEL_DIR / "chapter5_performance_inference.json"
ATTRIBUTION_PATH = MODEL_DIR / "chapter5_intervention_attribution.csv"
NAV_FIGURE_PATH = MODEL_DIR / "chapter5_cumulative_nav.png"
DRAWDOWN_FIGURE_PATH = MODEL_DIR / "chapter5_drawdown.png"

ANNUALIZATION = 252.0
TRANSACTION_COST = 0.00025
BOOTSTRAP_REPLICATIONS = 20_000
BOOTSTRAP_SEED = 20_260_818
PRIMARY_BLOCK_LENGTH = 20
SENSITIVITY_BLOCK_LENGTHS = (5, 10, 20, 40, 60)

VARIANTS = {
    "D1--5 comparator": "net_return__d1_5",
    "D1--3, no constraint": "net_return__d1_3",
    "Selected V03": "net_return__d1_3_storage_amplified",
}


def log_sharpe(net_return: np.ndarray) -> float:
    log_return = np.log1p(net_return)
    return float(
        log_return.mean() / log_return.std(ddof=1) * np.sqrt(ANNUALIZATION)
    )


def arithmetic_sharpe(net_return: np.ndarray) -> float:
    return float(
        net_return.mean()
        / net_return.std(ddof=1)
        * np.sqrt(ANNUALIZATION)
    )


def paired_circular_block_bootstrap(
    selected: np.ndarray,
    comparator: np.ndarray,
    *,
    block_length: int,
    replications: int,
    seed: int,
) -> dict[str, float | int | list[float]]:
    """Bootstrap a paired log-Sharpe difference in circular moving blocks."""

    effective_seed = seed + block_length
    rng = np.random.default_rng(effective_seed)
    observations = len(selected)
    block_count = int(np.ceil(observations / block_length))
    offsets = np.arange(block_length)
    differences = np.empty(replications)

    for replication in range(replications):
        starts = rng.integers(0, observations, size=block_count)
        indices = ((starts[:, None] + offsets) % observations).ravel()
        indices = indices[:observations]
        differences[replication] = log_sharpe(
            selected[indices]
        ) - log_sharpe(comparator[indices])

    interval = np.quantile(differences, [0.025, 0.5, 0.975])
    return {
        "block_length_sessions": block_length,
        "replications": replications,
        "base_seed": seed,
        "effective_seed": effective_seed,
        "percentile_interval_95": [float(interval[0]), float(interval[2])],
        "bootstrap_median": float(interval[1]),
        "bootstrap_mean": float(differences.mean()),
        "bootstrap_standard_deviation": float(differences.std(ddof=1)),
        "fraction_at_or_below_zero": float((differences <= 0.0).mean()),
    }


def net_return(position: pd.Series, futures_return: pd.Series) -> pd.Series:
    turnover = position.diff().abs().fillna(position.abs())
    return position * futures_return - turnover * TRANSACTION_COST


def intervention_attribution(daily: pd.DataFrame) -> pd.DataFrame:
    selected = daily["net_return__d1_3_storage_amplified"]
    position_without_event_veto = daily[
        "position_pre_veto__d1_3_storage_amplified"
    ]
    return_without_event_veto = net_return(
        position_without_event_veto,
        daily["roll_adjusted_return"],
    )
    event_increment = selected - return_without_event_veto

    working = daily.assign(
        year=pd.to_datetime(daily["date"]).dt.year,
        event_increment=event_increment,
    )
    rows: list[dict[str, int | float]] = []
    for year, group in working.groupby("year", sort=True):
        guard_increment = float(
            group["incremental_net_return_vs_d1_3"].sum()
        )
        event_value = float(group["event_increment"].sum())
        if abs(event_value) < 1e-12:
            event_value = 0.0
        rows.append(
            {
                "year": int(year),
                "guard_interventions": int(
                    group["guard_blocked_position_date"].sum()
                ),
                "guard_paired_net_return_contribution": guard_increment,
                "event_veto_interventions": int(
                    group["selected_event_veto_applied"].sum()
                ),
                "event_veto_paired_net_return_contribution": event_value,
            }
        )
    return pd.DataFrame(rows)


def wealth_and_drawdown(net_return: pd.Series) -> tuple[pd.Series, pd.Series]:
    wealth = (1.0 + net_return).cumprod()
    running_peak = wealth.cummax().clip(lower=1.0)
    return wealth, wealth / running_peak - 1.0


def write_figures(daily: pd.DataFrame) -> None:
    dates = pd.to_datetime(daily["date"])
    colors = ("#8d99a6", "#ed8b2c", "#0077b6")

    fig, axis = plt.subplots(figsize=(11.5, 5.8))
    for (label, column), color in zip(VARIANTS.items(), colors, strict=True):
        wealth, _ = wealth_and_drawdown(daily[column])
        axis.plot(dates, wealth, label=label, color=color, linewidth=2.0)
    axis.set_title("Cumulative net wealth after 2.5 bp turnover cost")
    axis.set_ylabel("Growth of initial wealth of 1.00")
    axis.grid(alpha=0.25)
    axis.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(NAV_FIGURE_PATH, dpi=180, bbox_inches="tight")
    plt.close(fig)

    fig, axis = plt.subplots(figsize=(11.5, 5.8))
    for (label, column), color in zip(VARIANTS.items(), colors, strict=True):
        _, drawdown = wealth_and_drawdown(daily[column])
        axis.plot(
            dates,
            drawdown,
            label=label,
            color=color,
            linewidth=1.8,
        )
    axis.set_title("Drawdown from prior net-wealth peak")
    axis.set_ylabel("Drawdown")
    axis.yaxis.set_major_formatter(mtick.PercentFormatter(1.0))
    axis.grid(alpha=0.25)
    axis.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(DRAWDOWN_FIGURE_PATH, dpi=180, bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    daily = pd.read_parquet(DAILY_PATH).sort_values("date").reset_index(drop=True)
    selected = daily["net_return__d1_3_storage_amplified"].to_numpy(float)
    comparator = daily["net_return__d1_5"].to_numpy(float)

    bootstrap = {
        str(block_length): paired_circular_block_bootstrap(
            selected,
            comparator,
            block_length=block_length,
            replications=BOOTSTRAP_REPLICATIONS,
            seed=BOOTSTRAP_SEED,
        )
        for block_length in SENSITIVITY_BLOCK_LENGTHS
    }
    positive = int((selected > 0.0).sum())
    zero = int((selected == 0.0).sum())
    negative = int((selected < 0.0).sum())
    output = {
        "input": str(DAILY_PATH.relative_to(ROOT)),
        "sample_start": str(pd.Timestamp(daily["date"].iloc[0]).date()),
        "sample_end": str(pd.Timestamp(daily["date"].iloc[-1]).date()),
        "trading_days": len(daily),
        "formal_statistic": "annualized log-return Sharpe difference, selected V03 minus D1--5 comparator",
        "selected_log_return_sharpe": log_sharpe(selected),
        "selected_arithmetic_return_sharpe": arithmetic_sharpe(selected),
        "observed_sharpe_difference": log_sharpe(selected)
        - log_sharpe(comparator),
        "bootstrap_method": "paired circular moving-block percentile bootstrap",
        "primary_block_length_sessions": PRIMARY_BLOCK_LENGTH,
        "bootstrap_by_block_length": bootstrap,
        "win_rate_definition": "positive net-return sessions divided by all reported trading sessions; zero-return and flat-position sessions remain in the denominator",
        "selected_return_day_counts": {
            "positive": positive,
            "zero": zero,
            "negative": negative,
        },
    }
    INFERENCE_PATH.write_text(json.dumps(output, indent=2) + "\n")

    attribution = intervention_attribution(daily)
    attribution.to_csv(ATTRIBUTION_PATH, index=False)
    write_figures(daily)


if __name__ == "__main__":
    main()
