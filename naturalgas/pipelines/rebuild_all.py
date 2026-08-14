"""Rebuild the master panel and approved strategies from immutable inputs.

The command verifies the 72 direct objects declared for the 155-column master
panel plus 254 weather partitions and two frozen capacity-weight snapshots.
It rebuilds the panel after applying the audited NYMEX session filter and
rebuilds the selected wind/solar artifacts plus the D1--3 horizon lineage
byte-for-byte, then uses the pinned EIA inputs to recompute the formal and
selected D1--3 strategies. All outputs are local; this entry point has no GCS
write capability.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from naturalgas.build_multisignal_panel import (
    DEFAULT_INPUT_MANIFEST as DEFAULT_PANEL_MANIFEST,
    build_from_manifest,
    load_input_manifest,
    write_panel,
)
from naturalgas.pipelines.rebuild_final_backtest import rebuild
from naturalgas.pipelines.rebuild_d1_3_strategy import (
    evaluate_selected_strategy_with_horizon,
)
from naturalgas.pipelines.rebuild_weather_factors import (
    load_factor_inputs,
    rebuild_selected_wind,
    rebuild_solar,
    rebuild_wind_horizons,
)
from naturalgas.evaluate_d1_3_storage_amplified_strategy import (
    DEFAULT_EVENT_REPORTS_PATH,
    SCORE_INPUTS,
    STORAGE_CALENDAR_CORRECTIONS,
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

        formal = rebuild(
            manifest_path=formal_manifest,
            root=root,
            output_dir=staging / "final_backtest",
            fetch=fetch,
            overwrite=False,
            artifact_overrides=artifact_overrides,
            receipt_output_dir=resolved_output / "final_backtest",
            receipt_override_paths=receipt_override_paths,
            contract_only_override_ids={"ng_multisignal_panel"},
        )
        selected_d1_3 = None
        if horizon_result is not None:
            selected_d1_3 = evaluate_selected_strategy_with_horizon(
                horizon_path=Path(horizon_result["output"]),
                formal_daily_path=(
                    staging / "final_backtest" / "strategy_daily.parquet"
                ),
                score_inputs_path=SCORE_INPUTS,
                storage_calendar_corrections_path=(
                    STORAGE_CALENDAR_CORRECTIONS
                ),
                event_reports_path=DEFAULT_EVENT_REPORTS_PATH,
                output_dir=staging / "d1_3_strategy",
                logical_output_dir=resolved_output / "d1_3_strategy",
                logical_formal_daily_path=(
                    resolved_output / "final_backtest" / "strategy_daily.parquet"
                ),
            )
        receipt = {
            "status": "verified",
            "panel_manifest": str(panel_manifest),
            "panel_manifest_sha256": sha256_file(panel_manifest),
            "formal_manifest": str(formal_manifest),
            "formal_manifest_sha256": sha256_file(formal_manifest),
            "weather_factor_rebuilt": rebuild_weather,
            "weather_factor_rebuild": weather_receipt,
            "rebuilt_panel": str(logical_panel_path),
            "rebuilt_panel_sha256": panel_sha256,
            "master_panel_objects": panel_object_count,
            "master_panel_rows": len(panel),
            "master_panel_columns": len(panel.columns),
            "formal_rebuild": formal,
            "selected_d1_3_rebuild": selected_d1_3,
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
    )
    print(json.dumps(receipt, default=str, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
