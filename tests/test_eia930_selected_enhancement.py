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


def test_selected_overlay_reproduces_headline_metrics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sync_requests: list[dict[str, Path]] = []

    def record_sync_request(**kwargs: Path) -> tuple[Path, ...]:
        sync_requests.append(kwargs)
        return ()

    monkeypatch.setattr(
        "naturalgas.evaluate_eia930_selected_enhancement."
        "synchronize_after_canonical_result",
        record_sync_request,
    )
    summary = run(
        formal_daily_path=FORMAL_DAILY,
        overlay_inputs_path=OVERLAY_INPUTS,
        event_reports_path=DEFAULT_EVENT_REPORTS_PATH,
        output_dir=tmp_path,
    )

    assert summary["trading_days"] == 1748
    assert summary["sample_start"] == pd.Timestamp("2019-07-25")
    assert summary["sample_end"] == pd.Timestamp("2026-07-13")
    assert summary["baseline_metrics"]["sharpe"] == pytest.approx(
        1.8564561015875451
    )
    assert summary["current_central_metrics"]["sharpe"] == pytest.approx(
        1.9514899273581137
    )
    assert summary["selected_metrics"]["sharpe"] == pytest.approx(
        2.0838457860556003
    )
    assert summary["selected_metrics"]["sortino"] == pytest.approx(
        3.5759264377444935
    )
    assert summary["change_vs_current_central"]["sortino"] == pytest.approx(
        0.3237455749278948
    )
    assert summary["selected_event_veto_days"] == 7
    assert summary["florida_available_ba_fallback_position_dates"] == 16
    assert summary["loss_day_diagnostics"]["central_loss_days"] == 808
    assert summary["loss_day_diagnostics"]["improved_loss_days"] == 544
    assert (tmp_path / "central_florida_weight_sweep.csv").exists()
    assert (tmp_path / "loss_day_yearly.csv").exists()
    assert sync_requests == [
        {
            "output_dir": tmp_path,
            "canonical_output_dir": (
                Path(__file__).resolve().parents[1]
                / "results/experiments/eia930_selected"
            ),
            "root": Path(__file__).resolve().parents[1],
        }
    ]


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
    assert metrics["maximum_drawdown"] == pytest.approx(-0.05287368295114803)
    assert metrics["total_turnover"] == pytest.approx(118.9203664660264)


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
    assert int(daily["florida_available_ba_fallback_position_date"].sum()) == 16
    assert daily["position_source_florida_available_ba_count"].min() == 6
    assert not daily["position_source_florida_respondents"].str.contains(
        "SCEG"
    ).any()


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
    assert loss_summary["central_loss_days"] == 808
    assert loss_summary["improved_loss_days"] == 544
    assert loss_summary["loss_day_incremental_net_return"] > 0.0
    assert loss_summary["nonloss_day_incremental_net_return"] < 0.0
    assert yearly["year"].tolist() == list(range(2019, 2027))
