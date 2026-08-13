import pandas as pd

from naturalgas.execution import apply_early_roll_return, build_ng_roll_calendar
from naturalgas.nymex_session_calendar import filter_confirmed_nymex_sessions


def test_early_roll_precedes_official_switch_by_five_sessions() -> None:
    dates = pd.bdate_range("2019-11-01", "2020-01-31")
    official = build_ng_roll_calendar(dates)
    early = build_ng_roll_calendar(dates, roll_advance_days=5)
    merged = official.merge(
        early[["delivery_month", "roll_switch_date"]],
        on="delivery_month",
        suffixes=("_official", "_early"),
        validate="one_to_one",
    )
    january = merged.loc[merged["delivery_month"].eq(pd.Timestamp("2020-01-01"))]
    assert january["roll_switch_date_early"].item() == pd.Timestamp("2019-12-20")
    assert january["roll_switch_date_official"].item() == pd.Timestamp("2019-12-30")

    sessions = filter_confirmed_nymex_sessions(pd.DataFrame({"date": dates}))
    early_switch = january["roll_switch_date_early"].item()
    official_switch = january["roll_switch_date_official"].item()
    early_window = sessions["date"].ge(early_switch) & sessions["date"].lt(
        official_switch
    )
    assert int(early_window.sum()) == 5


def test_known_holiday_carry_rows_are_not_nymex_sessions() -> None:
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(
                [
                    "2019-01-18",
                    "2019-01-21",
                    "2019-02-18",
                    "2019-05-27",
                    "2019-08-30",
                    "2019-09-02",
                    "2019-09-03",
                    "2019-12-25",
                ]
            ),
            "value": range(8),
        }
    )

    filtered = filter_confirmed_nymex_sessions(frame)

    assert filtered["date"].tolist() == [
        pd.Timestamp("2019-01-18"),
        pd.Timestamp("2019-08-30"),
        pd.Timestamp("2019-09-03"),
    ]


def test_memorial_day_carry_does_not_count_toward_early_roll() -> None:
    dates = pd.bdate_range("2019-04-01", "2019-06-28")
    official = build_ng_roll_calendar(dates)
    early = build_ng_roll_calendar(dates, roll_advance_days=5)
    schedule = official.merge(
        early[["delivery_month", "roll_switch_date"]],
        on="delivery_month",
        suffixes=("_official", "_early"),
        validate="one_to_one",
    )

    june = schedule.loc[
        schedule["delivery_month"].eq(pd.Timestamp("2019-06-01"))
    ]
    assert june["official_ltd"].item() == pd.Timestamp("2019-05-29")
    assert june["roll_switch_date_early"].item() == pd.Timestamp("2019-05-22")

    sessions = filter_confirmed_nymex_sessions(pd.DataFrame({"date": dates}))
    early_switch = june["roll_switch_date_early"].item()
    official_switch = june["roll_switch_date_official"].item()
    early_window = sessions["date"].ge(early_switch) & sessions["date"].lt(
        official_switch
    )
    assert int(early_window.sum()) == 5
    assert pd.Timestamp("2019-05-27") not in set(sessions["date"])


def test_early_roll_return_skips_christmas_carry_row() -> None:
    dates = pd.bdate_range("2019-11-01", "2020-01-31")
    panel = pd.DataFrame(
        {
            "date": dates,
            "c1": 2.0,
            "c2": 2.0,
            "roll_adjusted_return": 0.0,
            "is_roll_switch": False,
        }
    )
    panel.loc[panel["date"].eq("2019-12-19"), "c2"] = 2.00
    panel.loc[panel["date"].eq("2019-12-20"), "c2"] = 2.04

    result = apply_early_roll_return(panel)

    assert pd.Timestamp("2019-12-25") not in set(result["date"])
    dec_20 = result.loc[result["date"].eq("2019-12-20")]
    assert abs(dec_20["roll_adjusted_return"].item() - 0.02) < 1e-12
