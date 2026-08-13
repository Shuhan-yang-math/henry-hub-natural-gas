#!/usr/bin/env python3
"""Rebuild the frozen daily-available-BA Florida score history."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from naturalgas.eia930_florida_availability import (  # noqa: E402
    build_source_history,
    map_to_score_dates,
)


SOURCE_DAILY = (
    PROJECT_ROOT
    / "inputs/audit/eia930/eia930_southeast_daily_multifuel.parquet"
)
FORMAL_DAILY = (
    PROJECT_ROOT
    / "naturalgas/processed/south_central_storage_strategy/strategy_daily.parquet"
)
D1_SCORE_INPUTS = (
    PROJECT_ROOT / "inputs/audit/wind/d1_3_storage_amplifier_inputs.parquet"
)
OUTPUT = (
    PROJECT_ROOT
    / "inputs/audit/eia930/florida_available_ba_signal_history.parquet"
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def json_default(value: Any) -> Any:
    if isinstance(value, (pd.Timestamp, np.datetime64)):
        return pd.Timestamp(value).isoformat()
    if isinstance(value, np.integer):
        return int(value)
    raise TypeError(type(value).__name__)


def run(
    *,
    source_daily_path: Path = SOURCE_DAILY,
    formal_daily_path: Path = FORMAL_DAILY,
    d1_score_inputs_path: Path = D1_SCORE_INPUTS,
    output_path: Path = OUTPUT,
) -> dict[str, Any]:
    source = pd.read_parquet(source_daily_path)
    formal_dates = pd.read_parquet(formal_daily_path, columns=["date"])["date"]
    d1_dates = pd.read_parquet(d1_score_inputs_path, columns=["date"])["date"]
    strategy_dates = pd.concat([formal_dates, d1_dates], ignore_index=True)
    history = build_source_history(source)
    mapped = map_to_score_dates(history, strategy_dates)
    mapped = mapped.loc[
        mapped["date"].ge(pd.Timestamp("2019-07-24"))
        & mapped["signal__firm__florida"].notna()
    ].reset_index(drop=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    mapped.to_parquet(output_path, index=False, compression="zstd")
    fallback = mapped["florida_available_ba_count"].lt(9)
    return {
        "source": str(source_daily_path),
        "source_sha256": sha256(source_daily_path),
        "output": str(output_path),
        "output_sha256": sha256(output_path),
        "score_dates": len(mapped),
        "score_date_start": mapped["date"].min(),
        "score_date_end": mapped["date"].max(),
        "available_ba_fallback_score_dates": int(fallback.sum()),
        "minimum_available_ba_count": int(
            mapped["florida_available_ba_count"].min()
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-daily", type=Path, default=SOURCE_DAILY)
    parser.add_argument("--formal-daily", type=Path, default=FORMAL_DAILY)
    parser.add_argument("--d1-score-inputs", type=Path, default=D1_SCORE_INPUTS)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    print(
        json.dumps(
            run(
                source_daily_path=arguments.source_daily,
                formal_daily_path=arguments.formal_daily,
                d1_score_inputs_path=arguments.d1_score_inputs,
                output_path=arguments.output,
            ),
            default=json_default,
            indent=2,
            sort_keys=True,
        )
    )
