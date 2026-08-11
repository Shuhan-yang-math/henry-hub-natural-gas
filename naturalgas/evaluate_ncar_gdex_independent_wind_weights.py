#!/usr/bin/env python3
"""Evaluate an independent seasonal portfolio weight for the GDEX wind factor.

This is a local, research-only allocation experiment.  It starts from the
selected capacity-weighted nonlinear wind signal and separates it from the
legacy weather block.  Peak-demand weights are held fixed; only the March--May
and September--October wind weight changes.  No production panel or cloud
object is modified.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCAL_PANEL = (
    Path(__file__).resolve().parent
    / "processed/ng_multisignal_score/ng_multisignal_panel.parquet"
)
WIND_FEATURES = (
    Path(__file__).resolve().parent
    / "processed/ncar_gdex_complete_wind_factor/"
    "capacity_weighted_wind_features_daily.parquet"
)
DEFAULT_OUTPUT_DIR = (
    Path(__file__).resolve().parent
    / "processed/ncar_gdex_independent_wind_weights"
)

BACKTEST_START = pd.Timestamp("2016-07-06")
PRIMARY_CYCLE_UTC = 0
PEAK_MONTHS = (11, 12, 1, 2, 6, 7, 8)
SHOULDER_MONTHS = (3, 4, 5, 9, 10)
COLD_SEASON_MONTHS = (11, 12, 1, 2, 3)
PRODUCTION_LOCAL_LEVEL_THRESHOLD = 1.0
TRANSACTION_COSTS_BPS = (0.0, 2.5, 5.0)
PERIODS = {
    "full": (pd.Timestamp("2016-07-06"), pd.Timestamp("2026-07-31")),
    "development": (
        pd.Timestamp("2016-07-06"),
        pd.Timestamp("2020-12-31"),
    ),
    "validation": (pd.Timestamp("2021-01-01"), pd.Timestamp("2023-12-31")),
    "first_look_holdout": (
        pd.Timestamp("2024-01-01"),
        pd.Timestamp("2026-07-31"),
    ),
}

PRIMARY_WEATHER_COMPONENTS = (
    "sig_cpc_seasonal_revision",
    "sig_cpc_level",
    "sig_observed_weather",
)
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


@dataclass(frozen=True)
class Allocation:
    """Top-level weights for legacy weather, wind, and fundamentals."""

    name: str
    peak_legacy_weather: float
    peak_wind: float
    peak_fundamental: float
    shoulder_legacy_weather: float
    shoulder_wind: float
    shoulder_fundamental: float
    allocation_source: str
    eligible_for_development_selection: bool = False

    def __post_init__(self) -> None:
        for season, weights in (
            (
                "peak",
                (
                    self.peak_legacy_weather,
                    self.peak_wind,
                    self.peak_fundamental,
                ),
            ),
            (
                "shoulder",
                (
                    self.shoulder_legacy_weather,
                    self.shoulder_wind,
                    self.shoulder_fundamental,
                ),
            ),
        ):
            if any(weight < 0.0 for weight in weights):
                raise ValueError(f"{self.name} has a negative {season} weight")
            if not math.isclose(sum(weights), 1.0, abs_tol=1e-12):
                raise ValueError(
                    f"{self.name} {season} weights sum to {sum(weights)}"
                )


def candidate_allocations() -> tuple[Allocation, ...]:
    """Return the predeclared allocation grid used by this experiment."""

    allocations = [
        Allocation(
            name="no_wind_original",
            peak_legacy_weather=0.60,
            peak_wind=0.00,
            peak_fundamental=0.40,
            shoulder_legacy_weather=0.30,
            shoulder_wind=0.00,
            shoulder_fundamental=0.70,
            allocation_source="original seasonal score without GDEX wind",
        ),
        Allocation(
            name="embedded_shoulder_7p5",
            peak_legacy_weather=0.45,
            peak_wind=0.15,
            peak_fundamental=0.40,
            shoulder_legacy_weather=0.225,
            shoulder_wind=0.075,
            shoulder_fundamental=0.70,
            allocation_source="current four-component weather block",
        ),
    ]
    for wind_weight in np.arange(0.10, 0.4001, 0.025):
        label = f"{wind_weight * 100:g}".replace(".", "p")
        allocations.append(
            Allocation(
                name=f"independent_shoulder_{label}pct",
                peak_legacy_weather=0.45,
                peak_wind=0.15,
                peak_fundamental=0.40,
                shoulder_legacy_weather=0.225,
                shoulder_wind=float(round(wind_weight, 6)),
                shoulder_fundamental=float(
                    round(1.0 - 0.225 - wind_weight, 6)
                ),
                allocation_source=(
                    "increase shoulder wind at the expense of fundamentals"
                ),
                eligible_for_development_selection=True,
            )
        )
    for wind_weight in (0.10, 0.125, 0.15):
        label = f"{wind_weight * 100:g}".replace(".", "p")
        allocations.append(
            Allocation(
                name=f"weather_reallocation_shoulder_{label}pct",
                peak_legacy_weather=0.45,
                peak_wind=0.15,
                peak_fundamental=0.40,
                shoulder_legacy_weather=float(round(0.30 - wind_weight, 6)),
                shoulder_wind=wind_weight,
                shoulder_fundamental=0.70,
                allocation_source=(
                    "increase shoulder wind at the expense of legacy weather"
                ),
            )
        )
    return tuple(allocations)


def fixed_weight_mean(frame: pd.DataFrame, columns: tuple[str, ...]) -> pd.Series:
    """Equal fixed weights; missing inputs retain their neutral zero slot."""

    return frame.loc[:, columns].fillna(0.0).sum(axis=1) / len(columns)


def build_base_components(panel: pd.DataFrame) -> pd.DataFrame:
    result = panel.copy()
    result["legacy_weather"] = fixed_weight_mean(
        np.tanh(result[list(PRIMARY_WEATHER_COMPONENTS)] / 2.0),
        PRIMARY_WEATHER_COMPONENTS,
    )
    result["fundamental_rebuilt"] = pd.concat(
        [
            np.tanh(result[list(FUNDAMENTAL_FAST_COMPONENTS)] / 2.0),
            result[list(FUNDAMENTAL_MONTHLY_COMPONENTS)],
        ],
        axis=1,
    ).mean(axis=1, skipna=True)
    result["wind_transformed"] = np.tanh(result["sig_capacity_cf"] / 2.0)
    return result


def allocation_score(frame: pd.DataFrame, allocation: Allocation) -> pd.Series:
    """Build a seasonally allocated score with the existing freeze-off guard."""

    shoulder = frame["date"].dt.month.isin(SHOULDER_MONTHS)
    # Match the existing fixed-component policy: an unavailable wind update is
    # neutral zero and never causes the remaining components to be renormalized.
    wind = frame["wind_transformed"].fillna(0.0)
    raw = pd.Series(
        np.where(
            shoulder,
            allocation.shoulder_legacy_weather * frame["legacy_weather"]
            + allocation.shoulder_wind * wind
            + allocation.shoulder_fundamental * frame["fundamental_rebuilt"],
            allocation.peak_legacy_weather * frame["legacy_weather"]
            + allocation.peak_wind * wind
            + allocation.peak_fundamental * frame["fundamental_rebuilt"],
        ),
        index=frame.index,
    )
    production_control = (
        frame["date"].dt.month.isin(COLD_SEASON_MONTHS)
        & frame["prod_freeze_local_level_score"].ge(
            PRODUCTION_LOCAL_LEVEL_THRESHOLD
        )
        & frame["prod_freeze_local_revision_score"].ge(0.0)
    )
    return raw.where(~production_control, raw.clip(lower=0.0))


def performance(
    frame: pd.DataFrame,
    *,
    position_column: str,
    cost_bps: float,
) -> dict[str, Any]:
    position = frame[position_column]
    turnover = position.diff().abs().fillna(position.abs())
    net_return = (
        position * frame["roll_adjusted_return"]
        - turnover * cost_bps / 10_000.0
    )
    log_return = np.log1p(net_return)
    wealth = (1.0 + net_return).cumprod()
    drawdown = wealth / wealth.cummax() - 1.0
    years = (frame["date"].max() - frame["date"].min()).days / 365.2425
    volatility = log_return.std()
    return {
        "start": frame["date"].min(),
        "end": frame["date"].max(),
        "trading_days": len(frame),
        "total_return": float(wealth.iloc[-1] - 1.0),
        "cagr": float(np.exp(log_return.sum() / years) - 1.0),
        "annualized_volatility": float(volatility * np.sqrt(252)),
        "sharpe_zero_rf": float(
            log_return.mean() / volatility * np.sqrt(252)
        ),
        "maximum_drawdown": float(drawdown.min()),
        "mean_absolute_position": float(position.abs().mean()),
        "total_turnover": float(turnover.sum()),
    }


def build_research_panel(
    panel_path: Path,
    wind_path: Path,
    allocations: tuple[Allocation, ...],
) -> pd.DataFrame:
    panel = pd.read_parquet(panel_path)
    panel["date"] = pd.to_datetime(panel["date"])
    wind = pd.read_parquet(
        wind_path,
        columns=["date", "forecast_cycle_hour_utc", "sig_capacity_cf"],
    )
    wind = wind.loc[wind["forecast_cycle_hour_utc"].eq(PRIMARY_CYCLE_UTC), [
        "date",
        "sig_capacity_cf",
    ]]
    wind["date"] = pd.to_datetime(wind["date"])
    merged = panel.merge(wind, on="date", how="left", validate="one_to_one")
    scored = build_base_components(merged)
    for allocation in allocations:
        score_column = f"score_{allocation.name}"
        position_column = f"position_{allocation.name}"
        scored[score_column] = allocation_score(scored, allocation)
        scored[position_column] = scored[score_column].shift(1).clip(-1.0, 1.0)
    return scored


def common_sample(
    panel: pd.DataFrame,
    allocations: tuple[Allocation, ...],
    *,
    through_date: pd.Timestamp,
) -> pd.DataFrame:
    ready = (
        panel["date"].between(BACKTEST_START, through_date)
        & panel["required_input_complete"].fillna(False)
        & panel[list(GROUP_READY_COLUMNS)].notna().all(axis=1)
        & panel["roll_adjusted_return"].notna()
    )
    for allocation in allocations:
        ready &= panel[f"position_{allocation.name}"].notna()
    return panel.loc[ready].copy().reset_index(drop=True)


def evaluate_allocations(
    panel: pd.DataFrame,
    allocations: tuple[Allocation, ...],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    full_rows: list[dict[str, Any]] = []
    period_rows: list[dict[str, Any]] = []
    for allocation in allocations:
        position_column = f"position_{allocation.name}"
        for cost in TRANSACTION_COSTS_BPS:
            row = performance(
                panel, position_column=position_column, cost_bps=cost
            )
            row.update({"allocation": allocation.name, "cost_bps": cost})
            full_rows.append(row)
        for period, (start, end) in PERIODS.items():
            period_frame = panel.loc[panel["date"].between(start, end)]
            row = performance(
                period_frame,
                position_column=position_column,
                cost_bps=2.5,
            )
            row.update({"period": period, "allocation": allocation.name})
            period_rows.append(row)
    return pd.DataFrame(full_rows), pd.DataFrame(period_rows)


def select_development_allocation(
    periods: pd.DataFrame,
    allocations: tuple[Allocation, ...],
) -> Allocation:
    eligible = {
        item.name for item in allocations if item.eligible_for_development_selection
    }
    candidates = periods.loc[
        periods["period"].eq("development")
        & periods["allocation"].isin(eligible)
    ]
    selected_name = candidates.sort_values(
        ["sharpe_zero_rf", "allocation"], ascending=[False, True]
    ).iloc[0]["allocation"]
    return next(item for item in allocations if item.name == selected_name)


def selected_diagnostics(
    panel: pd.DataFrame,
    *,
    selected: Allocation,
    baseline_name: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    annual_rows: list[dict[str, Any]] = []
    for year, year_frame in panel.groupby(panel["date"].dt.year):
        for name in (baseline_name, selected.name):
            row = performance(
                year_frame,
                position_column=f"position_{name}",
                cost_bps=2.5,
            )
            row.update({"year": int(year), "allocation": name})
            annual_rows.append(row)

    baseline_position = panel[f"position_{baseline_name}"]
    selected_position = panel[f"position_{selected.name}"]
    baseline_turnover = baseline_position.diff().abs().fillna(
        baseline_position.abs()
    )
    selected_turnover = selected_position.diff().abs().fillna(
        selected_position.abs()
    )
    panel = panel.copy()
    panel["baseline_net_return"] = (
        baseline_position * panel["roll_adjusted_return"]
        - baseline_turnover * 2.5 / 10_000.0
    )
    panel["selected_net_return"] = (
        selected_position * panel["roll_adjusted_return"]
        - selected_turnover * 2.5 / 10_000.0
    )
    panel["baseline_log_return"] = np.log1p(panel["baseline_net_return"])
    panel["selected_log_return"] = np.log1p(panel["selected_net_return"])
    monthly = (
        panel.groupby(panel["date"].dt.month)
        .agg(
            trading_days=("date", "size"),
            baseline_log_return=("baseline_log_return", "sum"),
            selected_log_return=("selected_log_return", "sum"),
        )
        .reset_index(names="calendar_month")
    )
    monthly["selected_minus_baseline_log_return"] = (
        monthly["selected_log_return"] - monthly["baseline_log_return"]
    )
    return pd.DataFrame(annual_rows), monthly


def json_default(value: Any) -> Any:
    if isinstance(value, (pd.Timestamp, np.datetime64)):
        return str(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    raise TypeError(type(value).__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--panel", type=Path, default=LOCAL_PANEL)
    parser.add_argument("--wind-features", type=Path, default=WIND_FEATURES)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--through-date", type=pd.Timestamp, default="2026-07-31")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    allocations = candidate_allocations()
    scored = build_research_panel(args.panel, args.wind_features, allocations)
    daily = common_sample(
        scored, allocations, through_date=pd.Timestamp(args.through_date)
    )
    comparison, periods = evaluate_allocations(daily, allocations)
    selected = select_development_allocation(periods, allocations)
    baseline_name = "embedded_shoulder_7p5"
    annual, monthly = selected_diagnostics(
        daily, selected=selected, baseline_name=baseline_name
    )

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(asdict(item) for item in allocations).to_csv(
        output_dir / "independent_wind_weight_candidates.csv", index=False
    )
    comparison.to_csv(
        output_dir / "independent_wind_weight_comparison.csv", index=False
    )
    periods.to_csv(
        output_dir / "independent_wind_weight_periods.csv", index=False
    )
    annual.to_csv(
        output_dir / "independent_wind_weight_by_year.csv", index=False
    )
    monthly.to_csv(
        output_dir / "independent_wind_weight_month_attribution.csv", index=False
    )
    daily_columns = [
        "date",
        "roll_adjusted_return",
        "legacy_weather",
        "wind_transformed",
        "fundamental_rebuilt",
        f"score_{baseline_name}",
        f"position_{baseline_name}",
        f"score_{selected.name}",
        f"position_{selected.name}",
    ]
    daily[daily_columns].to_parquet(
        output_dir / "independent_wind_weight_selected_daily.parquet",
        index=False,
        compression="zstd",
    )

    period_index = periods.set_index(["period", "allocation"])
    manifest = {
        "experiment": "independent_seasonal_gdex_wind_weight",
        "status": "research_only",
        "production_panel_modified": False,
        "gcs_objects_modified": False,
        "common_sample_start": daily["date"].min(),
        "common_sample_end": daily["date"].max(),
        "common_trading_days": len(daily),
        "signal": "capacity-weighted nonlinear capacity-factor shortfall",
        "signal_transform": "tanh(signal / 2)",
        "peak_months": PEAK_MONTHS,
        "shoulder_months": SHOULDER_MONTHS,
        "selection_rule": (
            "highest 2.5 bps net Sharpe in 2016-07-06 through 2020-12-31 "
            "among candidates that hold legacy shoulder weather at 22.5% "
            "and reallocate fundamentals to wind"
        ),
        "selected_allocation": asdict(selected),
        "selected_period_results": {
            period: {
                "baseline_sharpe": period_index.loc[
                    (period, baseline_name), "sharpe_zero_rf"
                ],
                "selected_sharpe": period_index.loc[
                    (period, selected.name), "sharpe_zero_rf"
                ],
                "baseline_cagr": period_index.loc[
                    (period, baseline_name), "cagr"
                ],
                "selected_cagr": period_index.loc[
                    (period, selected.name), "cagr"
                ],
            }
            for period in PERIODS
        },
        "execution": {
            "position_lag": "one trading day",
            "transaction_cost_bps_for_selection": 2.5,
            "transaction_cost_sensitivity_bps": TRANSACTION_COSTS_BPS,
        },
        "caution": (
            "The allocation grid is a research sensitivity analysis, not a "
            "production parameter. Historical USWTDB weights are reconstructed "
            "from a current turbine snapshot rather than archived vintages."
        ),
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, default=json_default) + "\n",
        encoding="utf-8",
    )

    print("\nSelected independent wind allocation")
    print(pd.Series(asdict(selected)).to_string())
    print("\nPeriod results at 2.5 bps")
    print(
        periods.loc[
            periods["allocation"].isin([baseline_name, selected.name]),
            [
                "period",
                "allocation",
                "trading_days",
                "cagr",
                "annualized_volatility",
                "sharpe_zero_rf",
                "maximum_drawdown",
            ],
        ].round(4).to_string(index=False)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
