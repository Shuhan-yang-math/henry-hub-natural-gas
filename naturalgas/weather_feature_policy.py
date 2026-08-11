"""Shared point-in-time policy for natural-gas weather features.

The headline strategy uses only weather components with stable coverage across
the full research sample.  Later-starting GFS fields remain available for
diagnostics and separately labelled experiments, but they do not silently
change the primary strategy after their respective start dates.
"""

from __future__ import annotations

from collections.abc import Sequence

import pandas as pd


# Daily weather observations must not be carried across a data outage.  Three
# calendar days allow a normal Friday-to-Monday transition but reject longer
# gaps, including the 20-day GFS outage observed around January 2024.
MAX_WEATHER_STALENESS_DAYS = 3


# These three inputs have stable coverage across the 2016+ strategy sample.
# Keep the seasonally appropriate CPC revision and exclude the nearly duplicate
# all-season GDD revision from the primary score.
PRIMARY_WEATHER_COMPONENTS = (
    "sig_cpc_seasonal_revision",
    "sig_cpc_level",
    "sig_observed_weather",
)


# Retained in the feature panel for diagnostics or a separately labelled,
# common-coverage experiment.  They are not part of the headline strategy.
DIAGNOSTIC_GFS_WEATHER_COMPONENTS = (
    "sig_gfs_revision",
    "sig_gfs_wind",
    "sig_gfs_cloud",
)


def fixed_weight_mean(
    frame: pd.DataFrame,
    columns: Sequence[str],
) -> pd.Series:
    """Return an equal-weight score without renormalizing around missing data.

    A missing component contributes zero (neutral) while retaining its fixed
    portfolio weight.  This is deliberately different from ``skipna=True``
    means, which increase every remaining component's effective weight.
    """

    columns = tuple(columns)
    if not columns:
        raise ValueError("fixed_weight_mean requires at least one column")
    return frame.loc[:, columns].fillna(0.0).sum(axis=1) / len(columns)


def component_coverage(
    frame: pd.DataFrame,
    columns: Sequence[str],
) -> pd.DataFrame:
    """Return point-in-time component count, ratio, and full-coverage flag."""

    columns = tuple(columns)
    if not columns:
        raise ValueError("component_coverage requires at least one column")
    count = frame.loc[:, columns].notna().sum(axis=1)
    return pd.DataFrame(
        {
            "count": count,
            "ratio": count / len(columns),
            "full": count.eq(len(columns)),
        },
        index=frame.index,
    )
