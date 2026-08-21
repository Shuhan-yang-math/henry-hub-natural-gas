"""Rebuild model V03 from generation-pinned upstream inputs.

This standalone path downloads the approved processed master-panel and EIA
inputs, rebuilds wind and solar from the pinned raw weather/capacity archive,
reconstructs the Central/Florida power signals, fundamentals, production
controls, pre-guard scores, storage-calendar correction, and final guard, then
runs the selected evaluator.  The frozen compact score is used only as a
parity target.  Use :mod:`naturalgas.pipelines.rebuild_all` when the master
panel itself must also be rebuilt from its 72 direct inputs.  Neither command
can write to Google Cloud Storage.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from naturalgas.build_model_v03_score_inputs import (
    build_score_inputs,
    write_score_input_build,
)
from naturalgas.evaluate_model_v03_d1_3_storage_guard import (
    DEFAULT_OUTPUT_DIR as SHIPPED_OUTPUT_DIR,
    run as run_model_v03_evaluator,
)
from naturalgas.pipelines.rebuild_model_v01 import (
    artifact_paths,
    local_filesystem,
    rebuild_model_v01,
)
from naturalgas.pipelines.rebuild_weather_factors import (
    WIND_HORIZON_OUTPUT_NAME,
    load_factor_inputs,
    rebuild_selected_wind,
    rebuild_solar,
    rebuild_wind_horizons,
)
from naturalgas.reproducibility import (
    DEFAULT_MANIFEST as DEFAULT_FORMAL_MANIFEST,
    PROJECT_ROOT,
    assert_reproduction_values_match,
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
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "reproduced/models/v03_d1_3_storage_guard"
EXPECTED_SUMMARY = SHIPPED_OUTPUT_DIR / "summary.json"
WIND_SIGNAL_COLUMNS = ("wind_signal__d1_3", "wind_signal__d1_5")
SCORE_INPUT_ARTIFACT_ID = "selected_d1_3_storage_amplifier_inputs"
STORAGE_CORRECTION_ARTIFACT_ID = "selected_wngsr_d1_3_score_corrections"
EVENT_REPORT_ARTIFACT_ID = "selected_event_reports_aligned"
CENTRAL_EIA930_SOURCE_ARTIFACT_ID = "selected_eia930_central_daily_multifuel"
SOUTHEAST_EIA930_SOURCE_ARTIFACT_ID = (
    "selected_eia930_southeast_daily_multifuel"
)


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
        CENTRAL_EIA930_SOURCE_ARTIFACT_ID,
        SOUTHEAST_EIA930_SOURCE_ARTIFACT_ID,
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


def verify_model_v03_summary(
    actual_path: Path,
    expected_path: Path = EXPECTED_SUMMARY,
) -> None:
    """Verify every selected-summary field except its output-location label."""

    actual = json.loads(actual_path.read_text(encoding="utf-8"))
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    try:
        assert_reproduction_values_match(
            _without_dashboard(actual),
            _without_dashboard(expected),
            path="summary",
        )
    except AssertionError as exc:
        raise AssertionError(
            "GCS-lineage rebuild does not reproduce the shipped selected "
            f"summary: {exc}"
        ) from exc


def evaluate_model_v03_with_horizon(
    *,
    horizon_path: Path,
    model_v01_daily_path: Path,
    score_inputs_path: Path,
    storage_calendar_corrections_path: Path,
    event_reports_path: Path,
    output_dir: Path,
    logical_output_dir: Path | None = None,
    logical_model_v01_daily_path: Path | None = None,
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
    run_model_v03_evaluator(
        model_v01_daily_path=model_v01_daily_path,
        score_inputs_path=score_inputs_path,
        storage_calendar_corrections_path=storage_calendar_corrections_path,
        event_reports_path=event_reports_path,
        output_dir=output_dir,
    )
    summary_path = output_dir / "summary.json"
    verify_model_v03_summary(summary_path, expected_summary_path)

    logical = output_dir if logical_output_dir is None else logical_output_dir
    summary_payload = json.loads(summary_path.read_text(encoding="utf-8"))
    dashboard = logical / "dashboard.png"
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
        "model_v01_daily": str(
            model_v01_daily_path
            if logical_model_v01_daily_path is None
            else logical_model_v01_daily_path
        ),
        "model_v01_daily_sha256": sha256_file(model_v01_daily_path),
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


def rebuild_model_v03(
    *,
    weather_manifest: Path = DEFAULT_WEATHER_MANIFEST,
    formal_manifest: Path = DEFAULT_FORMAL_MANIFEST,
    selected_input_manifest: Path = DEFAULT_SELECTED_INPUT_MANIFEST,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Run the processed-upstream-to-selected-strategy V03 rebuild."""

    staging, resolved_output = create_staging_directory(
        output_dir,
        overwrite=overwrite,
    )
    try:
        fetch_manifest(formal_manifest, root=staging)
        formal_paths = artifact_paths(formal_manifest, root=staging)
        selected_paths = fetch_selected_strategy_inputs(
            selected_input_manifest,
            root=staging,
        )
        logical_selected_paths = {
            artifact_id: resolved_output / path.relative_to(staging)
            for artifact_id, path in selected_paths.items()
        }

        weather_dir = staging / "weather"
        wind_inputs = load_factor_inputs(weather_manifest, "wind")
        selected_wind = rebuild_selected_wind(
            inputs=wind_inputs,
            output_dir=weather_dir,
        )
        wind_horizons = rebuild_wind_horizons(
            inputs=wind_inputs,
            output_dir=weather_dir,
        )
        solar = rebuild_solar(
            inputs=load_factor_inputs(weather_manifest, "solar"),
            output_dir=weather_dir,
        )
        horizon_path = Path(wind_horizons["output"])
        logical_weather_dir = resolved_output / "weather"
        logical_weather_paths = {
            "capacity_weighted_wind_features_daily": (
                logical_weather_dir / Path(selected_wind["output"]).name
            ),
            "capacity_weighted_solar_signals": (
                logical_weather_dir / Path(solar["signal_output"]).name
            ),
            "capacity_weighted_location_leads": (
                logical_weather_dir / Path(solar["lead_output"]).name
            ),
        }

        model_v01 = rebuild_model_v01(
            manifest_path=formal_manifest,
            root=staging,
            output_dir=staging / "model_v01",
            fetch=False,
            overwrite=False,
            artifact_overrides={
                "capacity_weighted_wind_features_daily": Path(
                    selected_wind["output"]
                ),
                "capacity_weighted_solar_signals": Path(
                    solar["signal_output"]
                ),
                "capacity_weighted_location_leads": Path(
                    solar["lead_output"]
                ),
            },
            receipt_output_dir=resolved_output / "model_v01",
            receipt_override_paths=logical_weather_paths,
        )

        score_dir = staging / "score_inputs"
        score_build = build_score_inputs(
            panel_path=formal_paths["ng_multisignal_panel"],
            wind_path=Path(selected_wind["output"]),
            wind_horizon_path=horizon_path,
            solar_signal_path=Path(solar["signal_output"]),
            solar_lead_path=Path(solar["lead_output"]),
            central_eia930_path=selected_paths[
                CENTRAL_EIA930_SOURCE_ARTIFACT_ID
            ],
            southeast_eia930_path=selected_paths[
                SOUTHEAST_EIA930_SOURCE_ARTIFACT_ID
            ],
            filesystem=local_filesystem(formal_manifest, root=staging),
            frozen_score_inputs_path=selected_paths[SCORE_INPUT_ARTIFACT_ID],
            frozen_storage_corrections_path=selected_paths[
                STORAGE_CORRECTION_ARTIFACT_ID
            ],
        )
        score_receipt = write_score_input_build(
            score_build,
            output_dir=score_dir,
            receipt_output_dir=resolved_output / "score_inputs",
        )
        strategy = evaluate_model_v03_with_horizon(
            horizon_path=horizon_path,
            model_v01_daily_path=staging / "model_v01/strategy_daily.parquet",
            score_inputs_path=score_dir / "model_v03_score_inputs.parquet",
            storage_calendar_corrections_path=(
                score_dir / "wngsr_score_corrections.parquet"
            ),
            event_reports_path=selected_paths[EVENT_REPORT_ARTIFACT_ID],
            output_dir=staging / "strategy",
            logical_output_dir=resolved_output / "strategy",
            logical_model_v01_daily_path=(
                resolved_output / "model_v01/strategy_daily.parquet"
            ),
            logical_score_inputs_path=(
                resolved_output / "score_inputs/model_v03_score_inputs.parquet"
            ),
            logical_storage_calendar_corrections_path=(
                resolved_output / "score_inputs/wngsr_score_corrections.parquet"
            ),
            logical_event_reports_path=logical_selected_paths.get(
                EVENT_REPORT_ARTIFACT_ID
            ),
        )
        wind_receipt = dict(wind_horizons)
        wind_receipt["output"] = str(
            resolved_output / "weather" / WIND_HORIZON_OUTPUT_NAME
        )
        selected_wind_receipt = dict(selected_wind)
        selected_wind_receipt["output"] = str(
            logical_weather_paths["capacity_weighted_wind_features_daily"]
        )
        solar_receipt = dict(solar)
        solar_receipt["signal_output"] = str(
            logical_weather_paths["capacity_weighted_solar_signals"]
        )
        solar_receipt["lead_output"] = str(
            logical_weather_paths["capacity_weighted_location_leads"]
        )
        receipt = {
            "status": "verified",
            "rebuild_boundary": "processed upstream inputs through V03 result",
            "cpc_data_issue_changed": False,
            "weather_manifest": str(weather_manifest),
            "weather_manifest_sha256": sha256_file(weather_manifest),
            "formal_manifest": str(formal_manifest),
            "formal_manifest_sha256": sha256_file(formal_manifest),
            "selected_input_manifest": str(selected_input_manifest),
            "selected_input_manifest_sha256": sha256_file(
                selected_input_manifest
            ),
            "selected_input_artifacts_validated": len(selected_paths),
            "selected_wind_rebuild": selected_wind_receipt,
            "solar_rebuild": solar_receipt,
            "wind_horizon_rebuild": wind_receipt,
            "model_v01_rebuild": model_v01,
            "source_to_score_rebuild": score_receipt,
            "model_v03_rebuild": strategy,
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
        "--formal-manifest",
        type=Path,
        default=DEFAULT_FORMAL_MANIFEST,
    )
    parser.add_argument(
        "--selected-input-manifest",
        type=Path,
        default=DEFAULT_SELECTED_INPUT_MANIFEST,
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    receipt = rebuild_model_v03(
        weather_manifest=args.weather_manifest,
        formal_manifest=args.formal_manifest,
        selected_input_manifest=args.selected_input_manifest,
        output_dir=args.output_dir,
        overwrite=args.overwrite,
    )
    print(json.dumps(receipt, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
