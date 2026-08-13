from datetime import time

import pandas as pd
import pytest

from naturalgas.eia_storage_release_calendar import (
    AUDITED_THROUGH_WEEK_ENDING,
    WNGSR_RELEASE_EXCEPTIONS,
    wngsr_release_calendar,
)


def test_registry_contains_all_audited_annual_schedule_exceptions() -> None:
    assert len(WNGSR_RELEASE_EXCEPTIONS) == 31
    assert WNGSR_RELEASE_EXCEPTIONS["2019-06-28"] == (
        "2019-07-03",
        time(12, 0),
        "Independence Day",
    )
    assert WNGSR_RELEASE_EXCEPTIONS["2025-12-19"] == (
        "2025-12-29",
        time(12, 0),
        "Christmas Day",
    )


def test_release_calendar_handles_standard_early_and_delayed_releases() -> None:
    weeks = pd.Series(pd.to_datetime([
        "2025-02-07",  # normal Thursday
        "2025-06-13",  # early Wednesday
        "2025-11-07",  # delayed Friday
        "2025-12-19",  # delayed Monday ten days later
    ]))
    actual = wngsr_release_calendar(weeks)

    assert actual["storage_available_date"].tolist() == list(pd.to_datetime([
        "2025-02-13",
        "2025-06-18",
        "2025-11-14",
        "2025-12-29",
    ]))
    assert actual["wngsr_published_at_utc"].astype(str).tolist() == [
        "2025-02-13 15:30:00+00:00",
        "2025-06-18 16:00:00+00:00",
        "2025-11-14 15:30:00+00:00",
        "2025-12-29 17:00:00+00:00",
    ]
    assert actual["wngsr_release_is_exception"].tolist() == [
        False,
        True,
        True,
        True,
    ]
    assert actual["wngsr_schedule_status"].eq(
        [
            "audited_standard",
            "audited_exception",
            "audited_exception",
            "audited_exception",
        ]
    ).all()


def test_calendar_marks_dates_after_audit_cutoff_as_unverified() -> None:
    future_week = pd.Series([AUDITED_THROUGH_WEEK_ENDING + pd.Timedelta(days=7)])
    actual = wngsr_release_calendar(future_week)

    assert actual["storage_available_date"].iloc[0] == pd.Timestamp(
        "2026-07-16"
    )
    assert actual["wngsr_schedule_status"].iloc[0] == "normal_rule_unverified"


def test_release_calendar_rejects_non_friday_observation_dates() -> None:
    with pytest.raises(ValueError, match="must be Fridays"):
        wngsr_release_calendar(pd.Series(pd.to_datetime(["2025-01-02"])))
