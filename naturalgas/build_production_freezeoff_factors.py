"""Build EIA-production-weighted freeze-off weather factors.

Inputs
------
1. EIA STEO Figure 44: monthly marketed natural-gas production by region.
2. Open-Meteo production-region fixed-lead weather proxies already in GCS.
3. Roll-adjusted Henry Hub C1 returns for diagnostics only.

Important point-in-time limitation
----------------------------------
The current EIA workbook contains a revised/backcast history, not archived
historical vintages.  We conservatively lag each production month by three
months before using it as a weight, but this does not remove revision-vintage
leakage.  All outputs carry an explicit ``weight_vintage_point_in_time=False``
flag.  The weights are appropriate for research and current/live weighting;
strict historical simulation requires archived monthly STEO vintages.

Only the five EIA regions that map directly to the weather dataset enter the
national weighted factor: Appalachia, Permian, Haynesville, Bakken, and Eagle
Ford.  Anadarko and Rockies remain available as standalone regional weather
features, but are not assigned arbitrary shares of EIA's aggregate "Other".
"""

from __future__ import annotations

import argparse
import io
import json
from datetime import date, datetime, timezone
from pathlib import Path

import gcsfs
import numpy as np
import pandas as pd
import requests


BUCKET = "bcli-natgas-data-497807"
EIA_SOURCE_URL = "https://www.eia.gov/outlooks/steo/xls/Fig44.xlsx"
EIA_SOURCE_RELEASE_DATE = date(2026, 7, 7)
EIA_DIRECT_REGIONS = (
    "Appalachia",
    "Permian",
    "Haynesville",
    "Bakken",
    "Eagle Ford",
)
EXTREME_EVENT_M5_EXPOSURE_THRESHOLD = 20.0
TEMPERATURE_SEVERITY_WEIGHTS = {
    "freeze_degree_hours_0c": 0.25,
    "freeze_degree_hours_minus5c": 0.50,
    "freeze_degree_hours_minus10c": 1.00,
}
LEVEL_LEAD_WEIGHTS = {
    lead: float(2 ** (-(lead - 1) / 2)) for lead in range(1, 6)
}
REVISION_LEAD_WEIGHTS = {
    lead: float(2 ** (-(lead - 1) / 2)) for lead in range(1, 5)
}
LOCAL_CLIMATE_LOOKBACK_YEARS = 3
LOCAL_CLIMATE_SEASON_WINDOW_DAYS = 30
LOCAL_CLIMATE_MIN_OBSERVATIONS = 45
LOCAL_CLIMATE_COLD_QUANTILE = 0.10
WEATHER_GLOB = (
    f"{BUCKET}/processed/weather/open_meteo/production_regions/"
    "previous_runs/model=ncep_gfs_seamless/"
    "region_daily/year=*/month=*/data.parquet"
)
FUTURES_KEY = (
    f"{BUCKET}/processed/ng_hh_futures/ng_hh_futures_daily.parquet"
)
RAW_EIA_PREFIX = (
    f"{BUCKET}/raw/eia/steo/regional_marketed_gas_production/"
    f"vintage={EIA_SOURCE_RELEASE_DATE.isoformat()}"
)
OUTPUT_PREFIX = (
    f"{BUCKET}/processed/weather/production_freezeoff_factors/"
    "model=ncep_gfs_seamless"
)


def causal_z(
    series: pd.Series, window: int = 252, min_periods: int = 126
) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce").astype(float)
    prior = values.shift(1)
    mean = prior.rolling(window, min_periods=min_periods).mean()
    std = prior.rolling(window, min_periods=min_periods).std()
    return (values - mean) / std.replace(0, np.nan)


def causal_quantile_scale(
    series: pd.Series,
    window: int = 756,
    min_periods: int = 252,
    quantile: float = 0.95,
    absolute_scale: bool = False,
) -> pd.Series:
    """Scale by a trailing quantile that excludes the current observation."""
    values = pd.to_numeric(series, errors="coerce").astype(float)
    scale_input = values.abs() if absolute_scale else values
    scale = (
        scale_input.shift(1)
        .rolling(window, min_periods=min_periods)
        .quantile(quantile)
    )
    result = values / scale.replace(0, np.nan)
    return result.mask(values.eq(0) & scale.eq(0), 0.0)


def complete_weighted_sum(
    row: pd.Series, columns: dict[int, str], weights: dict[int, float]
) -> float:
    values = pd.Series(
        {lead: row[column] for lead, column in columns.items()},
        dtype=float,
    )
    if values.notna().sum() != len(columns):
        return np.nan
    aligned_weights = pd.Series(weights, dtype=float).reindex(values.index)
    return float((values * aligned_weights).sum() / aligned_weights.sum())


def continuous_temperature_severity(data: pd.DataFrame) -> pd.Series:
    return (
        TEMPERATURE_SEVERITY_WEIGHTS["freeze_degree_hours_0c"]
        * data["region_mean_freeze_degree_hours_0c"]
        + TEMPERATURE_SEVERITY_WEIGHTS[
            "freeze_degree_hours_minus5c"
        ]
        * data["region_mean_freeze_degree_hours_minus5c"]
        + TEMPERATURE_SEVERITY_WEIGHTS[
            "freeze_degree_hours_minus10c"
        ]
        * data["region_mean_freeze_degree_hours_minus10c"]
    )


def season_day(values: pd.Series) -> pd.Series:
    month_day = pd.to_datetime(values).dt.strftime("%m-%d")
    return pd.to_datetime(
        "2000-" + month_day, format="%Y-%m-%d"
    ).dt.dayofyear


def build_local_temperature_climatology(
    weather: pd.DataFrame,
) -> pd.DataFrame:
    """Prior-year, same-season local cold-tail standards by region."""
    source = weather.loc[
        weather["lead_days"].eq(1),
        [
            "production_region",
            "target_date",
            "region_temperature_mean_c",
            "region_coldest_point_min_c",
        ],
    ].drop_duplicates(["production_region", "target_date"])
    source = source.copy()
    source["target_date"] = pd.to_datetime(source["target_date"])
    source["target_year"] = source["target_date"].dt.year
    source["season_day"] = season_day(source["target_date"])

    requested = weather[
        ["production_region", "nominal_issue_date", "target_date"]
    ].copy()
    requested["issue_year"] = pd.to_datetime(
        requested["nominal_issue_date"]
    ).dt.year
    requested["season_day"] = season_day(requested["target_date"])
    requested = requested[
        ["production_region", "issue_year", "season_day"]
    ].drop_duplicates()

    rows = []
    for region, region_requested in requested.groupby(
        "production_region", sort=False
    ):
        region_source = source.loc[
            source["production_region"].eq(region)
        ]
        for issue_year, year_requested in region_requested.groupby(
            "issue_year", sort=False
        ):
            history = region_source.loc[
                region_source["target_year"].between(
                    issue_year - LOCAL_CLIMATE_LOOKBACK_YEARS,
                    issue_year - 1,
                )
            ]
            for day in year_requested["season_day"].unique():
                distance = (history["season_day"] - day).abs()
                circular_distance = np.minimum(distance, 366 - distance)
                sample = history.loc[
                    circular_distance.le(
                        LOCAL_CLIMATE_SEASON_WINDOW_DAYS
                    )
                ]
                row = {
                    "production_region": region,
                    "issue_year": int(issue_year),
                    "season_day": int(day),
                    "local_climate_observations": len(sample),
                    "local_climate_start_date": sample[
                        "target_date"
                    ].min(),
                    "local_climate_end_date": sample[
                        "target_date"
                    ].max(),
                }
                for field, prefix in [
                    ("region_temperature_mean_c", "local_mean"),
                    ("region_coldest_point_min_c", "local_min"),
                ]:
                    values = sample[field].dropna()
                    if (
                        len(values)
                        < LOCAL_CLIMATE_MIN_OBSERVATIONS
                    ):
                        row[f"{prefix}_cold_q10_c"] = np.nan
                        row[f"{prefix}_iqr_c"] = np.nan
                    else:
                        row[f"{prefix}_cold_q10_c"] = values.quantile(
                            LOCAL_CLIMATE_COLD_QUANTILE
                        )
                        row[f"{prefix}_iqr_c"] = (
                            values.quantile(0.75)
                            - values.quantile(0.25)
                        )
                rows.append(row)
    return pd.DataFrame(rows).sort_values(
        ["production_region", "issue_year", "season_day"]
    ).reset_index(drop=True)


def local_cold_severity(data: pd.DataFrame) -> pd.Series:
    mean_scale = data["local_mean_iqr_c"].replace(0, np.nan)
    min_scale = data["local_min_iqr_c"].replace(0, np.nan)
    mean_severity = (
        (
            data["local_mean_cold_q10_c"]
            - data["region_temperature_mean_c"]
        )
        / mean_scale
    ).clip(lower=0)
    min_severity = (
        (
            data["local_min_cold_q10_c"]
            - data["region_coldest_point_min_c"]
        )
        / min_scale
    ).clip(lower=0)
    return 0.5 * mean_severity + 0.5 * min_severity


def attach_local_temperature_standard(
    weighted_regions: pd.DataFrame,
    climatology: pd.DataFrame,
) -> pd.DataFrame:
    data = weighted_regions.copy()
    data["issue_year"] = data["nominal_issue_date"].dt.year
    data["season_day"] = season_day(data["target_date"])
    data = data.merge(
        climatology,
        on=["production_region", "issue_year", "season_day"],
        how="left",
        validate="many_to_one",
    )
    data["local_cold_severity"] = local_cold_severity(data)
    data["local_temperature_standard"] = (
        "prior 3 complete years, +/-30 seasonal days, local cold q10 / IQR"
    )
    return data


def download_eia_workbook() -> bytes:
    response = requests.get(EIA_SOURCE_URL, timeout=180)
    response.raise_for_status()
    return response.content


def parse_eia_regional_production(workbook: bytes) -> pd.DataFrame:
    raw = pd.read_excel(io.BytesIO(workbook), sheet_name="44", header=None)
    header_candidates = raw.index[
        raw.apply(
            lambda row: {
                str(value).strip()
                for value in row.dropna().tolist()
            }.issuperset({"Appalachia", "Permian", "Haynesville"}),
            axis=1,
        )
    ]
    if len(header_candidates) != 1:
        raise RuntimeError(
            f"expected one EIA data header row, found {header_candidates.tolist()}"
        )
    header_row = int(header_candidates[0])
    columns = ["production_month"] + [
        str(value).strip() for value in raw.loc[header_row].dropna().tolist()
    ]
    expected_regions = [
        "Eagle Ford", "Permian", "Bakken", "Appalachia", "Haynesville", "Other"
    ]
    if columns[1:] != expected_regions:
        raise RuntimeError(f"unexpected EIA region columns: {columns[1:]}")

    data = raw.iloc[header_row + 1 :, : len(columns)].copy()
    data.columns = columns
    data["production_month"] = pd.to_datetime(
        data["production_month"], errors="coerce"
    ).dt.to_period("M").dt.to_timestamp()
    data = data.loc[
        data["production_month"].ge(pd.Timestamp("2009-01-01"))
    ].copy()
    for column in expected_regions:
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data = data.dropna(subset=expected_regions, how="all")

    long = data.melt(
        id_vars="production_month",
        var_name="eia_production_region",
        value_name="marketed_gas_production_bcf_per_day",
    )
    totals = (
        long.groupby("production_month")[
            "marketed_gas_production_bcf_per_day"
        ]
        .sum(min_count=6)
        .rename("lower48_marketed_gas_production_bcf_per_day")
    )
    long = long.merge(totals, on="production_month", how="left")
    direct_total = (
        long.loc[
            long["eia_production_region"].isin(EIA_DIRECT_REGIONS)
        ]
        .groupby("production_month")[
            "marketed_gas_production_bcf_per_day"
        ]
        .sum(min_count=len(EIA_DIRECT_REGIONS))
        .rename("covered_regions_production_bcf_per_day")
    )
    long = long.merge(direct_total, on="production_month", how="left")
    long["production_share_lower48"] = (
        long["marketed_gas_production_bcf_per_day"]
        / long["lower48_marketed_gas_production_bcf_per_day"]
    )
    long["production_share_covered_regions"] = (
        long["marketed_gas_production_bcf_per_day"]
        / long["covered_regions_production_bcf_per_day"]
    )
    # Conservative proxy: reference month M becomes usable on M+3 month start.
    long["weight_available_date_proxy"] = (
        long["production_month"] + pd.DateOffset(months=3)
    )
    for column in [
        "production_month",
        "weight_available_date_proxy",
    ]:
        long[column] = long[column].astype("datetime64[ns]")
    long["source_url"] = EIA_SOURCE_URL
    long["source_release_date"] = pd.Timestamp(EIA_SOURCE_RELEASE_DATE)
    long["source_vintage"] = EIA_SOURCE_RELEASE_DATE.isoformat()
    long["weight_vintage_point_in_time"] = False
    long["availability_is_proxy"] = True
    long["units"] = "billion cubic feet per day"
    return long.sort_values(
        ["production_month", "eia_production_region"]
    ).reset_index(drop=True)


def read_gcs_parquets(
    fs: gcsfs.GCSFileSystem, pattern: str
) -> pd.DataFrame:
    keys = sorted(fs.glob(pattern))
    if not keys:
        raise FileNotFoundError(pattern)
    parts = []
    for key in keys:
        with fs.open(key, "rb") as handle:
            parts.append(pd.read_parquet(handle))
    return pd.concat(parts, ignore_index=True)


def attach_causal_production_weights(
    weather: pd.DataFrame, weights: pd.DataFrame
) -> pd.DataFrame:
    weather = weather.copy()
    weather["nominal_issue_date"] = pd.to_datetime(
        weather["nominal_issue_date"]
    ).astype("datetime64[ns]")
    weather["target_date"] = pd.to_datetime(
        weather["target_date"]
    ).astype("datetime64[ns]")
    direct = weather.loc[
        weather["production_region"].isin(EIA_DIRECT_REGIONS)
    ].copy()
    weight_columns = [
        "eia_production_region", "production_month",
        "weight_available_date_proxy",
        "marketed_gas_production_bcf_per_day",
        "lower48_marketed_gas_production_bcf_per_day",
        "covered_regions_production_bcf_per_day",
        "production_share_lower48",
        "production_share_covered_regions",
        "source_release_date", "source_vintage",
        "weight_vintage_point_in_time", "availability_is_proxy",
    ]
    merged_parts = []
    for region, region_weather in direct.groupby(
        "production_region", sort=False
    ):
        region_weights = weights.loc[
            weights["eia_production_region"].eq(region), weight_columns
        ].sort_values("weight_available_date_proxy")
        merged = pd.merge_asof(
            region_weather.sort_values("nominal_issue_date"),
            region_weights,
            left_on="nominal_issue_date",
            right_on="weight_available_date_proxy",
            direction="backward",
        )
        if not merged["eia_production_region"].eq(region).all():
            raise RuntimeError(f"missing production weights for {region}")
        merged_parts.append(merged)
    result = pd.concat(merged_parts, ignore_index=True)
    result["production_weighted"] = True
    result["weight_method"] = (
        "EIA STEO monthly marketed gas production, lagged 3 months"
    )
    result["weather_point_aggregation"] = (
        "equal weight within production region"
    )
    return result.sort_values(
        ["nominal_issue_date", "target_date", "production_region", "lead_days"]
    ).reset_index(drop=True)


def build_weighted_valid_date_panel(
    weighted_regions: pd.DataFrame,
) -> pd.DataFrame:
    data = weighted_regions.copy()
    risk = data["region_mean_heuristic_freezeoff_weather_risk"]
    data["risk_contribution_lower48"] = (
        risk * data["production_share_lower48"]
    )
    data["risk_contribution_covered"] = (
        risk * data["production_share_covered_regions"]
    )
    data["hours_m5_contribution_lower48"] = (
        data["region_mean_hours_below_minus5c"]
        * data["production_share_lower48"]
    )
    data["hours_m10_contribution_lower48"] = (
        data["region_mean_hours_below_minus10c"]
        * data["production_share_lower48"]
    )
    # Continuous, piecewise-linear temperature severity. Temperatures just
    # below freezing contribute a little; deeper cold adds progressively more
    # through the nested -5 C and -10 C degree-hour terms.
    data["continuous_temperature_severity"] = (
        continuous_temperature_severity(data)
    )
    data["continuous_temperature_contribution_lower48"] = (
        data["continuous_temperature_severity"]
        * data["production_share_lower48"]
    )
    data["local_cold_contribution_lower48"] = (
        data["local_cold_severity"]
        * data["production_share_lower48"]
    )
    data["severe_freeze_share_lower48"] = np.where(
        data["region_mean_hours_below_minus5c"].ge(12),
        data["production_share_lower48"],
        0.0,
    )
    data["extreme_freeze_share_lower48"] = np.where(
        data["region_mean_hours_below_minus10c"].ge(12),
        data["production_share_lower48"],
        0.0,
    )

    keys = ["nominal_issue_date", "target_date", "lead_days"]
    panel = (
        data.groupby(keys, as_index=False)
        .agg(
            weighted_freezeoff_risk_lower48_exposure=(
                "risk_contribution_lower48", "sum"
            ),
            weighted_freezeoff_risk_covered_mean=(
                "risk_contribution_covered", "sum"
            ),
            weighted_hours_below_m5_lower48_exposure=(
                "hours_m5_contribution_lower48", "sum"
            ),
            weighted_hours_below_m10_lower48_exposure=(
                "hours_m10_contribution_lower48", "sum"
            ),
            weighted_continuous_temperature_severity_lower48=(
                "continuous_temperature_contribution_lower48", "sum"
            ),
            weighted_local_cold_severity_lower48=(
                "local_cold_contribution_lower48",
                lambda values: complete_sum(
                    values, len(EIA_DIRECT_REGIONS)
                ),
            ),
            production_share_severe_freeze_lower48=(
                "severe_freeze_share_lower48", "sum"
            ),
            production_share_extreme_freeze_lower48=(
                "extreme_freeze_share_lower48", "sum"
            ),
            production_coverage_share_lower48=(
                "production_share_lower48", "sum"
            ),
            covered_production_bcf_per_day=(
                "marketed_gas_production_bcf_per_day", "sum"
            ),
            lower48_production_bcf_per_day=(
                "lower48_marketed_gas_production_bcf_per_day", "first"
            ),
            region_count=("production_region", "nunique"),
            production_weight_month=("production_month", "max"),
            weight_available_date_proxy=(
                "weight_available_date_proxy", "max"
            ),
        )
    )
    panel["production_weighted"] = True
    panel["weight_vintage_point_in_time"] = False
    panel["historical_weather_availability_verified"] = False
    return panel.sort_values(keys).reset_index(drop=True)


def build_temperature_revision_panel(
    weighted_regions: pd.DataFrame,
) -> pd.DataFrame:
    """Same-valid-date temperature revisions using current production weights."""
    data = weighted_regions.copy()
    data["continuous_temperature_severity"] = (
        continuous_temperature_severity(data)
    )
    current = data.loc[
        data["lead_days"].between(1, 4),
        [
            "nominal_issue_date",
            "target_date",
            "lead_days",
            "production_region",
            "continuous_temperature_severity",
            "local_cold_severity",
            "local_mean_cold_q10_c",
            "local_mean_iqr_c",
            "local_min_cold_q10_c",
            "local_min_iqr_c",
            "production_share_lower48",
        ],
    ].copy()
    previous = data[
        [
            "nominal_issue_date",
            "target_date",
            "production_region",
            "continuous_temperature_severity",
            "region_temperature_mean_c",
            "region_coldest_point_min_c",
        ]
    ].copy()
    previous["nominal_issue_date"] += pd.Timedelta(days=1)
    previous = previous.rename(
        columns={
            "continuous_temperature_severity": (
                "previous_continuous_temperature_severity"
            ),
            "region_temperature_mean_c": (
                "previous_region_temperature_mean_c"
            ),
            "region_coldest_point_min_c": (
                "previous_region_coldest_point_min_c"
            ),
        }
    )
    revisions = current.merge(
        previous,
        on=[
            "nominal_issue_date",
            "target_date",
            "production_region",
        ],
        how="left",
    )
    revisions["temperature_revision_contribution_lower48"] = (
        (
            revisions["continuous_temperature_severity"]
            - revisions["previous_continuous_temperature_severity"]
        )
        * revisions["production_share_lower48"]
    )
    previous_mean_severity = (
        (
            revisions["local_mean_cold_q10_c"]
            - revisions["previous_region_temperature_mean_c"]
        )
        / revisions["local_mean_iqr_c"].replace(0, np.nan)
    ).clip(lower=0)
    previous_min_severity = (
        (
            revisions["local_min_cold_q10_c"]
            - revisions["previous_region_coldest_point_min_c"]
        )
        / revisions["local_min_iqr_c"].replace(0, np.nan)
    ).clip(lower=0)
    revisions["previous_local_cold_severity_current_standard"] = (
        0.5 * previous_mean_severity + 0.5 * previous_min_severity
    )
    revisions["local_cold_revision_contribution_lower48"] = (
        (
            revisions["local_cold_severity"]
            - revisions[
                "previous_local_cold_severity_current_standard"
            ]
        )
        * revisions["production_share_lower48"]
    )
    keys = ["nominal_issue_date", "target_date", "lead_days"]
    return (
        revisions.groupby(keys, as_index=False)
        .agg(
            continuous_temperature_revision=(
                "temperature_revision_contribution_lower48",
                lambda values: complete_sum(values, len(EIA_DIRECT_REGIONS)),
            ),
            local_cold_revision=(
                "local_cold_revision_contribution_lower48",
                lambda values: complete_sum(values, len(EIA_DIRECT_REGIONS)),
            ),
            revision_region_count=("production_region", "nunique"),
        )
        .sort_values(keys)
        .reset_index(drop=True)
    )


def complete_sum(values: pd.Series, required: int) -> float:
    return values.sum() if values.notna().sum() == required else np.nan


def build_issue_factors(
    valid_panel: pd.DataFrame,
    temperature_revision_panel: pd.DataFrame,
) -> pd.DataFrame:
    level = (
        valid_panel.groupby("nominal_issue_date")
        .agg(
            prod_freeze_risk_5d_l48_exposure=(
                "weighted_freezeoff_risk_lower48_exposure",
                lambda values: complete_sum(values, 5),
            ),
            prod_freeze_risk_5d_covered_mean=(
                "weighted_freezeoff_risk_covered_mean",
                lambda values: complete_sum(values, 5),
            ),
            prod_freeze_hours_m5_5d_l48_exposure=(
                "weighted_hours_below_m5_lower48_exposure",
                lambda values: complete_sum(values, 5),
            ),
            prod_freeze_hours_m10_5d_l48_exposure=(
                "weighted_hours_below_m10_lower48_exposure",
                lambda values: complete_sum(values, 5),
            ),
            prod_freeze_severe_share_l48_max=(
                "production_share_severe_freeze_lower48", "max"
            ),
            prod_freeze_extreme_share_l48_max=(
                "production_share_extreme_freeze_lower48", "max"
            ),
            prod_freeze_coverage_share_l48=(
                "production_coverage_share_lower48", "min"
            ),
            prod_freeze_weight_month=("production_weight_month", "max"),
            prod_freeze_lead_count=("lead_days", "nunique"),
            prod_freeze_region_count=("region_count", "min"),
        )
        .reset_index()
    )
    temperature_level_wide = valid_panel.pivot(
        index="nominal_issue_date",
        columns="lead_days",
        values="weighted_continuous_temperature_severity_lower48",
    ).rename(
        columns={
            lead: f"prod_freeze_temp_level_lead{lead}"
            for lead in range(1, 6)
        }
    )
    temperature_level_columns = {
        lead: f"prod_freeze_temp_level_lead{lead}"
        for lead in range(1, 6)
    }
    level = level.merge(
        temperature_level_wide.reset_index(),
        on="nominal_issue_date",
        how="left",
    )
    level["prod_freeze_temp_level_5d_continuous"] = level.apply(
        complete_weighted_sum,
        axis=1,
        columns=temperature_level_columns,
        weights=LEVEL_LEAD_WEIGHTS,
    )
    local_level_wide = valid_panel.pivot(
        index="nominal_issue_date",
        columns="lead_days",
        values="weighted_local_cold_severity_lower48",
    ).rename(
        columns={
            lead: f"prod_freeze_local_level_lead{lead}"
            for lead in range(1, 6)
        }
    )
    local_level_columns = {
        lead: f"prod_freeze_local_level_lead{lead}"
        for lead in range(1, 6)
    }
    level = level.merge(
        local_level_wide.reset_index(),
        on="nominal_issue_date",
        how="left",
    )
    level["prod_freeze_local_level_5d_continuous"] = level.apply(
        complete_weighted_sum,
        axis=1,
        columns=local_level_columns,
        weights=LEVEL_LEAD_WEIGHTS,
    )

    # Same-valid-date revision: today's leads 1-4 against yesterday's
    # leads 2-5 for the same target dates.
    previous = valid_panel[
        [
            "nominal_issue_date", "target_date",
            "weighted_freezeoff_risk_lower48_exposure",
            "weighted_hours_below_m5_lower48_exposure",
        ]
    ].copy()
    previous["nominal_issue_date"] += pd.Timedelta(days=1)
    previous = previous.rename(
        columns={
            "weighted_freezeoff_risk_lower48_exposure": (
                "previous_weighted_freezeoff_risk"
            ),
            "weighted_hours_below_m5_lower48_exposure": (
                "previous_weighted_hours_m5"
            ),
        }
    )
    overlap = valid_panel.loc[
        valid_panel["lead_days"].between(1, 4)
    ].merge(previous, on=["nominal_issue_date", "target_date"], how="left")
    overlap["risk_revision"] = (
        overlap["weighted_freezeoff_risk_lower48_exposure"]
        - overlap["previous_weighted_freezeoff_risk"]
    )
    overlap["hours_m5_revision"] = (
        overlap["weighted_hours_below_m5_lower48_exposure"]
        - overlap["previous_weighted_hours_m5"]
    )
    revision = (
        overlap.groupby("nominal_issue_date")
        .agg(
            prod_freeze_risk_revision_4d_l48=(
                "risk_revision",
                lambda values: complete_sum(values, 4),
            ),
            prod_freeze_hours_m5_revision_4d_l48=(
                "hours_m5_revision",
                lambda values: complete_sum(values, 4),
            ),
            prod_freeze_revision_count=("risk_revision", "count"),
        )
        .reset_index()
    )
    temperature_revision_wide = temperature_revision_panel.pivot(
        index="nominal_issue_date",
        columns="lead_days",
        values="continuous_temperature_revision",
    ).rename(
        columns={
            lead: f"prod_freeze_temp_revision_lead{lead}"
            for lead in range(1, 5)
        }
    )
    temperature_revision_columns = {
        lead: f"prod_freeze_temp_revision_lead{lead}"
        for lead in range(1, 5)
    }
    revision = revision.merge(
        temperature_revision_wide.reset_index(),
        on="nominal_issue_date",
        how="left",
    )
    revision["prod_freeze_temp_revision_4d_continuous"] = revision.apply(
        complete_weighted_sum,
        axis=1,
        columns=temperature_revision_columns,
        weights=REVISION_LEAD_WEIGHTS,
    )
    local_revision_wide = temperature_revision_panel.pivot(
        index="nominal_issue_date",
        columns="lead_days",
        values="local_cold_revision",
    ).rename(
        columns={
            lead: f"prod_freeze_local_revision_lead{lead}"
            for lead in range(1, 5)
        }
    )
    local_revision_columns = {
        lead: f"prod_freeze_local_revision_lead{lead}"
        for lead in range(1, 5)
    }
    revision = revision.merge(
        local_revision_wide.reset_index(),
        on="nominal_issue_date",
        how="left",
    )
    revision["prod_freeze_local_revision_4d_continuous"] = (
        revision.apply(
            complete_weighted_sum,
            axis=1,
            columns=local_revision_columns,
            weights=REVISION_LEAD_WEIGHTS,
        )
    )
    factors = level.merge(revision, on="nominal_issue_date", how="left")
    factors["prod_freeze_risk_5d_log"] = np.log1p(
        factors["prod_freeze_risk_5d_l48_exposure"]
    )
    factors["prod_freeze_risk_revision_4d_signed_log"] = (
        np.sign(factors["prod_freeze_risk_revision_4d_l48"])
        * np.log1p(
            factors["prod_freeze_risk_revision_4d_l48"].abs()
        )
    )
    # One-sided, unbounded event factor.  The input unit is the sum over the
    # five-day horizon of Lower-48 production share exposed to temperatures
    # below -5 C.  A score of 1 therefore means the exposure is 20 units above
    # the extreme-event threshold; ordinary cold weather remains neutral.
    factors["prod_freeze_extreme_event_score"] = (
        (
            factors["prod_freeze_hours_m5_5d_l48_exposure"]
            - EXTREME_EVENT_M5_EXPOSURE_THRESHOLD
        ).clip(lower=0.0)
        / EXTREME_EVENT_M5_EXPOSURE_THRESHOLD
    )
    factors["prod_freeze_temp_level_5d_log"] = np.log1p(
        factors["prod_freeze_temp_level_5d_continuous"]
    )
    factors["prod_freeze_temp_revision_4d_signed_log"] = (
        np.sign(factors["prod_freeze_temp_revision_4d_continuous"])
        * np.log1p(
            factors["prod_freeze_temp_revision_4d_continuous"].abs()
        )
    )
    factors["prod_freeze_temp_level_score"] = causal_quantile_scale(
        factors["prod_freeze_temp_level_5d_log"]
    )
    factors["prod_freeze_temp_revision_score"] = causal_quantile_scale(
        factors["prod_freeze_temp_revision_4d_signed_log"],
        absolute_scale=True,
    )
    factors["prod_freeze_continuous_score"] = (
        factors["prod_freeze_temp_level_score"]
        + 0.25 * factors["prod_freeze_temp_revision_score"]
    )
    factors["prod_freeze_local_level_5d_log"] = np.log1p(
        factors["prod_freeze_local_level_5d_continuous"]
    )
    factors["prod_freeze_local_revision_4d_signed_log"] = (
        np.sign(factors["prod_freeze_local_revision_4d_continuous"])
        * np.log1p(
            factors["prod_freeze_local_revision_4d_continuous"].abs()
        )
    )
    factors["prod_freeze_local_level_score"] = causal_quantile_scale(
        factors["prod_freeze_local_level_5d_log"]
    )
    factors["prod_freeze_local_revision_score"] = (
        causal_quantile_scale(
            factors["prod_freeze_local_revision_4d_signed_log"],
            absolute_scale=True,
        )
    )
    factors["prod_freeze_local_continuous_score"] = (
        factors["prod_freeze_local_level_score"]
        + 0.25 * factors["prod_freeze_local_revision_score"]
    )
    causal_columns = [
        "prod_freeze_risk_5d_log",
        "prod_freeze_risk_revision_4d_signed_log",
        "prod_freeze_hours_m5_5d_l48_exposure",
        "prod_freeze_hours_m5_revision_4d_l48",
        "prod_freeze_severe_share_l48_max",
    ]
    for column in causal_columns:
        factors[f"{column}_causal_z"] = causal_z(factors[column])
    factors["factor_date"] = factors["nominal_issue_date"]
    factors["factor_orientation"] = (
        "positive means higher production freeze-off risk and is bullish gas"
    )
    factors["production_weighted"] = True
    factors["weight_vintage_point_in_time"] = False
    factors["historical_weather_availability_verified"] = False
    return factors.sort_values("factor_date").reset_index(drop=True)


def build_diagnostics(
    fs: gcsfs.GCSFileSystem, factors: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame]:
    with fs.open(FUTURES_KEY, "rb") as handle:
        futures = pd.read_parquet(
            handle, columns=["date", "roll_adjusted_return"]
        )
    futures["date"] = pd.to_datetime(futures["date"])
    diagnostic = futures.merge(
        factors, left_on="date", right_on="factor_date", how="inner"
    ).sort_values("date").reset_index(drop=True)
    diagnostic["next_c1_return"] = diagnostic[
        "roll_adjusted_return"
    ].shift(-1)
    factor_columns = [
        "prod_freeze_local_continuous_score",
        "prod_freeze_local_level_5d_continuous",
        "prod_freeze_local_revision_4d_continuous",
        *[
            f"prod_freeze_local_level_lead{lead}"
            for lead in range(1, 6)
        ],
        *[
            f"prod_freeze_local_revision_lead{lead}"
            for lead in range(1, 5)
        ],
        "prod_freeze_continuous_score",
        "prod_freeze_temp_level_5d_continuous",
        "prod_freeze_temp_revision_4d_continuous",
        *[
            f"prod_freeze_temp_level_lead{lead}"
            for lead in range(1, 6)
        ],
        *[
            f"prod_freeze_temp_revision_lead{lead}"
            for lead in range(1, 5)
        ],
        "prod_freeze_extreme_event_score",
        "prod_freeze_risk_5d_log",
        "prod_freeze_risk_revision_4d_signed_log",
        "prod_freeze_hours_m5_5d_l48_exposure",
        "prod_freeze_hours_m5_revision_4d_l48",
        "prod_freeze_severe_share_l48_max",
    ]
    rows = []
    for column in factor_columns:
        sample = diagnostic[[column, "next_c1_return"]].dropna()
        winter = diagnostic.loc[
            diagnostic["date"].dt.month.isin([10, 11, 12, 1, 2, 3]),
            [column, "next_c1_return"],
        ].dropna()
        active = sample.loc[sample[column].ne(0)]
        rows.append(
            {
                "factor": column,
                "observations": len(sample),
                "active_observations": len(active),
                "pearson_all": sample[column].corr(
                    sample["next_c1_return"]
                ),
                "spearman_all": sample[column].corr(
                    sample["next_c1_return"], method="spearman"
                ),
                "pearson_winter": winter[column].corr(
                    winter["next_c1_return"]
                ),
                "spearman_winter": winter[column].corr(
                    winter["next_c1_return"], method="spearman"
                ),
                "pearson_active": active[column].corr(
                    active["next_c1_return"]
                ),
                "mean_next_return_active": active[
                    "next_c1_return"
                ].mean(),
            }
        )
    return diagnostic, pd.DataFrame(rows)


def write_frame(
    fs: gcsfs.GCSFileSystem,
    frame: pd.DataFrame,
    key: str,
    local_dir: Path,
    *,
    upload: bool = False,
    overwrite: bool = False,
) -> None:
    local_dir.mkdir(parents=True, exist_ok=True)
    name = key.rsplit("/", 1)[-1]
    path = local_dir / name
    if path.exists() and not overwrite:
        raise FileExistsError(
            f"{path} already exists; pass --overwrite to replace it"
        )
    if name.endswith(".parquet"):
        frame.to_parquet(path, index=False)
    else:
        frame.to_csv(path, index=False)
    print(f"saved {path} | rows={len(frame):,}")
    if upload:
        fs.put_file(str(path), key)
        print(f"uploaded gs://{key}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--local-dir",
        type=Path,
        default=(
            Path(__file__).resolve().parent
            / "processed"
            / "production_freezeoff_factors"
        ),
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="Explicitly upload local outputs to the configured GCS keys.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace existing local files. Off by default.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    fs = gcsfs.GCSFileSystem()

    workbook = download_eia_workbook()
    workbook_path = args.local_dir / "Fig44.xlsx"
    workbook_path.parent.mkdir(parents=True, exist_ok=True)
    if workbook_path.exists() and not args.overwrite:
        raise FileExistsError(
            f"{workbook_path} already exists; pass --overwrite to replace it"
        )
    workbook_path.write_bytes(workbook)
    print(f"saved {workbook_path}")
    if args.upload:
        fs.put_file(str(workbook_path), f"{RAW_EIA_PREFIX}/Fig44.xlsx")
        print(f"uploaded gs://{RAW_EIA_PREFIX}/Fig44.xlsx")

    weights = parse_eia_regional_production(workbook)
    weather = read_gcs_parquets(fs, WEATHER_GLOB)
    weighted_regions = attach_causal_production_weights(weather, weights)
    local_climatology = build_local_temperature_climatology(
        weighted_regions
    )
    weighted_regions = attach_local_temperature_standard(
        weighted_regions, local_climatology
    )
    valid_panel = build_weighted_valid_date_panel(weighted_regions)
    temperature_revision_panel = build_temperature_revision_panel(
        weighted_regions
    )
    factors = build_issue_factors(valid_panel, temperature_revision_panel)
    diagnostic, ic = build_diagnostics(fs, factors)

    outputs = {
        "eia_regional_marketed_gas_production_weights.parquet": weights,
        "production_local_temperature_climatology.parquet": (
            local_climatology
        ),
        "production_weighted_region_daily.parquet": weighted_regions,
        "production_weighted_valid_date_daily.parquet": valid_panel,
        "production_temperature_revision_valid_date_daily.parquet": (
            temperature_revision_panel
        ),
        "production_freezeoff_factors_daily.parquet": factors,
        "production_freezeoff_factor_diagnostic_daily.parquet": diagnostic,
        "production_freezeoff_factor_ic.csv": ic,
    }
    for name, frame in outputs.items():
        write_frame(
            fs,
            frame,
            f"{OUTPUT_PREFIX}/{name}",
            args.local_dir,
            upload=args.upload,
            overwrite=args.overwrite,
        )

    manifest = {
        "dataset": "EIA-production-weighted freeze-off weather factors",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "eia_source_url": EIA_SOURCE_URL,
        "eia_source_release_date": EIA_SOURCE_RELEASE_DATE.isoformat(),
        "eia_direct_regions": list(EIA_DIRECT_REGIONS),
        "excluded_from_national_weighted_factor": [
            "Anadarko",
            "Rockies",
        ],
        "exclusion_reason": (
            "EIA publishes them only inside aggregate Other; no arbitrary "
            "split was imposed."
        ),
        "weight_method": (
            "monthly marketed gas production, lagged three months"
        ),
        "weight_vintage_point_in_time": False,
        "historical_weather_availability_verified": False,
        "factor_orientation": (
            "positive means greater modeled production freeze-off risk, "
            "bullish natural gas"
        ),
        "primary_factor": "prod_freeze_extreme_event_score",
        "primary_factor_definition": (
            "max(weighted five-day hours below -5 C exposure - 20, 0) / 20; "
            "one-sided and unbounded"
        ),
        "continuous_factor": "prod_freeze_local_continuous_score",
        "legacy_absolute_temperature_factor": (
            "prod_freeze_continuous_score"
        ),
        "continuous_temperature_formula": (
            "0.25 * degree-hours below 0 C + 0.50 * degree-hours below "
            "-5 C + 1.00 * degree-hours below -10 C"
        ),
        "continuous_level_lead_weights": LEVEL_LEAD_WEIGHTS,
        "continuous_revision_lead_weights": REVISION_LEAD_WEIGHTS,
        "continuous_factor_definition": (
            "causally scaled five-day continuous temperature level plus "
            "0.25 times the causally scaled same-valid-date revision"
        ),
        "local_temperature_factor": (
            "prod_freeze_local_continuous_score"
        ),
        "local_temperature_standard": {
            "lookback_complete_years": LOCAL_CLIMATE_LOOKBACK_YEARS,
            "season_window_days_each_side": (
                LOCAL_CLIMATE_SEASON_WINDOW_DAYS
            ),
            "minimum_observations": LOCAL_CLIMATE_MIN_OBSERVATIONS,
            "cold_quantile": LOCAL_CLIMATE_COLD_QUANTILE,
            "scale": "local interquartile range",
            "point_in_time_rule": (
                "only target temperatures from years before issue year"
            ),
        },
        "limitations": [
            "Weather risk proxy, not measured production loss.",
            "EIA workbook is a current revised/backcast vintage.",
            "Three-month lag limits label leakage but not revision-vintage leakage.",
            "Exact historical Open-Meteo API availability is not verified.",
            "Within each production region, weather sample points are equal weighted.",
            (
                "Local temperature standards use prior lead-1 forecasts, "
                "not measured station observations or official normals."
            ),
        ],
        "outputs": list(outputs),
    }
    manifest_path = args.local_dir / "manifest.json"
    if manifest_path.exists() and not args.overwrite:
        raise FileExistsError(
            f"{manifest_path} already exists; pass --overwrite to replace it"
        )
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"saved {manifest_path}")
    if args.upload:
        fs.put_file(str(manifest_path), f"{OUTPUT_PREFIX}/manifest.json")
        print(f"uploaded gs://{OUTPUT_PREFIX}/manifest.json")
    print("\nIC diagnostic")
    print(ic.to_string(index=False))


if __name__ == "__main__":
    main()
