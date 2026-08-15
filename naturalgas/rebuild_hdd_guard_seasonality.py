#!/usr/bin/env python3
"""Recompute the selected guard with HDD disabled in June--August."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from naturalgas.evaluate_model_v03_d1_3_storage_guard import (  # noqa: E402
    BLOCK,
    SCORE_D1_3,
    SCORE_INPUTS,
    SCORE_SELECTED,
    recompute_guard_states,
    validate_score_inputs,
)
from naturalgas.audit_inputs import (  # noqa: E402
    D1_3_SCORE_INPUTS_ARTIFACT_ID,
    resolve_audit_inputs,
)


DEFAULT_OUTPUT = (
    PROJECT_ROOT / "reproduced/audit/wind/d1_3_storage_amplifier_inputs.parquet"
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rebuild(inputs: pd.DataFrame) -> pd.DataFrame:
    """Recompute guard flags and the selected score; preserve base scores."""

    result = inputs.copy()
    result["date"] = pd.to_datetime(result["date"]).dt.normalize()
    states = recompute_guard_states(result)
    for column in states:
        result[f"fast_guard__{column}"] = states[column]
    result[BLOCK] = (
        states["fast_plus_storage_amplifier"]
        & result["wind_signal__d1_3"].lt(0.0)
        & result["score_without_wind"].gt(0.0)
        & result[SCORE_D1_3].lt(0.0)
    )
    result[SCORE_SELECTED] = result[SCORE_D1_3].mask(result[BLOCK], 0.0)
    validate_score_inputs(result)
    return result


def write_atomic(frame: pd.DataFrame, output: Path) -> None:
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{output.name}.",
            suffix=".tmp.parquet",
            dir=output.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
        frame.to_parquet(temporary, index=False, compression="zstd")
        temporary.replace(output)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def run(input_path: Path, output_path: Path) -> dict[str, object]:
    input_path = resolve_audit_inputs({
        D1_3_SCORE_INPUTS_ARTIFACT_ID: input_path,
    })[D1_3_SCORE_INPUTS_ARTIFACT_ID]
    rebuilt = rebuild(pd.read_parquet(input_path))
    write_atomic(rebuilt, output_path)
    return {
        "input": str(input_path.resolve()),
        "output": str(output_path.resolve()),
        "output_sha256": sha256(output_path),
        "score_dates": len(rebuilt),
        "guard_intervention_score_dates": int(rebuilt[BLOCK].sum()),
        "hdd_guard_months": [1, 2, 3, 4, 5, 9, 10, 11, 12],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=SCORE_INPUTS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    print(json.dumps(run(args.input, args.output), indent=2, sort_keys=True))
