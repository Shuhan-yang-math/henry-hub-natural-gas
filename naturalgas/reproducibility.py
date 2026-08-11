"""Versioned input download and validation utilities.

The research pipeline keeps large parquet inputs in private Google Cloud
Storage.  A plain ``gs://`` path is mutable, so it is not sufficient evidence
for a reproducible model build.  This module consumes a checked-in artifact
manifest, opens the recorded GCS object generation, validates its SHA-256 and
basic parquet contract, and exposes the downloaded files through the same
``open`` interface used by the existing factor code.

No remote object is ever written by this module.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, BinaryIO, Iterable, Mapping

import gcsfs
import pyarrow.parquet as pq


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = (
    PROJECT_ROOT / "manifests/input_artifacts_2026-07-13.json"
)


def normalize_gcs_key(uri: str) -> str:
    """Return the bucket/object form accepted by :mod:`gcsfs`."""

    return uri.removeprefix("gs://").split("#", 1)[0]


@dataclass(frozen=True)
class Artifact:
    """One immutable input declared in the checked-in manifest."""

    artifact_id: str
    uri: str
    generation: int
    sha256: str
    local_path: Path
    size_bytes: int | None = None
    rows: int | None = None
    columns: int | None = None
    required_columns: tuple[str, ...] = ()
    schema_fingerprint_sha256: str | None = None

    @property
    def gcs_key(self) -> str:
        return normalize_gcs_key(self.uri)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Artifact":
        artifact_id = value.get("id", value.get("artifact_id"))
        if not artifact_id:
            raise ValueError("Every manifest artifact requires 'id'")
        required = value.get("required_columns", ())
        schema = value.get("schema")
        if not required and isinstance(schema, Mapping):
            required = schema.get("required_columns", ())
        return cls(
            artifact_id=str(artifact_id),
            uri=str(value["uri"]),
            generation=int(value["generation"]),
            sha256=str(value["sha256"]).lower(),
            local_path=Path(value["local_path"]),
            size_bytes=_optional_int(value.get("size_bytes")),
            rows=_optional_int(value.get("rows")),
            columns=_optional_int(value.get("columns")),
            required_columns=tuple(str(column) for column in required),
            schema_fingerprint_sha256=value.get("schema_fingerprint_sha256"),
        )


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def load_manifest(path: Path = DEFAULT_MANIFEST) -> tuple[Artifact, ...]:
    """Load and validate the artifact declarations in *path*."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    entries = payload.get("artifacts")
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"Manifest has no artifacts list: {path}")
    artifacts = tuple(Artifact.from_mapping(entry) for entry in entries)
    identifiers = [artifact.artifact_id for artifact in artifacts]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError(f"Manifest contains duplicate artifact ids: {path}")
    return artifacts


def sha256_file(path: Path, *, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def validate_artifact(path: Path, artifact: Artifact) -> None:
    """Raise if a local file does not match its immutable declaration."""

    if not path.is_file():
        raise FileNotFoundError(path)
    if artifact.size_bytes is not None and path.stat().st_size != artifact.size_bytes:
        raise ValueError(
            f"Size mismatch for {artifact.artifact_id}: expected "
            f"{artifact.size_bytes}, got {path.stat().st_size}"
        )
    actual_hash = sha256_file(path)
    if actual_hash != artifact.sha256:
        raise ValueError(
            f"SHA-256 mismatch for {artifact.artifact_id}: "
            f"expected {artifact.sha256}, got {actual_hash}"
        )
    if (
        path.suffix.lower() != ".parquet"
        and artifact.local_path.suffix.lower() != ".parquet"
    ):
        return
    parquet = pq.ParquetFile(path)
    metadata = parquet.metadata
    if artifact.rows is not None and metadata.num_rows != artifact.rows:
        raise ValueError(
            f"Row-count mismatch for {artifact.artifact_id}: "
            f"expected {artifact.rows}, got {metadata.num_rows}"
        )
    if artifact.columns is not None and metadata.num_columns != artifact.columns:
        raise ValueError(
            f"Column-count mismatch for {artifact.artifact_id}: "
            f"expected {artifact.columns}, got {metadata.num_columns}"
        )
    present = set(parquet.schema_arrow.names)
    missing = sorted(set(artifact.required_columns) - present)
    if missing:
        raise ValueError(
            f"Missing columns for {artifact.artifact_id}: {missing}"
        )
    if artifact.schema_fingerprint_sha256:
        canonical_fields = [
            {
                "name": field.name,
                "nullable": field.nullable,
                "type": str(field.type),
            }
            for field in parquet.schema_arrow
        ]
        canonical = json.dumps(
            canonical_fields,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        fingerprint = hashlib.sha256(canonical).hexdigest()
        if fingerprint != artifact.schema_fingerprint_sha256:
            raise ValueError(
                f"Schema fingerprint mismatch for {artifact.artifact_id}: "
                f"expected {artifact.schema_fingerprint_sha256}, got "
                f"{fingerprint}"
            )


def local_artifact_path(artifact: Artifact, *, root: Path) -> Path:
    path = artifact.local_path
    if path.is_absolute():
        raise ValueError(
            f"Manifest local_path must be relative to root: {path}"
        )
    resolved_root = root.expanduser().resolve()
    resolved = (resolved_root / path).resolve()
    if not resolved.is_relative_to(resolved_root):
        raise ValueError(
            f"Manifest local_path escapes root: {path}"
        )
    return resolved


def _validate_output_target(path: Path) -> Path:
    """Reject broad or non-directory targets before any overwrite workflow."""

    resolved = path.expanduser().resolve()
    protected = {
        Path("/").resolve(),
        Path.home().resolve(),
        PROJECT_ROOT.resolve(),
        PROJECT_ROOT.parent.resolve(),
    }
    if resolved in protected:
        raise ValueError(f"Refusing broad output directory: {resolved}")
    if resolved.exists() and not resolved.is_dir():
        raise NotADirectoryError(resolved)
    return resolved


def create_staging_directory(
    target: Path,
    *,
    overwrite: bool,
) -> tuple[Path, Path]:
    """Create a sibling staging directory after checking target policy."""

    resolved = _validate_output_target(target)
    if resolved.exists() and not overwrite:
        raise FileExistsError(
            f"Output directory already exists: {resolved}; use --overwrite"
        )
    resolved.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(
        prefix=f".{resolved.name}.staging.",
        dir=resolved.parent,
    ))
    return staging, resolved


def publish_staging_directory(
    staging: Path,
    target: Path,
    *,
    overwrite: bool,
) -> None:
    """Atomically publish a verified directory, restoring old output on error."""

    staging = staging.resolve()
    target = _validate_output_target(target)
    if (
        not staging.is_dir()
        or staging.parent != target.parent
        or ".staging." not in staging.name
    ):
        raise ValueError("Staging directory must be a sibling of target")
    backup: Path | None = None
    if target.exists():
        if not overwrite:
            raise FileExistsError(
                f"Output directory already exists: {target}; use --overwrite"
            )
        backup = Path(tempfile.mkdtemp(
            prefix=f".{target.name}.previous.",
            dir=target.parent,
        ))
        backup.rmdir()
        target.replace(backup)
    try:
        staging.replace(target)
    except Exception:
        if backup is not None and backup.exists() and not target.exists():
            backup.replace(target)
        raise
    if backup is not None:
        shutil.rmtree(backup)


def discard_staging_directory(staging: Path) -> None:
    """Remove only a staging directory created by this module."""

    resolved = staging.resolve()
    if ".staging." not in resolved.name:
        raise ValueError(f"Refusing to discard non-staging path: {resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)


def fetch_artifact(
    filesystem: gcsfs.GCSFileSystem,
    artifact: Artifact,
    *,
    root: Path = PROJECT_ROOT,
    overwrite: bool = False,
) -> Path:
    """Download one exact GCS generation atomically and validate it."""

    destination = local_artifact_path(artifact, root=root)
    if destination.exists() and not overwrite:
        validate_artifact(destination, artifact)
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".part",
        dir=destination.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        # Use cat_file directly. With gcsfs 2026.7.0, passing generation to
        # GCSFileSystem.open does not reliably forward it to the later media
        # request made by GCSFile._fetch_range.
        payload = filesystem.cat_file(
            artifact.gcs_key,
            generation=str(artifact.generation),
            concurrency=1,
        )
        with temporary.open("wb") as target:
            target.write(payload)
        validate_artifact(temporary, artifact)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def fetch_manifest(
    manifest_path: Path = DEFAULT_MANIFEST,
    *,
    root: Path = PROJECT_ROOT,
    artifact_ids: Iterable[str] | None = None,
    overwrite: bool = False,
) -> dict[str, Path]:
    """Fetch selected manifest inputs and return id-to-local-path mapping."""

    selected = None if artifact_ids is None else set(artifact_ids)
    artifacts = load_manifest(manifest_path)
    if selected is not None:
        available = {artifact.artifact_id for artifact in artifacts}
        unknown = selected - available
        if unknown:
            raise KeyError(f"Unknown artifact ids: {sorted(unknown)}")
        artifacts = tuple(
            artifact for artifact in artifacts
            if artifact.artifact_id in selected
        )
    filesystem = gcsfs.GCSFileSystem()
    return {
        artifact.artifact_id: fetch_artifact(
            filesystem,
            artifact,
            root=root,
            overwrite=overwrite,
        )
        for artifact in artifacts
    }


class LocalArtifactFileSystem:
    """Read-only key-to-local-path adapter for legacy GCS-loading functions."""

    def __init__(self, paths_by_gcs_key: Mapping[str, Path]) -> None:
        self._paths = {
            normalize_gcs_key(key): Path(path)
            for key, path in paths_by_gcs_key.items()
        }

    @classmethod
    def from_manifest(
        cls,
        manifest_path: Path,
        *,
        root: Path = PROJECT_ROOT,
    ) -> "LocalArtifactFileSystem":
        mapping: dict[str, Path] = {}
        for artifact in load_manifest(manifest_path):
            path = local_artifact_path(artifact, root=root)
            validate_artifact(path, artifact)
            mapping[artifact.gcs_key] = path
        return cls(mapping)

    def open(self, key: str, mode: str = "rb", **_: Any) -> BinaryIO:
        if mode not in {"r", "rb"}:
            raise ValueError("LocalArtifactFileSystem is read-only")
        normalized = normalize_gcs_key(key)
        try:
            path = self._paths[normalized]
        except KeyError as exc:
            raise FileNotFoundError(
                f"No local artifact declared for {normalized}"
            ) from exc
        return path.open(mode)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--artifact", action="append", dest="artifacts")
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    paths = fetch_manifest(
        args.manifest,
        root=args.root,
        artifact_ids=args.artifacts,
        overwrite=args.overwrite,
    )
    print(json.dumps({key: str(value) for key, value in paths.items()}, indent=2))


if __name__ == "__main__":
    main()
