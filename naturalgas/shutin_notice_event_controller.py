#!/usr/bin/env python3
"""Post-score BSEE shut-in plus Sabine-notice position controller.

The controller is deliberately outside the factor score.  It never creates or
amplifies exposure.  When a BSEE offshore gas shut-in estimate worsens versus
the prior tradable report and a Sabine operational notice was posted within the
preceding three calendar days, an existing core short is set to zero.

Timing convention
-----------------
The BSEE hurricane-history page states that daily activity updates are normally
posted at 14:00 ET, before the 14:28--14:30 ET NG settlement window.  A report
dated on a strategy settlement date may therefore control the following
settlement-to-settlement return.  If the report is dated on a weekend or market
holiday, the prior settlement is not executable after the report; the event is
delayed through the first post-report settlement and controls only the next
settlement-to-settlement return.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVENT_REPORTS_PATH = (
    ROOT
    / "inputs/audit/events/event_reports_aligned.parquet"
)
CONTROLLER_NAME = "shutin_worsening_recent_operational_notice_veto"
CONTROLLED_POSITION_COLUMN = (
    "position__replace_all_storage__south_central_total__shutin_notice_veto"
)


def _first_strictly_later_date(
    date: pd.Timestamp, trading_dates: pd.DatetimeIndex
) -> pd.Timestamp:
    values = trading_dates.values
    index = int(np.searchsorted(values, np.datetime64(date), side="right"))
    return pd.Timestamp(values[index]) if index < len(values) else pd.NaT


def align_event_reports(
    reports: pd.DataFrame,
    trading_dates: pd.DatetimeIndex,
) -> pd.DataFrame:
    """Map point-in-time reports to an executable return date."""
    result = reports.copy()
    result["event_date"] = pd.to_datetime(result["event_date"]).dt.normalize()
    if "trade_date" in result:
        result["legacy_trade_date"] = pd.to_datetime(result["trade_date"])
    else:
        result["legacy_trade_date"] = pd.NaT

    result["rule_eligible"] = (
        pd.to_numeric(result["shutin_revision_mmcfd"], errors="coerce").gt(0.0)
        & result["recent_notice_any"].fillna(False).astype(bool)
    )
    date_set = set(pd.Timestamp(value) for value in trading_dates)
    result["report_date_is_strategy_settlement"] = result["event_date"].isin(
        date_set
    )

    entry_settlements: list[pd.Timestamp] = []
    return_dates: list[pd.Timestamp] = []
    for report_date, is_settlement in zip(
        result["event_date"],
        result["report_date_is_strategy_settlement"],
        strict=True,
    ):
        entry_settlement = (
            pd.Timestamp(report_date)
            if is_settlement
            else _first_strictly_later_date(pd.Timestamp(report_date), trading_dates)
        )
        return_date = (
            _first_strictly_later_date(entry_settlement, trading_dates)
            if pd.notna(entry_settlement)
            else pd.NaT
        )
        entry_settlements.append(entry_settlement)
        return_dates.append(return_date)

    result["controller_entry_settlement_date"] = entry_settlements
    result["controller_return_date"] = return_dates
    result["weekend_or_holiday_extra_delay"] = ~result[
        "report_date_is_strategy_settlement"
    ]
    result["controller_name"] = CONTROLLER_NAME
    result["enters_factor_score"] = False
    result["creates_or_amplifies_exposure"] = False
    return result


def _join_unique(values: pd.Series) -> str:
    cleaned = [
        str(value).strip()
        for value in values.dropna()
        if str(value).strip()
    ]
    return " | ".join(dict.fromkeys(cleaned))


def event_daily_panel(
    aligned_reports: pd.DataFrame,
    trading_dates: pd.DatetimeIndex,
) -> pd.DataFrame:
    selected = aligned_reports.loc[
        aligned_reports["rule_eligible"]
        & aligned_reports["controller_return_date"].notna()
    ].copy()
    panel = pd.DataFrame({"date": trading_dates})
    if selected.empty:
        panel["shutin_notice_controller_active"] = False
        panel["shutin_notice_source_report_count"] = 0
        panel["shutin_notice_source_report_dates"] = ""
        panel["shutin_notice_event_names"] = ""
        panel["shutin_notice_revision_mmcfd"] = np.nan
        panel["shutin_notice_subjects"] = ""
        panel["shutin_notice_weekend_or_holiday_delay"] = False
        return panel

    grouped = (
        selected.groupby("controller_return_date", as_index=False)
        .agg(
            shutin_notice_source_report_count=("event_date", "size"),
            shutin_notice_source_report_dates=("event_date", _join_unique),
            shutin_notice_event_names=("event_name", _join_unique),
            shutin_notice_revision_mmcfd=("shutin_revision_mmcfd", "sum"),
            shutin_notice_subjects=("recent_notice_subjects", _join_unique),
            shutin_notice_weekend_or_holiday_delay=(
                "weekend_or_holiday_extra_delay",
                "max",
            ),
        )
        .rename(columns={"controller_return_date": "date"})
    )
    panel = panel.merge(grouped, on="date", how="left", validate="one_to_one")
    panel["shutin_notice_controller_active"] = panel[
        "shutin_notice_source_report_count"
    ].notna()
    panel["shutin_notice_source_report_count"] = panel[
        "shutin_notice_source_report_count"
    ].fillna(0).astype(int)
    panel["shutin_notice_weekend_or_holiday_delay"] = panel[
        "shutin_notice_weekend_or_holiday_delay"
    ].fillna(False).astype(bool)
    for column in (
        "shutin_notice_source_report_dates",
        "shutin_notice_event_names",
        "shutin_notice_subjects",
    ):
        panel[column] = panel[column].fillna("")
    return panel


def apply_controller(
    daily: pd.DataFrame,
    reports: pd.DataFrame,
    *,
    core_position_column: str,
    controlled_position_column: str = CONTROLLED_POSITION_COLUMN,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return the strategy panel with a pure short-veto position and registry."""
    result = daily.copy()
    result["date"] = pd.to_datetime(result["date"]).dt.normalize()
    result = result.sort_values("date").reset_index(drop=True)
    trading_dates = pd.DatetimeIndex(result["date"])
    aligned = align_event_reports(reports, trading_dates)
    event_panel = event_daily_panel(aligned, trading_dates)
    result = result.merge(event_panel, on="date", how="left", validate="one_to_one")

    core = pd.to_numeric(result[core_position_column], errors="coerce")
    active = result["shutin_notice_controller_active"].astype(bool)
    veto = active & core.lt(0.0)
    result["shutin_notice_controller_veto_applied"] = veto
    result[controlled_position_column] = core.mask(veto, 0.0)
    result["shutin_notice_position_adjustment"] = (
        result[controlled_position_column] - core
    )
    return result, aligned


def controller_summary(
    daily: pd.DataFrame,
    aligned_reports: pd.DataFrame,
    *,
    controlled_position_column: str = CONTROLLED_POSITION_COLUMN,
) -> dict[str, Any]:
    selected = aligned_reports.loc[aligned_reports["rule_eligible"]]
    return {
        "name": CONTROLLER_NAME,
        "integration": "post_score_pure_position_veto",
        "enters_factor_score": False,
        "creates_or_amplifies_exposure": False,
        "signed_action": "worsening shut-in plus recent operational notice vetoes core shorts",
        "weekday_entry": "report-date settlement; BSEE normal posting 14:00 ET precedes NG settlement",
        "weekend_holiday_entry": "delay through first post-report settlement; control following settlement return",
        "eligible_source_reports": int(len(selected)),
        "active_strategy_days": int(
            daily["shutin_notice_controller_active"].sum()
        ),
        "actual_veto_days": int(
            daily["shutin_notice_controller_veto_applied"].sum()
        ),
        "weekend_or_holiday_delayed_reports": int(
            selected["weekend_or_holiday_extra_delay"].sum()
        ),
        "controlled_position_column": controlled_position_column,
    }
