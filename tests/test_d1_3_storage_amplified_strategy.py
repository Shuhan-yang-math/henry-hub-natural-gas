from __future__ import annotations

import numpy as np
import pandas as pd

from naturalgas.evaluate_d1_3_storage_amplified_strategy import (
    FORMAL_DAILY,
    NET_D1_3,
    NET_D1_5,
    NET_SELECTED,
    POS_D1_3,
    POS_D1_5,
    POS_SELECTED,
    SCORE_INPUTS,
    STORAGE_CALENDAR_CORRECTIONS,
    apply_storage_calendar_corrections,
    build_daily,
    intervention_summary,
    metrics_tables,
    performance,
    recompute_guard_states,
    validate_score_inputs,
)
from naturalgas.eia930_florida_availability import validate_score_history
from naturalgas.shutin_notice_event_controller import DEFAULT_EVENT_REPORTS_PATH


def test_low_storage_is_an_amplifier_not_a_standalone_trigger() -> None:
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(["2025-01-10", "2025-01-11"]),
            "prod_freeze_local_level_score": [0.0, 0.0],
            "prod_freeze_local_revision_score": [0.0, 0.0],
            "hdd_revision_5d_z": [0.0, 0.5],
            "central_firm_nongas_shortfall": [0.0, 0.0],
            "signal__firm__florida": [0.0, 0.0],
            "south_central_total_level_signal": [1.5, 1.5],
        }
    )
    states = recompute_guard_states(frame)

    assert states["low_storage"].all()
    assert not states.loc[0, "fast_plus_storage_amplifier"]
    assert states.loc[1, "storage_amplifier_only"]
    assert states.loc[1, "fast_plus_storage_amplifier"]


def test_hdd_revision_guard_is_disabled_only_in_june_through_august() -> None:
    months = np.arange(1, 13)
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(
                [f"2025-{month:02d}-15" for month in months]
            ),
            "prod_freeze_local_level_score": 0.0,
            "prod_freeze_local_revision_score": 0.0,
            "hdd_revision_5d_z": 2.0,
            "central_firm_nongas_shortfall": 0.0,
            "signal__firm__florida": 0.0,
            "south_central_total_level_signal": 0.0,
        }
    )

    states = recompute_guard_states(frame)

    assert set(months[states["hdd_strong"]]) == {
        1,
        2,
        3,
        4,
        5,
        9,
        10,
        11,
        12,
    }
    assert states["hdd_moderate"].equals(states["hdd_strong"])
    assert states["fast_strong"].equals(states["hdd_strong"])


def test_performance_includes_inception_drawdown_and_prior_position() -> None:
    metrics = performance(
        pd.Series([-0.10, 0.02]),
        pd.Series(pd.to_datetime(["2025-01-02", "2025-01-03"])),
        pd.Series([0.4, 0.4]),
        first_return_start_date=pd.Timestamp("2025-01-01"),
        prior_position=0.1,
    )

    assert np.isclose(metrics["maximum_drawdown"], -0.10)
    assert np.isclose(metrics["total_turnover"], 0.30)


def test_performance_cagr_includes_first_return_interval() -> None:
    returns = pd.Series([0.10, 0.0])
    metrics = performance(
        returns,
        pd.Series(pd.to_datetime(["2024-01-02", "2025-01-01"])),
        pd.Series([0.0, 0.0]),
        first_return_start_date=pd.Timestamp("2024-01-01"),
    )

    elapsed_years = 366.0 / 365.2425
    expected_cagr = np.exp(np.log1p(returns).sum() / elapsed_years) - 1.0
    label_only_years = 365.0 / 365.2425
    old_cagr = np.exp(np.log1p(returns).sum() / label_only_years) - 1.0

    assert np.isclose(metrics["cagr"], expected_cagr)
    assert not np.isclose(metrics["cagr"], old_cagr)


def test_period_turnover_inherits_previous_full_path_position() -> None:
    daily = pd.DataFrame(
        {
            "date": pd.to_datetime(
                ["2023-12-29", "2024-01-02", "2024-01-03"]
            ),
            "position_source_date": pd.to_datetime(
                ["2023-12-28", "2023-12-29", "2024-01-02"]
            ),
            NET_D1_5: [0.001, 0.002, -0.001],
            NET_D1_3: [0.001, 0.002, -0.001],
            NET_SELECTED: [0.001, 0.002, -0.001],
            POS_D1_5: [0.2, 0.5, 0.5],
            POS_D1_3: [0.2, 0.5, 0.5],
            POS_SELECTED: [0.2, 0.5, 0.5],
        }
    )

    _, periods, annual = metrics_tables(daily)
    first_look = periods.loc[
        periods["period"].eq("first_look_2024_plus")
        & periods["variant"].eq("d1_3_storage_amplified")
    ].iloc[0]
    year_2024 = annual.loc[
        annual["year"].eq(2024)
        & annual["variant"].eq("d1_3_storage_amplified")
    ].iloc[0]

    assert np.isclose(first_look["total_turnover"], 0.30)
    assert np.isclose(year_2024["total_turnover"], 0.30)


def test_return_difference_summary_separates_sum_and_compounding() -> None:
    daily = pd.DataFrame(
        {
            "guard_blocked_position_date": [True, True],
            "incremental_net_return_vs_d1_3": [0.10, 0.10],
            "incremental_net_return_vs_d1_5": [0.05, 0.05],
            NET_SELECTED: [0.10, 0.10],
            NET_D1_3: [0.0, 0.0],
            NET_D1_5: [0.05, 0.05],
        }
    )

    summary = intervention_summary(daily)

    assert "incremental_net_return_vs_d1_3" not in summary
    assert np.isclose(
        summary["sum_paired_daily_net_return_difference_vs_d1_3"],
        0.20,
    )
    assert np.isclose(
        summary["compounded_final_wealth_difference_vs_d1_3"],
        0.21,
    )
    assert np.isclose(
        summary["sum_paired_daily_net_return_difference_vs_d1_5"],
        0.10,
    )
    assert np.isclose(
        summary["compounded_final_wealth_difference_vs_d1_5"],
        0.1075,
    )


def test_shipped_selected_strategy_reproduces() -> None:
    daily, _ = build_daily(
        formal_daily_path=FORMAL_DAILY,
        score_inputs_path=SCORE_INPUTS,
        storage_calendar_corrections_path=STORAGE_CALENDAR_CORRECTIONS,
        event_reports_path=DEFAULT_EVENT_REPORTS_PATH,
    )
    metrics = performance(
        daily[NET_SELECTED],
        daily["date"],
        daily[POS_SELECTED],
        first_return_start_date=daily["position_source_date"].iloc[0],
    )

    assert len(daily) == 1748
    assert not daily["date"].isin(
        pd.to_datetime(["2019-09-02", "2019-12-25"])
    ).any()
    assert daily.loc[
        daily["date"].eq("2019-12-26"), "position_source_date"
    ].item() == pd.Timestamp("2019-12-24")
    assert daily["guard_blocked_position_date"].notna().all()
    assert int(daily["guard_blocked_position_date"].sum()) == 59
    assert int(
        daily["storage_release_calendar_corrected_position_date"].sum()
    ) == 23
    assert int(daily["florida_available_ba_fallback_position_date"].sum()) == 16
    assert daily["position_source_florida_available_ba_count"].min() == 6
    assert np.isclose(metrics["cagr"], 0.19050835194298243, atol=1e-12)
    assert np.isclose(metrics["sharpe"], 2.2280397376832175, atol=1e-12)
    assert np.isclose(metrics["sortino"], 3.8809211748765535, atol=1e-12)
    assert np.isclose(metrics["maximum_drawdown"], -0.041646633466991045, atol=1e-12)


def test_available_ba_history_restores_five_florida_outages() -> None:
    inputs = pd.read_parquet(SCORE_INPUTS)
    validate_score_history(inputs, signal_column="signal__firm__florida")
    outage_score_dates = pd.to_datetime(
        [
            "2020-02-07",
            "2020-09-15",
            "2023-11-01",
            "2023-11-02",
            "2026-05-19",
        ]
    )
    outage_rows = inputs.loc[inputs["date"].isin(outage_score_dates)]
    assert len(outage_rows) == 5
    assert outage_rows["florida_available_ba_count"].eq(8).all()
    np.testing.assert_allclose(
        outage_rows["signal__firm__florida"],
        [0.529981, -0.341932, 0.474080, 0.204972, 0.142570],
        atol=5e-7,
    )
    assert int(inputs["florida_available_ba_count"].lt(9).sum()) == 16


def test_storage_calendar_overlay_is_narrow_and_recomputes_guard_state() -> None:
    inputs = pd.read_parquet(SCORE_INPUTS)
    inputs["date"] = pd.to_datetime(inputs["date"]).dt.normalize()
    corrections = pd.read_parquet(STORAGE_CALENDAR_CORRECTIONS)
    corrections["date"] = pd.to_datetime(corrections["date"]).dt.normalize()
    validate_score_inputs(inputs)

    corrected = apply_storage_calendar_corrections(inputs, corrections)
    applied = corrected["storage_release_calendar_correction_applied"]
    assert int(applied.sum()) == 23
    np.testing.assert_allclose(
        corrected.loc[~applied, "score_d1_3_no_guard"],
        inputs.loc[~applied, "score_d1_3_no_guard"],
        atol=0.0,
        rtol=0.0,
    )

    christmas = corrected.loc[corrected["date"].eq("2024-12-26")].iloc[0]
    assert christmas["legacy_south_central_total_level_signal"] < 1.0
    assert christmas["south_central_total_level_signal"] > 1.0
    assert bool(christmas["fast_guard__low_storage"])

    mourning = corrected.loc[corrected["date"].eq("2025-01-08")].iloc[0]
    assert mourning["score_without_wind"] == 0.0
    assert mourning["score_d1_3_no_guard"] == 0.0
    assert mourning["score_d1_5_no_guard"] == 0.0
