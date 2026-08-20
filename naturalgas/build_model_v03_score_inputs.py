#!/usr/bin/env python3
"""Build the V03 score-date contract from independent upstream inputs.

This module closes the former compact-score reproducibility boundary.  It
rebuilds the South Central fundamental state, solar sleeve, Central and
Florida EIA-930 signals, production controls, D1--3/D1--5/no-wind scores,
storage-calendar correction, and final wind guard without reading the frozen
``d1_3_storage_amplifier_inputs.parquet`` as a model input.

The frozen compact score and correction artifacts may be supplied as parity
targets.  They are never used to calculate the rebuilt score.  CPC values are
consumed exactly as they appear in the generation-pinned master panel; this
builder deliberately does not change the CPC source-data policy.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from naturalgas.build_wngsr_d1_3_corrections import build_corrections
from naturalgas.eia930_florida_availability import (
    build_source_history as build_florida_source_history,
    map_to_score_dates as map_florida_to_score_dates,
)
from naturalgas.evaluate_model_v02_eia930_central_florida import (
    CENTRAL_SIGNAL,
    CORE_FUNDAMENTAL,
    CORE_SCORE,
    FLORIDA_SIGNAL,
)
from naturalgas.evaluate_model_v03_d1_3_storage_guard import (
    BLOCK,
    SCORE_D1_3,
    SCORE_D1_5,
    SCORE_SELECTED,
    recompute_guard_states,
    validate_score_inputs,
)
from naturalgas.evaluate_native_frequency_fundamentals import (
    SELECTED_WIND_ALLOCATION,
    apply_native_frequency_fundamentals,
)
from naturalgas.evaluate_ncar_gdex_complete_solar_factor import (
    apply_production_control,
    load_capacity_weighted_solar,
)
from naturalgas.evaluate_ncar_gdex_independent_wind_weights import (
    allocation_score,
    candidate_allocations,
)
from naturalgas.evaluate_no_consumption_fundamental_weights import (
    candidate_position,
    prepare_base_panel,
)
from naturalgas.evaluate_south_central_storage import (
    attach_regional_signals,
    regional_weekly_signals,
    strategy_fundamentals,
)
from naturalgas.reproducibility import sha256_file


CENTRAL_RESPONDENTS = ("ERCO", "MISO", "SWPP")
CENTRAL_OTHER_GENERATION_COLUMNS = (
    "coal_mwh",
    "nuclear_mwh",
    "petroleum_mwh",
    "hydro_mwh",
    "geothermal_mwh",
    "other_fuel_mwh",
    "unknown_fuel_mwh",
)
EIA_VALUE_COLUMNS = (
    "demand_mwh",
    "coal_mwh",
    "gas_mwh",
    "nuclear_mwh",
    "petroleum_mwh",
    "hydro_mwh",
    "pumped_storage_mwh",
    "solar_mwh",
    "wind_mwh",
    "battery_mwh",
    "other_storage_mwh",
    "unknown_storage_mwh",
    "geothermal_mwh",
    "other_fuel_mwh",
    "unknown_fuel_mwh",
)
CENTRAL_SHARE = 0.40
FLORIDA_SHARE = 0.60
EIA930_SLOT_WEIGHT = 0.10
SOLAR_NOMINAL_WEIGHT = 0.10
LEGACY_STORAGE_LAG_DAYS = 6
SCORE_START = pd.Timestamp("2019-07-24")
WIND_COLUMNS = ("wind_signal__d1_3", "wind_signal__d1_5")
BASE_SCORE_COLUMNS = (
    "score_without_wind",
    SCORE_D1_3,
    SCORE_D1_5,
)


@dataclass(frozen=True)
class ScoreInputBuild:
    """In-memory products needed by the V03 evaluator and audit receipt."""

    score_inputs: pd.DataFrame
    storage_corrections: pd.DataFrame
    central_history: pd.DataFrame
    florida_history: pd.DataFrame
    parity: dict[str, Any]


def causal_anomaly_signal(
    value: pd.Series,
    date: pd.Series,
    *,
    bullish_when_low: bool = True,
) -> pd.Series:
    """Past-eight-same-weekday innovation on a prior-252 innovation scale."""

    frame = pd.DataFrame({
        "value": pd.to_numeric(value, errors="coerce"),
        "date": pd.to_datetime(date).dt.normalize(),
    }).sort_values("date")
    frame["day_of_week"] = frame["date"].dt.dayofweek
    expected = frame.groupby("day_of_week", group_keys=False)[
        "value"
    ].transform(
        lambda values: values.shift(1).rolling(8, min_periods=4).mean()
    )
    innovation = frame["value"] - expected
    scale = innovation.shift(1).rolling(252, min_periods=126).std()
    z_score = innovation.div(scale).clip(-6.0, 6.0)
    if bullish_when_low:
        z_score = -z_score
    return np.tanh(z_score / 2.0).reindex(value.index)


def build_central_source_history(daily: pd.DataFrame) -> pd.DataFrame:
    """Build Central total- and firm-non-gas signals on source gas days."""

    required = {
        "date",
        "respondent",
        "complete_day",
        *EIA_VALUE_COLUMNS,
    }
    missing = required.difference(daily.columns)
    if missing:
        raise ValueError(f"Central EIA-930 source is missing: {sorted(missing)}")

    source = daily.loc[
        daily["respondent"].isin(CENTRAL_RESPONDENTS)
    ].copy()
    source["date"] = pd.to_datetime(source["date"]).dt.normalize()
    if source.duplicated(["date", "respondent"]).any():
        raise ValueError("Central EIA-930 source contains duplicate respondent-days")

    valid = source.loc[source["complete_day"].fillna(False).astype(bool)].copy()
    presence = valid.groupby("date")["respondent"].nunique()
    aggregate = valid.groupby("date", as_index=False)[
        list(EIA_VALUE_COLUMNS)
    ].sum(min_count=1)
    aggregate["central_respondent_count"] = aggregate["date"].map(presence)
    aggregate = aggregate.loc[
        aggregate["central_respondent_count"].eq(len(CENTRAL_RESPONDENTS))
    ].copy()

    # Preserve the selected research calculation exactly.  Coal, nuclear and
    # conventional hydro must be present; blanks in smaller fuel categories
    # are neutral zero contributions after that core completeness check.
    core_complete = aggregate[
        ["coal_mwh", "nuclear_mwh", "hydro_mwh"]
    ].notna().all(axis=1)
    aggregate["central_firm_nongas_mwh"] = aggregate[
        list(CENTRAL_OTHER_GENERATION_COLUMNS)
    ].fillna(0.0).sum(axis=1).where(core_complete)
    aggregate["central_total_nongas_mwh"] = (
        aggregate["central_firm_nongas_mwh"]
        + aggregate["wind_mwh"]
        + aggregate["solar_mwh"]
    )
    demand = aggregate["demand_mwh"].replace(0.0, np.nan)
    aggregate[CENTRAL_SIGNAL] = causal_anomaly_signal(
        aggregate["central_total_nongas_mwh"] / demand,
        aggregate["date"],
    )
    aggregate["central_firm_nongas_shortfall"] = causal_anomaly_signal(
        aggregate["central_firm_nongas_mwh"] / demand,
        aggregate["date"],
    )
    aggregate["central_respondents"] = "|".join(CENTRAL_RESPONDENTS)
    return aggregate[
        [
            "date",
            "central_respondent_count",
            "central_respondents",
            "central_firm_nongas_mwh",
            "central_total_nongas_mwh",
            CENTRAL_SIGNAL,
            "central_firm_nongas_shortfall",
        ]
    ].sort_values("date").reset_index(drop=True)


def map_central_to_score_dates(
    source_history: pd.DataFrame,
    strategy_dates: Iterable[pd.Timestamp],
) -> pd.DataFrame:
    """Map each Central gas day to the first strictly later score date."""

    dates = pd.DatetimeIndex(pd.to_datetime(list(strategy_dates)))
    dates = dates.dropna().unique().sort_values()
    value_columns = [column for column in source_history if column != "date"]
    rows: list[dict[str, object]] = []
    for row in source_history.itertuples(index=False):
        source_date = pd.Timestamp(row.date)
        target = int(dates.searchsorted(source_date, side="right"))
        if target >= len(dates):
            continue
        record: dict[str, object] = {
            "date": dates[target],
            "source_gas_day_central": source_date,
        }
        for column in value_columns:
            record[column] = getattr(row, column)
        rows.append(record)
    mapped = pd.DataFrame(rows)
    if mapped.empty:
        return mapped
    return (
        mapped.sort_values(["date", "source_gas_day_central"])
        .drop_duplicates("date", keep="last")
        .reset_index(drop=True)
    )


def _strategy_base(
    panel: pd.DataFrame,
    *,
    filesystem: Any,
    wind_path: Path,
    solar_signal_path: Path,
    solar_lead_path: Path,
    actual_storage_calendar: bool,
) -> tuple[pd.DataFrame, pd.Series, pd.DataFrame]:
    native = apply_native_frequency_fundamentals(panel, filesystem)
    solar = load_capacity_weighted_solar(solar_signal_path, solar_lead_path)
    base = prepare_base_panel(native, wind_path=wind_path, solar=solar)
    weekly = regional_weekly_signals(filesystem)
    if not actual_storage_calendar:
        weekly = weekly.copy()
        weekly["storage_available_date"] = (
            pd.to_datetime(weekly["week_ending"])
            + pd.Timedelta(days=LEGACY_STORAGE_LAG_DAYS)
        )
    base = attach_regional_signals(base, weekly)
    fundamental = strategy_fundamentals(base)[0][
        "replace_all_storage__south_central_total"
    ]
    return base, fundamental, weekly


def _score_with_sleeves(
    working: pd.DataFrame,
    fundamental: pd.Series,
    *,
    wind_signal: pd.Series,
    eia930_signal: pd.Series,
) -> pd.Series:
    allocation = next(
        candidate
        for candidate in candidate_allocations()
        if candidate.name == SELECTED_WIND_ALLOCATION
    )
    frame = working.copy()
    frame["fundamental_rebuilt"] = fundamental.to_numpy()
    frame["wind_transformed"] = wind_signal.fillna(0.0)
    score = allocation_score(frame, allocation)

    daylight = frame["daylight_scale"].fillna(0.0)
    solar_weight = SOLAR_NOMINAL_WEIGHT * daylight
    solar = np.tanh(frame["sig_solar_pv"] / 2.0).fillna(0.0)
    score = score + solar_weight * (solar - fundamental.to_numpy())
    score = score + EIA930_SLOT_WEIGHT * (
        eia930_signal - fundamental.to_numpy()
    )
    score = apply_production_control(
        frame,
        score,
    )
    return score


def _core_formal(
    base: pd.DataFrame,
    fundamental: pd.Series,
) -> pd.DataFrame:
    core_score, _ = candidate_position(base, fundamental)
    return pd.DataFrame({
        "date": base["date"],
        CORE_SCORE: core_score,
        CORE_FUNDAMENTAL: fundamental,
    })


def _production_short_block(frame: pd.DataFrame) -> pd.Series:
    return (
        frame["date"].dt.month.isin((11, 12, 1, 2, 3))
        & frame["prod_freeze_local_level_score"].ge(1.0)
        & frame["prod_freeze_local_revision_score"].ge(0.0)
    )


def _comparison(
    rebuilt: pd.DataFrame,
    frozen: pd.DataFrame,
    *,
    columns: Iterable[str],
) -> dict[str, Any]:
    left = rebuilt[["date", *columns]].copy()
    right = frozen[["date", *columns]].copy()
    left["date"] = pd.to_datetime(left["date"]).dt.normalize()
    right["date"] = pd.to_datetime(right["date"]).dt.normalize()
    joined = right.merge(
        left,
        on="date",
        how="inner",
        suffixes=("__frozen", "__rebuilt"),
        validate="one_to_one",
    )
    result: dict[str, Any] = {
        "frozen_rows": len(right),
        "rebuilt_rows": len(left),
        "matched_dates": len(joined),
        "frozen_only_dates": sorted(
            right.loc[~right["date"].isin(left["date"]), "date"]
            .dt.strftime("%Y-%m-%d")
            .tolist()
        ),
        "rebuilt_only_dates": sorted(
            left.loc[~left["date"].isin(right["date"]), "date"]
            .dt.strftime("%Y-%m-%d")
            .tolist()
        ),
        "columns": {},
    }
    for column in columns:
        frozen_value = joined[f"{column}__frozen"]
        rebuilt_value = joined[f"{column}__rebuilt"]
        if pd.api.types.is_bool_dtype(frozen_value.dtype):
            mismatch = frozen_value.fillna(False).astype(bool).ne(
                rebuilt_value.fillna(False).astype(bool)
            )
            maximum = None
        elif pd.api.types.is_numeric_dtype(frozen_value.dtype):
            mismatch = ~np.isclose(
                frozen_value,
                rebuilt_value,
                atol=1e-12,
                rtol=0.0,
                equal_nan=True,
            )
            difference = (frozen_value - rebuilt_value).abs().dropna()
            maximum = float(difference.max()) if not difference.empty else 0.0
        else:
            mismatch = ~(
                frozen_value.eq(rebuilt_value)
                | (frozen_value.isna() & rebuilt_value.isna())
            )
            maximum = None
        result["columns"][column] = {
            "mismatch_dates": int(np.asarray(mismatch).sum()),
            "maximum_absolute_difference": maximum,
        }
    return result


def _require_zero_mismatches(
    comparison: dict[str, Any],
    *,
    label: str,
) -> None:
    mismatches = {
        column: details["mismatch_dates"]
        for column, details in comparison["columns"].items()
        if details["mismatch_dates"]
    }
    if mismatches:
        raise AssertionError(f"{label} parity mismatch: {mismatches}")


def build_score_inputs(
    *,
    panel_path: Path,
    wind_path: Path,
    wind_horizon_path: Path,
    solar_signal_path: Path,
    solar_lead_path: Path,
    central_eia930_path: Path,
    southeast_eia930_path: Path,
    filesystem: Any,
    frozen_score_inputs_path: Path | None = None,
    frozen_storage_corrections_path: Path | None = None,
) -> ScoreInputBuild:
    """Rebuild every continuous and control input used before the event veto."""

    panel = pd.read_parquet(panel_path)
    panel["date"] = pd.to_datetime(panel["date"]).dt.normalize()
    legacy_base, legacy_fundamental, _ = _strategy_base(
        panel,
        filesystem=filesystem,
        wind_path=wind_path,
        solar_signal_path=solar_signal_path,
        solar_lead_path=solar_lead_path,
        actual_storage_calendar=False,
    )
    corrected_base, corrected_fundamental, corrected_weekly = _strategy_base(
        panel,
        filesystem=filesystem,
        wind_path=wind_path,
        solar_signal_path=solar_signal_path,
        solar_lead_path=solar_lead_path,
        actual_storage_calendar=True,
    )

    central_source = pd.read_parquet(central_eia930_path)
    central_history = build_central_source_history(central_source)
    central = map_central_to_score_dates(
        central_history,
        legacy_base["date"],
    )
    southeast_source = pd.read_parquet(southeast_eia930_path)
    florida_source = build_florida_source_history(southeast_source)
    florida = map_florida_to_score_dates(
        florida_source,
        legacy_base["date"],
    )

    horizons = pd.read_parquet(wind_horizon_path)
    horizons["date"] = pd.to_datetime(horizons["date"]).dt.normalize()
    if not horizons["date"].is_unique:
        raise ValueError("Wind horizon input contains duplicate dates")

    working = (
        legacy_base.merge(
            horizons[["date", *WIND_COLUMNS]],
            on="date",
            how="left",
            validate="one_to_one",
        )
        .merge(
            central[
                [
                    "date",
                    "source_gas_day_central",
                    CENTRAL_SIGNAL,
                    "central_firm_nongas_shortfall",
                    "central_respondents",
                ]
            ],
            on="date",
            how="left",
            validate="one_to_one",
        )
        .merge(
            florida[
                [
                    "date",
                    "source_gas_day_florida",
                    "signal__firm__florida",
                    "florida_available_ba_count",
                    "florida_respondents",
                ]
            ],
            on="date",
            how="left",
            validate="one_to_one",
        )
    )
    selected_eia = (
        CENTRAL_SHARE * working[CENTRAL_SIGNAL]
        + FLORIDA_SHARE * working["signal__firm__florida"]
    )
    zero_wind = pd.Series(0.0, index=working.index)
    scores = {
        "score_without_wind": _score_with_sleeves(
            working,
            legacy_fundamental,
            wind_signal=zero_wind,
            eia930_signal=selected_eia,
        ),
        SCORE_D1_3: _score_with_sleeves(
            working,
            legacy_fundamental,
            wind_signal=working["wind_signal__d1_3"],
            eia930_signal=selected_eia,
        ),
        SCORE_D1_5: _score_with_sleeves(
            working,
            legacy_fundamental,
            wind_signal=working["wind_signal__d1_5"],
            eia930_signal=selected_eia,
        ),
    }

    output = working[
        [
            "date",
            "central_firm_nongas_shortfall",
            "signal__firm__florida",
            "south_central_total_level_signal",
            "hdd_revision_5d_z",
            "prod_freeze_local_level_score",
            "prod_freeze_local_revision_score",
            *WIND_COLUMNS,
            "source_gas_day_florida",
            "florida_available_ba_count",
            "florida_respondents",
        ]
    ].copy()
    output["selected_nongas_signal"] = selected_eia
    for column, values in scores.items():
        output[column] = values
    states = recompute_guard_states(output)
    for column in states:
        output[f"fast_guard__{column}"] = states[column]
    output[BLOCK] = (
        states["fast_plus_storage_amplifier"]
        & output["wind_signal__d1_3"].lt(0.0)
        & output["score_without_wind"].gt(0.0)
        & output[SCORE_D1_3].lt(0.0)
    )
    output[SCORE_SELECTED] = output[SCORE_D1_3].mask(output[BLOCK], 0.0)
    output = output.loc[
        output["date"].ge(SCORE_START)
        & output["source_gas_day_florida"].notna()
        & output["signal__firm__florida"].notna()
        & output["selected_nongas_signal"].notna()
    ].copy()

    ordered_columns = [
        "date",
        "central_firm_nongas_shortfall",
        "signal__firm__florida",
        "selected_nongas_signal",
        "south_central_total_level_signal",
        "hdd_revision_5d_z",
        "prod_freeze_local_level_score",
        "prod_freeze_local_revision_score",
        *WIND_COLUMNS,
        *[f"fast_guard__{column}" for column in states],
        "score_without_wind",
        SCORE_D1_3,
        SCORE_D1_5,
        BLOCK,
        SCORE_SELECTED,
        "source_gas_day_florida",
        "florida_available_ba_count",
        "florida_respondents",
    ]
    output = output[ordered_columns].reset_index(drop=True)
    validate_score_inputs(output)

    overlay = working[
        ["date", CENTRAL_SIGNAL, "signal__firm__florida"]
    ].copy()
    overlay[FLORIDA_SIGNAL] = overlay["signal__firm__florida"]
    overlay["production_short_block_active"] = _production_short_block(working)
    legacy_formal = _core_formal(legacy_base, legacy_fundamental)
    corrected_formal = _core_formal(corrected_base, corrected_fundamental)
    corrections = build_corrections(
        legacy_formal=legacy_formal,
        corrected_formal=corrected_formal,
        overlay=overlay,
        score_inputs=output,
        corrected_weekly=corrected_weekly,
    )

    parity: dict[str, Any] = {
        "frozen_compact_used_for_calculation": False,
        "cpc_policy": "unchanged generation-pinned master-panel values",
    }
    if frozen_score_inputs_path is not None:
        frozen = pd.read_parquet(frozen_score_inputs_path)
        input_columns = [
            "central_firm_nongas_shortfall",
            "signal__firm__florida",
            "south_central_total_level_signal",
            "hdd_revision_5d_z",
            "prod_freeze_local_level_score",
            "prod_freeze_local_revision_score",
            *WIND_COLUMNS,
        ]
        parity["upstream_inputs"] = _comparison(
            output,
            frozen,
            columns=input_columns,
        )
        _require_zero_mismatches(
            parity["upstream_inputs"],
            label="V03 upstream input",
        )
        parity["pre_guard_scores"] = _comparison(
            output,
            frozen,
            columns=BASE_SCORE_COLUMNS,
        )
        _require_zero_mismatches(
            parity["pre_guard_scores"],
            label="V03 pre-guard score",
        )
    if frozen_storage_corrections_path is not None:
        frozen_corrections = pd.read_parquet(frozen_storage_corrections_path)
        correction_columns = [
            "wngsr_score_delta_before_production_control",
            "legacy_south_central_total_level_signal",
            "corrected_south_central_total_level_signal",
            "production_short_block_active",
        ]
        parity["storage_corrections"] = _comparison(
            corrections,
            frozen_corrections,
            columns=correction_columns,
        )
        _require_zero_mismatches(
            parity["storage_corrections"],
            label="V03 storage correction",
        )
    return ScoreInputBuild(
        score_inputs=output,
        storage_corrections=corrections,
        central_history=central,
        florida_history=florida,
        parity=parity,
    )


def write_score_input_build(
    build: ScoreInputBuild,
    *,
    output_dir: Path,
    receipt_output_dir: Path | None = None,
) -> dict[str, Any]:
    """Write the rebuilt score contract and its independent lineage tables."""

    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "score_inputs": output_dir / "model_v03_score_inputs.parquet",
        "storage_corrections": output_dir / "wngsr_score_corrections.parquet",
        "central_history": output_dir / "central_eia930_signal_history.parquet",
        "florida_history": output_dir / "florida_eia930_signal_history.parquet",
    }
    build.score_inputs.to_parquet(
        paths["score_inputs"], index=False, compression="zstd"
    )
    build.storage_corrections.to_parquet(
        paths["storage_corrections"], index=False, compression="zstd"
    )
    build.central_history.to_parquet(
        paths["central_history"], index=False, compression="zstd"
    )
    build.florida_history.to_parquet(
        paths["florida_history"], index=False, compression="zstd"
    )
    logical_dir = output_dir if receipt_output_dir is None else receipt_output_dir
    receipt = {
        "status": "rebuilt_from_independent_upstream_inputs",
        "score_dates": len(build.score_inputs),
        "storage_correction_dates": len(build.storage_corrections),
        "cpc_data_issue_changed": False,
        "outputs": {
            name: {
                "path": str(logical_dir / path.name),
                "sha256": sha256_file(path),
            }
            for name, path in paths.items()
        },
        "parity": build.parity,
    }
    (output_dir / "score_input_rebuild_receipt.json").write_text(
        json.dumps(receipt, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return receipt
