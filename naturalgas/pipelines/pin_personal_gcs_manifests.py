"""Retarget checked-in manifests to the personal, byte-identical GCS copy.

Cloud Storage assigns a new generation when an object is copied across
buckets.  This one-time migration helper looks up those destination
generations, checks available size/CRC/MD5 metadata, and updates only the
machine-readable manifests in this personal repository.

The command never uploads, overwrites, or deletes a cloud object.  Without
``--write`` it performs a dry run.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any

from google.cloud import storage

from naturalgas.reproducibility import PROJECT_ROOT
from naturalgas.storage_config import (
    PERSONAL_GCS_BUCKET,
    PERSONAL_GCS_PREFIX,
    PERSONAL_GCS_URI_ROOT,
)


SOURCE_URI_ROOT = "gs://bcli-natgas-data-497807"
DEFAULT_MANIFEST_DIR = PROJECT_ROOT / "manifests"


def _source_object(uri: str) -> str | None:
    if not uri.startswith(f"{SOURCE_URI_ROOT}/"):
        return None
    without_generation = uri.split("#", 1)[0]
    relative = without_generation.removeprefix(f"{SOURCE_URI_ROOT}/")
    if any(character in relative for character in "*?[]"):
        return None
    return relative


def _destination_uri(relative: str, generation: int | None = None) -> str:
    uri = f"{PERSONAL_GCS_URI_ROOT}/{relative}"
    return uri if generation is None else f"{uri}#{generation}"


def load_destination_blobs() -> dict[str, storage.Blob]:
    """List the personal prefix once and index blobs by source-relative key."""

    client = storage.Client()
    prefix = PERSONAL_GCS_PREFIX.rstrip("/") + "/"
    result: dict[str, storage.Blob] = {}
    for blob in client.list_blobs(PERSONAL_GCS_BUCKET, prefix=prefix):
        relative = blob.name.removeprefix(prefix)
        result[relative] = blob
    return result


def _verify_metadata(entry: dict[str, Any], blob: storage.Blob) -> None:
    expected_size = entry.get("size_bytes")
    if expected_size is not None and int(expected_size) != int(blob.size):
        raise ValueError(
            f"size mismatch for {blob.name}: {blob.size} != {expected_size}"
        )
    expected_crc = entry.get("crc32c")
    if expected_crc is not None and expected_crc != blob.crc32c:
        raise ValueError(
            f"CRC32C mismatch for {blob.name}: {blob.crc32c} != {expected_crc}"
        )
    expected_md5 = entry.get("md5_base64")
    if expected_md5 is not None and expected_md5 != blob.md5_hash:
        raise ValueError(
            f"MD5 mismatch for {blob.name}: {blob.md5_hash} != {expected_md5}"
        )


def _retarget_value(value: Any, blobs: dict[str, storage.Blob]) -> Any:
    if isinstance(value, list):
        return [_retarget_value(item, blobs) for item in value]
    if not isinstance(value, dict):
        if not isinstance(value, str) or not value.startswith(SOURCE_URI_ROOT):
            return value
        relative = value.split("#", 1)[0].removeprefix(
            f"{SOURCE_URI_ROOT}/"
        )
        if any(character in relative for character in "*?[]"):
            return value.replace(SOURCE_URI_ROOT, PERSONAL_GCS_URI_ROOT, 1)
        blob = blobs.get(relative)
        if blob is None:
            raise FileNotFoundError(_destination_uri(relative))
        generation = int(blob.generation) if "#" in value else None
        return _destination_uri(relative, generation)

    result = copy.deepcopy(value)
    declared_uri = result.get("uri", result.get("default_uri"))
    relative = _source_object(declared_uri) if isinstance(declared_uri, str) else None
    blob = None if relative is None else blobs.get(relative)
    if relative is not None:
        if blob is None:
            raise FileNotFoundError(_destination_uri(relative))
        _verify_metadata(result, blob)

    for key, item in tuple(result.items()):
        result[key] = _retarget_value(item, blobs)

    if blob is not None and "generation" in result:
        old_generation = result["generation"]
        new_generation = int(blob.generation)
        result["generation"] = (
            str(new_generation)
            if isinstance(old_generation, str)
            else new_generation
        )
    if blob is not None and "generation_pinned_uri" in result:
        result["generation_pinned_uri"] = _destination_uri(
            relative,
            int(blob.generation),
        )
    return result


def retarget_manifests(
    manifest_dir: Path = DEFAULT_MANIFEST_DIR,
    *,
    write: bool,
) -> dict[str, int]:
    blobs = load_destination_blobs()
    changed_files = 0
    changed_references = 0
    for path in sorted(manifest_dir.glob("*.json")):
        original_text = path.read_text(encoding="utf-8")
        original = json.loads(original_text)
        updated = _retarget_value(original, blobs)
        updated_text = json.dumps(updated, indent=2, ensure_ascii=False) + "\n"
        if updated_text == original_text:
            continue
        changed_files += 1
        changed_references += original_text.count(SOURCE_URI_ROOT)
        if write:
            path.write_text(updated_text, encoding="utf-8")

    if write:
        remaining = [
            str(path)
            for path in sorted(manifest_dir.glob("*.json"))
            if SOURCE_URI_ROOT in path.read_text(encoding="utf-8")
        ]
        if remaining:
            raise AssertionError(
                f"company GCS references remain in manifests: {remaining}"
            )
    return {
        "destination_objects_listed": len(blobs),
        "manifest_files_changed": changed_files,
        "gcs_references_changed": changed_references,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest-dir", type=Path, default=DEFAULT_MANIFEST_DIR)
    parser.add_argument("--write", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = retarget_manifests(args.manifest_dir, write=args.write)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
