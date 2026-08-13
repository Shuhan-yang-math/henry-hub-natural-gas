"""Build the Florida EIA-930 signal from each day's complete respondents.

The history is deliberately a single rolling series.  If one or more Florida
balancing authorities are incomplete on gas day t, the physical share for t
uses the remaining complete authorities.  That observation then stays in the
ordinary rolling history used by later dates; it is not standardized against
a separate same-subset history.
"""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np
import pandas as pd


FLORIDA_RESPONDENTS = (
    "FMPP",
    "FPC",
    "FPL",
    "GVL",
    "HST",
    "JEA",
    "SEC",
    "TAL",
    "TEC",
)
FLORIDA_SET = frozenset(FLORIDA_RESPONDENTS)
GENERATION_COLUMNS = (
    "gas_mwh",
    "coal_mwh",
    "nuclear_mwh",
    "petroleum_mwh",
    "hydro_mwh",
    "pumped_storage_mwh",
    "solar_mwh",
    "wind_mwh",
    "geothermal_mwh",
    "other_fuel_mwh",
    "unknown_fuel_mwh",
)


def build_source_history(daily: pd.DataFrame) -> pd.DataFrame:
    """Return the single causal Florida history on source gas days."""

    required = {
        "date",
        "respondent",
        "demand_mwh",
        "complete_day",
        *GENERATION_COLUMNS,
    }
    missing = required.difference(daily.columns)
    if missing:
        raise ValueError(f"Florida EIA-930 source is missing: {sorted(missing)}")

    source = daily.loc[daily["respondent"].isin(FLORIDA_SET)].copy()
    source["date"] = pd.to_datetime(source["date"]).dt.normalize()
    if source.duplicated(["date", "respondent"]).any():
        raise ValueError("Florida source contains duplicate respondent-days")
    presence = source.groupby("date")["respondent"].nunique()
    if not presence.eq(len(FLORIDA_RESPONDENTS)).all():
        raise ValueError("Florida source is missing one or more respondent rows")

    # Match the frozen research builder: blank BA-fuel categories represent
    # technologies the BA does not own/report and are treated as zero. Demand
    # remains strict because it is the denominator. The upstream daily cache
    # sets demand to NaN unless every hourly demand value is available.
    source[list(GENERATION_COLUMNS)] = source[
        list(GENERATION_COLUMNS)
    ].fillna(0.0)
    source["water_mwh"] = (
        source["hydro_mwh"] + source["pumped_storage_mwh"]
    )
    source["firm_nongas_mwh"] = source[
        ["coal_mwh", "nuclear_mwh", "water_mwh"]
    ].sum(axis=1, min_count=3)
    source["valid_row"] = (
        source["complete_day"].fillna(False).astype(bool)
        & source["demand_mwh"].notna()
        & source["firm_nongas_mwh"].notna()
    )
    valid = source.loc[source["valid_row"]].copy()
    coverage = valid.groupby("date")["respondent"].agg(
        florida_available_ba_count="count",
        florida_respondents=lambda values: "|".join(sorted(values)),
    )
    if coverage.empty or coverage["florida_available_ba_count"].eq(0).any():
        raise ValueError("No complete Florida BA is available on a source day")

    physical = valid.groupby("date")[["demand_mwh", "firm_nongas_mwh"]].sum(
        min_count=1
    )
    result = physical.join(coverage, validate="one_to_one").reset_index()
    result = result.sort_values("date").reset_index(drop=True)
    result["florida_firm_nongas_share"] = (
        result["firm_nongas_mwh"] / result["demand_mwh"]
    )

    result["day_of_week"] = result["date"].dt.dayofweek
    result["florida_share_past_8_same_weekday_mean"] = result.groupby(
        "day_of_week", group_keys=False
    )["florida_firm_nongas_share"].transform(
        lambda values: values.shift(1).rolling(8, min_periods=4).mean()
    )
    result["florida_share_innovation"] = (
        result["florida_firm_nongas_share"]
        - result["florida_share_past_8_same_weekday_mean"]
    )
    result["florida_prior_252_innovation_std"] = result[
        "florida_share_innovation"
    ].shift(1).rolling(252, min_periods=126).std()
    raw_z = -result["florida_share_innovation"].div(
        result["florida_prior_252_innovation_std"]
    )
    result["signal__firm__florida"] = np.tanh(raw_z.clip(-6.0, 6.0) / 2.0)
    return result


def map_to_score_dates(
    source_history: pd.DataFrame,
    strategy_dates: Iterable[pd.Timestamp],
) -> pd.DataFrame:
    """Map source day t to the first strictly later strategy score date."""

    dates = pd.DatetimeIndex(pd.to_datetime(list(strategy_dates)))
    dates = dates.dropna().unique().sort_values()
    rows: list[dict[str, object]] = []
    values = [column for column in source_history if column != "date"]
    for row in source_history.itertuples(index=False):
        source_date = pd.Timestamp(row.date)
        target = int(dates.searchsorted(source_date, side="right"))
        if target >= len(dates):
            continue
        record: dict[str, object] = {
            "date": dates[target],
            "source_gas_day_florida": source_date,
        }
        for column in values:
            record[column] = getattr(row, column)
        rows.append(record)
    mapped = pd.DataFrame(rows)
    if mapped.empty:
        return mapped
    # Weekend source days can share Monday availability. Use the latest
    # completed source day, even when that day has fewer than nine valid BAs.
    mapped = mapped.sort_values(
        ["date", "source_gas_day_florida"]
    ).drop_duplicates("date", keep="last")
    mapped = mapped.reset_index(drop=True)
    if not (mapped["source_gas_day_florida"] < mapped["date"]).all():
        raise AssertionError("Florida source day must precede its score date")
    return mapped


def validate_score_history(
    frame: pd.DataFrame,
    *,
    signal_column: str,
) -> None:
    """Validate Florida score-date lineage embedded in a frozen artifact."""

    required = {
        "date",
        "source_gas_day_florida",
        signal_column,
        "florida_available_ba_count",
        "florida_respondents",
    }
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"Florida score history is missing: {sorted(missing)}")
    if frame["date"].duplicated().any():
        raise ValueError("Florida score history contains duplicate dates")
    source_day = pd.to_datetime(frame["source_gas_day_florida"])
    if not (source_day < pd.to_datetime(frame["date"])).all():
        raise ValueError("Florida source day must precede score date")
    counts = pd.to_numeric(frame["florida_available_ba_count"], errors="coerce")
    if counts.isna().any() or not counts.between(1, 9).all():
        raise ValueError("Florida available BA count is outside 1--9")
    for row in frame.itertuples(index=False):
        respondents = str(row.florida_respondents).split("|")
        if len(respondents) != int(row.florida_available_ba_count):
            raise ValueError("Florida BA count does not match respondent list")
        if not set(respondents).issubset(FLORIDA_SET):
            raise ValueError("Florida signal contains a non-Florida respondent")
