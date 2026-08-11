from pathlib import Path

import pandas as pd
import pytest

from naturalgas.evaluate_eia930_selected_enhancement import (
    BASE_NET_RETURN,
    CENTRAL_SHARE,
    CENTRAL_SIGNAL,
    CURRENT_CENTRAL_NET_RETURN,
    DEFAULT_EVENT_REPORTS_PATH,
    FLORIDA_SHARE,
    FLORIDA_SIGNAL,
    FORMAL_DAILY,
    OVERLAY_INPUTS,
    SELECTED_SIGNAL,
    SELECTED_NET_RETURN,
    SELECTED_POSITION,
    build_daily,
    loss_day_diagnostics,
    performance,
    run,
    weight_sweep_metrics,
)


def test_selected_overlay_reproduces_headline_metrics(tmp_path: Path) -> None:
    summary = run(
        formal_daily_path=FORMAL_DAILY,
        overlay_inputs_path=OVERLAY_INPUTS,
        event_reports_path=DEFAULT_EVENT_REPORTS_PATH,
        output_dir=tmp_path,
    )

    assert summary["trading_days"] == 1737
    assert summary["sample_start"] == pd.Timestamp("2019-07-25")
    assert summary["sample_end"] == pd.Timestamp("2026-07-13")
    assert summary["baseline_metrics"]["sharpe"] == pytest.approx(
        1.8750072860084992
    )
    assert summary["current_central_metrics"]["sharpe"] == pytest.approx(
        1.9924526340708062
    )
    assert summary["selected_metrics"]["sharpe"] == pytest.approx(
        2.1149418826774244
    )
    assert summary["selected_metrics"]["sortino"] == pytest.approx(
        3.642640821643531
    )
    assert summary["change_vs_current_central"]["sortino"] == pytest.approx(
        0.31355419456722
    )
    assert summary["selected_event_veto_days"] == 7
    assert summary["loss_day_diagnostics"]["central_loss_days"] == 801
    assert summary["loss_day_diagnostics"]["improved_loss_days"] == 541
    assert (tmp_path / "central_florida_weight_sweep.csv").exists()
    assert (tmp_path / "loss_day_yearly.csv").exists()


def test_selected_daily_contains_costed_return_series() -> None:
    daily, _ = build_daily(
        formal_daily_path=FORMAL_DAILY,
        overlay_inputs_path=OVERLAY_INPUTS,
        event_reports_path=DEFAULT_EVENT_REPORTS_PATH,
    )
    metrics = performance(
        daily[SELECTED_NET_RETURN], daily["date"], daily[SELECTED_POSITION]
    )

    assert daily[BASE_NET_RETURN].notna().all()
    assert daily[CURRENT_CENTRAL_NET_RETURN].notna().all()
    assert daily[SELECTED_NET_RETURN].notna().all()
    assert metrics["maximum_drawdown"] == pytest.approx(-0.05270656800006157)
    assert metrics["total_turnover"] == pytest.approx(118.65967322864064)


def test_selected_signal_weights_and_timing_are_fixed() -> None:
    daily, _ = build_daily(
        formal_daily_path=FORMAL_DAILY,
        overlay_inputs_path=OVERLAY_INPUTS,
        event_reports_path=DEFAULT_EVENT_REPORTS_PATH,
    )
    expected = CENTRAL_SHARE * daily[CENTRAL_SIGNAL] + FLORIDA_SHARE * daily[
        FLORIDA_SIGNAL
    ]
    assert daily[SELECTED_SIGNAL].equals(expected)
    assert (daily["position_source_gas_day_central"] < daily["date"]).all()
    assert (daily["position_source_gas_day_florida"] < daily["date"]).all()


def test_weight_sweep_and_loss_day_diagnostics() -> None:
    daily, _ = build_daily(
        formal_daily_path=FORMAL_DAILY,
        overlay_inputs_path=OVERLAY_INPUTS,
        event_reports_path=DEFAULT_EVENT_REPORTS_PATH,
    )
    sweep = weight_sweep_metrics(daily)
    loss_summary, yearly = loss_day_diagnostics(daily)

    assert len(sweep) == 11
    assert sweep.loc[sweep["full_sharpe"].idxmax(), "florida_weight"] == 0.8
    validation = "validation_2021_2023__sharpe"
    assert sweep.loc[sweep[validation].idxmax(), "florida_weight"] == 0.6
    assert loss_summary["central_loss_days"] == 801
    assert loss_summary["improved_loss_days"] == 541
    assert loss_summary["loss_day_incremental_net_return"] > 0.0
    assert loss_summary["nonloss_day_incremental_net_return"] < 0.0
    assert yearly["year"].tolist() == list(range(2019, 2027))
