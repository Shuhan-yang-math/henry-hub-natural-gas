from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from naturalgas.build_multisignal_panel import DEFAULT_INPUT_MANIFEST
from naturalgas.pipelines.rebuild_all import (
    EXPECTED_CORRECTED_MASTER_PANEL_SHA256,
    rebuild_all,
)
from naturalgas.reproducibility import DEFAULT_MANIFEST


@pytest.mark.skipif(
    os.environ.get("RUN_HENRY_HUB_FULL_CHAIN") != "1",
    reason="set RUN_HENRY_HUB_FULL_CHAIN=1 for the 72-object rebuild",
)
def test_pinned_master_panel_through_formal_backtest(tmp_path: Path) -> None:
    receipt = rebuild_all(
        panel_manifest=DEFAULT_INPUT_MANIFEST,
        weather_manifest=(
            Path(__file__).resolve().parents[1]
            / "manifests/weather_factor_inputs_2026-07-28.json"
        ),
        formal_manifest=DEFAULT_MANIFEST,
        root=tmp_path,
        output_dir=tmp_path / "full_chain",
        fetch=True,
        overwrite=False,
        rebuild_weather=False,
    )
    panel_path = Path(receipt["rebuilt_panel"])
    assert hashlib.sha256(panel_path.read_bytes()).hexdigest() == (
        EXPECTED_CORRECTED_MASTER_PANEL_SHA256
    )
    assert receipt["master_panel_rows"] == 8144
    assert receipt["master_panel_columns"] == 155
    summary = receipt["formal_rebuild"]["summary"]
    assert summary["trading_days"] == 2264
    assert summary["selected_full_metrics"]["sharpe_zero_rf"] == (
        1.667459455270079
    )
