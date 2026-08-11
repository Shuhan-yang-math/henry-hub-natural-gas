import pandas as pd

from naturalgas.execution import build_ng_roll_calendar


def test_early_roll_precedes_official_switch_by_five_sessions() -> None:
    dates = pd.bdate_range("2025-01-02", "2025-04-30")
    official = build_ng_roll_calendar(dates)
    early = build_ng_roll_calendar(dates, roll_advance_days=5)
    merged = official.merge(
        early[["delivery_month", "roll_switch_date"]],
        on="delivery_month",
        suffixes=("_official", "_early"),
        validate="one_to_one",
    )
    assert (merged["roll_switch_date_early"] < merged["roll_switch_date_official"]).all()
