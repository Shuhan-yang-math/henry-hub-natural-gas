"""Audited NYMEX natural-gas business-session classifications.

The legacy EIA settlement input contains rows on calendar holidays that are
not separate CME/NYMEX business trade dates.  CME assigns holiday trading to
the next business trade date and carries the prior settlement on the holiday
row.  These classifications are the dates found by the 2026-07 data audit.

Primary references:
https://www.cmegroup.com/trading-hours.html
https://www.cmegroup.com/tools-information/holiday-calendar/files/2015-thanksgiving-holiday-schedule.pdf
https://www.cmegroup.com/tools-information/holiday-calendar/files/2019-mlk-day-advisory.pdf
https://www.cmegroup.com/tools-information/holiday-calendar/files/2019-presidents-day-advisory.pdf
https://www.cmegroup.com/tools-information/holiday-calendar/files/2019-memorial-day-advisory.pdf
https://www.cmegroup.com/tools-information/lookups/advisories/market-data/Q2010-49.html
"""

from __future__ import annotations

from collections.abc import Iterable

import pandas as pd


# False means the calendar date is not a separate NYMEX business trade date.
# True means it is a genuine session even though Henry Hub spot is unavailable.
AUDITED_NYMEX_SESSION_STATUS = {
    "2014-01-20": False,  # Martin Luther King Jr. Day
    "2014-02-17": False,  # Presidents Day
    "2018-01-01": False,  # New Year's Day
    "2018-01-05": True,
    "2018-01-15": False,  # Martin Luther King Jr. Day
    "2018-02-19": False,  # Presidents Day
    "2018-03-30": False,  # Good Friday
    "2018-05-28": False,  # Memorial Day
    "2018-07-04": False,  # Independence Day
    "2018-09-03": False,  # Labor Day
    "2018-11-22": False,  # Thanksgiving
    "2018-11-23": True,   # Black Friday shortened session
    "2018-12-24": True,   # Christmas Eve shortened session
    "2018-12-25": False,  # Christmas Day
    "2018-12-31": True,
    "2019-01-01": False,  # New Year's Day
    "2019-01-21": False,  # Martin Luther King Jr. Day
    "2019-02-18": False,  # Presidents Day
    "2019-04-19": False,  # Good Friday
    "2019-05-27": False,  # Memorial Day
    "2019-07-04": False,  # Independence Day
    "2019-07-05": True,
    "2019-09-02": False,  # Labor Day
    "2019-11-28": False,  # Thanksgiving
    "2019-12-25": False,  # Christmas Day
    "2020-03-24": True,
    "2022-10-10": True,   # Columbus Day: valid Globex session
    "2023-10-09": True,   # Columbus Day: valid Globex session
    "2024-10-14": True,   # Columbus Day: valid Globex session
    "2025-10-13": True,   # Columbus Day: valid Globex session
    "2025-11-11": True,   # Veterans Day: valid Globex session
    "2025-11-28": True,   # Black Friday shortened session
    "2025-12-26": True,
    "2026-01-02": True,
}

AUDITED_NYMEX_SESSION_STATUS = {
    pd.Timestamp(date): is_session
    for date, is_session in AUDITED_NYMEX_SESSION_STATUS.items()
}

CONFIRMED_NON_SESSION_DATES = pd.DatetimeIndex(
    [
        date
        for date, is_session in AUDITED_NYMEX_SESSION_STATUS.items()
        if not is_session
    ]
).sort_values()

CONFIRMED_GENUINE_SESSION_DATES_WITHOUT_SPOT = pd.DatetimeIndex(
    [
        date
        for date, is_session in AUDITED_NYMEX_SESSION_STATUS.items()
        if is_session
    ]
).sort_values()


def normalize_dates(values: Iterable) -> pd.DatetimeIndex:
    """Return normalized, timezone-naive dates."""

    dates = pd.DatetimeIndex(pd.to_datetime(values)).tz_localize(None)
    return dates.normalize()


def filter_confirmed_nymex_sessions(
    frame: pd.DataFrame,
    date_column: str = "date",
) -> pd.DataFrame:
    """Remove audited legacy rows that are not NYMEX business trade dates."""

    normalized = normalize_dates(frame[date_column])
    keep = ~normalized.isin(CONFIRMED_NON_SESSION_DATES)
    result = frame.loc[keep].copy()
    result[date_column] = pd.to_datetime(result[date_column]).dt.normalize()
    return result


def audited_session_status(values: Iterable) -> pd.Series:
    """Return the audited status for known dates; unknown dates remain null."""

    normalized = normalize_dates(values)
    return pd.Series(
        [AUDITED_NYMEX_SESSION_STATUS.get(date, pd.NA) for date in normalized],
        index=getattr(values, "index", None),
        dtype="boolean",
    )
