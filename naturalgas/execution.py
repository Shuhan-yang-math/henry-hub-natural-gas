"""NYMEX natural-gas futures return and early-roll helpers.

This module contains only the execution-calendar logic required by the final
strategy.  It deliberately avoids importing any of the historical GDEX data
downloaders used during the research phase.
"""

from __future__ import annotations

from typing import Iterable

import numpy as np
import pandas as pd


EARLY_ROLL_TRADING_DAYS = 5


def build_ng_roll_calendar(
    trading_dates: Iterable[pd.Timestamp],
    *,
    roll_advance_days: int = 0,
) -> pd.DataFrame:
    """Build official NG last-trading-day and earlier switch dates."""

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
    """Replace official front-month returns with the fixed five-day roll."""

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
