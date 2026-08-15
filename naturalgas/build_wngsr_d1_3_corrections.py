#!/usr/bin/env python3
"""Build the narrow WNGSR-calendar correction overlay for frozen D1--3 scores.

The selected D1--3 input is a frozen derived boundary. Replacing it wholesale
would mix this timing fix with unrelated historical version differences. This
builder differences a legacy ``week_ending + 6`` formal run and the corrected
formal run, then records only score dates whose storage information set changed.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from naturalgas.evaluate_d1_3_storage_amplified_strategy import SCORE_INPUTS
from naturalgas.evaluate_eia930_selected_enhancement import (
    CENTRAL_SHARE,
    CENTRAL_SIGNAL,
    CORE_FUNDAMENTAL,
    CORE_SCORE,
    EIA930_SLOT_WEIGHT,
    FLORIDA_SHARE,
    FLORIDA_SIGNAL,
    OVERLAY_INPUTS,
)
from naturalgas.evaluate_south_central_storage import (
    attach_regional_signals,
    regional_weekly_signals,
)
from naturalgas.pipelines.rebuild_final_backtest import local_filesystem
from naturalgas.reproducibility import DEFAULT_MANIFEST
from naturalgas.audit_inputs import (
    D1_3_SCORE_INPUTS_ARTIFACT_ID,
    EIA930_OVERLAY_ARTIFACT_ID,
    LEGACY_WNGSR_ARTIFACT_ID,
    audit_input_path,
    resolve_audit_inputs,
)


LEGACY_FORMAL_DAILY = audit_input_path(LEGACY_WNGSR_ARTIFACT_ID)
DEFAULT_OUTPUT = (
    PROJECT_ROOT / "reproduced/audit/storage/wngsr_d1_3_score_corrections.parquet"
)
SCORE_DELTA = "wngsr_score_delta_before_production_control"
CORRECTED_LEVEL = "corrected_south_central_total_level_signal"
LEGACY_LEVEL = "legacy_south_central_total_level_signal"


def _load_dates(path: Path) -> pd.DataFrame:
    frame = pd.read_parquet(path)
    frame["date"] = pd.to_datetime(frame["date"]).dt.normalize()
    if not frame["date"].is_unique:
        raise ValueError(f"Input contains duplicate dates: {path}")
    return frame


def _eia_score_before_production_control(
    formal: pd.DataFrame,
    overlay: pd.DataFrame,
    *,
    prefix: str,
) -> pd.DataFrame:
    merged = formal[["date", CORE_SCORE, CORE_FUNDAMENTAL]].merge(
        overlay,
        on="date",
        how="left",
        validate="one_to_one",
    )
    blended = (
        CENTRAL_SHARE * merged[CENTRAL_SIGNAL]
        + FLORIDA_SHARE * merged[FLORIDA_SIGNAL]
    )
    score = merged[CORE_SCORE] + EIA930_SLOT_WEIGHT * (
        blended - merged[CORE_FUNDAMENTAL]
    )
    return pd.DataFrame({
        "date": merged["date"],
        f"{prefix}_core_score": merged[CORE_SCORE],
        f"{prefix}_core_fundamental": merged[CORE_FUNDAMENTAL],
        f"{prefix}_eia_score_before_production_control": score,
    })


def build_corrections(
    *,
    legacy_formal: pd.DataFrame,
    corrected_formal: pd.DataFrame,
    overlay: pd.DataFrame,
    score_inputs: pd.DataFrame,
    corrected_weekly: pd.DataFrame,
) -> pd.DataFrame:
    """Return score-date corrections caused only by WNGSR release timing."""

    legacy = _eia_score_before_production_control(
        legacy_formal,
        overlay,
        prefix="legacy",
    )
    corrected = _eia_score_before_production_control(
        corrected_formal,
        overlay,
        prefix="corrected",
    )
    comparison = legacy.merge(
        corrected,
        on="date",
        how="inner",
        validate="one_to_one",
    )
    comparison[SCORE_DELTA] = (
        comparison["corrected_eia_score_before_production_control"]
        - comparison["legacy_eia_score_before_production_control"]
    )

    corrected_levels = attach_regional_signals(
        score_inputs[["date"]],
        corrected_weekly,
    )[["date", "storage_available_date", "south_central_total_level_signal"]]
    corrected_levels = corrected_levels.rename(columns={
        "storage_available_date": "corrected_storage_available_date",
        "south_central_total_level_signal": CORRECTED_LEVEL,
    })
    result = score_inputs[["date", "south_central_total_level_signal"]].rename(
        columns={"south_central_total_level_signal": LEGACY_LEVEL}
    )
    result = result.merge(
        comparison,
        on="date",
        how="left",
        validate="one_to_one",
    ).merge(
        corrected_levels,
        on="date",
        how="left",
        validate="one_to_one",
    )
    result = result.merge(
        overlay[["date", "production_short_block_active"]],
        on="date",
        how="left",
        validate="one_to_one",
    )

    score_changed = result[SCORE_DELTA].abs().gt(1e-15)
    level_changed = ~np.isclose(
        result[LEGACY_LEVEL],
        result[CORRECTED_LEVEL],
        atol=1e-12,
        rtol=0.0,
        equal_nan=True,
    )
    if not score_changed.equals(pd.Series(level_changed, index=result.index)):
        raise AssertionError(
            "Score and South Central level changes must identify the same dates"
        )
    result = result.loc[score_changed].copy().reset_index(drop=True)
    result["production_short_block_active"] = result[
        "production_short_block_active"
    ].fillna(False).astype(bool)
    if result.empty:
        raise AssertionError("WNGSR correction overlay unexpectedly has no rows")
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--legacy-formal",
        type=Path,
        default=LEGACY_FORMAL_DAILY,
    )
    parser.add_argument("--corrected-formal", type=Path, required=True)
    parser.add_argument("--overlay-inputs", type=Path, default=OVERLAY_INPUTS)
    parser.add_argument("--score-inputs", type=Path, default=SCORE_INPUTS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    audit_paths = resolve_audit_inputs({
        LEGACY_WNGSR_ARTIFACT_ID: args.legacy_formal,
        EIA930_OVERLAY_ARTIFACT_ID: args.overlay_inputs,
        D1_3_SCORE_INPUTS_ARTIFACT_ID: args.score_inputs,
    })
    legacy_formal = _load_dates(audit_paths[LEGACY_WNGSR_ARTIFACT_ID])
    corrected_formal = _load_dates(args.corrected_formal)
    overlay = _load_dates(audit_paths[EIA930_OVERLAY_ARTIFACT_ID])
    score_inputs = _load_dates(audit_paths[D1_3_SCORE_INPUTS_ARTIFACT_ID])
    filesystem = local_filesystem(DEFAULT_MANIFEST, root=PROJECT_ROOT)
    corrected_weekly = regional_weekly_signals(filesystem)
    corrections = build_corrections(
        legacy_formal=legacy_formal,
        corrected_formal=corrected_formal,
        overlay=overlay,
        score_inputs=score_inputs,
        corrected_weekly=corrected_weekly,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    corrections.to_parquet(args.output, index=False, compression="zstd")
    print(f"wrote {len(corrections)} corrections to {args.output}")


if __name__ == "__main__":
    main()
