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
    reason=(
        "set RUN_HENRY_HUB_FULL_CHAIN=1 for the 72-object and 254-weather "
        "full rebuild"
    ),
)
def test_pinned_gcs_inputs_through_formal_and_selected_backtests(
    tmp_path: Path,
) -> None:
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
        rebuild_weather=True,
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
    horizons = receipt["weather_factor_rebuild"]["wind_horizons"]
    assert horizons["output_integrity"]["sha256"] == (
        "34fb31802a41144e5ed842d2433a1b67db8d93810cf900835c875913f62db94c"
    )
    selected = receipt["selected_d1_3_rebuild"]
    assert receipt["selected_input_artifacts_validated"] == 13
    assert selected["wind_lineage"]["status"] == "exact"
    assert selected["summary"]["trading_days"] == 1748
    assert selected["summary"]["selected_metrics"]["sharpe"] == (
        2.2280397376832175
    )
