"""Rebuild the final Sabine nomination-revision overlay from pinned GCS inputs.

The pipeline mirrors the selected V03 reproduction contract. It downloads
exact Google Cloud Storage object generations, validates their hashes and
Parquet schemas, rebuilds the retained TransCameron LNG and Jefferson Island
storage revisions from the raw all-cycle Sabine archive, requires exact parity
with the assembled research panel, validates the processed execution-window
contract, runs the final evaluator, and verifies the results against the
shipped research artifacts.

Raw NYMEX trade files are controlled inputs and are not redistributed. The
generation-pinned execution-window Parquet is therefore the exact processed
trade-price contract for this handoff.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from naturalgas.audit_inputs import (
    SABINE_NOMINATION_ALL_CYCLE_ARTIFACT_ID,
    SABINE_NOMINATION_EXECUTION_ARTIFACT_ID,
    SABINE_NOMINATION_PANEL_ARTIFACT_ID,
)
from naturalgas.evaluate_sabine_nomination_revision_intraday_overlay_final import (
    DEFAULT_V03_DAILY,
    logical_path,
    run as run_overlay_evaluator,
)
from naturalgas.reproducibility import (
    PROJECT_ROOT,
    create_staging_directory,
    discard_staging_directory,
    fetch_manifest,
    load_manifest,
    publish_staging_directory,
    sha256_file,
)


DEFAULT_INPUT_MANIFEST = (
    PROJECT_ROOT
    / "manifests/sabine_nomination_overlay_inputs_2026-08-19.json"
)
DEFAULT_OUTPUT_DIR = (
    PROJECT_ROOT
    / "reproduced/experiments/sabine_nomination_revision_intraday_overlay_final"
)
EXPECTED_OUTPUT_DIR = (
    PROJECT_ROOT
    / "results/experiments/sabine_nomination_revision_intraday_overlay_final"
)
EXPECTED_SUMMARY = EXPECTED_OUTPUT_DIR / "summary.json"

FINAL_CYCLE = "Intraday 3"
LNG_EARLIER_CYCLE = "Intraday 1"
STORAGE_EARLIER_CYCLE = "Timely"
LNG_LOCATION = "TransCameron Pipeline"
STORAGE_LOCATION = "Jefferson Island - HH"
HISTORY_WINDOWS = (20, 60, 120)
PARITY_TABLES = (
    "headline_metrics.csv",
    "annual_attribution.csv",
    "source_attribution.csv",
    "formulation_comparison.csv",
    "cost_sensitivity.csv",
    "history_sensitivity.csv",
)


def causal_expanding_z(values: pd.Series, minimum_history: int) -> pd.Series:
    """Standardize one revision using strictly earlier gas days."""

    history = values.shift(1).expanding(min_periods=minimum_history)
    scale = history.std(ddof=1).replace(0.0, np.nan)
    return (values - history.mean()) / scale


def rebuild_nomination_revisions(raw_path: Path) -> pd.DataFrame:
    """Rebuild the retained cycle revisions from the raw all-cycle archive."""

    columns = [
        "gas_date",
        "cycle",
        "posting_time_utc",
        "location_name",
        "flow_indicator",
        "total_scheduled_quantity_dth_per_day",
    ]
    raw = pd.read_parquet(raw_path, columns=columns)
    raw["gas_date"] = pd.to_datetime(raw["gas_date"]).dt.tz_localize(None)
    raw["posting_time_utc"] = pd.to_datetime(
        raw["posting_time_utc"], utc=True
    )
    quantity = pd.to_numeric(
        raw["total_scheduled_quantity_dth_per_day"], errors="coerce"
    ).fillna(0.0)
    raw["lng_delivery_dthd"] = quantity.where(
        raw["location_name"].eq(LNG_LOCATION)
        & raw["flow_indicator"].eq("D"),
        0.0,
    )
    raw["storage_injection_dthd"] = quantity.where(
        raw["location_name"].eq(STORAGE_LOCATION)
        & raw["flow_indicator"].eq("D"),
        0.0,
    )
    raw["storage_withdrawal_dthd"] = quantity.where(
        raw["location_name"].eq(STORAGE_LOCATION)
        & raw["flow_indicator"].eq("R"),
        0.0,
    )
    cycles = raw.groupby(["gas_date", "cycle"], as_index=False).agg(
        posting_time_utc=("posting_time_utc", "max"),
        lng_delivery_dthd=("lng_delivery_dthd", "sum"),
        storage_injection_dthd=("storage_injection_dthd", "sum"),
        storage_withdrawal_dthd=("storage_withdrawal_dthd", "sum"),
    )
    cycles["storage_tightness_dthd"] = (
        cycles["storage_injection_dthd"]
        - cycles["storage_withdrawal_dthd"]
    )
    values = cycles.pivot(
        index="gas_date",
        columns="cycle",
        values=["lng_delivery_dthd", "storage_tightness_dthd"],
    )
    postings = cycles.pivot(
        index="gas_date",
        columns="cycle",
        values="posting_time_utc",
    )
    required_cycles = {
        FINAL_CYCLE,
        LNG_EARLIER_CYCLE,
        STORAGE_EARLIER_CYCLE,
    }
    missing = required_cycles.difference(postings.columns)
    if missing:
        raise ValueError(f"All-cycle archive is missing cycles: {sorted(missing)}")

    result = pd.DataFrame(index=values.index.sort_values())
    result["posting_time_utc"] = postings[FINAL_CYCLE]
    result["lng_feedgas_revision_from_intraday_1_raw"] = (
        values[("lng_delivery_dthd", FINAL_CYCLE)]
        - values[("lng_delivery_dthd", LNG_EARLIER_CYCLE)]
    )
    result["storage_tightness_revision_from_timely_raw"] = (
        values[("storage_tightness_dthd", FINAL_CYCLE)]
        - values[("storage_tightness_dthd", STORAGE_EARLIER_CYCLE)]
    )
    for minimum_history in HISTORY_WINDOWS:
        result[f"lng_revision_z_{minimum_history}"] = causal_expanding_z(
            result["lng_feedgas_revision_from_intraday_1_raw"],
            minimum_history,
        )
        result[f"storage_revision_z_{minimum_history}"] = (
            causal_expanding_z(
                result["storage_tightness_revision_from_timely_raw"],
                minimum_history,
            )
        )
    return result.reset_index()


def verify_nomination_revision_lineage(
    *,
    raw_path: Path,
    research_panel_path: Path,
) -> dict[str, Any]:
    """Require every mapped factor value to equal the raw OAC rebuild."""

    rebuilt = rebuild_nomination_revisions(raw_path)
    panel_columns = [
        "date",
        "gas_date",
        "posting_time_utc_factor",
        "lng_feedgas_revision_from_intraday_1_raw",
        "storage_tightness_revision_from_timely_raw",
        *[
            f"{family}_revision_z_{window}"
            for window in HISTORY_WINDOWS
            for family in ("lng", "storage")
        ],
    ]
    panel = pd.read_parquet(research_panel_path, columns=panel_columns)
    panel["date"] = pd.to_datetime(panel["date"]).dt.tz_localize(None)
    panel = panel.loc[panel["gas_date"].notna()].copy()
    panel["gas_date"] = pd.to_datetime(panel["gas_date"]).dt.tz_localize(None)
    if not panel["date"].is_unique or not panel["gas_date"].is_unique:
        raise ValueError("Assembled nomination panel requires unique mapped dates")
    comparison = panel.merge(
        rebuilt,
        on="gas_date",
        how="left",
        validate="one_to_one",
        suffixes=("__panel", "__rebuilt"),
    )
    if comparison["posting_time_utc"].isna().any():
        missing_dates = comparison.loc[
            comparison["posting_time_utc"].isna(), "gas_date"
        ].dt.strftime("%Y-%m-%d").tolist()
        raise AssertionError(
            f"Raw OAC rebuild is missing mapped gas dates: {missing_dates}"
        )
    panel_posting = pd.to_datetime(
        comparison["posting_time_utc_factor"], utc=True
    )
    rebuilt_posting = pd.to_datetime(
        comparison["posting_time_utc"], utc=True
    )
    if not panel_posting.equals(rebuilt_posting):
        raise AssertionError("Raw-rebuilt Intraday 3 posting timestamps differ")

    value_columns = [
        "lng_feedgas_revision_from_intraday_1_raw",
        "storage_tightness_revision_from_timely_raw",
        *[
            f"{family}_revision_z_{window}"
            for window in HISTORY_WINDOWS
            for family in ("lng", "storage")
        ],
    ]
    maximum_difference: dict[str, float] = {}
    for column in value_columns:
        frozen = pd.to_numeric(
            comparison[f"{column}__panel"], errors="coerce"
        )
        raw_rebuilt = pd.to_numeric(
            comparison[f"{column}__rebuilt"], errors="coerce"
        )
        if not frozen.isna().equals(raw_rebuilt.isna()):
            raise AssertionError(f"Raw-rebuilt {column} missingness differs")
        difference = (frozen - raw_rebuilt).abs().dropna()
        maximum = float(difference.max()) if not difference.empty else 0.0
        maximum_difference[column] = maximum
        if not np.allclose(
            frozen,
            raw_rebuilt,
            rtol=0.0,
            atol=0.0,
            equal_nan=True,
        ):
            raise AssertionError(
                f"Raw-rebuilt {column} differs; max difference={maximum}"
            )
    return {
        "status": "exact",
        "raw_oac_rows": int(
            pd.read_parquet(raw_path, columns=["gas_date"]).shape[0]
        ),
        "raw_gas_days": int(rebuilt["gas_date"].nunique()),
        "mapped_score_dates": int(len(panel)),
        "maximum_absolute_difference": maximum_difference,
        "raw_oac_sha256": sha256_file(raw_path),
        "research_panel_sha256": sha256_file(research_panel_path),
    }


def _comparable_summary(payload: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(payload)
    result.pop("generated_utc", None)
    result.pop("input_lineage", None)
    return result


def verify_overlay_summary(
    actual_path: Path,
    expected_path: Path = EXPECTED_SUMMARY,
) -> None:
    """Verify the complete result summary apart from run-local lineage labels."""

    actual = json.loads(actual_path.read_text(encoding="utf-8"))
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    if _comparable_summary(actual) != _comparable_summary(expected):
        raise AssertionError(
            "Pinned-input rebuild does not reproduce the shipped overlay summary"
        )


def verify_output_parity(
    actual_dir: Path,
    expected_dir: Path = EXPECTED_OUTPUT_DIR,
) -> dict[str, Any]:
    """Require exact table and daily-path parity with the shipped artifacts."""

    table_hashes: dict[str, str] = {}
    for name in PARITY_TABLES:
        actual_path = actual_dir / name
        expected_path = expected_dir / name
        actual = pd.read_csv(actual_path)
        expected = pd.read_csv(expected_path)
        pd.testing.assert_frame_equal(actual, expected, check_exact=True)
        table_hashes[name] = sha256_file(actual_path)

    actual_daily = pd.read_parquet(actual_dir / "daily_strategy_path.parquet")
    expected_daily = pd.read_parquet(
        expected_dir / "daily_strategy_path.parquet"
    )
    pd.testing.assert_frame_equal(actual_daily, expected_daily, check_exact=True)
    return {
        "status": "exact",
        "tables": table_hashes,
        "daily_rows": int(len(actual_daily)),
        "daily_columns": int(len(actual_daily.columns)),
        "daily_strategy_path_sha256": sha256_file(
            actual_dir / "daily_strategy_path.parquet"
        ),
    }


def rebuild_sabine_nomination_overlay(
    *,
    input_manifest: Path = DEFAULT_INPUT_MANIFEST,
    v03_daily_path: Path = DEFAULT_V03_DAILY,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Run the generation-pinned raw-OAC-to-final-overlay rebuild."""

    staging, resolved_output = create_staging_directory(
        output_dir,
        overwrite=overwrite,
    )
    try:
        paths = fetch_manifest(input_manifest, root=staging)
        required = {
            SABINE_NOMINATION_ALL_CYCLE_ARTIFACT_ID,
            SABINE_NOMINATION_PANEL_ARTIFACT_ID,
            SABINE_NOMINATION_EXECUTION_ARTIFACT_ID,
        }
        missing = required.difference(paths)
        if missing:
            raise KeyError(
                f"Nomination input manifest is missing: {sorted(missing)}"
            )
        raw_path = paths[SABINE_NOMINATION_ALL_CYCLE_ARTIFACT_ID]
        panel_path = paths[SABINE_NOMINATION_PANEL_ARTIFACT_ID]
        execution_path = paths[SABINE_NOMINATION_EXECUTION_ARTIFACT_ID]
        lineage = verify_nomination_revision_lineage(
            raw_path=raw_path,
            research_panel_path=panel_path,
        )
        strategy_dir = staging / "strategy"
        run_overlay_evaluator(
            research_panel_path=panel_path,
            execution_windows_path=execution_path,
            all_cycle_source_path=raw_path,
            v03_daily_path=v03_daily_path,
            output_dir=strategy_dir,
        )
        verify_overlay_summary(strategy_dir / "summary.json")
        output_parity = verify_output_parity(strategy_dir)

        manifest_artifacts = load_manifest(input_manifest)
        logical_inputs = {
            artifact.artifact_id: logical_path(
                resolved_output
                / paths[artifact.artifact_id].relative_to(staging)
            )
            for artifact in manifest_artifacts
        }
        summary_path = strategy_dir / "summary.json"
        summary_payload = json.loads(summary_path.read_text(encoding="utf-8"))
        for key, artifact_id in (
            ("all_cycle_source", SABINE_NOMINATION_ALL_CYCLE_ARTIFACT_ID),
            ("assembled_research_panel", SABINE_NOMINATION_PANEL_ARTIFACT_ID),
            ("execution_windows", SABINE_NOMINATION_EXECUTION_ARTIFACT_ID),
        ):
            summary_payload["input_lineage"][key]["path"] = logical_inputs[
                artifact_id
            ]
        summary_payload["input_lineage"]["formal_v03_daily"][
            "path"
        ] = logical_path(v03_daily_path)
        summary_path.write_text(
            json.dumps(summary_payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        receipt = {
            "status": "verified",
            "input_manifest": str(input_manifest),
            "input_manifest_sha256": sha256_file(input_manifest),
            "input_artifacts_validated": len(paths),
            "nomination_revision_lineage": lineage,
            "execution_contract": {
                "status": "exact",
                "processed_input_sha256": sha256_file(execution_path),
                "raw_ticks_redistributed": False,
            },
            "v03_daily": str(v03_daily_path),
            "v03_daily_sha256": sha256_file(v03_daily_path),
            "output_dir": str(resolved_output / "strategy"),
            "verified_against": str(EXPECTED_OUTPUT_DIR),
            "output_parity": output_parity,
            "summary": summary_payload,
        }
        (staging / "reproduction_receipt.json").write_text(
            json.dumps(receipt, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        publish_staging_directory(staging, resolved_output, overwrite=overwrite)
        return receipt
    except Exception:
        discard_staging_directory(staging)
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-manifest",
        type=Path,
        default=DEFAULT_INPUT_MANIFEST,
    )
    parser.add_argument("--v03-daily", type=Path, default=DEFAULT_V03_DAILY)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    receipt = rebuild_sabine_nomination_overlay(
        input_manifest=args.input_manifest,
        v03_daily_path=args.v03_daily,
        output_dir=args.output_dir,
        overwrite=args.overwrite,
    )
    print(json.dumps(receipt, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
