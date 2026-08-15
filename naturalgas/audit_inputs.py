"""Materialize immutable audit inputs from generation-pinned GCS objects.

Intermediate audit tables are not tracked in Git.  Their immutable identities
live in checked-in manifests, and consumers download exact object generations
into the ignored ``inputs/gcs`` cache before reading them.  Explicit paths
passed by callers are left untouched so tests and one-off research builds can
still inject local fixtures.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Iterable, Mapping

from naturalgas.reproducibility import (
    PROJECT_ROOT,
    Artifact,
    fetch_manifest,
    load_manifest,
    local_artifact_path,
)


DEFAULT_SELECTED_INPUT_MANIFEST = (
    PROJECT_ROOT / "manifests/selected_strategy_inputs_2026-08-14.json"
)
DEFAULT_WIND_NOTEBOOK_INPUT_MANIFEST = (
    PROJECT_ROOT / "manifests/wind_notebook_audit_inputs_2026-08-15.json"
)
DEFAULT_AUDIT_CACHE_ROOT = PROJECT_ROOT / "inputs/gcs"

EIA930_SOURCE_ARTIFACT_ID = "selected_eia930_southeast_daily_multifuel"
FLORIDA_HISTORY_ARTIFACT_ID = "selected_florida_available_ba_signal_history"
EIA930_OVERLAY_ARTIFACT_ID = "selected_eia930_overlay_inputs"
EVENT_REPORTS_ARTIFACT_ID = "selected_event_reports_aligned"
WIND_WEIGHTS_ARTIFACT_ID = "selected_annual_location_weights"
SOLAR_WEIGHTS_ARTIFACT_ID = "selected_monthly_location_weights"
LEGACY_WNGSR_ARTIFACT_ID = "selected_legacy_wngsr_formal_scores"
WNGSR_CORRECTIONS_ARTIFACT_ID = "selected_wngsr_d1_3_score_corrections"
D1_3_SCORE_INPUTS_ARTIFACT_ID = "selected_d1_3_storage_amplifier_inputs"
WIND_WEIGHTS_CSV_ARTIFACT_ID = "wind_annual_location_weights_csv"
WIND_DIAGNOSTICS_CSV_ARTIFACT_ID = "wind_annual_fleet_diagnostics_csv"

AUDIT_ARTIFACT_IDS = (
    EIA930_SOURCE_ARTIFACT_ID,
    FLORIDA_HISTORY_ARTIFACT_ID,
    EIA930_OVERLAY_ARTIFACT_ID,
    EVENT_REPORTS_ARTIFACT_ID,
    WIND_WEIGHTS_ARTIFACT_ID,
    SOLAR_WEIGHTS_ARTIFACT_ID,
    LEGACY_WNGSR_ARTIFACT_ID,
    WNGSR_CORRECTIONS_ARTIFACT_ID,
    D1_3_SCORE_INPUTS_ARTIFACT_ID,
    WIND_WEIGHTS_CSV_ARTIFACT_ID,
    WIND_DIAGNOSTICS_CSV_ARTIFACT_ID,
)

_MANIFESTS = (
    DEFAULT_SELECTED_INPUT_MANIFEST,
    DEFAULT_WIND_NOTEBOOK_INPUT_MANIFEST,
)


def _artifact_registry() -> dict[str, tuple[Path, Artifact]]:
    registry: dict[str, tuple[Path, Artifact]] = {}
    for manifest_path in _MANIFESTS:
        for artifact in load_manifest(manifest_path):
            if artifact.artifact_id not in AUDIT_ARTIFACT_IDS:
                continue
            if artifact.artifact_id in registry:
                raise ValueError(
                    f"Duplicate audit artifact id: {artifact.artifact_id}"
                )
            registry[artifact.artifact_id] = (manifest_path, artifact)
    missing = set(AUDIT_ARTIFACT_IDS).difference(registry)
    if missing:
        raise KeyError(f"Audit manifests are missing: {sorted(missing)}")
    return registry


def audit_input_path(
    artifact_id: str,
    *,
    cache_root: Path = DEFAULT_AUDIT_CACHE_ROOT,
) -> Path:
    """Return the ignored local cache path declared for one audit object."""

    try:
        _, artifact = _artifact_registry()[artifact_id]
    except KeyError as exc:
        raise KeyError(f"Unknown audit artifact id: {artifact_id}") from exc
    return local_artifact_path(artifact, root=cache_root)


def materialize_audit_inputs(
    artifact_ids: Iterable[str] = AUDIT_ARTIFACT_IDS,
    *,
    cache_root: Path = DEFAULT_AUDIT_CACHE_ROOT,
    overwrite: bool = False,
) -> dict[str, Path]:
    """Download, validate, and return exact GCS generations for audit inputs."""

    requested = tuple(dict.fromkeys(artifact_ids))
    registry = _artifact_registry()
    unknown = set(requested).difference(registry)
    if unknown:
        raise KeyError(f"Unknown audit artifact ids: {sorted(unknown)}")

    result: dict[str, Path] = {}
    for manifest_path in _MANIFESTS:
        selected = [
            artifact_id
            for artifact_id in requested
            if registry[artifact_id][0] == manifest_path
        ]
        if selected:
            result.update(
                fetch_manifest(
                    manifest_path,
                    root=cache_root,
                    artifact_ids=selected,
                    overwrite=overwrite,
                )
            )
    return {artifact_id: result[artifact_id] for artifact_id in requested}


def resolve_audit_inputs(
    paths_by_artifact_id: Mapping[str, Path],
) -> dict[str, Path]:
    """Materialize default cache paths while preserving explicit overrides."""

    resolved = {
        artifact_id: Path(path)
        for artifact_id, path in paths_by_artifact_id.items()
    }
    defaults = [
        artifact_id
        for artifact_id, path in resolved.items()
        if path.expanduser().resolve() == audit_input_path(
            artifact_id
        ).expanduser().resolve()
    ]
    if defaults:
        resolved.update(materialize_audit_inputs(defaults))
    return resolved


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", action="append", dest="artifacts")
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_AUDIT_CACHE_ROOT)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    paths = materialize_audit_inputs(
        args.artifacts or AUDIT_ARTIFACT_IDS,
        cache_root=args.cache_root,
        overwrite=args.overwrite,
    )
    print(json.dumps({key: str(value) for key, value in paths.items()}, indent=2))


if __name__ == "__main__":
    main()
