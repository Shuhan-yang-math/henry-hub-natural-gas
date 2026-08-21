"""Personal Google Cloud Storage defaults for this repository.

The personal repository is intentionally independent of Braeswood's bucket.
Generation-pinned rebuilds obtain their exact object locations from the
checked-in manifests; legacy research and backfill entry points use the root
below when a cloud path is needed.
"""

from __future__ import annotations


PERSONAL_GCS_BUCKET = "datafinancial0"
PERSONAL_GCS_PREFIX = "henry-hub-natural-gas"
PERSONAL_GCS_ROOT = f"{PERSONAL_GCS_BUCKET}/{PERSONAL_GCS_PREFIX}"
PERSONAL_GCS_URI_ROOT = f"gs://{PERSONAL_GCS_ROOT}"


def personal_gcs_key(path: str) -> str:
    """Return a gcsfs key below the personal Henry Hub prefix."""

    cleaned = path.strip("/")
    if not cleaned:
        return PERSONAL_GCS_ROOT
    return f"{PERSONAL_GCS_ROOT}/{cleaned}"


def personal_gcs_uri(path: str) -> str:
    """Return a ``gs://`` URI below the personal Henry Hub prefix."""

    return f"gs://{personal_gcs_key(path)}"
