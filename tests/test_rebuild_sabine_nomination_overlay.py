from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd
import pytest

from naturalgas.audit_inputs import (
    SABINE_NOMINATION_ALL_CYCLE_ARTIFACT_ID,
    SABINE_NOMINATION_EXECUTION_ARTIFACT_ID,
    SABINE_NOMINATION_PANEL_ARTIFACT_ID,
)
from naturalgas.pipelines.rebuild_sabine_nomination_overlay import (
    DEFAULT_INPUT_MANIFEST,
    rebuild_sabine_nomination_overlay,
    verify_nomination_revision_lineage,
    verify_overlay_summary,
)
from naturalgas.reproducibility import load_manifest


def test_nomination_manifest_pins_complete_gcs_handoff() -> None:
    artifacts = load_manifest(DEFAULT_INPUT_MANIFEST)
    by_id = {artifact.artifact_id: artifact for artifact in artifacts}
    assert set(by_id) == {
        SABINE_NOMINATION_ALL_CYCLE_ARTIFACT_ID,
        SABINE_NOMINATION_PANEL_ARTIFACT_ID,
        SABINE_NOMINATION_EXECUTION_ARTIFACT_ID,
    }
    assert all(
        artifact.uri.startswith(
            "gs://bcli-natgas-data-497807/research/henry_hub_strategy/"
            "v2/inputs/sabine_nomination_overlay_final/2026-08-19/"
        )
        and artifact.generation > 0
        and len(artifact.sha256) == 64
        and artifact.size_bytes
        and artifact.rows
        and artifact.columns
        and artifact.schema_fingerprint_sha256
        for artifact in artifacts
    )


def _write_lineage_fixture(tmp_path: Path) -> tuple[Path, Path]:
    gas_dates = pd.date_range("2024-01-01", periods=4, freq="D")
    rows: list[dict[str, object]] = []
    for index, gas_date in enumerate(gas_dates):
        for cycle, hour in (
            ("Timely", 18),
            ("Intraday 1", 24),
            ("Intraday 3", 27),
        ):
            posting = pd.Timestamp(gas_date, tz="UTC") + pd.Timedelta(hours=hour)
            rows.extend([
                {
                    "gas_date": gas_date,
                    "cycle": cycle,
                    "posting_time_utc": posting,
                    "location_name": "TransCameron Pipeline",
                    "flow_indicator": "D",
                    "total_scheduled_quantity_dth_per_day": (
                        100 + index + (10 if cycle == "Intraday 3" else 0)
                    ),
                },
                {
                    "gas_date": gas_date,
                    "cycle": cycle,
                    "posting_time_utc": posting,
                    "location_name": "Jefferson Island - HH",
                    "flow_indicator": "D",
                    "total_scheduled_quantity_dth_per_day": (
                        50 + index + (5 if cycle == "Intraday 3" else 0)
                    ),
                },
                {
                    "gas_date": gas_date,
                    "cycle": cycle,
                    "posting_time_utc": posting,
                    "location_name": "Jefferson Island - HH",
                    "flow_indicator": "R",
                    "total_scheduled_quantity_dth_per_day": 10 + index,
                },
            ])
    raw = pd.DataFrame(rows)
    raw_path = tmp_path / "raw.parquet"
    raw.to_parquet(raw_path, index=False)

    from naturalgas.pipelines.rebuild_sabine_nomination_overlay import (
        rebuild_nomination_revisions,
    )

    rebuilt = rebuild_nomination_revisions(raw_path)
    panel = rebuilt.rename(
        columns={"posting_time_utc": "posting_time_utc_factor"}
    )
    panel["date"] = panel["gas_date"] + pd.Timedelta(days=1)
    panel_path = tmp_path / "panel.parquet"
    panel.to_parquet(panel_path, index=False)
    return raw_path, panel_path


def test_raw_nomination_lineage_requires_exact_factor_parity(
    tmp_path: Path,
) -> None:
    raw_path, panel_path = _write_lineage_fixture(tmp_path)
    receipt = verify_nomination_revision_lineage(
        raw_path=raw_path,
        research_panel_path=panel_path,
    )
    assert receipt["status"] == "exact"
    assert receipt["raw_gas_days"] == 4
    assert receipt["mapped_score_dates"] == 4
    assert set(receipt["maximum_absolute_difference"].values()) == {0.0}

    panel = pd.read_parquet(panel_path)
    panel.loc[0, "lng_feedgas_revision_from_intraday_1_raw"] += 1.0
    panel.to_parquet(panel_path, index=False)
    with pytest.raises(AssertionError, match="Raw-rebuilt"):
        verify_nomination_revision_lineage(
            raw_path=raw_path,
            research_panel_path=panel_path,
        )


def test_summary_verification_ignores_only_run_local_lineage(
    tmp_path: Path,
) -> None:
    expected = tmp_path / "expected.json"
    actual = tmp_path / "actual.json"
    expected.write_text(json.dumps({
        "generated_utc": "old",
        "input_lineage": {"a": {"path": "old", "sha256": "one"}},
        "active_evaluation": {"events": 635},
    }))
    actual.write_text(json.dumps({
        "generated_utc": "new",
        "input_lineage": {"a": {"path": "new", "sha256": "two"}},
        "active_evaluation": {"events": 635},
    }))
    verify_overlay_summary(actual, expected)
    actual.write_text(json.dumps({
        "generated_utc": "new",
        "input_lineage": {},
        "active_evaluation": {"events": 634},
    }))
    with pytest.raises(AssertionError, match="does not reproduce"):
        verify_overlay_summary(actual, expected)


@pytest.mark.skipif(
    os.environ.get("RUN_HENRY_HUB_SABINE_NOMINATION_CHAIN") != "1",
    reason=(
        "set RUN_HENRY_HUB_SABINE_NOMINATION_CHAIN=1 for the pinned GCS rebuild"
    ),
)
def test_pinned_gcs_raw_oac_through_final_overlay(tmp_path: Path) -> None:
    receipt = rebuild_sabine_nomination_overlay(
        output_dir=tmp_path / "sabine_nomination_overlay",
        overwrite=False,
    )
    assert receipt["status"] == "verified"
    assert receipt["input_artifacts_validated"] == 3
    assert receipt["nomination_revision_lineage"]["raw_oac_rows"] == 231679
    assert receipt["nomination_revision_lineage"]["raw_gas_days"] == 1096
    assert receipt["nomination_revision_lineage"]["mapped_score_dates"] == 710
    assert receipt["output_parity"]["daily_rows"] == 1748
    assert receipt["summary"]["active_evaluation"]["events"] == 635
    assert receipt["summary"]["active_evaluation"][
        "selected_intraday_sharpe"
    ] == 2.453697607607192
