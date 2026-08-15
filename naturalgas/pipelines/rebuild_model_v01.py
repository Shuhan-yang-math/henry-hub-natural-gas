"""Rebuild model V01 from generation-pinned input artifacts.

This is the strict processed-input reproduction entry point.  It downloads
the seven approved parquet generations, validates their content hashes and
contracts, routes the EIA reads to the downloaded local snapshots, runs the
formal strategy, and checks the headline metrics against the shipped result.
It never writes to Google Cloud Storage.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from naturalgas.evaluate_native_frequency_fundamentals import (
    STRATEGY_START,
    THROUGH_DATE,
)
from naturalgas.evaluate_model_v01_south_central_storage import (
    json_default,
    run,
)
from naturalgas.reproducibility import (
    DEFAULT_MANIFEST,
    LocalArtifactFileSystem,
    PROJECT_ROOT,
    create_staging_directory,
    discard_staging_directory,
    fetch_manifest,
    load_manifest,
    local_artifact_path,
    publish_staging_directory,
    sha256_file,
    validate_artifact,
    validate_artifact_contract,
)


DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "reproduced/models/v01_south_central_storage"
EXPECTED_SUMMARY = (
    PROJECT_ROOT / "results/models/v01_south_central_storage/summary.json"
)
REQUIRED_IDS = {
    "ng_multisignal_panel",
    "capacity_weighted_wind_features_daily",
    "capacity_weighted_solar_signals",
    "capacity_weighted_location_leads",
    "storage_weekly",
    "fundamentals_monthly",
    "eia_country_monthly",
}


def artifact_paths(
    manifest_path: Path,
    *,
    root: Path,
    overrides: Mapping[str, Path] | None = None,
    contract_only_override_ids: set[str] | None = None,
) -> dict[str, Path]:
    artifacts = load_manifest(manifest_path)
    overrides = {} if overrides is None else dict(overrides)
    contract_only_override_ids = (
        set() if contract_only_override_ids is None
        else set(contract_only_override_ids)
    )
    missing_contract_overrides = contract_only_override_ids - set(overrides)
    if missing_contract_overrides:
        raise ValueError(
            "Contract-only validation requires an explicit artifact override: "
            f"{sorted(missing_contract_overrides)}"
        )
    found = {artifact.artifact_id for artifact in artifacts}
    missing = REQUIRED_IDS - found
    if missing:
        raise ValueError(f"Manifest is missing required artifacts: {sorted(missing)}")
    paths: dict[str, Path] = {}
    for artifact in artifacts:
        if artifact.artifact_id not in REQUIRED_IDS:
            continue
        path = overrides.get(
            artifact.artifact_id,
            local_artifact_path(artifact, root=root),
        )
        if artifact.artifact_id in contract_only_override_ids:
            validate_artifact_contract(path, artifact)
        else:
            validate_artifact(path, artifact)
        paths[artifact.artifact_id] = path
    return paths


def local_filesystem(manifest_path: Path, *, root: Path) -> LocalArtifactFileSystem:
    artifacts = load_manifest(manifest_path)
    return LocalArtifactFileSystem({
        artifact.gcs_key: local_artifact_path(artifact, root=root)
        for artifact in artifacts
        if artifact.artifact_id in REQUIRED_IDS
    })


def verify_summary(
    actual: dict[str, Any],
    expected_path: Path = EXPECTED_SUMMARY,
    *,
    tolerance: float = 1e-12,
) -> dict[str, Any]:
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    normalized_actual = json.loads(json.dumps(actual, default=json_default))
    if int(actual["trading_days"]) != int(expected["trading_days"]):
        raise AssertionError(
            f"Trading days differ: {actual['trading_days']} != "
            f"{expected['trading_days']}"
        )
    actual_metrics = actual["selected_full_metrics"]
    expected_metrics = expected["selected_full_metrics"]
    for name in ("sharpe_zero_rf", "cagr", "maximum_drawdown", "win_rate"):
        left = float(actual_metrics[name])
        right = float(expected_metrics[name])
        if not math.isclose(left, right, rel_tol=0.0, abs_tol=tolerance):
            raise AssertionError(f"Metric {name} differs: {left} != {right}")
    comparison_field = "change_vs_lower48"
    actual_core = dict(normalized_actual)
    expected_core = dict(expected)
    actual_comparison = actual_core.pop(comparison_field)
    expected_comparison = expected_core.pop(comparison_field)
    if actual_core != expected_core:
        raise AssertionError(
            "V01 summary differs outside the allowed legacy Lower 48 "
            "comparison field"
        )
    return {
        "status": "verified",
        "scope": "all V01 fields except legacy Lower 48 comparison deltas",
        "comparison_field": comparison_field,
        "comparison_field_matches": actual_comparison == expected_comparison,
        "actual_comparison": actual_comparison,
        "expected_comparison": expected_comparison,
    }


def rebuild_model_v01(
    *,
    manifest_path: Path,
    root: Path,
    output_dir: Path,
    fetch: bool,
    overwrite: bool,
    artifact_overrides: Mapping[str, Path] | None = None,
    receipt_output_dir: Path | None = None,
    receipt_override_paths: Mapping[str, Path] | None = None,
    contract_only_override_ids: set[str] | None = None,
) -> dict[str, Any]:
    artifact_overrides = (
        {} if artifact_overrides is None else dict(artifact_overrides)
    )
    receipt_override_paths = (
        {} if receipt_override_paths is None
        else dict(receipt_override_paths)
    )
    staging, resolved_output = create_staging_directory(
        output_dir,
        overwrite=overwrite,
    )
    try:
        if fetch:
            fetch_manifest(
                manifest_path,
                root=root,
                artifact_ids=REQUIRED_IDS - set(artifact_overrides),
                overwrite=False,
            )
        paths = artifact_paths(
            manifest_path,
            root=root,
            overrides=artifact_overrides,
            contract_only_override_ids=contract_only_override_ids,
        )
        result = run(
            panel_path=paths["ng_multisignal_panel"],
            wind_path=paths["capacity_weighted_wind_features_daily"],
            solar_signal_path=paths["capacity_weighted_solar_signals"],
            solar_lead_path=paths["capacity_weighted_location_leads"],
            output_dir=staging,
            start=pd.Timestamp(STRATEGY_START),
            through_date=pd.Timestamp(THROUGH_DATE),
            filesystem=local_filesystem(manifest_path, root=root),
        )
        summary_verification = verify_summary(result)
        rebuilt_summary_path = staging / "summary.json"
        rebuilt_summary_sha256 = sha256_file(rebuilt_summary_path)
        expected_summary_sha256 = sha256_file(EXPECTED_SUMMARY)
        summary_byte_match = rebuilt_summary_sha256 == expected_summary_sha256
        manifest_artifacts = load_manifest(manifest_path)
        receipt = {
            "status": "verified",
            "manifest": str(manifest_path),
            "manifest_sha256": sha256_file(manifest_path),
            "input_artifacts": [
                {
                    "id": artifact.artifact_id,
                    "generation": artifact.generation,
                    "sha256": artifact.sha256,
                }
                for artifact in manifest_artifacts
                if artifact.artifact_id in REQUIRED_IDS
            ],
            "artifact_overrides": {
                key: {
                    "path_at_build_time": str(value),
                    "logical_path": str(
                        receipt_override_paths.get(key, value)
                    ),
                    "sha256": sha256_file(value),
                }
                for key, value in artifact_overrides.items()
            },
            "output_dir": str(
                resolved_output
                if receipt_output_dir is None
                else receipt_output_dir.expanduser().resolve()
            ),
            "verified_against": str(EXPECTED_SUMMARY),
            "rebuilt_summary_sha256": rebuilt_summary_sha256,
            "verified_summary_sha256": expected_summary_sha256,
            "summary_byte_match": summary_byte_match,
            "summary_verification": summary_verification,
            "summary": result,
        }
        (staging / "reproduction_receipt.json").write_text(
            json.dumps(receipt, default=str, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        publish_staging_directory(
            staging,
            resolved_output,
            overwrite=overwrite,
        )
        return receipt
    except Exception:
        discard_staging_directory(staging)
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Validate and use existing local inputs without contacting GCS",
    )
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    receipt = rebuild_model_v01(
        manifest_path=args.manifest,
        root=args.root,
        output_dir=args.output_dir,
        fetch=not args.offline,
        overwrite=args.overwrite,
    )
    print(json.dumps(receipt, default=str, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
