"""Rebuild the master panel and approved strategies from immutable inputs.

The command verifies the 72 direct objects declared for the 155-column master
panel plus 254 weather partitions, two raw capacity snapshots, and the complete
selected-strategy input archive.
It rebuilds the panel after applying the audited NYMEX session filter and
rebuilds the selected wind/solar artifacts plus the D1--3 horizon lineage
byte-for-byte, reconstructs the V03 Central/Florida signals and every
pre-guard score from independent upstream inputs, and then recomputes the
formal and selected D1--3 strategies. All outputs are local; this entry point
has no GCS write capability.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from naturalgas.build_model_v03_score_inputs import (
    build_score_inputs,
    write_score_input_build,
)
from naturalgas.build_multisignal_panel import (
    DEFAULT_INPUT_MANIFEST as DEFAULT_PANEL_MANIFEST,
    build_from_manifest,
    load_input_manifest,
    write_panel,
)
from naturalgas.pipelines.rebuild_model_v01 import (
    local_filesystem,
    rebuild_model_v01,
)
from naturalgas.pipelines.rebuild_model_v03 import (
    CENTRAL_EIA930_SOURCE_ARTIFACT_ID,
    DEFAULT_SELECTED_INPUT_MANIFEST,
    EVENT_REPORT_ARTIFACT_ID,
    SCORE_INPUT_ARTIFACT_ID,
    SOUTHEAST_EIA930_SOURCE_ARTIFACT_ID,
    STORAGE_CORRECTION_ARTIFACT_ID,
    evaluate_model_v03_with_horizon,
    fetch_selected_strategy_inputs,
)
from naturalgas.pipelines.rebuild_weather_factors import (
    load_factor_inputs,
    rebuild_selected_wind,
    rebuild_solar,
    rebuild_wind_horizons,
)
from naturalgas.reproducibility import (
    DEFAULT_MANIFEST as DEFAULT_FORMAL_MANIFEST,
    PROJECT_ROOT,
    create_staging_directory,
    discard_staging_directory,
    publish_staging_directory,
    sha256_file,
)


DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "reproduced/full_chain"
DEFAULT_WEATHER_MANIFEST = (
    PROJECT_ROOT / "manifests/weather_factor_inputs_2026-07-28.json"
)
EXPECTED_CORRECTED_MASTER_PANEL_SHA256 = (
    "da68649891fcfd913a6ac46e01cf14dac951c7c71d8ec5e2480bbc9da27057c4"
)


def rebuild_all(
    *,
    panel_manifest: Path,
    weather_manifest: Path,
    formal_manifest: Path,
    root: Path,
    output_dir: Path,
    fetch: bool,
    overwrite: bool,
    rebuild_weather: bool = True,
    selected_input_manifest: Path = DEFAULT_SELECTED_INPUT_MANIFEST,
) -> dict:
    staging, resolved_output = create_staging_directory(
        output_dir,
        overwrite=overwrite,
    )
    try:
        panel_inputs = load_input_manifest(panel_manifest)
        panel_object_count = sum(
            len(item["objects"]) if item["partitioned"] else 1
            for item in panel_inputs["inputs"]
        )
        panel_path = staging / "ng_multisignal_panel.parquet"
        logical_panel_path = resolved_output / panel_path.name
        panel = build_from_manifest(panel_manifest)
        write_panel(panel, panel_path, overwrite=False)
        panel_sha256 = sha256_file(panel_path)
        if panel_sha256 != EXPECTED_CORRECTED_MASTER_PANEL_SHA256:
            raise AssertionError(
                "Holiday-corrected master panel hash differs: "
                f"{panel_sha256} != {EXPECTED_CORRECTED_MASTER_PANEL_SHA256}"
            )

        artifact_overrides = {"ng_multisignal_panel": panel_path}
        receipt_override_paths = {
            "ng_multisignal_panel": logical_panel_path,
        }
        weather_receipt: dict | None = None
        horizon_result: dict | None = None
        if rebuild_weather:
            weather_dir = staging / "weather_factors"
            wind_inputs = load_factor_inputs(weather_manifest, "wind")
            wind_result = rebuild_selected_wind(
                inputs=wind_inputs,
                output_dir=weather_dir,
            )
            horizon_result = rebuild_wind_horizons(
                inputs=wind_inputs,
                output_dir=weather_dir,
            )
            solar_result = rebuild_solar(
                inputs=load_factor_inputs(weather_manifest, "solar"),
                output_dir=weather_dir,
            )
            artifact_overrides.update({
                "capacity_weighted_wind_features_daily": Path(
                    wind_result["output"]
                ),
                "capacity_weighted_solar_signals": Path(
                    solar_result["signal_output"]
                ),
                "capacity_weighted_location_leads": Path(
                    solar_result["lead_output"]
                ),
            })
            logical_weather_dir = resolved_output / "weather_factors"
            receipt_override_paths.update({
                "capacity_weighted_wind_features_daily": (
                    logical_weather_dir
                    / Path(wind_result["output"]).name
                ),
                "capacity_weighted_solar_signals": (
                    logical_weather_dir
                    / Path(solar_result["signal_output"]).name
                ),
                "capacity_weighted_location_leads": (
                    logical_weather_dir
                    / Path(solar_result["lead_output"]).name
                ),
            })
            wind_receipt = dict(wind_result)
            wind_receipt["output"] = str(
                receipt_override_paths[
                    "capacity_weighted_wind_features_daily"
                ]
            )
            solar_receipt = dict(solar_result)
            solar_receipt["signal_output"] = str(
                receipt_override_paths["capacity_weighted_solar_signals"]
            )
            solar_receipt["lead_output"] = str(
                receipt_override_paths["capacity_weighted_location_leads"]
            )
            horizon_receipt = dict(horizon_result)
            horizon_receipt["output"] = str(
                logical_weather_dir / Path(horizon_result["output"]).name
            )
            weather_receipt = {
                "manifest": str(weather_manifest),
                "manifest_sha256": sha256_file(weather_manifest),
                "wind": wind_receipt,
                "wind_horizons": horizon_receipt,
                "solar": solar_receipt,
            }

        model_v01 = rebuild_model_v01(
            manifest_path=formal_manifest,
            root=root,
            output_dir=staging / "models/v01_south_central_storage",
            fetch=fetch,
            overwrite=False,
            artifact_overrides=artifact_overrides,
            receipt_output_dir=(
                resolved_output / "models/v01_south_central_storage"
            ),
            receipt_override_paths=receipt_override_paths,
            contract_only_override_ids={"ng_multisignal_panel"},
        )
        if not model_v01["summary_byte_match"]:
            raise AssertionError(
                "Corrected-panel V01 summary does not match the canonical "
                "summary byte-for-byte"
            )
        model_v03 = None
        selected_input_artifact_count = 0
        if horizon_result is not None:
            selected_paths = fetch_selected_strategy_inputs(
                selected_input_manifest,
                root=staging,
            )
            logical_selected_paths = {
                artifact_id: resolved_output / path.relative_to(staging)
                for artifact_id, path in selected_paths.items()
            }
            selected_input_artifact_count = len(selected_paths)
            score_input_dir = staging / "models/v03_score_inputs"
            score_build = build_score_inputs(
                panel_path=panel_path,
                wind_path=artifact_overrides[
                    "capacity_weighted_wind_features_daily"
                ],
                wind_horizon_path=Path(horizon_result["output"]),
                solar_signal_path=artifact_overrides[
                    "capacity_weighted_solar_signals"
                ],
                solar_lead_path=artifact_overrides[
                    "capacity_weighted_location_leads"
                ],
                central_eia930_path=selected_paths[
                    CENTRAL_EIA930_SOURCE_ARTIFACT_ID
                ],
                southeast_eia930_path=selected_paths[
                    SOUTHEAST_EIA930_SOURCE_ARTIFACT_ID
                ],
                filesystem=local_filesystem(formal_manifest, root=root),
                frozen_score_inputs_path=selected_paths[
                    SCORE_INPUT_ARTIFACT_ID
                ],
                frozen_storage_corrections_path=selected_paths[
                    STORAGE_CORRECTION_ARTIFACT_ID
                ],
            )
            score_input_receipt = write_score_input_build(
                score_build,
                output_dir=score_input_dir,
                receipt_output_dir=(
                    resolved_output / "models/v03_score_inputs"
                ),
            )
            model_v03 = evaluate_model_v03_with_horizon(
                horizon_path=Path(horizon_result["output"]),
                model_v01_daily_path=(
                    staging
                    / "models/v01_south_central_storage/strategy_daily.parquet"
                ),
                score_inputs_path=(
                    score_input_dir / "model_v03_score_inputs.parquet"
                ),
                storage_calendar_corrections_path=(
                    score_input_dir / "wngsr_score_corrections.parquet"
                ),
                event_reports_path=selected_paths[EVENT_REPORT_ARTIFACT_ID],
                output_dir=staging / "models/v03_d1_3_storage_guard",
                logical_output_dir=(
                    resolved_output / "models/v03_d1_3_storage_guard"
                ),
                logical_model_v01_daily_path=(
                    resolved_output
                    / "models/v01_south_central_storage/strategy_daily.parquet"
                ),
                logical_score_inputs_path=(
                    resolved_output
                    / "models/v03_score_inputs/model_v03_score_inputs.parquet"
                ),
                logical_storage_calendar_corrections_path=(
                    resolved_output
                    / "models/v03_score_inputs/wngsr_score_corrections.parquet"
                ),
                logical_event_reports_path=logical_selected_paths[
                    EVENT_REPORT_ARTIFACT_ID
                ],
            )
            model_v03["source_to_score_rebuild"] = score_input_receipt
        receipt = {
            "status": "verified",
            "panel_manifest": str(panel_manifest),
            "panel_manifest_sha256": sha256_file(panel_manifest),
            "formal_manifest": str(formal_manifest),
            "formal_manifest_sha256": sha256_file(formal_manifest),
            "selected_input_manifest": str(selected_input_manifest),
            "selected_input_manifest_sha256": sha256_file(
                selected_input_manifest
            ),
            "selected_input_artifacts_validated": (
                selected_input_artifact_count
            ),
            "weather_factor_rebuilt": rebuild_weather,
            "weather_factor_rebuild": weather_receipt,
            "rebuilt_panel": str(logical_panel_path),
            "rebuilt_panel_sha256": panel_sha256,
            "master_panel_objects": panel_object_count,
            "master_panel_rows": len(panel),
            "master_panel_columns": len(panel.columns),
            "model_v01_rebuild": model_v01,
            "model_v03_rebuild": model_v03,
        }
        (staging / "full_chain_receipt.json").write_text(
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
    parser.add_argument(
        "--panel-manifest",
        type=Path,
        default=DEFAULT_PANEL_MANIFEST,
    )
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
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--use-approved-weather-artifacts",
        action="store_true",
        help=(
            "Skip raw weather-factor reconstruction and download the three "
            "approved wind/solar artifacts from the formal manifest; the "
            "selected D1-3 raw-lineage rebuild is also skipped."
        ),
    )
    parser.add_argument(
        "--offline-formal-inputs",
        action="store_true",
        help=(
            "Use existing local wind/solar/EIA inputs. The master-panel inputs "
            "are still read at their pinned GCS generations."
        ),
    )
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    receipt = rebuild_all(
        panel_manifest=args.panel_manifest,
        weather_manifest=args.weather_manifest,
        formal_manifest=args.formal_manifest,
        root=args.root,
        output_dir=args.output_dir,
        fetch=not args.offline_formal_inputs,
        overwrite=args.overwrite,
        rebuild_weather=not args.use_approved_weather_artifacts,
        selected_input_manifest=args.selected_input_manifest,
    )
    print(json.dumps(receipt, default=str, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
