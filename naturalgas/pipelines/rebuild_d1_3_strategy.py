"""Rebuild the selected D1--3 strategy from generation-pinned GCS inputs.

The large NCAR/GDEX point archive remains in Google Cloud Storage.  This
pipeline reads the exact object generations declared in the checked-in weather
manifest, rebuilds the causal D1/D1--3/D1--5 wind signals, proves that the wind
columns consumed by the compact selected-strategy audit input are identical,
downloads and validates every selected-strategy audit object declared in the
selected-input manifest, and then runs the selected evaluator. It has no GCS
write capability.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from naturalgas.evaluate_d1_3_storage_amplified_strategy import (
    DEFAULT_EVENT_REPORTS_PATH,
    DEFAULT_OUTPUT_DIR as SHIPPED_OUTPUT_DIR,
    FORMAL_DAILY,
    SCORE_INPUTS,
    STORAGE_CALENDAR_CORRECTIONS,
    run as run_selected_evaluator,
)
from naturalgas.pipelines.rebuild_weather_factors import (
    WIND_HORIZON_OUTPUT_NAME,
    load_factor_inputs,
    rebuild_wind_horizons,
)
from naturalgas.reproducibility import (
    PROJECT_ROOT,
    create_staging_directory,
    discard_staging_directory,
    fetch_manifest,
    publish_staging_directory,
    sha256_file,
)


DEFAULT_WEATHER_MANIFEST = (
    PROJECT_ROOT / "manifests/weather_factor_inputs_2026-07-28.json"
)
DEFAULT_SELECTED_INPUT_MANIFEST = (
    PROJECT_ROOT / "manifests/selected_strategy_inputs_2026-08-14.json"
)
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "reproduced/d1_3_strategy"
EXPECTED_SUMMARY = SHIPPED_OUTPUT_DIR / "summary.json"
WIND_SIGNAL_COLUMNS = ("wind_signal__d1_3", "wind_signal__d1_5")
SCORE_INPUT_ARTIFACT_ID = "selected_d1_3_storage_amplifier_inputs"
STORAGE_CORRECTION_ARTIFACT_ID = "selected_wngsr_d1_3_score_corrections"
EVENT_REPORT_ARTIFACT_ID = "selected_event_reports_aligned"


def fetch_selected_strategy_inputs(
    manifest_path: Path,
    *,
    root: Path,
) -> dict[str, Path]:
    """Download and validate the complete immutable selected-input archive."""

    paths = fetch_manifest(manifest_path, root=root)
    required = {
        SCORE_INPUT_ARTIFACT_ID,
        STORAGE_CORRECTION_ARTIFACT_ID,
        EVENT_REPORT_ARTIFACT_ID,
    }
    missing = required.difference(paths)
    if missing:
        raise KeyError(
            f"selected-input manifest is missing evaluator inputs: {sorted(missing)}"
        )
    return paths


def verify_score_input_wind_lineage(
    *,
    horizon_path: Path,
    score_inputs_path: Path,
) -> dict[str, Any]:
    """Require raw-rebuilt wind signals to equal every frozen strategy value."""

    horizons = pd.read_parquet(horizon_path)
    score_inputs = pd.read_parquet(
        score_inputs_path,
        columns=["date", *WIND_SIGNAL_COLUMNS],
    )
    required_horizon = {
        "date",
        "forecast_reference_time_utc",
        "wind_z__d1_3",
        *WIND_SIGNAL_COLUMNS,
    }
    missing = required_horizon.difference(horizons.columns)
    if missing:
        raise ValueError(
            f"rebuilt wind horizon artifact is missing {sorted(missing)}"
        )
    horizons["date"] = pd.to_datetime(horizons["date"]).dt.normalize()
    score_inputs["date"] = pd.to_datetime(score_inputs["date"]).dt.normalize()
    if not horizons["date"].is_unique or not score_inputs["date"].is_unique:
        raise ValueError("wind lineage inputs must contain unique dates")

    issue = pd.to_datetime(horizons["forecast_reference_time_utc"], utc=True)
    issue_date = issue.dt.tz_localize(None).dt.normalize()
    if not issue.dt.hour.eq(0).all() or not issue_date.equals(horizons["date"]):
        raise AssertionError(
            "wind horizon artifact must map each score date to its same-day 00Z issue"
        )

    renamed = horizons[["date", *WIND_SIGNAL_COLUMNS]].rename(
        columns={column: f"rebuilt__{column}" for column in WIND_SIGNAL_COLUMNS}
    )
    comparison = score_inputs.merge(
        renamed,
        on="date",
        how="left",
        validate="one_to_one",
    )
    maximum_difference: dict[str, float] = {}
    for column in WIND_SIGNAL_COLUMNS:
        frozen = pd.to_numeric(comparison[column], errors="coerce")
        rebuilt = pd.to_numeric(
            comparison[f"rebuilt__{column}"], errors="coerce"
        )
        if not frozen.isna().equals(rebuilt.isna()):
            mismatch_dates = comparison.loc[
                frozen.isna().ne(rebuilt.isna()), "date"
            ].dt.strftime("%Y-%m-%d").tolist()
            raise AssertionError(
                f"{column} missing-date mismatch: {mismatch_dates}"
            )
        difference = (frozen - rebuilt).abs().dropna()
        maximum = float(difference.max()) if not difference.empty else 0.0
        maximum_difference[column] = maximum
        if not np.allclose(
            frozen,
            rebuilt,
            rtol=0.0,
            atol=0.0,
            equal_nan=True,
        ):
            raise AssertionError(
                f"raw-rebuilt {column} differs from selected score input; "
                f"max absolute difference={maximum}"
            )

    missing_dates = comparison.loc[
        comparison["rebuilt__wind_signal__d1_3"].isna(), "date"
    ].dt.strftime("%Y-%m-%d").tolist()
    return {
        "status": "exact",
        "score_dates": len(score_inputs),
        "matched_non_null_d1_3_dates": int(
            comparison["rebuilt__wind_signal__d1_3"].notna().sum()
        ),
        "missing_initialization_dates": missing_dates,
        "maximum_absolute_difference": maximum_difference,
        "horizon_sha256": sha256_file(horizon_path),
        "score_inputs_sha256": sha256_file(score_inputs_path),
        "issue_cycle_utc": 0,
    }


def _without_dashboard(payload: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    result.pop("dashboard", None)
    return result


def verify_selected_summary(
    actual_path: Path,
    expected_path: Path = EXPECTED_SUMMARY,
) -> None:
    """Verify every selected-summary field except its output-location label."""

    actual = json.loads(actual_path.read_text(encoding="utf-8"))
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    if _without_dashboard(actual) != _without_dashboard(expected):
        raise AssertionError(
            "GCS-lineage rebuild does not reproduce the shipped selected summary"
        )


def evaluate_selected_strategy_with_horizon(
    *,
    horizon_path: Path,
    formal_daily_path: Path,
    score_inputs_path: Path,
    storage_calendar_corrections_path: Path,
    event_reports_path: Path,
    output_dir: Path,
    logical_output_dir: Path | None = None,
    logical_formal_daily_path: Path | None = None,
    logical_score_inputs_path: Path | None = None,
    logical_storage_calendar_corrections_path: Path | None = None,
    logical_event_reports_path: Path | None = None,
    expected_summary_path: Path = EXPECTED_SUMMARY,
) -> dict[str, Any]:
    """Validate upstream wind parity, run the evaluator, and verify results."""

    lineage = verify_score_input_wind_lineage(
        horizon_path=horizon_path,
        score_inputs_path=score_inputs_path,
    )
    run_selected_evaluator(
        formal_daily_path=formal_daily_path,
        score_inputs_path=score_inputs_path,
        storage_calendar_corrections_path=storage_calendar_corrections_path,
        event_reports_path=event_reports_path,
        output_dir=output_dir,
    )
    summary_path = output_dir / "summary.json"
    verify_selected_summary(summary_path, expected_summary_path)

    logical = output_dir if logical_output_dir is None else logical_output_dir
    summary_payload = json.loads(summary_path.read_text(encoding="utf-8"))
    dashboard = logical / "latest_strategy_dashboard.png"
    summary_payload["dashboard"] = str(
        dashboard.relative_to(PROJECT_ROOT)
        if dashboard.is_relative_to(PROJECT_ROOT)
        else dashboard
    )
    summary_path.write_text(
        json.dumps(summary_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    receipt = {
        "status": "verified",
        "wind_lineage": lineage,
        "formal_daily": str(
            formal_daily_path
            if logical_formal_daily_path is None
            else logical_formal_daily_path
        ),
        "formal_daily_sha256": sha256_file(formal_daily_path),
        "score_inputs": str(
            score_inputs_path
            if logical_score_inputs_path is None
            else logical_score_inputs_path
        ),
        "storage_calendar_corrections": str(
            storage_calendar_corrections_path
            if logical_storage_calendar_corrections_path is None
            else logical_storage_calendar_corrections_path
        ),
        "event_reports": str(
            event_reports_path
            if logical_event_reports_path is None
            else logical_event_reports_path
        ),
        "output_dir": str(logical),
        "verified_against": str(expected_summary_path),
        "summary": summary_payload,
    }
    (output_dir / "reproduction_receipt.json").write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return receipt


def rebuild_d1_3_strategy(
    *,
    weather_manifest: Path = DEFAULT_WEATHER_MANIFEST,
    selected_input_manifest: Path | None = DEFAULT_SELECTED_INPUT_MANIFEST,
    formal_daily_path: Path = FORMAL_DAILY,
    score_inputs_path: Path = SCORE_INPUTS,
    storage_calendar_corrections_path: Path = STORAGE_CALENDAR_CORRECTIONS,
    event_reports_path: Path = DEFAULT_EVENT_REPORTS_PATH,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Run the standalone pinned-GCS-wind-to-selected-strategy rebuild."""

    staging, resolved_output = create_staging_directory(
        output_dir,
        overwrite=overwrite,
    )
    try:
        selected_paths: dict[str, Path] | None = None
        logical_selected_paths: dict[str, Path] = {}
        if selected_input_manifest is not None:
            selected_paths = fetch_selected_strategy_inputs(
                selected_input_manifest,
                root=staging,
            )
            logical_selected_paths = {
                artifact_id: resolved_output / path.relative_to(staging)
                for artifact_id, path in selected_paths.items()
            }
            score_inputs_path = selected_paths[SCORE_INPUT_ARTIFACT_ID]
            storage_calendar_corrections_path = selected_paths[
                STORAGE_CORRECTION_ARTIFACT_ID
            ]
            event_reports_path = selected_paths[EVENT_REPORT_ARTIFACT_ID]
        wind = rebuild_wind_horizons(
            inputs=load_factor_inputs(weather_manifest, "wind"),
            output_dir=staging / "wind",
        )
        horizon_path = Path(wind["output"])
        strategy = evaluate_selected_strategy_with_horizon(
            horizon_path=horizon_path,
            formal_daily_path=formal_daily_path,
            score_inputs_path=score_inputs_path,
            storage_calendar_corrections_path=storage_calendar_corrections_path,
            event_reports_path=event_reports_path,
            output_dir=staging / "strategy",
            logical_output_dir=resolved_output / "strategy",
            logical_score_inputs_path=logical_selected_paths.get(
                SCORE_INPUT_ARTIFACT_ID
            ),
            logical_storage_calendar_corrections_path=(
                logical_selected_paths.get(STORAGE_CORRECTION_ARTIFACT_ID)
            ),
            logical_event_reports_path=logical_selected_paths.get(
                EVENT_REPORT_ARTIFACT_ID
            ),
        )
        wind_receipt = dict(wind)
        wind_receipt["output"] = str(
            resolved_output / "wind" / WIND_HORIZON_OUTPUT_NAME
        )
        receipt = {
            "status": "verified",
            "weather_manifest": str(weather_manifest),
            "weather_manifest_sha256": sha256_file(weather_manifest),
            "selected_input_manifest": (
                None
                if selected_input_manifest is None
                else str(selected_input_manifest)
            ),
            "selected_input_manifest_sha256": (
                None
                if selected_input_manifest is None
                else sha256_file(selected_input_manifest)
            ),
            "selected_input_artifacts_validated": (
                0 if selected_paths is None else len(selected_paths)
            ),
            "wind_horizon_rebuild": wind_receipt,
            "selected_strategy_rebuild": strategy,
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
        "--weather-manifest",
        type=Path,
        default=DEFAULT_WEATHER_MANIFEST,
    )
    parser.add_argument(
        "--selected-input-manifest",
        type=Path,
        default=DEFAULT_SELECTED_INPUT_MANIFEST,
    )
    parser.add_argument(
        "--use-checked-in-selected-inputs",
        action="store_true",
        help=(
            "Use the checked-in compact score, WNGSR correction, and event "
            "tables instead of downloading the immutable GCS archive."
        ),
    )
    parser.add_argument("--formal-daily", type=Path, default=FORMAL_DAILY)
    parser.add_argument("--score-inputs", type=Path, default=SCORE_INPUTS)
    parser.add_argument(
        "--storage-calendar-corrections",
        type=Path,
        default=STORAGE_CALENDAR_CORRECTIONS,
    )
    parser.add_argument(
        "--event-reports",
        type=Path,
        default=DEFAULT_EVENT_REPORTS_PATH,
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    receipt = rebuild_d1_3_strategy(
        weather_manifest=args.weather_manifest,
        selected_input_manifest=(
            None
            if args.use_checked_in_selected_inputs
            else args.selected_input_manifest
        ),
        formal_daily_path=args.formal_daily,
        score_inputs_path=args.score_inputs,
        storage_calendar_corrections_path=args.storage_calendar_corrections,
        event_reports_path=args.event_reports,
        output_dir=args.output_dir,
        overwrite=args.overwrite,
    )
    print(json.dumps(receipt, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
