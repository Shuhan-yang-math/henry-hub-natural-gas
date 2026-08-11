from __future__ import annotations

import pandas as pd

from naturalgas.shutin_notice_event_controller import (
    CONTROLLED_POSITION_COLUMN,
    align_event_reports,
    apply_controller,
)


def _reports(dates: list[str]) -> pd.DataFrame:
    return pd.DataFrame({
        "event_date": pd.to_datetime(dates),
        "event_name": ["test_storm"] * len(dates),
        "shutin_revision_mmcfd": [100.0] * len(dates),
        "recent_notice_any": [True] * len(dates),
        "recent_notice_subjects": ["operational notice"] * len(dates),
    })


def test_weekday_report_controls_following_settlement_return() -> None:
    dates = pd.DatetimeIndex(pd.to_datetime([
        "2024-09-09",
        "2024-09-10",
        "2024-09-11",
    ]))
    aligned = align_event_reports(_reports(["2024-09-10"]), dates)

    assert aligned.loc[0, "controller_entry_settlement_date"] == pd.Timestamp(
        "2024-09-10"
    )
    assert aligned.loc[0, "controller_return_date"] == pd.Timestamp(
        "2024-09-11"
    )
    assert not bool(aligned.loc[0, "weekend_or_holiday_extra_delay"])


def test_weekend_report_skips_untradeable_weekend_return_interval() -> None:
    dates = pd.DatetimeIndex(pd.to_datetime([
        "2020-10-09",
        "2020-10-12",
        "2020-10-13",
    ]))
    aligned = align_event_reports(_reports(["2020-10-11"]), dates)

    assert aligned.loc[0, "controller_entry_settlement_date"] == pd.Timestamp(
        "2020-10-12"
    )
    assert aligned.loc[0, "controller_return_date"] == pd.Timestamp(
        "2020-10-13"
    )
    assert bool(aligned.loc[0, "weekend_or_holiday_extra_delay"])


def test_controller_only_vetoes_an_existing_short() -> None:
    daily = pd.DataFrame({
        "date": pd.to_datetime([
            "2024-09-09",
            "2024-09-10",
            "2024-09-11",
            "2024-09-12",
        ]),
        "core": [0.2, 0.3, -0.4, 0.5],
    })
    controlled, _ = apply_controller(
        daily,
        _reports(["2024-09-10"]),
        core_position_column="core",
    )

    assert controlled[CONTROLLED_POSITION_COLUMN].tolist() == [
        0.2,
        0.3,
        0.0,
        0.5,
    ]
    assert controlled["shutin_notice_controller_veto_applied"].sum() == 1
    assert (
        controlled[CONTROLLED_POSITION_COLUMN].abs()
        <= controlled["core"].abs()
    ).all()
