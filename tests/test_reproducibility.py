from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pandas as pd
import pytest

from naturalgas.reproducibility import (
    Artifact,
    LocalArtifactFileSystem,
    create_staging_directory,
    fetch_artifact,
    local_artifact_path,
    normalize_gcs_key,
    publish_staging_directory,
    validate_artifact,
)


def test_validate_artifact_checks_hash_and_parquet_contract(tmp_path: Path) -> None:
    path = tmp_path / "input.parquet"
    pd.DataFrame({"date": ["2026-01-01"], "value": [1.0]}).to_parquet(
        path,
        index=False,
    )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    artifact = Artifact(
        artifact_id="example",
        uri="gs://example-bucket/input.parquet",
        generation=123,
        sha256=digest,
        local_path=Path("input.parquet"),
        rows=1,
        columns=2,
        required_columns=("date", "value"),
    )
    validate_artifact(path, artifact)

    wrong = Artifact(
        artifact_id="example",
        uri=artifact.uri,
        generation=artifact.generation,
        sha256="0" * 64,
        local_path=artifact.local_path,
    )
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        validate_artifact(path, wrong)


def test_local_artifact_filesystem_is_mapped_and_read_only(tmp_path: Path) -> None:
    path = tmp_path / "input.bin"
    path.write_bytes(b"approved generation")
    filesystem = LocalArtifactFileSystem({
        "gs://example-bucket/input.bin#123": path,
    })
    with filesystem.open("example-bucket/input.bin", "rb") as handle:
        assert handle.read() == b"approved generation"
    with pytest.raises(ValueError, match="read-only"):
        filesystem.open("example-bucket/input.bin", "wb")
    assert normalize_gcs_key("gs://example-bucket/input.bin#123") == (
        "example-bucket/input.bin"
    )


def test_manifest_local_path_cannot_escape_root(tmp_path: Path) -> None:
    common = {
        "artifact_id": "escape",
        "uri": "gs://example-bucket/input.parquet",
        "generation": 1,
        "sha256": "0" * 64,
    }
    with pytest.raises(ValueError, match="must be relative"):
        local_artifact_path(
            Artifact(local_path=Path("/tmp/outside"), **common),
            root=tmp_path,
        )
    with pytest.raises(ValueError, match="escapes root"):
        local_artifact_path(
            Artifact(local_path=Path("../outside"), **common),
            root=tmp_path,
        )


def test_fetch_passes_generation_and_validates_before_publish(
    tmp_path: Path,
) -> None:
    payload_buffer = io.BytesIO()
    pd.DataFrame({"value": [1.0]}).to_parquet(payload_buffer, index=False)
    payload = payload_buffer.getvalue()

    class FakeFileSystem:
        calls: list[tuple[str, dict[str, object]]]

        def __init__(self) -> None:
            self.calls = []

        def cat_file(self, key: str, **kwargs: object) -> bytes:
            self.calls.append((key, kwargs))
            return payload

    filesystem = FakeFileSystem()
    artifact = Artifact(
        artifact_id="pinned",
        uri="gs://example-bucket/input.parquet",
        generation=987654321,
        sha256=hashlib.sha256(payload).hexdigest(),
        local_path=Path("inputs/input.parquet"),
        size_bytes=len(payload),
        rows=1,
        columns=1,
    )
    destination = fetch_artifact(filesystem, artifact, root=tmp_path)
    assert filesystem.calls == [(
        "example-bucket/input.parquet",
        {"generation": "987654321", "concurrency": 1},
    )]
    assert destination.read_bytes() == payload


def test_verified_directory_publish_replaces_output_only_at_end(
    tmp_path: Path,
) -> None:
    target = tmp_path / "result"
    target.mkdir()
    (target / "reproduction_receipt.json").write_text("old")
    with pytest.raises(FileExistsError):
        create_staging_directory(target, overwrite=False)

    staging, resolved = create_staging_directory(target, overwrite=True)
    assert (target / "reproduction_receipt.json").read_text() == "old"
    (staging / "reproduction_receipt.json").write_text("new")
    publish_staging_directory(staging, resolved, overwrite=True)
    assert (target / "reproduction_receipt.json").read_text() == "new"
