import hashlib
import io
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from naturalgas import build_multisignal_panel as panel_builder
from naturalgas.build_multisignal_panel import (
    DEFAULT_GCS_OUTPUT,
    DEFAULT_INPUT_MANIFEST,
    DEFAULT_INPUTS,
    build_from_manifest,
    build_from_sources,
    build_gfs_features,
    build_monthly_fundamental_features,
    build_parser,
    build_storage_4w_features,
    causal_z,
    merge_point_in_time,
    read_parquet,
    write_panel,
)
from naturalgas.build_production_freezeoff_factors import (
    build_parser as build_freezeoff_parser,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_pinned_panel_read_forwards_generation_to_media_request() -> None:
    buffer = io.BytesIO()
    pd.DataFrame({"value": [1.0]}).to_parquet(buffer, index=False)
    payload = buffer.getvalue()

    class FakeFileSystem:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def cat_file(self, key: str, **kwargs: object) -> bytes:
            self.calls.append((key, kwargs))
            return payload

    filesystem = FakeFileSystem()
    artifact = {
        "uri": "gs://example-bucket/direct-input.parquet",
        "generation": "1234567890123456",
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    actual = panel_builder._read_pinned_parquet(filesystem, artifact)

    assert filesystem.calls == [(
        "example-bucket/direct-input.parquet",
        {"generation": "1234567890123456", "concurrency": 1},
    )]
    assert actual["value"].tolist() == [1.0]


def test_causal_z_uses_only_prior_observations() -> None:
    values = pd.Series([1.0, 2.0, 4.0, 8.0, 16.0, 32.0])
    actual = causal_z(values, window=3, min_periods=3)

    expected = (
        values.iloc[3] - values.iloc[:3].mean()
    ) / values.iloc[:3].std()
    assert actual.iloc[3] == pytest.approx(expected)

    changed_future = values.copy()
    changed_future.iloc[-1] = 1_000_000.0
    changed = causal_z(changed_future, window=3, min_periods=3)
    pd.testing.assert_series_equal(
        actual.iloc[:-1], changed.iloc[:-1], check_exact=True
    )


def test_point_in_time_merge_never_selects_future_or_stale_row() -> None:
    left = pd.DataFrame(
        {"date": pd.to_datetime(["2026-01-02", "2026-01-05", "2026-01-08"])}
    )
    right = pd.DataFrame(
        {
            "available_date": pd.to_datetime(
                ["2026-01-01", "2026-01-04", "2026-01-09"]
            ),
            "value": [1.0, 4.0, 9.0],
        }
    )

    merged = merge_point_in_time(
        left,
        right,
        left_date="date",
        available_date="available_date",
        tolerance_days=2,
    )

    assert merged["value"].iloc[:2].tolist() == [1.0, 4.0]
    assert pd.isna(merged["value"].iloc[2])
    matched = merged["available_date"].notna()
    assert (
        merged.loc[matched, "available_date"]
        <= merged.loc[matched, "date"]
    ).all()


def test_storage_features_have_release_lag_and_no_future_feedback() -> None:
    weeks = pd.date_range("2018-01-05", periods=420, freq="7D")
    source = pd.DataFrame(
        {
            "week_ending": weeks,
            "lower48": 2_500.0 + np.arange(len(weeks)) * 0.5,
        }
    )
    baseline = build_storage_4w_features(source)

    expected_available = baseline["week_ending"] + pd.Timedelta(days=6)
    pd.testing.assert_series_equal(
        baseline["storage_4w_available_date"],
        expected_available,
        check_names=False,
    )

    revised_future = source.copy()
    revised_future.loc[revised_future.index[-1], "lower48"] = 99_999.0
    revised = build_storage_4w_features(revised_future)
    pd.testing.assert_frame_equal(
        baseline.iloc[:-1],
        revised.iloc[:-1],
        check_exact=True,
    )


def test_monthly_features_are_available_at_m_plus_three_only() -> None:
    months = pd.date_range("2018-01-01", periods=72, freq="MS")
    fundamentals = pd.DataFrame(
        {
            "month": months,
            "dry_prod": 2_000.0 + np.arange(len(months)),
            "total_cons": 1_800.0 + np.arange(len(months)) * 0.8,
            "exports": 200.0 + np.arange(len(months)) * 0.2,
            "imports": 150.0 + np.arange(len(months)) * 0.1,
        }
    )
    lng = pd.DataFrame(
        {
            "dataset": "country_exports",
            "is_us_aggregate": True,
            "process-name": "Liquefied Natural Gas Exports",
            "metric": "volume",
            "month": months,
            "value": 50.0 + np.arange(len(months)) * 0.3,
        }
    )
    baseline = build_monthly_fundamental_features(fundamentals, lng)

    expected = baseline["month"] + pd.DateOffset(months=3)
    pd.testing.assert_series_equal(
        baseline["fundamentals_available_date"],
        expected,
        check_names=False,
    )

    revised_fundamentals = fundamentals.copy()
    revised_fundamentals.loc[
        revised_fundamentals.index[-1], "dry_prod"
    ] = 999_999.0
    revised_lng = lng.copy()
    revised_lng.loc[revised_lng.index[-1], "value"] = 999_999.0
    revised = build_monthly_fundamental_features(
        revised_fundamentals, revised_lng
    )
    pd.testing.assert_frame_equal(
        baseline.iloc[:-1],
        revised.iloc[:-1],
        check_exact=True,
    )


def test_gfs_features_do_not_change_before_a_revised_future_issue() -> None:
    rows = []
    issues = pd.date_range("2025-01-01", periods=40, freq="D")
    for issue_number, issue_date in enumerate(issues):
        for lead in range(1, 6):
            rows.append(
                {
                    "location_id": "test-city",
                    "target_date": issue_date + pd.Timedelta(days=lead),
                    "lead_days": lead,
                    "hdd65_f": 10.0 + issue_number + lead,
                    "cdd65_f": 2.0 + lead,
                    "wind_speed_80m_mean_kmh": 20.0 + lead,
                    "cloud_cover_mean_pct": 30.0 + lead,
                }
            )
    source = pd.DataFrame(rows)
    baseline = build_gfs_features(source)

    revised_source = source.copy()
    future_rows = (
        revised_source["target_date"]
        - pd.to_timedelta(revised_source["lead_days"], unit="D")
    ).eq(issues[-1])
    revised_source.loc[future_rows, "hdd65_f"] = 999_999.0
    revised = build_gfs_features(revised_source)

    historical = baseline["nominal_issue_date"].lt(issues[-1])
    pd.testing.assert_frame_equal(
        baseline.loc[historical].reset_index(drop=True),
        revised.loc[historical].reset_index(drop=True),
        check_exact=True,
    )


def test_local_write_strips_nonserializable_attrs_and_refuses_overwrite(
    tmp_path: Path,
) -> None:
    panel = pd.DataFrame(
        {"date": pd.to_datetime(["2026-01-01"]), "value": [1.0]}
    )
    panel.attrs["timestamp"] = pd.Timestamp("2026-01-01")
    panel.attrs["dataframe"] = pd.DataFrame({"x": [1]})
    output = tmp_path / "panel.parquet"

    assert write_panel(panel, output) == output.resolve()
    written = pd.read_parquet(output)
    pd.testing.assert_frame_equal(written, panel, check_exact=True)
    with pytest.raises(FileExistsError, match="--overwrite"):
        write_panel(panel, output)

    changed = panel.copy()
    changed["value"] = 2.0
    write_panel(changed, output, overwrite=True)
    assert pd.read_parquet(output)["value"].iloc[0] == 2.0
    assert not list(tmp_path.glob(".*.tmp.parquet"))


def test_cloud_writes_are_opt_in_for_both_builders() -> None:
    panel_args = build_parser().parse_args([])
    freezeoff_args = build_freezeoff_parser().parse_args([])

    assert panel_args.upload is False
    assert panel_args.overwrite is False
    assert panel_args.live_inputs is False
    assert panel_args.input_manifest == DEFAULT_INPUT_MANIFEST
    assert freezeoff_args.upload is False
    assert freezeoff_args.overwrite is False


def test_direct_input_manifest_is_generation_pinned() -> None:
    path = (
        PROJECT_ROOT
        / "manifests"
        / "master_panel_inputs_2026-07-13.json"
    )
    manifest = json.loads(path.read_text(encoding="utf-8"))
    inputs = manifest["inputs"]

    assert manifest["input_count"] == 8
    assert len(inputs) == 8
    assert all(item["required_columns"] for item in inputs)
    static_inputs = [item for item in inputs if not item["partitioned"]]
    assert all(
        item.get("generation")
        and item.get("size_bytes")
        and item.get("sha256")
        for item in static_inputs
    )

    gfs = next(item for item in inputs if item["id"] == "gfs_daily")
    assert gfs["partition_count"] == 65
    assert len(gfs["objects"]) == 65
    assert all(
        item["generation"] and item["sha256"]
        for item in gfs["objects"]
    )


@pytest.mark.skipif(
    os.environ.get("RUN_GCS_PANEL_PARITY") != "1",
    reason="set RUN_GCS_PANEL_PARITY=1 for the approved GCS parity check",
)
def test_current_direct_inputs_match_approved_panel_exactly() -> None:
    actual = build_from_manifest(DEFAULT_INPUT_MANIFEST)
    approved = read_parquet(DEFAULT_GCS_OUTPUT)

    pd.testing.assert_frame_equal(
        actual,
        approved,
        check_exact=True,
        check_dtype=True,
        check_like=False,
    )
