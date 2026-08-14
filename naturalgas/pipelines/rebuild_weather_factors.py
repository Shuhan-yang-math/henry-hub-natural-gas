"""Rebuild the wind and solar weather-factor parquet inputs.

This factor-only entry point deliberately stops at the processed research
input boundary. It reads explicitly listed local or GCS weather partitions,
requires a frozen capacity snapshot, and writes only local parquet files. It
does not fetch live USWTDB/EIA capacity data, submit NCAR/GDEX jobs, upload to
GCS, or run any strategy evaluation.

An input manifest is a small JSON document with one or both of these sections:

{
  "manifest_version": 1,
  "wind": {
    "weather_partitions": [{
      "uri": "gs://bucket/.../data.parquet",
      "generation": "1234567890",
      "size_bytes": 1234,
      "sha256": "..."
    }],
    "capacity_snapshot": {
      "uri": "/snapshots/uswtdb_turbines.parquet",
      "size_bytes": 1234,
      "sha256": "..."
    },
    "capacity_kind": "uswtdb_turbines"
  }
}

Local paths in a manifest are resolved relative to the manifest. Instead of a
manifest, local paths can be supplied explicitly on the command line. GCS
inputs require the manifest so their generation, size, and digest are pinned.
Frozen derived weight snapshots are also accepted with capacity kinds
annual_location_weights and monthly_location_weights.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from typing import Any, BinaryIO, Iterable, Sequence

import gcsfs
import numpy as np
import pandas as pd

from naturalgas.evaluate_ncar_gdex_complete_wind_factor import (
    SHEAR_EXPONENT,
    build_annual_location_weights,
    build_capacity_features,
)
from naturalgas.evaluate_ncar_gdex_solar_factor import build_solar_signals
from naturalgas.ncar_gdex_capacity_weighted_solar import (
    DAILY_COLUMNS,
    build_capacity_weighted_location_leads,
    build_monthly_location_weights,
)
from naturalgas.ncar_gdex_wind_backfill_to_gcs import LOCATIONS
from naturalgas.ncar_gdex_nonlinear_wind import (
    causal_zscore,
    nonlinear_power_components,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_DIR = (
    PROJECT_ROOT / "naturalgas/processed/rebuilt_weather_factors"
)
WIND_OUTPUT_NAME = "capacity_weighted_wind_features_daily.parquet"
WIND_HORIZON_OUTPUT_NAME = "wind_horizon_signals.parquet"
SOLAR_SIGNAL_OUTPUT_NAME = "capacity_weighted_solar_signals.parquet"
SOLAR_LEAD_OUTPUT_NAME = "capacity_weighted_location_leads.parquet"
PRIMARY_CYCLE_UTC = 0
WIND_HORIZONS: dict[str, tuple[int, ...]] = {
    "d1": (1,),
    "d1_3": (1, 2, 3),
    "d1_5": (1, 2, 3, 4, 5),
}
WIND_Z_CAP = 2.0
WIND_Z_WINDOW = 60
WIND_Z_MIN_PERIODS = 30
EXPECTED_VALID_HOURS_PER_LEAD = 4
CAPACITY_LAG_MONTHS = 2
MINIMUM_SOLAR_CAPACITY_COVERAGE = 0.995
EXPECTED_LOCATION_IDS = frozenset(location.location_id for location in LOCATIONS)

WIND_CAPACITY_KINDS = {
    "uswtdb_turbines",
    "annual_location_weights",
}
SOLAR_CAPACITY_KINDS = {
    "eia_generators",
    "monthly_location_weights",
}


class FactorBuildInputError(ValueError):
    """Raised when declared factor-build inputs are incomplete or ambiguous."""


@dataclass(frozen=True)
class InputArtifact:
    """One optionally generation-pinned and content-addressed input."""

    uri: str
    generation: str | None = None
    sha256: str | None = None
    size_bytes: int | None = None


@dataclass(frozen=True)
class ApprovedOutput:
    """Expected serialization for a reviewed factor output."""

    filename: str
    rows: int
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class FactorInputs:
    """Resolved, explicit inputs for one factor family."""

    weather_partitions: tuple[InputArtifact, ...]
    capacity_snapshot: InputArtifact
    capacity_kind: str
    approved_outputs: tuple[ApprovedOutput, ...] = ()

    @property
    def weather_uris(self) -> tuple[str, ...]:
        return tuple(artifact.uri for artifact in self.weather_partitions)

    @property
    def artifacts(self) -> tuple[InputArtifact, ...]:
        return (*self.weather_partitions, self.capacity_snapshot)


class ReadOnlyPartitionFileSystem:
    """Open local or GCS inputs while rejecting every write mode."""

    def __init__(self, artifacts: Iterable[InputArtifact] = ()) -> None:
        self._gcs: gcsfs.GCSFileSystem | None = None
        self._artifacts = {artifact.uri: artifact for artifact in artifacts}

    def open(self, path: str, mode: str = "rb") -> BinaryIO:
        if mode != "rb":
            raise PermissionError(
                f"factor input filesystem is read-only; rejected mode {mode!r}"
            )
        artifact = self._artifacts.get(path, InputArtifact(uri=path))
        if _is_gcs(path):
            if self._gcs is None:
                self._gcs = gcsfs.GCSFileSystem()
            # Do not use GCSFileSystem.open(..., generation=...). In gcsfs
            # 2026.7.0, GCSFile._fetch_range does not forward that constructor
            # argument to its eventual cat_file request. A direct one-shot
            # download forwards the generation into the media URL.
            payload = self._gcs.cat_file(
                _gcs_key(path),
                generation=artifact.generation,
                concurrency=1,
            )
        else:
            payload = Path(path).read_bytes()
        _verify_payload(payload, artifact)
        return io.BytesIO(payload)


def _is_gcs(path: str) -> bool:
    return path.startswith("gs://")


def _gcs_key(path: str) -> str:
    return path[5:] if _is_gcs(path) else path


def _verify_payload(payload: bytes, artifact: InputArtifact) -> None:
    if artifact.size_bytes is not None and len(payload) != artifact.size_bytes:
        raise FactorBuildInputError(
            f"size mismatch for {artifact.uri}: {len(payload)} != "
            f"{artifact.size_bytes}"
        )
    if artifact.sha256 is not None:
        digest = hashlib.sha256(payload).hexdigest()
        if digest != artifact.sha256:
            raise FactorBuildInputError(
                f"SHA-256 mismatch for {artifact.uri}: "
                f"{digest} != {artifact.sha256}"
            )


def _path_suffix(path: str) -> str:
    return Path(path.split("?", maxsplit=1)[0]).suffix.lower()


def _reject_implicit_glob(path: str) -> None:
    if any(character in path for character in "*?[]"):
        raise FactorBuildInputError(
            "weather partitions must be explicitly enumerated; "
            f"wildcards are not accepted: {path}"
        )


def _resolve_manifest_path(value: Any, *, base_dir: Path) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FactorBuildInputError("manifest paths must be non-empty strings")
    path = value.strip()
    if _is_gcs(path):
        return path
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    return str(candidate.resolve())


def _manifest_artifact(
    value: Any,
    *,
    base_dir: Path,
    label: str,
) -> InputArtifact:
    if not isinstance(value, dict):
        raise FactorBuildInputError(
            f"manifest {label} must be an artifact object with uri, "
            "sha256 and size_bytes"
        )
    uri = _resolve_manifest_path(value.get("uri"), base_dir=base_dir)
    sha256 = value.get("sha256")
    size_bytes = value.get("size_bytes")
    generation = value.get("generation")
    if (
        not isinstance(sha256, str)
        or len(sha256) != 64
        or any(character not in "0123456789abcdef" for character in sha256)
    ):
        raise FactorBuildInputError(
            f"manifest {label}.sha256 must be a lowercase 64-character digest"
        )
    if not isinstance(size_bytes, int) or size_bytes <= 0:
        raise FactorBuildInputError(
            f"manifest {label}.size_bytes must be a positive integer"
        )
    if _is_gcs(uri):
        if not isinstance(generation, (str, int)) or not str(generation).isdigit():
            raise FactorBuildInputError(
                f"manifest {label}.generation is required for GCS inputs"
            )
        generation = str(generation)
    elif generation is not None:
        raise FactorBuildInputError(
            f"manifest {label}.generation is only valid for GCS inputs"
        )
    return InputArtifact(
        uri=uri,
        generation=generation,
        sha256=sha256,
        size_bytes=size_bytes,
    )


def load_factor_inputs(
    manifest_path: str | Path,
    component: str,
) -> FactorInputs:
    """Load and validate one component from a pinned factor-input manifest."""

    if component not in {"wind", "solar"}:
        raise FactorBuildInputError(
            f"factor component must be 'wind' or 'solar', not {component!r}"
        )
    manifest_path = Path(manifest_path).expanduser().resolve()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FactorBuildInputError(
            f"input manifest does not exist: {manifest_path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise FactorBuildInputError(
            f"input manifest is not valid JSON: {manifest_path}"
        ) from exc
    if not isinstance(payload, dict):
        raise FactorBuildInputError(
            f"input manifest root must be a JSON object: {manifest_path}"
        )
    if payload.get("manifest_version") != 1:
        raise FactorBuildInputError(
            "input manifest manifest_version must be 1"
        )
    section = payload.get(component)
    if not isinstance(section, dict):
        raise FactorBuildInputError(
            f"input manifest has no {component!r} object"
        )
    partitions = section.get("weather_partitions")
    if not isinstance(partitions, list) or not partitions:
        raise FactorBuildInputError(
            f"manifest {component}.weather_partitions must be a non-empty list"
        )
    weather = tuple(
        _manifest_artifact(
            item,
            base_dir=manifest_path.parent,
            label=f"{component}.weather_partitions[{number}]",
        )
        for number, item in enumerate(partitions)
    )
    capacity = _manifest_artifact(
        section.get("capacity_snapshot"),
        base_dir=manifest_path.parent,
        label=f"{component}.capacity_snapshot",
    )
    kind = section.get("capacity_kind")
    if not isinstance(kind, str) or not kind:
        raise FactorBuildInputError(
            f"manifest {component}.capacity_kind must be set"
        )
    approved_outputs = _manifest_approved_outputs(section, component=component)
    return _validated_inputs(
        FactorInputs(
            weather_partitions=weather,
            capacity_snapshot=capacity,
            capacity_kind=kind,
            approved_outputs=approved_outputs,
        ),
        component=component,
    )


def _manifest_approved_outputs(
    section: dict[str, Any],
    *,
    component: str,
) -> tuple[ApprovedOutput, ...]:
    raw = section.get("approved_outputs")
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise FactorBuildInputError(
            f"manifest {component}.approved_outputs must be an object"
        )
    expected_names = (
        {WIND_OUTPUT_NAME, WIND_HORIZON_OUTPUT_NAME}
        if component == "wind"
        else {SOLAR_LEAD_OUTPUT_NAME, SOLAR_SIGNAL_OUTPUT_NAME}
    )
    if set(raw) != expected_names:
        raise FactorBuildInputError(
            f"manifest {component}.approved_outputs must contain exactly "
            f"{sorted(expected_names)}"
        )
    outputs: list[ApprovedOutput] = []
    for filename in sorted(raw):
        value = raw[filename]
        if not isinstance(value, dict):
            raise FactorBuildInputError(
                f"manifest approved output {filename} must be an object"
            )
        rows = value.get("rows")
        sha256 = value.get("sha256")
        size_bytes = value.get("size_bytes")
        if not isinstance(rows, int) or rows <= 0:
            raise FactorBuildInputError(
                f"manifest approved output {filename}.rows must be positive"
            )
        if (
            not isinstance(sha256, str)
            or len(sha256) != 64
            or any(character not in "0123456789abcdef" for character in sha256)
        ):
            raise FactorBuildInputError(
                f"manifest approved output {filename}.sha256 is invalid"
            )
        if not isinstance(size_bytes, int) or size_bytes <= 0:
            raise FactorBuildInputError(
                f"manifest approved output {filename}.size_bytes must be positive"
            )
        outputs.append(
            ApprovedOutput(
                filename=filename,
                rows=rows,
                sha256=sha256,
                size_bytes=size_bytes,
            )
        )
    return tuple(outputs)


def _approved_outputs_for(
    inputs: FactorInputs,
    *filenames: str,
) -> tuple[ApprovedOutput, ...]:
    """Select the approved serializations produced by one build operation."""

    requested = set(filenames)
    selected = tuple(
        item for item in inputs.approved_outputs if item.filename in requested
    )
    if inputs.approved_outputs and {item.filename for item in selected} != requested:
        missing = sorted(requested - {item.filename for item in selected})
        raise FactorBuildInputError(
            f"manifest has no approved output declaration for {missing}"
        )
    return selected


def _validated_inputs(inputs: FactorInputs, *, component: str) -> FactorInputs:
    allowed = WIND_CAPACITY_KINDS if component == "wind" else SOLAR_CAPACITY_KINDS
    if inputs.capacity_kind not in allowed:
        raise FactorBuildInputError(
            f"invalid {component} capacity kind {inputs.capacity_kind!r}; "
            f"expected one of {sorted(allowed)}"
        )
    if not inputs.weather_partitions:
        raise FactorBuildInputError(
            f"at least one {component} weather partition is required"
        )
    for artifact in inputs.artifacts:
        _reject_implicit_glob(artifact.uri)
    if len(set(inputs.weather_uris)) != len(inputs.weather_uris):
        raise FactorBuildInputError(
            f"{component} weather partition list contains duplicates"
        )
    return inputs


def _cli_inputs(args: argparse.Namespace, *, component: str) -> FactorInputs:
    direct_values = (
        args.weather_partition,
        args.capacity_snapshot,
        args.capacity_kind,
    )
    if args.input_manifest is not None:
        if any(value for value in direct_values):
            raise FactorBuildInputError(
                "--input-manifest cannot be combined with direct weather or "
                "capacity arguments"
            )
        return load_factor_inputs(args.input_manifest, component)
    if (
        not args.weather_partition
        or not args.capacity_snapshot
        or not args.capacity_kind
    ):
        raise FactorBuildInputError(
            "provide --input-manifest, or provide all of "
            "--weather-partition, --capacity-snapshot and --capacity-kind; "
            f"a frozen {component} capacity snapshot is mandatory"
        )
    direct_gcs = [
        item
        for item in [*args.weather_partition, args.capacity_snapshot]
        if _is_gcs(item)
    ]
    if direct_gcs:
        raise FactorBuildInputError(
            "direct CLI GCS inputs are not allowed because they cannot carry "
            "generation, SHA-256 and size pins; declare them in an input manifest"
        )
    inputs = FactorInputs(
        weather_partitions=tuple(
            InputArtifact(
                uri=(
                    str(Path(item).expanduser().resolve())
                    if not _is_gcs(item)
                    else item
                )
            )
            for item in args.weather_partition
        ),
        capacity_snapshot=InputArtifact(
            uri=(
                str(Path(args.capacity_snapshot).expanduser().resolve())
                if not _is_gcs(args.capacity_snapshot)
                else args.capacity_snapshot
            )
        ),
        capacity_kind=args.capacity_kind,
    )
    return _validated_inputs(inputs, component=component)


def _local_output_dir(value: str | Path) -> Path:
    text = str(value)
    if "://" in text:
        raise FactorBuildInputError(
            f"output must be a local directory, not a URI: {text}"
        )
    return Path(text).expanduser().resolve()


def _ensure_targets_absent(targets: Iterable[Path]) -> None:
    existing = [path for path in targets if path.exists()]
    if existing:
        joined = ", ".join(str(path) for path in existing)
        raise FileExistsError(
            f"refusing to overwrite existing factor output(s): {joined}"
        )


def _write_parquet_outputs_no_overwrite(
    outputs: Sequence[tuple[pd.DataFrame, Path, str]],
    *,
    approved_outputs: Sequence[ApprovedOutput] = (),
) -> dict[str, dict[str, Any]]:
    """Stage and verify outputs, with create-only exception-safe promotion.

    Each hard-link promotion is atomic and cannot overwrite a target. If a
    Python exception, SystemExit, or KeyboardInterrupt occurs during a
    multi-file promotion, already-created targets are rolled back. This is not
    a transactional guarantee across process death or SIGKILL between links.
    """

    targets = [target for _, target, _ in outputs]
    _ensure_targets_absent(targets)
    for target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)

    approved = {item.filename: item for item in approved_outputs}
    if approved and set(approved) != {target.name for target in targets}:
        raise FactorBuildInputError(
            "approved output names do not match the requested factor outputs"
        )

    staged: list[tuple[Path, Path]] = []
    promoted: list[tuple[Path, Path]] = []
    metadata: dict[str, dict[str, Any]] = {}
    try:
        for frame, target, compression in outputs:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.",
                suffix=".tmp",
                dir=target.parent,
            )
            os.close(descriptor)
            temporary = Path(temporary_name)
            staged.append((temporary, target))
            with temporary.open("wb") as handle:
                frame.to_parquet(
                    handle,
                    index=False,
                    compression=compression,
                )
                handle.flush()
                os.fsync(handle.fileno())
            payload = temporary.read_bytes()
            observed = {
                "rows": len(frame),
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
            expected = approved.get(target.name)
            if expected is not None and observed != {
                "rows": expected.rows,
                "size_bytes": expected.size_bytes,
                "sha256": expected.sha256,
            }:
                raise FactorBuildInputError(
                    f"rebuilt {target.name} does not match its approved "
                    f"serialization: observed {observed}"
                )
            metadata[target.name] = observed

        # Each hard link is an atomic, create-only promotion on the same local
        # filesystem. If another process creates a target after preflight,
        # os.link fails instead of replacing that file.
        for temporary, target in staged:
            try:
                os.link(temporary, target)
            except FileExistsError as exc:
                raise FileExistsError(
                    f"refusing to overwrite existing factor output: {target}"
                ) from exc
            promoted.append((temporary, target))
    except BaseException:
        for temporary, target in promoted:
            try:
                # Do not delete a file another process may have atomically
                # substituted after our link. The staged path is still alive
                # here, so matching device/inode identifies our own target.
                if os.path.samestat(temporary.stat(), target.stat()):
                    target.unlink()
            except OSError:
                pass
        raise
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)
    return metadata


def _read_frame(
    path: str,
    filesystem: ReadOnlyPartitionFileSystem,
    *,
    columns: Sequence[str] | None = None,
) -> pd.DataFrame:
    suffix = _path_suffix(path)
    try:
        with filesystem.open(path, "rb") as handle:
            if suffix == ".parquet":
                return pd.read_parquet(handle, columns=columns)
            if suffix == ".csv":
                frame = pd.read_csv(handle, float_precision="round_trip")
                if columns is not None:
                    missing = set(columns).difference(frame.columns)
                    if missing:
                        raise FactorBuildInputError(
                            f"{path} is missing columns {sorted(missing)}"
                        )
                    frame = frame.loc[:, list(columns)]
                return frame
        raise FactorBuildInputError(
            f"unsupported input format for {path}; use parquet or CSV"
        )
    except FactorBuildInputError:
        raise
    except Exception as exc:
        raise FactorBuildInputError(
            f"could not read declared input {path}: {exc}"
        ) from exc


def _require_columns(
    frame: pd.DataFrame,
    required: Iterable[str],
    *,
    label: str,
) -> None:
    missing = set(required).difference(frame.columns)
    if missing:
        raise FactorBuildInputError(
            f"{label} is missing required columns {sorted(missing)}"
        )


def _validate_annual_wind_weights(weights: pd.DataFrame) -> None:
    numeric = weights[
        ["issue_year", "fleet_cutoff_year", "capacity_mw", "hub_height_m"]
    ].to_numpy(dtype=float)
    if not np.isfinite(numeric).all():
        raise FactorBuildInputError(
            "annual wind weights contain non-finite numeric values"
        )
    if not weights["fleet_cutoff_year"].eq(weights["issue_year"] - 1).all():
        raise FactorBuildInputError(
            "annual wind weights must use fleet_cutoff_year = issue_year - 1"
        )
    if weights["capacity_mw"].lt(0.0).any():
        raise FactorBuildInputError("annual wind capacity cannot be negative")
    if weights["hub_height_m"].le(0.0).any():
        raise FactorBuildInputError("annual wind hub heights must be positive")
    unknown = set(weights["location_id"]).difference(EXPECTED_LOCATION_IDS)
    if unknown:
        raise FactorBuildInputError(
            f"annual wind weights contain unknown locations: {sorted(unknown)}"
        )
    for issue_year, group in weights.groupby("issue_year", observed=True):
        locations = set(group["location_id"])
        if locations != EXPECTED_LOCATION_IDS:
            missing = sorted(EXPECTED_LOCATION_IDS.difference(locations))
            raise FactorBuildInputError(
                f"annual wind weights for {issue_year} omit locations {missing}"
            )
        if not group["capacity_mw"].sum() > 0.0:
            raise FactorBuildInputError(
                f"annual wind weights for {issue_year} have no positive capacity"
            )


def _validate_monthly_solar_weights(weights: pd.DataFrame) -> None:
    numeric_columns = ["capacity_mw", "capacity_share"]
    if "total_capacity_mw" in weights:
        numeric_columns.append("total_capacity_mw")
    if not np.isfinite(weights[numeric_columns].to_numpy(dtype=float)).all():
        raise FactorBuildInputError(
            "monthly solar weights contain non-finite numeric values"
        )
    if weights["capacity_mw"].le(0.0).any():
        raise FactorBuildInputError("monthly solar capacity must be positive")
    if weights["capacity_share"].le(0.0).any():
        raise FactorBuildInputError("monthly solar capacity shares must be positive")
    unknown = set(weights["location_id"]).difference(EXPECTED_LOCATION_IDS)
    if unknown:
        raise FactorBuildInputError(
            f"monthly solar weights contain unknown locations: {sorted(unknown)}"
        )
    shares = weights.groupby("period", observed=True)["capacity_share"].sum()
    invalid = shares.loc[~np.isclose(shares, 1.0, rtol=0.0, atol=1e-10)]
    if not invalid.empty:
        raise FactorBuildInputError(
            "monthly solar capacity shares do not sum to one for periods "
            f"{invalid.index.astype(str).tolist()}"
        )
    if "total_capacity_mw" in weights:
        totals = weights.groupby("period", observed=True).agg(
            calculated=("capacity_mw", "sum"),
            reported_min=("total_capacity_mw", "min"),
            reported_max=("total_capacity_mw", "max"),
        )
        consistent = (
            np.isclose(
                totals["calculated"],
                totals["reported_min"],
                rtol=0.0,
                atol=1e-8,
            )
            & np.isclose(
                totals["reported_min"],
                totals["reported_max"],
                rtol=0.0,
                atol=1e-8,
            )
        )
        if not consistent.all():
            raise FactorBuildInputError(
                "monthly solar reported capacity totals are inconsistent"
            )


def _wind_issue_year_bounds(
    weather_partitions: Sequence[str],
    filesystem: ReadOnlyPartitionFileSystem,
) -> tuple[int, int]:
    minimum: pd.Timestamp | None = None
    maximum: pd.Timestamp | None = None
    for path in weather_partitions:
        reference = _read_frame(
            path,
            filesystem,
            columns=["forecast_reference_time_utc"],
        )["forecast_reference_time_utc"]
        values = pd.to_datetime(reference, utc=True, errors="coerce").dropna()
        if values.empty:
            continue
        current_min = values.min()
        current_max = values.max()
        minimum = current_min if minimum is None else min(minimum, current_min)
        maximum = current_max if maximum is None else max(maximum, current_max)
    if minimum is None or maximum is None:
        raise FactorBuildInputError(
            "wind weather partitions contain no forecast reference times"
        )
    return int(minimum.year), int(maximum.year)


def _validate_wind_weather(
    weather_partitions: Sequence[str],
    filesystem: ReadOnlyPartitionFileSystem,
) -> tuple[int, int]:
    columns = [
        "forecast_reference_time_utc",
        "forecast_cycle_hour_utc",
        "location_id",
        "lead_days",
        "valid_time_utc",
        "wind_speed_80m_mps",
    ]
    structural_columns = columns[:-1]
    seen_references: set[pd.Timestamp] = set()
    expected_rows = len(EXPECTED_LOCATION_IDS) * 5 * 4
    for path in weather_partitions:
        frame = _read_frame(path, filesystem, columns=columns)
        for column in ("forecast_reference_time_utc", "valid_time_utc"):
            frame[column] = pd.to_datetime(
                frame[column], utc=True, errors="coerce"
            )
        for column in ("forecast_cycle_hour_utc", "lead_days"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        wind_speed = pd.to_numeric(
            frame["wind_speed_80m_mps"], errors="coerce"
        )
        if frame[structural_columns].isna().any().any():
            raise FactorBuildInputError(
                f"wind weather partition has null structural fields: {path}"
            )
        malformed_wind = frame["wind_speed_80m_mps"].notna() & wind_speed.isna()
        observed_wind = wind_speed.dropna()
        if malformed_wind.any() or not np.isfinite(
            observed_wind.to_numpy(dtype=float)
        ).all():
            raise FactorBuildInputError(
                f"wind weather partition has non-finite wind speeds: {path}"
            )
        if observed_wind.lt(0.0).any():
            raise FactorBuildInputError(
                f"wind weather partition has negative wind speeds: {path}"
            )
        selected_chain = frame["forecast_cycle_hour_utc"].isin((0, 18))
        if (selected_chain & wind_speed.isna()).any():
            raise FactorBuildInputError(
                "wind weather partition has missing speeds in the selected "
                f"00Z/revision-predecessor 18Z chain: {path}"
            )
        if frame.duplicated(
            ["forecast_reference_time_utc", "location_id", "valid_time_utc"]
        ).any():
            raise FactorBuildInputError(
                f"wind weather partition has duplicate point/time rows: {path}"
            )
        unknown = set(frame["location_id"]).difference(EXPECTED_LOCATION_IDS)
        if unknown:
            raise FactorBuildInputError(
                f"wind weather partition has unknown locations: {sorted(unknown)}"
            )
        if not frame["lead_days"].isin(range(1, 6)).all():
            raise FactorBuildInputError(
                f"wind weather partition has lead days outside 1-5: {path}"
            )
        expected_valid_dates = (
            frame["forecast_reference_time_utc"].dt.normalize()
            + pd.to_timedelta(frame["lead_days"], unit="D")
        )
        valid_times = frame["valid_time_utc"]
        if not (
            valid_times.dt.normalize().eq(expected_valid_dates)
            & valid_times.dt.hour.isin((0, 6, 12, 18))
            & valid_times.dt.minute.eq(0)
            & valid_times.dt.second.eq(0)
        ).all():
            raise FactorBuildInputError(
                f"wind valid times disagree with lead-day/6-hour grid: {path}"
            )
        reference_hours = frame["forecast_reference_time_utc"].dt.hour
        if not frame["forecast_cycle_hour_utc"].eq(reference_hours).all():
            raise FactorBuildInputError(
                f"wind cycle hour disagrees with reference time: {path}"
            )
        references = set(frame["forecast_reference_time_utc"].unique())
        overlap = seen_references.intersection(references)
        if overlap:
            raise FactorBuildInputError(
                "wind forecast initializations overlap across partitions; "
                f"first duplicate is {min(overlap)}"
            )
        seen_references.update(references)
        sizes = frame.groupby("forecast_reference_time_utc", observed=True).size()
        if sizes.gt(expected_rows).any():
            raise FactorBuildInputError(
                f"wind initialization exceeds {expected_rows} rows in {path}"
            )
        complete_refs = sizes.index[sizes.eq(expected_rows)]
        complete = frame.loc[
            frame["forecast_reference_time_utc"].isin(complete_refs)
        ]
        if complete.empty:
            continue
        per_location_lead = complete.groupby(
            ["forecast_reference_time_utc", "location_id", "lead_days"],
            observed=True,
        ).agg(
            rows=("valid_time_utc", "size"),
            unique_times=("valid_time_utc", "nunique"),
        )
        if not (
            per_location_lead["rows"].eq(4)
            & per_location_lead["unique_times"].eq(4)
        ).all():
            raise FactorBuildInputError(
                f"complete wind initialization has imbalanced samples in {path}"
            )
        per_reference = complete.groupby(
            "forecast_reference_time_utc", observed=True
        ).agg(
            locations=("location_id", "nunique"),
            leads=("lead_days", "nunique"),
        )
        if not (
            per_reference["locations"].eq(len(EXPECTED_LOCATION_IDS))
            & per_reference["leads"].eq(5)
        ).all():
            raise FactorBuildInputError(
                f"complete wind initialization has incomplete structure in {path}"
            )
    if not seen_references:
        raise FactorBuildInputError(
            "wind weather partitions contain no forecast initializations"
        )
    first = min(seen_references)
    last = max(seen_references)
    return first.year, last.year


def _wind_weights(
    inputs: FactorInputs,
    filesystem: ReadOnlyPartitionFileSystem,
) -> tuple[pd.DataFrame, pd.DataFrame | None]:
    snapshot = _read_frame(inputs.capacity_snapshot.uri, filesystem)
    if inputs.capacity_kind == "annual_location_weights":
        required = (
            "issue_year",
            "fleet_cutoff_year",
            "location_id",
            "capacity_mw",
            "hub_height_m",
        )
        _require_columns(snapshot, required, label="annual wind weights")
        result = snapshot.copy()
        for column in (
            "issue_year",
            "fleet_cutoff_year",
            "capacity_mw",
            "hub_height_m",
        ):
            result[column] = pd.to_numeric(result[column], errors="coerce")
        if result[list(required)].isna().any().any():
            raise FactorBuildInputError(
                "annual wind weights contain null or non-numeric required values"
            )
        if result.duplicated(["issue_year", "location_id"]).any():
            raise FactorBuildInputError(
                "annual wind weights contain duplicate issue-year/location rows"
            )
        _validate_annual_wind_weights(result)
        return result, None

    required = ("case_id", "p_year", "t_cap", "t_hh", "xlong", "ylat")
    _require_columns(snapshot, required, label="frozen USWTDB turbine snapshot")
    turbines = snapshot.copy()
    for column in ("p_year", "t_cap", "t_hh", "xlong", "ylat"):
        turbines[column] = pd.to_numeric(turbines[column], errors="coerce")
    first_year, last_year = _wind_issue_year_bounds(
        inputs.weather_uris,
        filesystem,
    )
    weights, diagnostics = build_annual_location_weights(
        turbines,
        first_year=first_year,
        last_year=last_year,
    )
    _validate_annual_wind_weights(weights)
    return weights, diagnostics


def build_wind_horizon_signals(
    *,
    inputs: FactorInputs,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Rebuild causal D1, D1--3 and D1--5 signals from pinned raw points."""

    filesystem = ReadOnlyPartitionFileSystem(inputs.artifacts)
    first_year, last_year = _validate_wind_weather(
        inputs.weather_uris,
        filesystem,
    )
    weights, _ = _wind_weights(inputs, filesystem)
    missing_years = sorted(
        set(range(first_year, last_year + 1)).difference(weights["issue_year"])
    )
    if missing_years:
        raise FactorBuildInputError(
            f"annual wind weights do not cover issue years {missing_years}"
        )

    columns = [
        "forecast_reference_time_utc",
        "forecast_cycle_hour_utc",
        "location_id",
        "lead_days",
        "wind_speed_80m_mps",
    ]
    frames: list[pd.DataFrame] = []
    input_00z_rows = 0
    for path in sorted(inputs.weather_uris):
        month = _read_frame(path, filesystem, columns=columns)
        month = month.loc[
            month["forecast_cycle_hour_utc"].eq(PRIMARY_CYCLE_UTC)
        ].copy()
        input_00z_rows += len(month)
        month["issue_year"] = pd.to_datetime(
            month["forecast_reference_time_utc"], utc=True
        ).dt.year
        month = month.merge(
            weights[
                ["issue_year", "location_id", "capacity_mw", "hub_height_m"]
            ],
            on=["issue_year", "location_id"],
            how="left",
            validate="many_to_one",
        )
        if month[["capacity_mw", "hub_height_m"]].isna().any().any():
            raise FactorBuildInputError(
                f"wind partition has no annual capacity weight: {path}"
            )
        month["wind_speed_hub_mps"] = month["wind_speed_80m_mps"] * (
            month["hub_height_m"] / 80.0
        ) ** SHEAR_EXPONENT
        month["total_shortfall_cf"] = nonlinear_power_components(
            month["wind_speed_hub_mps"]
        )["total_shortfall_cf"]
        month["weighted_shortfall"] = (
            month["total_shortfall_cf"] * month["capacity_mw"]
        )

        monthly: pd.DataFrame | None = None
        for name, lead_days in WIND_HORIZONS.items():
            subset = month.loc[month["lead_days"].isin(lead_days)]
            grouped = subset.groupby(
                "forecast_reference_time_utc",
                as_index=False,
            ).agg(
                sample_count=("wind_speed_80m_mps", "count"),
                weight_sum=("capacity_mw", "sum"),
                weighted_shortfall_sum=("weighted_shortfall", "sum"),
            )
            expected = (
                len(LOCATIONS)
                * EXPECTED_VALID_HOURS_PER_LEAD
                * len(lead_days)
            )
            grouped = grouped.loc[grouped["sample_count"].eq(expected)].copy()
            grouped[f"shortfall_cf__{name}"] = (
                grouped["weighted_shortfall_sum"] / grouped["weight_sum"]
            )
            grouped = grouped[
                ["forecast_reference_time_utc", f"shortfall_cf__{name}"]
            ]
            monthly = (
                grouped
                if monthly is None
                else monthly.merge(
                    grouped,
                    on="forecast_reference_time_utc",
                    how="inner",
                    validate="one_to_one",
                )
            )
        if monthly is None:
            raise FactorBuildInputError(
                f"wind partition produced no horizon features: {path}"
            )
        frames.append(monthly)

    result = (
        pd.concat(frames, ignore_index=True)
        .sort_values("forecast_reference_time_utc")
        .reset_index(drop=True)
    )
    result["date"] = pd.to_datetime(
        result["forecast_reference_time_utc"], utc=True
    ).dt.tz_localize(None).dt.normalize()
    if not result["date"].is_unique:
        raise FactorBuildInputError(
            "wind horizon build produced duplicate 00Z issue dates"
        )
    for name in WIND_HORIZONS:
        result[f"wind_z__{name}"] = causal_zscore(
            result[f"shortfall_cf__{name}"],
            window=WIND_Z_WINDOW,
            min_periods=WIND_Z_MIN_PERIODS,
        )
        result[f"wind_signal__{name}"] = np.tanh(
            result[f"wind_z__{name}"].clip(-WIND_Z_CAP, WIND_Z_CAP) / 2.0
        )

    issue = pd.to_datetime(result["forecast_reference_time_utc"], utc=True)
    if not issue.dt.hour.eq(PRIMARY_CYCLE_UTC).all():
        raise FactorBuildInputError("wind horizon output contains a non-00Z issue")
    return result, {
        "weather_partition_count": len(inputs.weather_partitions),
        "input_00z_rows": input_00z_rows,
        "complete_initializations": len(result),
        "forecast_horizons": WIND_HORIZONS,
        "expected_d1_3_samples_per_initialization": (
            len(LOCATIONS) * EXPECTED_VALID_HOURS_PER_LEAD * 3
        ),
        "rolling_window": WIND_Z_WINDOW,
        "rolling_min_periods": WIND_Z_MIN_PERIODS,
        "rolling_current_observation_excluded": True,
    }


def rebuild_wind_horizons(
    *,
    inputs: FactorInputs,
    output_dir: Path,
) -> dict[str, Any]:
    """Write the generation-pinned D1/D1--3/D1--5 lineage artifact."""

    output = output_dir / WIND_HORIZON_OUTPUT_NAME
    _ensure_targets_absent([output])
    signals, quality = build_wind_horizon_signals(inputs=inputs)
    metadata = _write_parquet_outputs_no_overwrite(
        [(signals, output, "zstd")],
        approved_outputs=_approved_outputs_for(
            inputs,
            WIND_HORIZON_OUTPUT_NAME,
        ),
    )
    return {
        "component": "wind_horizons",
        "status": "built",
        "output": str(output),
        "rows": len(signals),
        "first_reference_time": signals["forecast_reference_time_utc"].min(),
        "last_reference_time": signals["forecast_reference_time_utc"].max(),
        "capacity_kind": inputs.capacity_kind,
        "output_integrity": metadata[output.name],
        "quality": quality,
    }


def rebuild_selected_wind(
    *,
    inputs: FactorInputs,
    output_dir: Path,
) -> dict[str, Any]:
    """Build only the selected 00Z wind-factor parquet."""

    output = output_dir / WIND_OUTPUT_NAME
    _ensure_targets_absent([output])
    filesystem = ReadOnlyPartitionFileSystem(inputs.artifacts)
    first_year, last_year = _validate_wind_weather(
        inputs.weather_uris,
        filesystem,
    )
    weights, diagnostics = _wind_weights(inputs, filesystem)
    missing_years = sorted(
        set(range(first_year, last_year + 1)).difference(weights["issue_year"])
    )
    if missing_years:
        raise FactorBuildInputError(
            f"annual wind weights do not cover issue years {missing_years}"
        )
    all_cycles, quality = build_capacity_features(
        filesystem,
        sorted(inputs.weather_uris),
        weights,
    )
    selected = (
        all_cycles.loc[
            all_cycles["forecast_cycle_hour_utc"].eq(PRIMARY_CYCLE_UTC)
        ]
        .sort_values("forecast_reference_time_utc")
        .reset_index(drop=True)
    )
    if selected.empty:
        raise FactorBuildInputError(
            "wind inputs produced no complete 00Z initializations"
        )
    output_metadata = _write_parquet_outputs_no_overwrite(
        [(selected, output, "zstd")],
        approved_outputs=_approved_outputs_for(inputs, WIND_OUTPUT_NAME),
    )
    return {
        "component": "wind",
        "status": "built",
        "output": str(output),
        "rows": len(selected),
        "first_reference_time": selected["forecast_reference_time_utc"].min(),
        "last_reference_time": selected["forecast_reference_time_utc"].max(),
        "capacity_kind": inputs.capacity_kind,
        "weather_partition_count": len(inputs.weather_partitions),
        "output_integrity": output_metadata[output.name],
        "quality": quality,
        "fleet_diagnostic_rows": (
            None if diagnostics is None else len(diagnostics)
        ),
    }


def _solar_daily(
    inputs: FactorInputs,
    filesystem: ReadOnlyPartitionFileSystem,
) -> pd.DataFrame:
    frames = [
        _read_frame(
            path,
            filesystem,
            columns=list(DAILY_COLUMNS),
        )
        for path in sorted(inputs.weather_uris)
    ]
    daily = pd.concat(frames, ignore_index=True)
    if daily.empty:
        raise FactorBuildInputError("solar weather partitions contain no rows")
    daily["forecast_reference_time_utc"] = pd.to_datetime(
        daily["forecast_reference_time_utc"],
        utc=True,
        errors="coerce",
    )
    daily["target_date"] = pd.to_datetime(
        daily["target_date"],
        errors="coerce",
    )
    if daily[["forecast_reference_time_utc", "target_date"]].isna().any().any():
        raise FactorBuildInputError(
            "solar weather partitions contain invalid issue or target dates"
        )
    structural_columns = [
        "forecast_reference_time_utc",
        "target_date",
        "lead_days",
        "location_id",
        "nominal_issue_date",
    ]
    if daily[structural_columns].isna().any().any():
        raise FactorBuildInputError(
            "solar weather partitions contain null structural fields"
        )

    lead_days = pd.to_numeric(daily["lead_days"], errors="coerce")
    sample_counts = pd.to_numeric(daily["solar_sample_count"], errors="coerce")
    value_columns = [
        "downward_shortwave_mean_wm2",
        "downward_shortwave_energy_kwh_m2",
        "total_cloud_cover_mean_pct",
        "temperature_2m_mean_c",
    ]
    numeric_values = daily[value_columns].apply(pd.to_numeric, errors="coerce")
    if lead_days.isna().any() or not lead_days.isin(range(1, 6)).all():
        raise FactorBuildInputError(
            "solar weather partitions contain lead days outside 1-5"
        )
    if sample_counts.isna().any() or sample_counts.lt(0).any():
        raise FactorBuildInputError(
            "solar weather partitions contain invalid sample counts"
        )
    daily["lead_days"] = lead_days
    daily["solar_sample_count"] = sample_counts
    for column in value_columns:
        daily[column] = numeric_values[column]

    for coordinate in ("requested_latitude", "requested_longitude"):
        values = pd.to_numeric(daily[coordinate], errors="coerce")
        if not np.isfinite(values.to_numpy(dtype=float)).all():
            raise FactorBuildInputError(
                f"solar weather partitions contain invalid {coordinate}"
            )
        daily[coordinate] = values
    if daily.duplicated(
        [
            "forecast_reference_time_utc",
            "target_date",
            "lead_days",
            "location_id",
        ]
    ).any():
        raise FactorBuildInputError(
            "solar weather partitions contain duplicate "
            "initialization/lead/location rows"
        )
    unknown = set(daily["location_id"]).difference(EXPECTED_LOCATION_IDS)
    if unknown:
        raise FactorBuildInputError(
            f"solar weather partitions contain unknown locations: {sorted(unknown)}"
        )
    groups = daily.groupby(
        ["forecast_reference_time_utc", "target_date", "lead_days"],
        observed=True,
    ).agg(rows=("location_id", "size"), locations=("location_id", "nunique"))
    if not (
        groups["rows"].eq(len(EXPECTED_LOCATION_IDS))
        & groups["locations"].eq(len(EXPECTED_LOCATION_IDS))
    ).all():
        raise FactorBuildInputError(
            "each solar initialization/lead must contain all configured locations"
        )

    reference_dates = (
        daily["forecast_reference_time_utc"].dt.tz_localize(None).dt.normalize()
    )
    expected_targets = reference_dates + pd.to_timedelta(lead_days, unit="D")
    if not daily["target_date"].dt.normalize().eq(expected_targets).all():
        raise FactorBuildInputError(
            "solar target dates do not agree with initialization and lead day"
        )
    nominal_dates = pd.to_datetime(
        daily["nominal_issue_date"], errors="coerce"
    ).dt.normalize()
    if nominal_dates.isna().any() or not nominal_dates.eq(reference_dates).all():
        raise FactorBuildInputError(
            "solar nominal issue dates do not agree with initialization dates"
        )

    if not pd.api.types.is_bool_dtype(daily["solar_sample_complete"]):
        raise FactorBuildInputError(
            "solar_sample_complete must be a non-null boolean field"
        )
    complete = daily["solar_sample_complete"]
    if complete.isna().any():
        raise FactorBuildInputError("solar_sample_complete contains null values")
    if (complete & ~sample_counts.eq(4)).any():
        raise FactorBuildInputError(
            "complete solar rows must contain exactly four samples"
        )
    if not np.isfinite(numeric_values.loc[complete].to_numpy(dtype=float)).all():
        raise FactorBuildInputError(
            "complete solar rows contain non-finite weather values"
        )
    return daily


def _solar_weights(
    inputs: FactorInputs,
    filesystem: ReadOnlyPartitionFileSystem,
) -> tuple[pd.DataFrame, dict[str, Any] | None]:
    snapshot = _read_frame(inputs.capacity_snapshot.uri, filesystem)
    if inputs.capacity_kind == "monthly_location_weights":
        required = ("period", "location_id", "capacity_mw", "capacity_share")
        _require_columns(snapshot, required, label="monthly solar weights")
        result = snapshot.copy()
        result["period"] = result["period"].astype(str)
        numeric_columns = ["capacity_mw", "capacity_share"]
        if "total_capacity_mw" in result:
            numeric_columns.append("total_capacity_mw")
        for column in numeric_columns:
            result[column] = pd.to_numeric(result[column], errors="coerce")
        if result[list(required) + numeric_columns[2:]].isna().any().any():
            raise FactorBuildInputError(
                "monthly solar weights contain null or non-numeric values"
            )
        if result.duplicated(["period", "location_id"]).any():
            raise FactorBuildInputError(
                "monthly solar weights contain duplicate period/location rows"
            )
        _validate_monthly_solar_weights(result)
        return result, None

    required = (
        "period",
        "stateid",
        "plantid",
        "generatorid",
        "nameplate-capacity-mw",
        "latitude",
        "longitude",
    )
    _require_columns(snapshot, required, label="frozen EIA generator snapshot")
    generators = snapshot.copy()
    for column in ("nameplate-capacity-mw", "latitude", "longitude"):
        generators[column] = pd.to_numeric(generators[column], errors="coerce")
    weights, diagnostics = build_monthly_location_weights(generators)
    _validate_monthly_solar_weights(weights)
    return weights, diagnostics


def rebuild_solar(
    *,
    inputs: FactorInputs,
    output_dir: Path,
) -> dict[str, Any]:
    """Build only the capacity-weighted solar leads and causal signals."""

    signal_output = output_dir / SOLAR_SIGNAL_OUTPUT_NAME
    lead_output = output_dir / SOLAR_LEAD_OUTPUT_NAME
    _ensure_targets_absent([signal_output, lead_output])
    filesystem = ReadOnlyPartitionFileSystem(inputs.artifacts)
    daily = _solar_daily(inputs, filesystem)
    weights, weight_diagnostics = _solar_weights(inputs, filesystem)
    required_capacity_periods = set(
        (
            daily["forecast_reference_time_utc"]
            .dt.tz_localize(None)
            .dt.to_period("M")
            - CAPACITY_LAG_MONTHS
        ).astype(str)
    )
    missing_periods = sorted(required_capacity_periods.difference(weights["period"]))
    if missing_periods:
        raise FactorBuildInputError(
            f"monthly solar weights do not cover capacity periods {missing_periods}"
        )
    leads, aggregation_diagnostics = build_capacity_weighted_location_leads(
        daily,
        weights,
        capacity_lag_months=CAPACITY_LAG_MONTHS,
        minimum_capacity_coverage=MINIMUM_SOLAR_CAPACITY_COVERAGE,
    )
    signals = build_solar_signals(leads)
    if leads.empty or signals.empty:
        raise FactorBuildInputError(
            "solar inputs produced no capacity-weighted leads or signals"
        )
    # Match the original solar builder's pandas/pyarrow default compression.
    output_metadata = _write_parquet_outputs_no_overwrite(
        [
            (leads, lead_output, "snappy"),
            (signals, signal_output, "snappy"),
        ],
        approved_outputs=inputs.approved_outputs,
    )
    return {
        "component": "solar",
        "status": "built",
        "lead_output": str(lead_output),
        "signal_output": str(signal_output),
        "lead_rows": len(leads),
        "signal_rows": len(signals),
        "first_reference_time": signals["forecast_reference_time_utc"].min(),
        "last_reference_time": signals["forecast_reference_time_utc"].max(),
        "capacity_kind": inputs.capacity_kind,
        "weather_partition_count": len(inputs.weather_partitions),
        "output_integrity": output_metadata,
        "capacity_weight_diagnostics": weight_diagnostics,
        "weather_aggregation_diagnostics": aggregation_diagnostics,
    }


def _add_input_arguments(
    parser: argparse.ArgumentParser,
    *,
    component: str,
) -> None:
    kinds = WIND_CAPACITY_KINDS if component == "wind" else SOLAR_CAPACITY_KINDS
    parser.add_argument(
        "--input-manifest",
        type=Path,
        help=(
            "JSON manifest containing an explicit "
            f"{component} input section"
        ),
    )
    parser.add_argument(
        "--weather-partition",
        action="append",
        help=(
            "Explicit local path; repeat for every monthly partition. GCS "
            "inputs require --input-manifest."
        ),
    )
    parser.add_argument(
        "--capacity-snapshot",
        help=(
            "Frozen local path. GCS inputs require --input-manifest; live "
            "capacity API access is deliberately disabled."
        ),
    )
    parser.add_argument(
        "--capacity-kind",
        choices=sorted(kinds),
        help="Schema of the frozen capacity snapshot",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help=(
            "Local output directory. Existing target files are never "
            "overwritten."
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "A frozen USWTDB turbine/annual-weight snapshot is required for "
            "wind, and a frozen EIA generator/monthly-weight snapshot is "
            "required for solar. No live-capacity fallback exists."
        ),
    )
    subparsers = parser.add_subparsers(dest="component", required=True)
    wind = subparsers.add_parser(
        "wind",
        help="rebuild the selected 00Z capacity-weighted wind parquet",
    )
    _add_input_arguments(wind, component="wind")
    wind_horizons = subparsers.add_parser(
        "wind-horizons",
        help="rebuild causal 00Z D1, D1-3 and D1-5 wind signals",
    )
    _add_input_arguments(wind_horizons, component="wind")
    solar = subparsers.add_parser(
        "solar",
        help="rebuild capacity-weighted solar leads and causal signals",
    )
    _add_input_arguments(solar, component="solar")
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        input_component = (
            "wind" if args.component == "wind-horizons" else args.component
        )
        inputs = _cli_inputs(args, component=input_component)
        output_dir = _local_output_dir(args.output_dir)
        if args.component == "wind":
            result = rebuild_selected_wind(
                inputs=inputs,
                output_dir=output_dir,
            )
        elif args.component == "wind-horizons":
            result = rebuild_wind_horizons(
                inputs=inputs,
                output_dir=output_dir,
            )
        else:
            result = rebuild_solar(
                inputs=inputs,
                output_dir=output_dir,
            )
    except (FactorBuildInputError, FileExistsError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, default=str, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
