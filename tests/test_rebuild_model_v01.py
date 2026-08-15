from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from naturalgas.audit_inputs import (
    WIND_DIAGNOSTICS_CSV_ARTIFACT_ID,
    WIND_WEIGHTS_CSV_ARTIFACT_ID,
    materialize_audit_inputs,
)
from naturalgas.pipelines.rebuild_model_v01 import rebuild_model_v01
from naturalgas.reproducibility import DEFAULT_MANIFEST, PROJECT_ROOT


SCHEMA_REGISTRY = PROJECT_ROOT / "schemas/input_schemas_2026-07-13.json"


def test_input_manifest_matches_checked_schema_registry() -> None:
    manifest = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    registry = json.loads(SCHEMA_REGISTRY.read_text(encoding="utf-8"))
    schemas = {entry["id"]: entry for entry in registry["schemas"]}

    assert len(manifest["artifacts"]) == 7
    assert set(schemas) == {entry["id"] for entry in manifest["artifacts"]}
    for artifact in manifest["artifacts"]:
        assert {
            "id", "uri", "generation", "sha256", "local_path",
            "rows", "columns", "date_range",
            "schema_fingerprint_sha256",
        } <= set(artifact)
        assert artifact["uri"].startswith("gs://")
        assert str(artifact["generation"]).isdigit()
        assert len(artifact["sha256"]) == 64

        fields = schemas[artifact["id"]]["arrow_fields"]
        canonical = json.dumps(
            fields,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        fingerprint = hashlib.sha256(canonical).hexdigest()
        assert fingerprint == schemas[artifact["id"]]["fingerprint_sha256"]
        assert fingerprint == artifact["schema_fingerprint_sha256"]
        assert len(fields) == artifact["columns"]


def test_wind_audit_artifacts_are_exact() -> None:
    audit_paths = materialize_audit_inputs(
        [WIND_WEIGHTS_CSV_ARTIFACT_ID, WIND_DIAGNOSTICS_CSV_ARTIFACT_ID]
    )
    expected = {
        audit_paths[WIND_WEIGHTS_CSV_ARTIFACT_ID]: (
            "677cdabc8f3f051514f3ad3ed29e24332c6755dc9902bd8361b35dddd8ff23db"
        ),
        audit_paths[WIND_DIAGNOSTICS_CSV_ARTIFACT_ID]: (
            "57fdd655c5b2e5c21c060d71e6ceb6d1884ad803f15ed484308e48c8920f596e"
        ),
        PROJECT_ROOT / "results/experiments/wind/complete_wind_factor_ic.csv": (
            "93a1faf2e1d73618b0cde4b95eb572801adebd6953bcdae9fd39c4fd9080a930"
        ),
    }
    for path, expected_hash in expected.items():
        content = path.read_bytes()
        assert hashlib.sha256(content).hexdigest() == expected_hash


@pytest.mark.skipif(
    os.environ.get("RUN_HENRY_HUB_MODEL_V01_CHAIN") != "1",
    reason="set RUN_HENRY_HUB_MODEL_V01_CHAIN=1 for the pinned V01 rebuild",
)
def test_generation_pinned_formal_evaluator_rebuild(tmp_path: Path) -> None:
    receipt = rebuild_model_v01(
        manifest_path=DEFAULT_MANIFEST,
        root=tmp_path,
        output_dir=tmp_path / "reproduced/models/v01_south_central_storage",
        fetch=True,
        overwrite=True,
    )
    summary = receipt["summary"]
    assert receipt["status"] == "verified"
    assert receipt["summary_verification"]["status"] == "verified"
    assert receipt["summary_verification"]["scope"] == (
        "all V01 fields except legacy Lower 48 comparison deltas"
    )
    assert summary["model_id"] == "hh_v01_south_central_storage"
    assert summary["model_sequence"] == 1
    assert summary["lifecycle_state"] == "frozen_formal_baseline"
    assert summary["trading_days"] == 2264
    assert str(summary["sample_end"]) == "2026-07-13 00:00:00"
    assert (
        abs(summary["selected_full_metrics"]["sharpe_zero_rf"]
            - 1.667459455270079)
        < 1e-12
    )
