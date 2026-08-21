from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from naturalgas.build_multisignal_panel import DEFAULT_INPUTS
from naturalgas.reproducibility import PROJECT_ROOT
from naturalgas.storage_config import (
    PERSONAL_GCS_ROOT,
    PERSONAL_GCS_URI_ROOT,
)


def _gcs_strings(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [item for child in value.values() for item in _gcs_strings(child)]
    if isinstance(value, list):
        return [item for child in value for item in _gcs_strings(child)]
    if isinstance(value, str) and value.startswith("gs://"):
        return [value]
    return []


def test_every_checked_in_manifest_uses_personal_gcs_archive() -> None:
    manifests = sorted((PROJECT_ROOT / "manifests").glob("*.json"))
    assert manifests
    for path in manifests:
        payload = json.loads(path.read_text(encoding="utf-8"))
        gcs_values = _gcs_strings(payload)
        assert gcs_values, path
        assert all(
            value.startswith(f"{PERSONAL_GCS_URI_ROOT}/")
            for value in gcs_values
        ), path


def test_legacy_panel_defaults_use_personal_gcs_archive() -> None:
    assert PERSONAL_GCS_ROOT == "datafinancial0/henry-hub-natural-gas"
    assert all(
        value.startswith(f"{PERSONAL_GCS_URI_ROOT}/")
        for value in DEFAULT_INPUTS.values()
    )
