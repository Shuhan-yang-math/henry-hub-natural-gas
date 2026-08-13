from __future__ import annotations

import numpy as np
import pandas as pd

from naturalgas.evaluate_d1_3_storage_amplified_strategy import (
    FORMAL_DAILY,
    NET_SELECTED,
    POS_SELECTED,
    SCORE_INPUTS,
    build_daily,
    performance,
    recompute_guard_states,
)
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


def test_shipped_selected_strategy_reproduces() -> None:
    daily, _ = build_daily(
        formal_daily_path=FORMAL_DAILY,
        score_inputs_path=SCORE_INPUTS,
        event_reports_path=DEFAULT_EVENT_REPORTS_PATH,
    )
    metrics = performance(daily[NET_SELECTED], daily["date"], daily[POS_SELECTED])

    assert len(daily) == 1735
    assert not daily["date"].isin(
        pd.to_datetime(["2019-09-02", "2019-12-25"])
    ).any()
    assert daily.loc[
        daily["date"].eq("2019-12-26"), "position_source_date"
    ].item() == pd.Timestamp("2019-12-24")
    assert daily["guard_blocked_position_date"].notna().all()
    assert int(daily["guard_blocked_position_date"].sum()) == 60
    assert np.isclose(metrics["sharpe"], 2.2399508852521746, atol=1e-12)
    assert np.isclose(metrics["sortino"], 3.9104199437025824, atol=1e-12)
    assert np.isclose(metrics["maximum_drawdown"], -0.04151797069188734, atol=1e-12)
