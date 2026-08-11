#!/usr/bin/env python3
"""Nonlinear wind-power proxy and causal gas-pressure signal helpers.

The functions in this module are intentionally independent of the formal GDEX
backfill.  They transform each point/hour wind observation before aggregation,
which preserves the low-wind and high-wind cut-out regimes that disappear when
wind speed is averaged first.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class PowerCurveSpec:
    """Transparent generic fleet-level wind power curve assumptions."""

    cut_in_mps: float = 3.0
    rated_mps: float = 12.0
    high_wind_derate_start_mps: float = 20.0
    cut_out_mps: float = 25.0

    def __post_init__(self) -> None:
        if not (
            0 <= self.cut_in_mps
            < self.rated_mps
            <= self.high_wind_derate_start_mps
            <= self.cut_out_mps
        ):
            raise ValueError(
                "power-curve thresholds must satisfy "
                "0 <= cut-in < rated <= derate-start <= cut-out"
            )


DEFAULT_POWER_CURVE = PowerCurveSpec()


def nonlinear_power_components(
    wind_speed_mps: np.ndarray | pd.Series,
    *,
    spec: PowerCurveSpec = DEFAULT_POWER_CURVE,
) -> dict[str, np.ndarray]:
    """Convert point-level wind speed into power and shortfall components.

    ``power_cf_no_cutout`` is a normalized cubic ramp followed by rated power.
    ``effective_power_cf`` applies a cosine fleet-level derating between
    ``high_wind_derate_start_mps`` and ``cut_out_mps``.  Setting those two
    thresholds equal produces a hard individual-turbine-style cut-out.

    The gas-supporting shortfall is decomposed exactly:

    ``total_shortfall_cf = low_wind_shortfall_cf + high_wind_cutout_loss_cf``.
    """

    speed = np.asarray(wind_speed_mps, dtype=float)
    valid = np.isfinite(speed) & (speed >= 0)

    power_no_cutout = np.full(speed.shape, np.nan, dtype=float)
    power_no_cutout[valid] = 0.0
    ramp = (
        valid
        & (speed >= spec.cut_in_mps)
        & (speed < spec.rated_mps)
    )
    power_no_cutout[ramp] = (
        speed[ramp] ** 3 - spec.cut_in_mps**3
    ) / (spec.rated_mps**3 - spec.cut_in_mps**3)
    power_no_cutout[valid & (speed >= spec.rated_mps)] = 1.0

    high_wind_availability = np.full(speed.shape, np.nan, dtype=float)
    high_wind_availability[valid] = 1.0
    derating = (
        valid
        & (speed > spec.high_wind_derate_start_mps)
        & (speed < spec.cut_out_mps)
    )
    derating_width = (
        spec.cut_out_mps - spec.high_wind_derate_start_mps
    )
    if derating_width > 0:
        phase = (
            speed[derating] - spec.high_wind_derate_start_mps
        ) / derating_width
        high_wind_availability[derating] = 0.5 * (
            1.0 + np.cos(np.pi * phase)
        )
    high_wind_availability[
        valid & (speed >= spec.cut_out_mps)
    ] = 0.0

    effective_power = power_no_cutout * high_wind_availability
    low_wind_shortfall = 1.0 - power_no_cutout
    high_wind_cutout_loss = power_no_cutout - effective_power
    total_shortfall = 1.0 - effective_power

    return {
        "power_cf_no_cutout": power_no_cutout,
        "high_wind_availability": high_wind_availability,
        "effective_power_cf": effective_power,
        "low_wind_shortfall_cf": low_wind_shortfall,
        "high_wind_cutout_loss_cf": high_wind_cutout_loss,
        "total_shortfall_cf": total_shortfall,
        "below_cut_in": (
            valid & (speed < spec.cut_in_mps)
        ).astype(float),
        "ramp_regime": (
            valid
            & (speed >= spec.cut_in_mps)
            & (speed < spec.rated_mps)
        ).astype(float),
        "rated_regime": (
            valid
            & (speed >= spec.rated_mps)
            & (speed <= spec.high_wind_derate_start_mps)
        ).astype(float),
        "high_wind_regime": (
            valid
            & (speed > spec.high_wind_derate_start_mps)
            & (speed < spec.cut_out_mps)
        ).astype(float),
        "cut_out_regime": (
            valid & (speed >= spec.cut_out_mps)
        ).astype(float),
    }


def aggregate_nonlinear_features(
    points: pd.DataFrame,
    *,
    speed_column: str = "wind_speed_80m_mps",
    spec: PowerCurveSpec = DEFAULT_POWER_CURVE,
) -> pd.DataFrame:
    """Build one five-day nonlinear feature row per GFS initialization.

    Input rows should contain the point/hour samples for forecast lead days
    1--5.  Every point/hour receives the power curve before the equally
    weighted aggregation.  This function deliberately does not introduce
    turbine-capacity weights.
    """

    required = {
        "forecast_reference_time_utc",
        "forecast_cycle_hour_utc",
        "location_id",
        "valid_time_utc",
        speed_column,
    }
    missing = sorted(required.difference(points.columns))
    if missing:
        raise KeyError(f"missing nonlinear wind columns: {missing}")

    work = points[
        [
            "forecast_reference_time_utc",
            "forecast_cycle_hour_utc",
            "location_id",
            "valid_time_utc",
            speed_column,
        ]
    ].copy()
    components = nonlinear_power_components(
        work[speed_column],
        spec=spec,
    )
    for name, values in components.items():
        work[name] = values

    features = (
        work.groupby(
            [
                "forecast_reference_time_utc",
                "forecast_cycle_hour_utc",
            ],
            as_index=False,
        )
        .agg(
            gfs_nonlinear_sample_count=(speed_column, "count"),
            gfs_nonlinear_location_count=("location_id", "nunique"),
            gfs_nonlinear_valid_time_count=(
                "valid_time_utc",
                "nunique",
            ),
            gfs_wind80_mean_5d_mps=(speed_column, "mean"),
            gfs_wind80_max_5d_mps=(speed_column, "max"),
            gfs_power_cf_no_cutout_5d=(
                "power_cf_no_cutout",
                "mean",
            ),
            gfs_effective_power_cf_5d=(
                "effective_power_cf",
                "mean",
            ),
            gfs_low_wind_shortfall_cf_5d=(
                "low_wind_shortfall_cf",
                "mean",
            ),
            gfs_high_wind_cutout_loss_cf_5d=(
                "high_wind_cutout_loss_cf",
                "mean",
            ),
            gfs_total_wind_shortfall_cf_5d=(
                "total_shortfall_cf",
                "mean",
            ),
            gfs_below_cut_in_share_5d=("below_cut_in", "mean"),
            gfs_ramp_regime_share_5d=("ramp_regime", "mean"),
            gfs_rated_regime_share_5d=("rated_regime", "mean"),
            gfs_high_wind_regime_share_5d=(
                "high_wind_regime",
                "mean",
            ),
            gfs_cut_out_regime_share_5d=("cut_out_regime", "mean"),
        )
        .sort_values("forecast_reference_time_utc")
        .reset_index(drop=True)
    )
    features["nominal_issue_date"] = pd.to_datetime(
        features["forecast_reference_time_utc"],
        utc=True,
    ).dt.tz_localize(None).dt.normalize()
    return features


def causal_zscore(
    series: pd.Series,
    *,
    window: int = 60,
    min_periods: int = 30,
) -> pd.Series:
    """Standardize against observations strictly before the current row."""

    values = pd.to_numeric(series, errors="coerce").astype(float)
    history = values.shift(1).rolling(
        window,
        min_periods=min_periods,
    )
    return (
        (values - history.mean())
        / history.std().replace(0.0, np.nan)
    )


def add_nonlinear_signals(
    features: pd.DataFrame,
    *,
    window: int = 60,
    min_periods: int = 30,
) -> pd.DataFrame:
    """Add causal total, low-wind, and cut-out gas-pressure signals."""

    required = {
        "forecast_cycle_hour_utc",
        "forecast_reference_time_utc",
        "gfs_total_wind_shortfall_cf_5d",
        "gfs_low_wind_shortfall_cf_5d",
        "gfs_high_wind_cutout_loss_cf_5d",
    }
    missing = sorted(required.difference(features.columns))
    if missing:
        raise KeyError(f"missing nonlinear feature columns: {missing}")

    result = features.sort_values(
        [
            "forecast_cycle_hour_utc",
            "forecast_reference_time_utc",
        ]
    ).copy()
    signal_specs = {
        "sig_gdex_wind_nonlinear": (
            "gfs_total_wind_shortfall_cf_5d"
        ),
        "sig_gdex_wind_low": "gfs_low_wind_shortfall_cf_5d",
        "sig_gdex_wind_cutout": (
            "gfs_high_wind_cutout_loss_cf_5d"
        ),
    }
    for signal_column, feature_column in signal_specs.items():
        result[signal_column] = result.groupby(
            "forecast_cycle_hour_utc",
            group_keys=False,
        )[feature_column].transform(
            lambda values: causal_zscore(
                values,
                window=window,
                min_periods=min_periods,
            )
        )
    return result.sort_values(
        "forecast_reference_time_utc"
    ).reset_index(drop=True)
