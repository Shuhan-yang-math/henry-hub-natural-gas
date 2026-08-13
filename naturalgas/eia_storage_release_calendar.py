"""Point-in-time release calendar for the EIA Weekly Natural Gas Storage Report.

The normal WNGSR release is Thursday at 10:30 a.m. Eastern, six calendar
days after the Friday observation date.  EIA publishes an annual holiday
schedule with exceptions to that rule.  The strategy cannot infer those
exceptions from the revised weekly bulk data because that data contains the
observation period, not the original publication timestamp.

The exception registry below was transcribed from the final official EIA
schedule page for each year.  Historical pages were retrieved from the
Internet Archive; the current page is https://ir.eia.gov/ngs/schedule.html.
The audited strategy interval ends with the week ending 2026-07-03.  Dates
outside the audited interval retain the normal rule and are explicitly marked
as unaudited.
"""

from __future__ import annotations

from datetime import time
from typing import Final

import pandas as pd


EASTERN_TIME_ZONE: Final = "America/New_York"
NORMAL_RELEASE_TIME: Final = time(10, 30)
STRATEGY_INFORMATION_CUTOFF_TIME: Final = time(14, 30)
AUDITED_START_WEEK_ENDING: Final = pd.Timestamp("2017-01-06")
AUDITED_THROUGH_WEEK_ENDING: Final = pd.Timestamp("2026-07-03")
CURRENT_SCHEDULE_URL: Final = "https://ir.eia.gov/ngs/schedule.html"

# week-ending Friday -> (actual release date, Eastern time, reason)
#
# This registry includes every exception on the final official annual schedule
# that maps to a report week in 2017--2025.  The 2026 schedule has no exception
# before the strategy's audited data cutoff.
WNGSR_RELEASE_EXCEPTIONS: Final[dict[str, tuple[str, time, str]]] = {
    "2017-06-30": ("2017-07-07", time(10, 30), "Independence Day"),
    "2017-11-17": ("2017-11-22", time(12, 0), "Thanksgiving Day"),
    "2018-06-29": ("2018-07-06", time(10, 30), "Independence Day"),
    "2018-11-16": ("2018-11-21", time(12, 0), "Thanksgiving Day"),
    "2018-11-30": (
        "2018-12-07",
        time(10, 30),
        "National Day of Mourning",
    ),
    "2018-12-21": ("2018-12-28", time(10, 30), "Christmas Day"),
    "2018-12-28": ("2019-01-04", time(10, 30), "New Year's Day"),
    "2019-06-28": ("2019-07-03", time(12, 0), "Independence Day"),
    "2019-11-22": ("2019-11-27", time(12, 0), "Thanksgiving Day"),
    "2019-12-20": ("2019-12-27", time(10, 30), "Christmas Day"),
    "2019-12-27": ("2020-01-03", time(10, 30), "New Year's Day"),
    "2020-11-06": ("2020-11-13", time(10, 30), "Veterans Day"),
    "2020-11-20": ("2020-11-25", time(12, 0), "Thanksgiving Day"),
    "2020-12-18": ("2020-12-23", time(12, 0), "Christmas Day"),
    "2021-01-15": ("2021-01-22", time(10, 30), "Inauguration Day"),
    "2021-11-05": ("2021-11-10", time(12, 0), "Veterans Day"),
    "2021-11-19": ("2021-11-24", time(12, 0), "Thanksgiving Day"),
    "2022-11-18": ("2022-11-23", time(12, 0), "Thanksgiving Day"),
    "2023-06-30": ("2023-07-07", time(10, 30), "Independence Day"),
    "2023-11-17": ("2023-11-22", time(12, 0), "Thanksgiving Day"),
    "2024-06-14": ("2024-06-21", time(10, 30), "Juneteenth"),
    "2024-06-28": ("2024-07-03", time(12, 0), "Independence Day"),
    "2024-11-22": ("2024-11-27", time(12, 0), "Thanksgiving Day"),
    "2024-12-20": ("2024-12-27", time(10, 30), "Christmas Day"),
    "2024-12-27": ("2025-01-03", time(10, 30), "New Year's Day"),
    "2025-01-03": (
        "2025-01-08",
        time(12, 0),
        "National Day of Mourning",
    ),
    "2025-06-13": ("2025-06-18", time(12, 0), "Juneteenth"),
    "2025-11-07": ("2025-11-14", time(10, 30), "Veterans Day"),
    "2025-11-21": ("2025-11-26", time(12, 0), "Thanksgiving Day"),
    "2025-12-19": ("2025-12-29", time(12, 0), "Christmas Day"),
    "2025-12-26": ("2025-12-31", time(12, 0), "New Year's Day"),
}


def wngsr_release_calendar(week_ending: pd.Series) -> pd.DataFrame:
    """Return actual WNGSR publication timestamps for weekly observations.

    ``storage_available_date`` is the first strategy date on which the value
    may be consumed.  Every audited release occurred before the strategy's
    2:30 p.m. Eastern information cutoff, so it is the publication date.  The
    timestamp is retained to make that cutoff assertion testable.
    """

    weeks = pd.to_datetime(week_ending, errors="raise").dt.normalize()
    if weeks.isna().any():
        raise ValueError("WNGSR week-ending dates cannot be missing")
    if not weeks.dt.dayofweek.eq(4).all():
        invalid = weeks.loc[~weeks.dt.dayofweek.eq(4)].dt.strftime(
            "%Y-%m-%d"
        ).tolist()
        raise ValueError(f"WNGSR week-ending dates must be Fridays: {invalid}")

    release_date = weeks + pd.Timedelta(days=6)
    release_minutes = pd.Series(
        NORMAL_RELEASE_TIME.hour * 60 + NORMAL_RELEASE_TIME.minute,
        index=weeks.index,
        dtype="int64",
    )
    reason = pd.Series("standard Thursday release", index=weeks.index)
    is_exception = pd.Series(False, index=weeks.index, dtype=bool)

    for week_text, (release_text, release_time, release_reason) in (
        WNGSR_RELEASE_EXCEPTIONS.items()
    ):
        mask = weeks.eq(pd.Timestamp(week_text))
        if not mask.any():
            continue
        release_date.loc[mask] = pd.Timestamp(release_text)
        release_minutes.loc[mask] = (
            release_time.hour * 60 + release_time.minute
        )
        reason.loc[mask] = release_reason
        is_exception.loc[mask] = True

    local_naive = release_date + pd.to_timedelta(release_minutes, unit="m")
    published_at_utc = local_naive.dt.tz_localize(
        EASTERN_TIME_ZONE,
        ambiguous="raise",
        nonexistent="raise",
    ).dt.tz_convert("UTC")
    cutoff_local = release_date + pd.to_timedelta(
        STRATEGY_INFORMATION_CUTOFF_TIME.hour * 60
        + STRATEGY_INFORMATION_CUTOFF_TIME.minute,
        unit="m",
    )
    cutoff_utc = cutoff_local.dt.tz_localize(
        EASTERN_TIME_ZONE,
        ambiguous="raise",
        nonexistent="raise",
    ).dt.tz_convert("UTC")

    audited = weeks.between(
        AUDITED_START_WEEK_ENDING,
        AUDITED_THROUGH_WEEK_ENDING,
    )
    if not published_at_utc.loc[audited].le(cutoff_utc.loc[audited]).all():
        raise AssertionError(
            "An audited WNGSR release is after the strategy information cutoff"
        )
    schedule_status = pd.Series(
        "normal_rule_unverified",
        index=weeks.index,
        dtype="object",
    )
    schedule_status.loc[audited] = "audited_standard"
    schedule_status.loc[is_exception] = "audited_exception"

    return pd.DataFrame({
        "wngsr_published_at_utc": published_at_utc,
        "storage_available_date": release_date.astype("datetime64[ns]"),
        "wngsr_release_is_exception": is_exception,
        "wngsr_release_reason": reason,
        "wngsr_schedule_status": schedule_status,
    })
