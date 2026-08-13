#!/usr/bin/env python3
"""Build the Henry Hub multi-signal master panel from its direct inputs.

The module contains only feature construction and point-in-time alignment.
It intentionally excludes notebook plots, strategy backtests, and automatic
cloud writes. The command line defaults to a local, ignored output path and
refuses to replace an existing file unless --overwrite is supplied. A GCS
upload happens only when --upload is explicitly supplied.

The monthly fundamentals and EIA production weights are revised/backcast
histories rather than archived vintages. This script preserves the original
conservative availability lags, but cannot turn those inputs into vintage-
correct histories.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import io
import json
import os
import tempfile
from pathlib import Path
from typing import Sequence

import gcsfs
import numpy as np
import pandas as pd

from naturalgas.eia_storage_release_calendar import wngsr_release_calendar

try:
    from naturalgas.nymex_session_calendar import (
        CONFIRMED_NON_SESSION_DATES,
        filter_confirmed_nymex_sessions,
    )
    from naturalgas.weather_feature_policy import (
        DIAGNOSTIC_GFS_WEATHER_COMPONENTS,
        MAX_WEATHER_STALENESS_DAYS,
        PRIMARY_WEATHER_COMPONENTS,
        component_coverage,
        fixed_weight_mean,
    )
except ModuleNotFoundError:
    from nymex_session_calendar import (
        CONFIRMED_NON_SESSION_DATES,
        filter_confirmed_nymex_sessions,
    )
    from weather_feature_policy import (
        DIAGNOSTIC_GFS_WEATHER_COMPONENTS,
        MAX_WEATHER_STALENESS_DAYS,
        PRIMARY_WEATHER_COMPONENTS,
        component_coverage,
        fixed_weight_mean,
    )


BUCKET = "bcli-natgas-data-497807"
DEFAULT_INPUTS = {
    "daily_features": f"gs://{BUCKET}/features/daily_features.parquet",
    "fundamentals_monthly": (
        f"gs://{BUCKET}/processed/fundamentals_monthly.parquet"
    ),
    "storage_weekly": f"gs://{BUCKET}/processed/storage_weekly.parquet",
    "lng_trade": (
        f"gs://{BUCKET}/raw/eia/trade_detail/country_monthly.parquet"
    ),
    "cpc_features": (
        f"gs://{BUCKET}/processed/ng_weather_signals/"
        "ng_hdd_cdd_features.parquet"
    ),
    "futures_daily": (
        f"gs://{BUCKET}/processed/ng_hh_futures/"
        "ng_hh_futures_daily.parquet"
    ),
    "gfs_daily_glob": (
        f"gs://{BUCKET}/processed/weather/open_meteo/previous_runs/"
        "model=ncep_gfs_seamless/daily/year=*/month=*/data.parquet"
    ),
    "freezeoff_factors": (
        f"gs://{BUCKET}/processed/weather/production_freezeoff_factors/"
        "model=ncep_gfs_seamless/production_freezeoff_factors_daily.parquet"
    ),
}
DEFAULT_LOCAL_OUTPUT = (
    Path(__file__).resolve().parent
    / "processed"
    / "ng_multisignal_score"
    / "ng_multisignal_panel.parquet"
)
DEFAULT_GCS_OUTPUT = (
    f"gs://{BUCKET}/processed/ng_multisignal_score/"
    "ng_multisignal_panel.parquet"
)
DEFAULT_GCS_AUDIT_OUTPUT = (
    f"gs://{BUCKET}/processed/ng_multisignal_score/"
    "ng_multisignal_weights_audit.csv"
)
DEFAULT_INPUT_MANIFEST = (
    Path(__file__).resolve().parents[1]
    / "manifests"
    / "master_panel_inputs_2026-07-13.json"
)

DAILY_Z_WINDOW = 252
DAILY_Z_MIN = 126
ISSUE_Z_WINDOW = 60
ISSUE_Z_MIN = 30
SCORE_DEADBAND = 0.25
CONFIDENCE_ACTIVE_MIN = 0.075
CONFIDENCE_HIGH_LONG_MIN = 0.184
CONFIDENCE_HIGH_SHORT_LOW = -0.144
CONFIDENCE_HIGH_SHORT_HIGH = -0.090
CONFIDENCE_HIGH_MULTIPLIER = 1.50
CONFIDENCE_LOW_MULTIPLIER = 0.50
FREEZEOFF_CONTROL_LEVEL_THRESHOLD = 1.0
FREEZEOFF_CONTROL_LONG_MULTIPLIER = 1.50

GROUP_WEIGHTS = {
    "weather_score": 0.35,
    "fundamental_score": 0.30,
    "market_score": 0.25,
    "macro_risk_score": 0.10,
}

GFS_COLUMNS = [
    "location_id",
    "target_date",
    "lead_days",
    "hdd65_f",
    "cdd65_f",
    "wind_speed_80m_mean_kmh",
    "cloud_cover_mean_pct",
]


def _gcs_key(uri: str) -> str:
    return uri[5:] if uri.startswith("gs://") else uri


def read_parquet(uri: str, columns: Sequence[str] | None = None) -> pd.DataFrame:
    """Read one local or GCS parquet without writing external state."""
    if uri.startswith("gs://"):
        fs = gcsfs.GCSFileSystem()
        with fs.open(_gcs_key(uri), "rb") as handle:
            return pd.read_parquet(handle, columns=columns)
    return pd.read_parquet(Path(uri), columns=columns)


def read_partitioned_parquets(
    pattern: str,
    columns: Sequence[str] | None = None,
) -> pd.DataFrame:
    """Read and concatenate all partitions matched by a local or GCS glob."""
    frames = []
    if pattern.startswith("gs://"):
        fs = gcsfs.GCSFileSystem()
        keys = sorted(fs.glob(_gcs_key(pattern)))
        for key in keys:
            with fs.open(key, "rb") as handle:
                frames.append(
                    pd.read_parquet(handle, columns=columns)
                )
    else:
        keys = sorted(glob.glob(pattern))
        frames = [
            pd.read_parquet(key, columns=columns) for key in keys
        ]
    if not keys:
        raise FileNotFoundError(f"no parquet partitions matched {pattern}")
    return pd.concat(frames, ignore_index=True)


def load_input_manifest(path: Path) -> dict:
    """Load and validate the generation-pinned direct-input contract."""
    manifest = json.loads(path.read_text(encoding="utf-8"))
    inputs = manifest.get("inputs", [])
    expected_ids = {
        "daily_features",
        "fundamentals_monthly",
        "storage_weekly",
        "lng_trade",
        "cpc_features",
        "futures_daily",
        "gfs_daily",
        "freezeoff_factors",
    }
    actual_ids = {item.get("id") for item in inputs}
    if manifest.get("input_count") != 8 or actual_ids != expected_ids:
        raise ValueError(
            "input manifest must contain exactly the eight direct inputs"
        )
    for item in inputs:
        if not item.get("required_columns"):
            raise ValueError(
                f"{item.get('id')} has no required_columns contract"
            )
        objects = item.get("objects", []) if item["partitioned"] else [item]
        if item["partitioned"] and len(objects) != item.get(
            "partition_count"
        ):
            raise ValueError(
                f"{item['id']} partition count does not match its inventory"
            )
        for obj in objects:
            for field in ("generation", "size_bytes", "sha256"):
                if not obj.get(field):
                    raise ValueError(
                        f"{item['id']} object is missing {field}"
                    )
    return manifest


def _read_pinned_parquet(
    fs: gcsfs.GCSFileSystem,
    obj: dict,
    *,
    columns: Sequence[str] | None = None,
) -> pd.DataFrame:
    """Read one exact GCS generation and verify size and SHA-256."""
    uri = obj["default_uri"] if "default_uri" in obj else obj["uri"]
    if not uri.startswith("gs://"):
        raise ValueError(f"pinned manifest URI must use gs://: {uri}")
    # Use a one-shot read so the pinned generation reaches the GCS media
    # request. gcsfs 2026.7.0 does not reliably propagate the generation
    # passed to GCSFileSystem.open through GCSFile._fetch_range.
    payload = fs.cat_file(
        _gcs_key(uri),
        generation=str(obj["generation"]),
        concurrency=1,
    )
    expected_size = int(obj["size_bytes"])
    if len(payload) != expected_size:
        raise ValueError(
            f"size mismatch for {uri}: {len(payload)} != {expected_size}"
        )
    actual_sha256 = hashlib.sha256(payload).hexdigest()
    if actual_sha256 != obj["sha256"]:
        raise ValueError(
            f"SHA-256 mismatch for {uri}: "
            f"{actual_sha256} != {obj['sha256']}"
        )
    return pd.read_parquet(io.BytesIO(payload), columns=columns)


def _require_columns(
    frame: pd.DataFrame,
    required: Sequence[str],
    dataset_id: str,
) -> None:
    missing = sorted(set(required).difference(frame.columns))
    if missing:
        raise ValueError(
            f"{dataset_id} is missing required columns: {missing}"
        )


def merge_point_in_time(
    left: pd.DataFrame,
    right: pd.DataFrame,
    *,
    left_date: str,
    available_date: str,
    tolerance_days: int | None = None,
) -> pd.DataFrame:
    """Backward as-of merge and assert that no future record was selected."""
    tolerance = (
        pd.Timedelta(days=tolerance_days)
        if tolerance_days is not None
        else None
    )
    merged = pd.merge_asof(
        left.sort_values(left_date),
        right.sort_values(available_date),
        left_on=left_date,
        right_on=available_date,
        direction="backward",
        tolerance=tolerance,
    )
    matched = merged[available_date].notna()
    if not (
        merged.loc[matched, available_date] <= merged.loc[matched, left_date]
    ).all():
        raise AssertionError(
            f"{available_date} must never be later than {left_date}"
        )
    return merged


def causal_z(series: pd.Series, window: int, min_periods: int) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce").astype(float)
    mean = values.shift(1).rolling(window, min_periods=min_periods).mean()
    std = values.shift(1).rolling(window, min_periods=min_periods).std()
    return (values - mean) / std.replace(0, np.nan)


def available_mean(frame: pd.DataFrame, columns: list[str]) -> pd.Series:
    return frame[columns].mean(axis=1, skipna=True)


def weighted_available(frame: pd.DataFrame, weights: dict[str, float]) -> pd.Series:
    numerator = pd.Series(0.0, index=frame.index)
    denominator = pd.Series(0.0, index=frame.index)
    for column, weight in weights.items():
        valid = frame[column].notna()
        numerator = numerator + frame[column].fillna(0) * weight
        denominator = denominator + valid.astype(float) * weight
    return numerator / denominator.replace(0, np.nan)

def build_gfs_features(gfs_raw: pd.DataFrame) -> pd.DataFrame:
    """Build causal issue-date GFS level and revision features."""
    gfs_raw = gfs_raw.copy()
    gfs_raw["target_date"] = pd.to_datetime(gfs_raw["target_date"])
    gfs_raw["lead_days"] = pd.to_numeric(gfs_raw["lead_days"]).astype(int)
    gfs_raw["nominal_issue_date"] = (
        gfs_raw["target_date"] - pd.to_timedelta(gfs_raw["lead_days"], unit="D")
    )
    gfs_raw["gdd65_f"] = gfs_raw["hdd65_f"] + gfs_raw["cdd65_f"]

    # Equal-weight cities for each issue/target/lead.
    gfs_city_mean = (
        gfs_raw.groupby(["nominal_issue_date", "target_date", "lead_days"], as_index=False)
        .agg(
            gdd65_f=("gdd65_f", "mean"),
            wind80_kmh=("wind_speed_80m_mean_kmh", "mean"),
            cloud_cover_pct=("cloud_cover_mean_pct", "mean"),
            location_count=("location_id", "nunique"),
        )
    )

    gfs_level = (
        gfs_city_mean.groupby("nominal_issue_date")
        .agg(
            gfs_gdd_5d=("gdd65_f", lambda values: values.sum(min_count=5)),
            gfs_wind80_5d=("wind80_kmh", lambda values: values.mean() if values.notna().sum() == 5 else np.nan),
            gfs_cloud_5d=("cloud_cover_pct", lambda values: values.mean() if values.notna().sum() == 5 else np.nan),
            gfs_lead_count=("lead_days", "nunique"),
            gfs_min_locations=("location_count", "min"),
        )
        .reset_index()
    )

    # Same-valid-date revision: current leads 1-4 vs previous issue's leads 2-5.
    gfs_previous = gfs_city_mean[["nominal_issue_date", "target_date", "gdd65_f"]].copy()
    gfs_previous["nominal_issue_date"] = gfs_previous["nominal_issue_date"] + pd.Timedelta(days=1)
    gfs_previous = gfs_previous.rename(columns={"gdd65_f": "gdd65_f_previous"})
    gfs_overlap = gfs_city_mean.loc[gfs_city_mean["lead_days"].between(1, 4)].merge(
        gfs_previous, on=["nominal_issue_date", "target_date"], how="left"
    )
    gfs_overlap["gdd_revision"] = gfs_overlap["gdd65_f"] - gfs_overlap["gdd65_f_previous"]
    gfs_revision = (
        gfs_overlap.groupby("nominal_issue_date")
        .agg(
            gfs_gdd_revision_4d=("gdd_revision", lambda values: values.sum(min_count=4)),
            gfs_revision_count=("gdd_revision", "count"),
        )
        .reset_index()
    )
    gfs_features = gfs_level.merge(gfs_revision, on="nominal_issue_date", how="left")
    for column in ["gfs_gdd_5d", "gfs_wind80_5d", "gfs_cloud_5d", "gfs_gdd_revision_4d"]:
        gfs_features[f"{column}_z"] = causal_z(
            gfs_features[column], ISSUE_Z_WINDOW, ISSUE_Z_MIN
        )
    gfs_features["nominal_issue_date"] = pd.to_datetime(gfs_features["nominal_issue_date"]).astype("datetime64[ns]")
    gfs_features["gfs_available_date"] = gfs_features["nominal_issue_date"]
    return gfs_features


def build_storage_4w_features(
    storage_weekly: pd.DataFrame,
) -> pd.DataFrame:
    """Build storage changes aligned to the actual WNGSR release calendar."""
    storage = storage_weekly.copy().sort_values("week_ending")
    storage["week_ending"] = pd.to_datetime(
        storage["week_ending"]
    ).astype("datetime64[ns]")
    storage["storage_4w_change_bcf"] = storage["lower48"].diff(4)
    storage["week_of_year"] = (
        storage["week_ending"].dt.isocalendar().week.astype(int)
    )
    storage["storage_4w_change_normal_bcf"] = (
        storage.groupby("week_of_year")["storage_4w_change_bcf"]
        .transform(
            lambda values: values.shift(1).rolling(
                5, min_periods=3
            ).mean()
        )
    )
    storage["storage_4w_change_surprise_bcf"] = (
        storage["storage_4w_change_bcf"]
        - storage["storage_4w_change_normal_bcf"]
    )
    storage["storage_4w_change_issue_z"] = causal_z(
        storage["storage_4w_change_surprise_bcf"], 104, 52
    )
    release_calendar = wngsr_release_calendar(storage["week_ending"])
    storage["storage_4w_available_date"] = release_calendar[
        "storage_available_date"
    ]
    return storage


FUNDAMENTAL_MERGE_COLUMNS = [
    "fundamentals_reference_month",
    "fundamentals_release_date_proxy",
    "fundamentals_available_date",
    "total_cons_yoy",
    "net_import_ratio",
    "dry_prod_mom",
    "total_cons_mom",
    "lng_exp_mom",
    "net_import_ratio_mom_change",
    "dry_prod_mom_issue_z",
    "total_cons_mom_issue_z",
    "lng_exp_mom_issue_z",
    "net_import_ratio_mom_change_issue_z",
]


def build_monthly_fundamental_features(
    fundamentals_monthly: pd.DataFrame,
    lng_trade: pd.DataFrame,
) -> pd.DataFrame:
    """Build monthly features with a conservative M+3 availability date."""
    fundamentals = fundamentals_monthly.copy().sort_values("month")
    fundamentals["month"] = pd.to_datetime(
        fundamentals["month"]
    ).astype("datetime64[ns]")
    fundamentals["days_in_month"] = (
        fundamentals["month"].dt.days_in_month.astype(float)
    )
    for column in ["dry_prod", "total_cons", "exports", "imports"]:
        rate = fundamentals[column] / fundamentals["days_in_month"]
        fundamentals[f"{column}_daily_rate"] = rate
        fundamentals[f"{column}_mom"] = rate.pct_change()
    fundamentals["total_cons_yoy"] = (
        fundamentals["total_cons"]
        / fundamentals["total_cons"].shift(12)
        - 1
    )
    fundamentals["net_import_ratio"] = (
        fundamentals["imports"] - fundamentals["exports"]
    ) / fundamentals["total_cons"]
    fundamentals["net_import_ratio_mom_change"] = fundamentals[
        "net_import_ratio"
    ].diff()

    lng_monthly = lng_trade.copy()
    lng_monthly = (
        lng_monthly.loc[
            lng_monthly["dataset"].eq("country_exports")
            & lng_monthly["is_us_aggregate"].fillna(False)
            & lng_monthly["process-name"].eq(
                "Liquefied Natural Gas Exports"
            )
            & lng_monthly["metric"].eq("volume"),
            ["month", "value"],
        ]
        .rename(columns={"value": "lng_exports"})
        .sort_values("month")
    )
    lng_monthly["month"] = pd.to_datetime(
        lng_monthly["month"]
    ).astype("datetime64[ns]")
    lng_monthly["lng_exports_daily_rate"] = (
        lng_monthly["lng_exports"]
        / lng_monthly["month"].dt.days_in_month
    )
    lng_monthly["lng_exp_mom"] = lng_monthly[
        "lng_exports_daily_rate"
    ].pct_change()
    fundamentals = fundamentals.merge(
        lng_monthly[["month", "lng_exp_mom"]],
        on="month",
        how="left",
    )
    for column in [
        "dry_prod_mom",
        "total_cons_mom",
        "lng_exp_mom",
        "net_import_ratio_mom_change",
    ]:
        fundamentals[f"{column}_issue_z"] = causal_z(
            fundamentals[column], 60, 36
        )

    # Reference month M becomes usable on the first day of M+3. This is a
    # conservative release proxy, not a historical-vintage guarantee.
    fundamentals["fundamentals_reference_month"] = fundamentals["month"]
    fundamentals["fundamentals_release_date_proxy"] = (
        fundamentals["month"]
        + pd.DateOffset(months=3)
        - pd.Timedelta(days=1)
    ).astype("datetime64[ns]")
    fundamentals["fundamentals_available_date"] = (
        fundamentals["fundamentals_release_date_proxy"]
        + pd.Timedelta(days=1)
    ).astype("datetime64[ns]")
    if not (
        fundamentals["fundamentals_available_date"]
        > fundamentals["month"] + pd.offsets.MonthEnd(0)
    ).all():
        raise AssertionError(
            "monthly fundamentals must be available after reference month"
        )
    return fundamentals


def build_panel_base(
    daily: pd.DataFrame,
    cpc: pd.DataFrame,
    gfs_features: pd.DataFrame,
    freezeoff: pd.DataFrame,
    storage_weekly: pd.DataFrame,
    fundamentals_monthly: pd.DataFrame,
    lng_trade: pd.DataFrame,
    futures_daily: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.Timestamp]:
    """Align every source to the NYMEX calendar at its availability date."""
    daily = daily.copy()
    daily["date"] = pd.to_datetime(daily["date"]).astype("datetime64[ns]")
    daily = daily.drop(columns=["target_next_ret", "target_5d"], errors="ignore")
    assert not any(column.startswith("target") for column in daily.columns)
    assert daily["date"].is_monotonic_increasing and daily["date"].is_unique
    REQUIRED_INPUT_CUTOFF = daily["date"].max()

    cpc = cpc.copy()
    cpc["issue_date"] = pd.to_datetime(cpc["issue_date"]).astype("datetime64[ns]")
    cpc["signal_available_date"] = pd.to_datetime(cpc["signal_available_date"]).astype("datetime64[ns]")
    cpc_columns = [
        "issue_date", "signal_available_date", "hdd_6d", "cdd_6d", "gdd_6d",
        "hdd_revision_5d", "cdd_revision_5d", "gdd_revision_5d",
        "hdd_revision_5d_z", "cdd_revision_5d_z", "gdd_revision_5d_z", "gdd_6d_z",
    ]

    panel = pd.merge_asof(
        daily.sort_values("date"),
        cpc[cpc_columns].sort_values("signal_available_date"),
        left_on="date",
        right_on="signal_available_date",
        direction="backward",
        tolerance=pd.Timedelta(days=MAX_WEATHER_STALENESS_DAYS),
    )
    panel["cpc_weather_age_days"] = (
        panel["date"] - panel["signal_available_date"]
    ).dt.days
    assert panel["cpc_weather_age_days"].dropna().le(
        MAX_WEATHER_STALENESS_DAYS
    ).all()
    panel = pd.merge_asof(
        panel.sort_values("date"),
        gfs_features.sort_values("gfs_available_date"),
        left_on="date",
        right_on="gfs_available_date",
        direction="backward",
        tolerance=pd.Timedelta(days=MAX_WEATHER_STALENESS_DAYS),
    )
    panel["gfs_weather_age_days"] = (
        panel["date"] - panel["gfs_available_date"]
    ).dt.days
    assert panel["gfs_weather_age_days"].dropna().le(
        MAX_WEATHER_STALENESS_DAYS
    ).all()

    # Production-region freeze-off risk uses local, prior-year seasonal temperature standards.
    # The final backtest shifts every score by one trading day, so a factor dated t
    # becomes a position on the following futures session.
    freezeoff = freezeoff.copy()
    freezeoff["freezeoff_available_date"] = pd.to_datetime(
        freezeoff["factor_date"]
    ).astype("datetime64[ns]")
    freezeoff_columns = [
        "freezeoff_available_date", "prod_freeze_local_continuous_score",
        "prod_freeze_local_level_score", "prod_freeze_local_revision_score",
        "prod_freeze_local_level_5d_continuous",
        "prod_freeze_local_revision_4d_continuous",
    ]
    panel = pd.merge_asof(
        panel.sort_values("date"),
        freezeoff[freezeoff_columns].sort_values("freezeoff_available_date"),
        left_on="date",
        right_on="freezeoff_available_date",
        direction="backward",
        tolerance=pd.Timedelta(days=MAX_WEATHER_STALENESS_DAYS),
    )
    panel["freezeoff_weather_age_days"] = (
        panel["date"] - panel["freezeoff_available_date"]
    ).dt.days
    freezeoff_rows = panel["freezeoff_available_date"].notna()
    assert (
        panel.loc[freezeoff_rows, "freezeoff_available_date"]
        <= panel.loc[freezeoff_rows, "date"]
    ).all()
    assert panel["freezeoff_weather_age_days"].dropna().le(
        MAX_WEATHER_STALENESS_DAYS
    ).all()

    # Weekly storage: add a roughly monthly (4-week) change and remove its seasonal norm.
    storage_monthly_change = build_storage_4w_features(storage_weekly)
    panel = pd.merge_asof(
        panel.sort_values("date"),
        storage_monthly_change[[
            "storage_4w_available_date", "storage_4w_change_bcf",
            "storage_4w_change_normal_bcf", "storage_4w_change_surprise_bcf",
            "storage_4w_change_issue_z",
        ]].sort_values("storage_4w_available_date"),
        left_on="date", right_on="storage_4w_available_date", direction="backward",
    )

    fundamentals = build_monthly_fundamental_features(
        fundamentals_monthly, lng_trade
    )
    assert (panel.loc[panel["issue_date"].notna(), "issue_date"] < panel.loc[panel["issue_date"].notna(), "date"]).all()
    assert (panel.loc[panel["nominal_issue_date"].notna(), "nominal_issue_date"] <= panel.loc[panel["nominal_issue_date"].notna(), "date"]).all()

    # Futures market features are known only after that day's settlement; the final position is shifted later.
    futures = futures_daily.copy()
    futures["date"] = pd.to_datetime(futures["date"]).astype("datetime64[ns]")
    futures = filter_confirmed_nymex_sessions(futures).sort_values("date").reset_index(drop=True)
    assert not futures["date"].isin(CONFIRMED_NON_SESSION_DATES).any()
    market_columns = [
        "date", "c1", "c2", "c4", "c2_c1_log_spread", "front_spot_log_basis", "return_20d",
        "roll_adjusted_return", "is_roll_switch",
    ]
    panel = futures[market_columns].merge(panel, on="date", how="left").sort_values("date").reset_index(drop=True)
    # Merge slow monthly fundamentals onto the final futures trading calendar.
    # This avoids losing the latest known monthly vintage when daily_features ends
    # before the futures panel does.
    panel = pd.merge_asof(
        panel.sort_values("date"),
        fundamentals[FUNDAMENTAL_MERGE_COLUMNS].sort_values("fundamentals_available_date"),
        left_on="date",
        right_on="fundamentals_available_date",
        direction="backward",
    ).reset_index(drop=True)
    fundamental_rows = panel["fundamentals_available_date"].notna()
    assert (
        panel.loc[fundamental_rows, "date"]
        >= panel.loc[fundamental_rows, "fundamentals_available_date"]
    ).all()
    panel["c4_c1_log_spread"] = np.log(panel["c4"] / panel["c1"])

    # The market layer may extend beyond the latest complete feature date.  Keep its
    # observed futures prices/returns, but do not carry or impute any strategy feature
    # beyond the explicit feature watermark.
    panel["required_input_cutoff"] = REQUIRED_INPUT_CUTOFF
    panel["is_after_required_input_cutoff"] = panel["date"].gt(REQUIRED_INPUT_CUTOFF)
    market_passthrough_columns = set(market_columns + [
        "c4_c1_log_spread", "required_input_cutoff", "is_after_required_input_cutoff",
    ])
    trailing_feature_columns = [
        column for column in panel.columns
        if column not in market_passthrough_columns
    ]
    panel.loc[panel["is_after_required_input_cutoff"], trailing_feature_columns] = np.nan
    assert panel.loc[
        panel["is_after_required_input_cutoff"],
        ["hh_spot", "ret_21d", "obs_hdd7_anom", "gpr_shock"],
    ].isna().all().all()
    return panel, REQUIRED_INPUT_CUTOFF

def add_panel_scores(
    panel: pd.DataFrame,
    required_input_cutoff: pd.Timestamp,
) -> tuple[pd.DataFrame, dict[str, list[str]]]:
    """Add causal signals, group scores, and completeness controls."""
    panel = panel.copy()
    # Causal z-scores for daily/weekly/monthly fields already aligned to availability.
    raw_daily_components = [
        "obs_hdd7_anom", "obs_cdd7_anom", "storage_dev", "storage_chg_surprise",
        "prod_yoy", "lng_exp_yoy", "total_cons_yoy", "net_import_ratio",
        "return_20d", "c2_c1_log_spread", "c4_c1_log_spread", "front_spot_log_basis", "ret_21d",
        "usd_ret_5d", "sp500_ret_5d", "term_spread_10y_3m", "gpr_shock",
    ]
    for column in raw_daily_components:
        panel[f"{column}_causal_z"] = causal_z(panel[column], DAILY_Z_WINDOW, DAILY_Z_MIN)

    month = panel["date"].dt.month
    seasonal_cpc_revision_z = np.select(
        [month.isin([10, 11, 12, 1, 2, 3]), month.isin([5, 6, 7, 8, 9])],
        [panel["hdd_revision_5d_z"], panel["cdd_revision_5d_z"]],
        default=panel["gdd_revision_5d_z"],
    )
    observed_gdd_anom = panel["obs_hdd7_anom"] + panel["obs_cdd7_anom"]
    observed_gdd_anom_z = causal_z(observed_gdd_anom, DAILY_Z_WINDOW, DAILY_Z_MIN)

    # Weather components.
    # Retain the all-season GDD revision for diagnostics/backward compatibility,
    # but never include it beside the seasonally selected revision in a strategy.
    panel["sig_cpc_revision"] = panel["gdd_revision_5d_z"]
    panel["sig_cpc_seasonal_revision"] = pd.Series(seasonal_cpc_revision_z, index=panel.index)
    panel["sig_cpc_level"] = panel["gdd_6d_z"]
    panel["sig_observed_weather"] = observed_gdd_anom_z
    panel["sig_gfs_revision"] = panel["gfs_gdd_revision_4d_z"]
    panel["sig_gfs_wind"] = -panel["gfs_wind80_5d_z"]
    panel["sig_gfs_cloud"] = panel["gfs_cloud_5d_z"]
    panel["sig_production_freezeoff"] = panel["prod_freeze_local_continuous_score"]

    # Inventory / supply-demand components.
    panel["sig_low_storage"] = -panel["storage_dev_causal_z"]
    panel["sig_storage_change"] = -panel["storage_chg_surprise_causal_z"]
    panel["sig_storage_4w_change"] = -panel["storage_4w_change_issue_z"]
    panel["sig_low_production_growth"] = -panel["prod_yoy_causal_z"]
    panel["sig_lng_export_growth"] = panel["lng_exp_yoy_causal_z"]
    panel["sig_consumption_growth"] = panel["total_cons_yoy_causal_z"]
    panel["sig_net_import_supply"] = -panel["net_import_ratio_causal_z"]
    # Monthly supply-demand changes are also raw causal z-scores.
    panel["sig_production_mom"] = -panel["dry_prod_mom_issue_z"]
    panel["sig_lng_export_mom"] = panel["lng_exp_mom_issue_z"]
    panel["sig_consumption_mom"] = panel["total_cons_mom_issue_z"]
    panel["sig_net_import_change"] = -panel["net_import_ratio_mom_change_issue_z"]

    # Market components.
    panel["sig_futures_momentum"] = panel["return_20d_causal_z"]
    panel["sig_backwardation_carry"] = -panel["c2_c1_log_spread_causal_z"]
    panel["sig_long_curve_carry"] = -panel["c4_c1_log_spread_causal_z"]
    panel["sig_front_spot_basis"] = -panel["front_spot_log_basis_causal_z"]
    panel["sig_spot_momentum"] = panel["ret_21d_causal_z"]

    # Macro / risk components; deliberately low total group weight.
    panel["sig_usd_weakness"] = -panel["usd_ret_5d_causal_z"]
    panel["sig_equity_growth"] = panel["sp500_ret_5d_causal_z"]
    panel["sig_term_spread"] = panel["term_spread_10y_3m_causal_z"]
    panel["sig_geopolitical_risk"] = panel["gpr_shock_causal_z"]

    WEATHER_COMPONENTS_WITHOUT_FREEZEOFF = list(PRIMARY_WEATHER_COMPONENTS)
    WEATHER_DIAGNOSTIC_COMPONENTS = list(DIAGNOSTIC_GFS_WEATHER_COMPONENTS)
    assert "sig_cpc_revision" not in WEATHER_COMPONENTS_WITHOUT_FREEZEOFF
    assert set(WEATHER_COMPONENTS_WITHOUT_FREEZEOFF).isdisjoint(
        WEATHER_DIAGNOSTIC_COMPONENTS
    )
    COMPONENT_GROUPS = {
        "weather_score": WEATHER_COMPONENTS_WITHOUT_FREEZEOFF,
        "fundamental_score": [
            "sig_low_storage", "sig_storage_change", "sig_storage_4w_change",
            "sig_low_production_growth", "sig_lng_export_growth",
            "sig_consumption_growth", "sig_net_import_supply",
            "sig_production_mom", "sig_lng_export_mom",
            "sig_consumption_mom", "sig_net_import_change",
        ],
        "market_score": [
            "sig_futures_momentum", "sig_backwardation_carry", "sig_long_curve_carry",
            "sig_front_spot_basis", "sig_spot_momentum",
        ],
        "macro_risk_score": [
            "sig_usd_weakness", "sig_equity_growth", "sig_term_spread", "sig_geopolitical_risk",
        ],
    }
    weather_coverage = component_coverage(
        panel, WEATHER_COMPONENTS_WITHOUT_FREEZEOFF
    )
    panel["weather_score"] = fixed_weight_mean(
        panel, WEATHER_COMPONENTS_WITHOUT_FREEZEOFF
    )
    panel["weather_score_component_count"] = weather_coverage["count"]
    panel["weather_score_coverage_ratio"] = weather_coverage["ratio"]
    panel["weather_score_full_coverage"] = weather_coverage["full"]
    for group, columns in COMPONENT_GROUPS.items():
        if group == "weather_score":
            continue
        panel[group] = available_mean(panel, columns)
        panel[f"{group}_component_count"] = panel[columns].notna().sum(axis=1)
    panel["weather_score_without_freezeoff"] = panel["weather_score"]

    # The strongest risk-adjusted C1 specification in the exploratory comparison:
    # tanh(z/2) for daily/weekly weather and fundamentals, raw z for slow monthly
    # changes, and only the weather/fundamental groups. Positive always means bullish.
    # Use the same fixed primary-weather universe in every sample period.
    mixed_weather_columns = WEATHER_COMPONENTS_WITHOUT_FREEZEOFF
    mixed_fundamental_fast_columns = [
        "sig_low_storage", "sig_storage_change", "sig_storage_4w_change",
        "sig_low_production_growth", "sig_lng_export_growth",
        "sig_consumption_growth", "sig_net_import_supply",
    ]
    mixed_fundamental_monthly_columns = [
        "sig_production_mom", "sig_lng_export_mom",
        "sig_consumption_mom", "sig_net_import_change",
    ]
    panel["mixed_weather_score"] = fixed_weight_mean(
        np.tanh(panel[mixed_weather_columns] / 2), mixed_weather_columns
    )
    panel["mixed_fundamental_score"] = pd.concat(
        [
            np.tanh(panel[mixed_fundamental_fast_columns] / 2),
            panel[mixed_fundamental_monthly_columns],
        ],
        axis=1,
    ).mean(axis=1, skipna=True)
    panel["mixed_wf_score"] = (
        0.35 * panel["mixed_weather_score"] + 0.30 * panel["mixed_fundamental_score"]
    ) / 0.65

    # Increase exposure only in the two historically highest-hit-rate bands;
    # reduce all other active bands and stay flat near zero. The backtest shifts
    # this score by one trading day before it becomes a position.
    high_confidence_band = (
        panel["mixed_wf_score"].ge(CONFIDENCE_HIGH_LONG_MIN)
        | panel["mixed_wf_score"].between(
            CONFIDENCE_HIGH_SHORT_LOW, CONFIDENCE_HIGH_SHORT_HIGH, inclusive="left"
        )
    )
    active_band = panel["mixed_wf_score"].abs().ge(CONFIDENCE_ACTIVE_MIN)
    panel["confidence_multiplier"] = np.select(
        [high_confidence_band, active_band],
        [CONFIDENCE_HIGH_MULTIPLIER, CONFIDENCE_LOW_MULTIPLIER],
        default=0.0,
    )
    panel["confidence_scaled_score"] = (
        panel["mixed_wf_score"] * panel["confidence_multiplier"]
    ).clip(-1, 1)

    panel["composite_score_without_freezeoff"] = weighted_available(
        panel,
        {
            "weather_score_without_freezeoff": GROUP_WEIGHTS["weather_score"],
            "fundamental_score": GROUP_WEIGHTS["fundamental_score"],
            "market_score": GROUP_WEIGHTS["market_score"],
            "macro_risk_score": GROUP_WEIGHTS["macro_risk_score"],
        },
    ).clip(-1, 1)
    panel["composite_score"] = weighted_available(panel, GROUP_WEIGHTS).clip(-1, 1)

    # Freeze-off is an independent, causal position controller—not a score component.
    # When the local-climate cold level reaches its trailing q95 scale and the latest
    # same-valid-date revision is not warmer, shorts are suppressed and existing
    # long exposure is multiplied. The backtest shifts this controlled score by one
    # trading day, exactly like every other strategy score.
    panel["freezeoff_control_active"] = (
        panel["prod_freeze_local_level_score"].ge(FREEZEOFF_CONTROL_LEVEL_THRESHOLD)
        & panel["prod_freeze_local_revision_score"].ge(0)
    )
    panel["freezeoff_control_multiplier"] = np.where(
        panel["freezeoff_control_active"],
        FREEZEOFF_CONTROL_LONG_MULTIPLIER,
        1.0,
    )
    panel["composite_freezeoff_control_score"] = np.where(
        panel["freezeoff_control_active"],
        np.maximum(panel["composite_score"], 0.0)
        * panel["freezeoff_control_multiplier"],
        panel["composite_score"],
    )
    panel["composite_threshold_score"] = np.where(
        panel["composite_score"].gt(SCORE_DEADBAND), 1.0,
        np.where(panel["composite_score"].lt(-SCORE_DEADBAND), -1.0, 0.0),
    )
    panel["always_long_score"] = 1.0

    score_columns = [
        "weather_score", "weather_score_without_freezeoff", "fundamental_score",
        "market_score", "macro_risk_score", "sig_production_freezeoff",
        "composite_score", "composite_score_without_freezeoff",
        "composite_freezeoff_control_score",
        "mixed_wf_score", "confidence_scaled_score",
    ]
    required_groups_ready = panel[list(GROUP_WEIGHTS)].notna().all(axis=1)
    panel["required_input_complete"] = (
        panel["date"].le(required_input_cutoff)
        & required_groups_ready
        & panel["mixed_wf_score"].notna()
        & panel["roll_adjusted_return"].notna()
    )
    latest_complete_date = panel.loc[panel["required_input_complete"], "date"].max()
    assert latest_complete_date == required_input_cutoff, (
        latest_complete_date, required_input_cutoff
    )
    assert not panel.loc[
        panel["date"].gt(required_input_cutoff), "required_input_complete"
    ].any()
    return panel, COMPONENT_GROUPS

def build_weights_audit(
    panel: pd.DataFrame,
    component_groups: dict[str, list[str]],
) -> pd.DataFrame:
    weight_rows = []
    for group, columns in component_groups.items():
        group_weight = GROUP_WEIGHTS[group]
        for component in columns:
            weight_rows.append({
                "group": group,
                "group_weight": group_weight,
                "component": component,
                "nominal_component_weight": group_weight / len(columns),
                "first_available_date": panel.loc[panel[component].notna(), "date"].min(),
                "last_available_date": panel.loc[panel[component].notna(), "date"].max(),
            })
    weights_audit = pd.DataFrame(weight_rows)
    return weights_audit

def build_multisignal_panel(
    *,
    daily_features: pd.DataFrame,
    fundamentals_monthly: pd.DataFrame,
    storage_weekly: pd.DataFrame,
    lng_trade: pd.DataFrame,
    cpc_features: pd.DataFrame,
    futures_daily: pd.DataFrame,
    gfs_daily: pd.DataFrame,
    freezeoff_factors: pd.DataFrame,
) -> pd.DataFrame:
    """Build the complete panel from eight in-memory direct inputs."""
    gfs_features = build_gfs_features(gfs_daily)
    panel, cutoff = build_panel_base(
        daily=daily_features,
        cpc=cpc_features,
        gfs_features=gfs_features,
        freezeoff=freezeoff_factors,
        storage_weekly=storage_weekly,
        fundamentals_monthly=fundamentals_monthly,
        lng_trade=lng_trade,
        futures_daily=futures_daily,
    )
    panel, component_groups = add_panel_scores(panel, cutoff)
    panel.attrs["required_input_cutoff"] = cutoff.isoformat()
    panel.attrs["weights_audit_rows"] = len(
        build_weights_audit(panel, component_groups)
    )
    panel.attrs["component_groups"] = component_groups
    return panel


def build_from_sources(inputs: dict[str, str]) -> pd.DataFrame:
    """Load the eight direct inputs and return the built panel."""
    frames = {
        "daily_features": read_parquet(inputs["daily_features"]),
        "fundamentals_monthly": read_parquet(
            inputs["fundamentals_monthly"]
        ),
        "storage_weekly": read_parquet(inputs["storage_weekly"]),
        "lng_trade": read_parquet(inputs["lng_trade"]),
        "cpc_features": read_parquet(inputs["cpc_features"]),
        "futures_daily": read_parquet(inputs["futures_daily"]),
        "gfs_daily": read_partitioned_parquets(
            inputs["gfs_daily_glob"], columns=GFS_COLUMNS
        ),
        "freezeoff_factors": read_parquet(inputs["freezeoff_factors"]),
    }
    return build_multisignal_panel(**frames)


def build_from_manifest(path: Path = DEFAULT_INPUT_MANIFEST) -> pd.DataFrame:
    """Build from the exact generations and hashes in an input manifest."""
    manifest = load_input_manifest(path)
    items = {item["id"]: item for item in manifest["inputs"]}
    fs = gcsfs.GCSFileSystem()
    frames: dict[str, pd.DataFrame] = {}
    for dataset_id, item in items.items():
        if item["partitioned"]:
            parts = [
                _read_pinned_parquet(
                    fs,
                    obj,
                    columns=item["required_columns"],
                )
                for obj in item["objects"]
            ]
            frame = pd.concat(parts, ignore_index=True)
        else:
            frame = _read_pinned_parquet(fs, item)
        _require_columns(
            frame, item["required_columns"], dataset_id
        )
        frames[dataset_id] = frame
    return build_multisignal_panel(**frames)


def write_panel(
    panel: pd.DataFrame,
    output: Path,
    *,
    overwrite: bool = False,
) -> Path:
    """Atomically write a local parquet, refusing overwrite by default."""
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() and not overwrite:
        raise FileExistsError(
            f"{output} already exists; pass --overwrite to replace it"
        )
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{output.name}.",
            suffix=".tmp.parquet",
            dir=output.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
        serializable = panel.copy()
        serializable.attrs = {}
        serializable.to_parquet(temporary, index=False)
        if output.exists() and not overwrite:
            raise FileExistsError(
                f"{output} already exists; pass --overwrite to replace it"
            )
        os.replace(temporary, output)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return output


def write_weights_audit(
    audit: pd.DataFrame,
    output: Path,
    *,
    overwrite: bool = False,
) -> Path:
    """Atomically write the weights audit separately from parquet attrs."""
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() and not overwrite:
        raise FileExistsError(
            f"{output} already exists; pass --overwrite to replace it"
        )
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{output.name}.",
            suffix=".tmp.csv",
            dir=output.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
        audit.to_csv(temporary, index=False)
        if output.exists() and not overwrite:
            raise FileExistsError(
                f"{output} already exists; pass --overwrite to replace it"
            )
        os.replace(temporary, output)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return output


def upload_panel(
    panel: pd.DataFrame,
    uri: str,
    *,
    overwrite: bool = False,
) -> str:
    """Upload to GCS; callers must make the explicit upload decision."""
    if not uri.startswith("gs://"):
        raise ValueError("GCS output must begin with gs://")
    fs = gcsfs.GCSFileSystem()
    key = _gcs_key(uri)
    if fs.exists(key) and not overwrite:
        raise FileExistsError(
            f"{uri} already exists; pass --overwrite to replace it"
        )
    serializable = panel.copy()
    serializable.attrs = {}
    with fs.open(key, "wb") as handle:
        serializable.to_parquet(handle, index=False)
    return uri


def upload_weights_audit(
    audit: pd.DataFrame,
    uri: str,
    *,
    overwrite: bool = False,
) -> str:
    """Upload the separate weights audit when upload is explicit."""
    if not uri.startswith("gs://"):
        raise ValueError("GCS audit output must begin with gs://")
    fs = gcsfs.GCSFileSystem()
    key = _gcs_key(uri)
    if fs.exists(key) and not overwrite:
        raise FileExistsError(
            f"{uri} already exists; pass --overwrite to replace it"
        )
    with fs.open(key, "wb") as handle:
        audit.to_csv(handle, index=False)
    return uri


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build the point-in-time Henry Hub multi-signal panel."
    )
    for name, default in DEFAULT_INPUTS.items():
        parser.add_argument(
            "--" + name.replace("_", "-"),
            default=default,
            help=f"Direct input for {name}. Default: {default}",
        )
    parser.add_argument(
        "--input-manifest",
        type=Path,
        default=DEFAULT_INPUT_MANIFEST,
        help=(
            "Generation/hash-pinned input contract used by default. "
            "Direct URI options apply only with --live-inputs."
        ),
    )
    parser.add_argument(
        "--live-inputs",
        action="store_true",
        help=(
            "Bypass the pinned manifest and read the mutable direct URIs. "
            "Intended for diagnostics, not an exact rebuild."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_LOCAL_OUTPUT,
        help="Local parquet output; never a cloud URI.",
    )
    parser.add_argument(
        "--weights-audit-output",
        type=Path,
        help=(
            "Local weights-audit CSV. Defaults beside --output as "
            "ng_multisignal_weights_audit.csv."
        ),
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="Explicitly upload the panel to GCS after the local write.",
    )
    parser.add_argument(
        "--gcs-output",
        default=DEFAULT_GCS_OUTPUT,
        help="GCS destination used only with --upload.",
    )
    parser.add_argument(
        "--gcs-weights-audit-output",
        default=DEFAULT_GCS_AUDIT_OUTPUT,
        help="GCS audit destination used only with --upload.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace existing local/GCS output. Off by default.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build and validate the panel without writing any output.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if str(args.output).startswith("gs://"):
        raise SystemExit(
            "--output must be local; use --upload and --gcs-output for GCS"
        )
    audit_output = args.weights_audit_output or args.output.with_name(
        "ng_multisignal_weights_audit.csv"
    )
    existing_outputs = [
        path
        for path in (args.output, audit_output)
        if path.expanduser().exists()
    ]
    if (
        not args.dry_run
        and existing_outputs
        and not args.overwrite
    ):
        raise SystemExit(
            f"{existing_outputs[0]} already exists; "
            "pass --overwrite to replace it"
        )
    if args.live_inputs:
        inputs = {
            name: getattr(args, name)
            for name in DEFAULT_INPUTS
        }
        panel = build_from_sources(inputs)
    else:
        changed_inputs = [
            name
            for name, default in DEFAULT_INPUTS.items()
            if getattr(args, name) != default
        ]
        if changed_inputs:
            raise SystemExit(
                "direct URI overrides require --live-inputs; "
                f"changed: {', '.join(changed_inputs)}"
            )
        panel = build_from_manifest(args.input_manifest)
    cutoff = pd.Timestamp(panel.attrs["required_input_cutoff"])
    print(
        f"built rows={len(panel):,} columns={len(panel.columns):,} "
        f"dates={panel['date'].min().date()}..{panel['date'].max().date()} "
        f"required_input_cutoff={cutoff.date()}"
    )
    if args.dry_run:
        print("dry run: no files written")
        return
    audit = build_weights_audit(
        panel, panel.attrs["component_groups"]
    )
    local_path = write_panel(
        panel, args.output, overwrite=args.overwrite
    )
    print(f"saved {local_path}")
    audit_path = write_weights_audit(
        audit, audit_output, overwrite=args.overwrite
    )
    print(f"saved {audit_path}")
    if args.upload:
        uri = upload_panel(
            panel, args.gcs_output, overwrite=args.overwrite
        )
        print(f"uploaded {uri}")
        audit_uri = upload_weights_audit(
            audit,
            args.gcs_weights_audit_output,
            overwrite=args.overwrite,
        )
        print(f"uploaded {audit_uri}")


if __name__ == "__main__":
    main()
