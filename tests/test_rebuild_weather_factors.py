from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pandas as pd
import pytest

from naturalgas.ncar_gdex_wind_backfill_to_gcs import LOCATIONS
from naturalgas.pipelines.rebuild_weather_factors import (
    SOLAR_LEAD_OUTPUT_NAME,
    SOLAR_SIGNAL_OUTPUT_NAME,
    WIND_OUTPUT_NAME,
    FactorBuildInputError,
    InputArtifact,
    ReadOnlyPartitionFileSystem,
    _write_parquet_outputs_no_overwrite,
    load_factor_inputs,
    main,
    rebuild_selected_wind,
    rebuild_solar,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEATHER_MANIFEST = (
    PROJECT_ROOT / "manifests/weather_factor_inputs_2026-07-28.json"
)


def _artifact(path: Path) -> dict[str, object]:
    payload = path.read_bytes()
    return {
        "uri": path.name,
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def test_approved_weather_manifest_is_complete_and_matches_formal_inputs() -> None:
    weather = json.loads(WEATHER_MANIFEST.read_text(encoding="utf-8"))
    formal = json.loads(
        (PROJECT_ROOT / "manifests/input_artifacts_2026-07-13.json")
        .read_text(encoding="utf-8")
    )
    formal_by_id = {item["id"]: item for item in formal["artifacts"]}
    expected_outputs = {
        "capacity_weighted_wind_features_daily.parquet": (
            "capacity_weighted_wind_features_daily"
        ),
        "capacity_weighted_solar_signals.parquet": (
            "capacity_weighted_solar_signals"
        ),
        "capacity_weighted_location_leads.parquet": (
            "capacity_weighted_location_leads"
        ),
    }
    for component in ("wind", "solar"):
        section = weather[component]
        partitions = section["weather_partitions"]
        assert len(partitions) == 127
        assert len({item["uri"] for item in partitions}) == 127
        assert all(
            str(item["generation"]).isdigit()
            and item["size_bytes"] > 0
            and len(item["sha256"]) == 64
            for item in partitions
        )
        capacity = section["capacity_snapshot"]
        capacity_path = (WEATHER_MANIFEST.parent / capacity["uri"]).resolve()
        assert capacity_path.is_relative_to(PROJECT_ROOT)
        assert capacity_path.stat().st_size == capacity["size_bytes"]
        assert hashlib.sha256(capacity_path.read_bytes()).hexdigest() == (
            capacity["sha256"]
        )
        for filename, approved in section["approved_outputs"].items():
            formal_artifact = formal_by_id[expected_outputs[filename]]
            assert approved["rows"] == formal_artifact["rows"]
            assert approved["size_bytes"] == formal_artifact["size_bytes"]
            assert approved["sha256"] == formal_artifact["sha256"]


@pytest.mark.skipif(
    os.environ.get("RUN_HENRY_HUB_WEATHER_CHAIN") != "1",
    reason="set RUN_HENRY_HUB_WEATHER_CHAIN=1 for 254 weather partitions",
)
def test_pinned_weather_partitions_rebuild_approved_outputs(
    tmp_path: Path,
) -> None:
    wind = rebuild_selected_wind(
        inputs=load_factor_inputs(WEATHER_MANIFEST, "wind"),
        output_dir=tmp_path,
    )
    solar = rebuild_solar(
        inputs=load_factor_inputs(WEATHER_MANIFEST, "solar"),
        output_dir=tmp_path,
    )
    assert wind["rows"] == 3857
    assert solar["signal_rows"] == 15448
    assert solar["lead_rows"] == 77225


def _wind_inputs(root: Path) -> tuple[Path, Path]:
    reference = pd.Timestamp("2020-01-01T00:00:00Z")
    rows = []
    for location in LOCATIONS:
        for lead_day in range(1, 6):
            for sample_hour in (0, 6, 12, 18):
                rows.append({
                    "forecast_reference_time_utc": reference,
                    "forecast_cycle_hour_utc": 0,
                    "location_id": location.location_id,
                    "lead_days": lead_day,
                    "valid_time_utc": reference + pd.Timedelta(
                        days=lead_day,
                        hours=sample_hour,
                    ),
                    "wind_speed_80m_mps": 8.0 + sample_hour / 100.0,
                })
    weather = root / "wind_weather.parquet"
    pd.DataFrame(rows).to_parquet(weather, index=False)

    weights = root / "annual_location_weights.parquet"
    pd.DataFrame([
        {
            "issue_year": 2020,
            "fleet_cutoff_year": 2019,
            "location_id": location.location_id,
            "capacity_mw": 1.0,
            "hub_height_m": 80.0,
        }
        for location in LOCATIONS
    ]).to_parquet(weights, index=False)
    return weather, weights


def _solar_inputs(root: Path) -> tuple[Path, Path]:
    reference = pd.Timestamp("2020-03-01T00:00:00Z")
    rows = []
    for location in LOCATIONS:
        for lead_day in range(1, 6):
            rows.append({
                "forecast_reference_time_utc": reference,
                "nominal_issue_date": reference.date(),
                "target_date": reference.tz_localize(None).normalize()
                + pd.Timedelta(days=lead_day),
                "lead_days": lead_day,
                "location_id": location.location_id,
                "requested_latitude": location.latitude,
                "requested_longitude": location.longitude,
                "solar_sample_count": 4,
                "downward_shortwave_mean_wm2": 200.0,
                "downward_shortwave_energy_kwh_m2": 4.0,
                "total_cloud_cover_mean_pct": 30.0,
                "temperature_2m_mean_c": 20.0,
                "solar_sample_complete": True,
            })
    weather = root / "solar_daily.parquet"
    pd.DataFrame(rows).to_parquet(weather, index=False)

    weights = root / "monthly_location_weights.parquet"
    pd.DataFrame([
        {
            "period": "2020-01",
            "location_id": location.location_id,
            "capacity_mw": 1.0,
            "capacity_share": 1.0 / len(LOCATIONS),
        }
        for location in LOCATIONS
    ]).to_parquet(weights, index=False)
    return weather, weights


def test_help_states_frozen_snapshot_boundary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["--help"])
    assert caught.value.code == 0
    output = capsys.readouterr().out
    assert "frozen USWTDB" in output
    assert "frozen EIA" in output
    assert "{wind,solar}" in output


def test_input_filesystem_rejects_writes(tmp_path: Path) -> None:
    filesystem = ReadOnlyPartitionFileSystem()
    with pytest.raises(PermissionError, match="read-only"):
        filesystem.open(str(tmp_path / "forbidden.parquet"), "wb")


def test_gcs_download_forwards_pinned_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"generation-pinned payload"
    calls: list[tuple[str, dict[str, object]]] = []

    class FakeGCSFileSystem:
        def cat_file(self, path: str, **kwargs: object) -> bytes:
            calls.append((path, kwargs))
            return payload

    monkeypatch.setattr(
        "naturalgas.pipelines.rebuild_weather_factors.gcsfs.GCSFileSystem",
        FakeGCSFileSystem,
    )
    artifact = InputArtifact(
        uri="gs://example-bucket/history/data.parquet",
        generation="1234567890123456",
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    filesystem = ReadOnlyPartitionFileSystem([artifact])
    with filesystem.open(artifact.uri, "rb") as source:
        assert source.read() == payload
    assert calls == [(
        "example-bucket/history/data.parquet",
        {"generation": "1234567890123456", "concurrency": 1},
    )]


def test_multi_output_promotion_rolls_back_keyboard_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first.parquet"
    second = tmp_path / "second.parquet"
    real_link = os.link
    calls = 0

    def interrupt_second_link(source: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        real_link(source, target)

    monkeypatch.setattr(
        "naturalgas.pipelines.rebuild_weather_factors.os.link",
        interrupt_second_link,
    )
    with pytest.raises(KeyboardInterrupt):
        _write_parquet_outputs_no_overwrite([
            (pd.DataFrame({"value": [1]}), first, "snappy"),
            (pd.DataFrame({"value": [2]}), second, "snappy"),
        ])
    assert not first.exists()
    assert not second.exists()
    assert not list(tmp_path.glob(".*.tmp"))


def test_local_factor_only_wind_and_solar_smoke(tmp_path: Path) -> None:
    wind_weather, wind_weights = _wind_inputs(tmp_path)
    wind_output = tmp_path / "wind_output"
    manifest = tmp_path / "factor_inputs.json"
    manifest.write_text(
        json.dumps({
            "manifest_version": 1,
            "wind": {
                "weather_partitions": [_artifact(wind_weather)],
                "capacity_snapshot": _artifact(wind_weights),
                "capacity_kind": "annual_location_weights",
            }
        }),
        encoding="utf-8",
    )
    assert main([
        "wind",
        "--input-manifest",
        str(manifest),
        "--output-dir",
        str(wind_output),
    ]) == 0

    wind_path = wind_output / WIND_OUTPUT_NAME
    wind = pd.read_parquet(wind_path)
    assert len(wind) == 1
    assert wind["forecast_cycle_hour_utc"].eq(0).all()
    assert wind["sample_count"].eq(len(LOCATIONS) * 5 * 4).all()

    original_bytes = wind_path.read_bytes()
    with pytest.raises(SystemExit) as caught:
        main([
            "wind",
            "--input-manifest",
            str(manifest),
            "--output-dir",
            str(wind_output),
        ])
    assert caught.value.code == 2
    assert wind_path.read_bytes() == original_bytes

    solar_weather, solar_weights = _solar_inputs(tmp_path)
    solar_output = tmp_path / "solar_output"
    assert main([
        "solar",
        "--weather-partition",
        str(solar_weather),
        "--capacity-snapshot",
        str(solar_weights),
        "--capacity-kind",
        "monthly_location_weights",
        "--output-dir",
        str(solar_output),
    ]) == 0

    leads = pd.read_parquet(solar_output / SOLAR_LEAD_OUTPUT_NAME)
    signals = pd.read_parquet(solar_output / SOLAR_SIGNAL_OUTPUT_NAME)
    assert len(leads) == 5
    assert len(signals) == 1
    assert signals["input_complete"].all()


def test_manifest_hash_mismatch_is_rejected_before_output(tmp_path: Path) -> None:
    wind_weather, wind_weights = _wind_inputs(tmp_path)
    weather_artifact = _artifact(wind_weather)
    weather_artifact["sha256"] = "0" * 64
    manifest = tmp_path / "bad_hash.json"
    manifest.write_text(
        json.dumps({
            "manifest_version": 1,
            "wind": {
                "weather_partitions": [weather_artifact],
                "capacity_snapshot": _artifact(wind_weights),
                "capacity_kind": "annual_location_weights",
            },
        }),
        encoding="utf-8",
    )
    output = tmp_path / "bad_hash_output"
    with pytest.raises(SystemExit) as caught:
        main([
            "wind",
            "--input-manifest",
            str(manifest),
            "--output-dir",
            str(output),
        ])
    assert caught.value.code == 2
    assert not (output / WIND_OUTPUT_NAME).exists()


def test_manifest_size_mismatch_is_rejected_before_output(tmp_path: Path) -> None:
    wind_weather, wind_weights = _wind_inputs(tmp_path)
    weather_artifact = _artifact(wind_weather)
    weather_artifact["size_bytes"] = int(weather_artifact["size_bytes"]) + 1
    manifest = tmp_path / "bad_size.json"
    manifest.write_text(
        json.dumps({
            "manifest_version": 1,
            "wind": {
                "weather_partitions": [weather_artifact],
                "capacity_snapshot": _artifact(wind_weights),
                "capacity_kind": "annual_location_weights",
            },
        }),
        encoding="utf-8",
    )
    output = tmp_path / "bad_size_output"
    with pytest.raises(SystemExit) as caught:
        main([
            "wind",
            "--input-manifest",
            str(manifest),
            "--output-dir",
            str(output),
        ])
    assert caught.value.code == 2
    assert not (output / WIND_OUTPUT_NAME).exists()


def test_gcs_manifest_requires_generation(tmp_path: Path) -> None:
    _, wind_weights = _wind_inputs(tmp_path)
    manifest = tmp_path / "missing_generation.json"
    manifest.write_text(
        json.dumps({
            "manifest_version": 1,
            "wind": {
                "weather_partitions": [{
                    "uri": "gs://example-bucket/history/data.parquet",
                    "size_bytes": 123,
                    "sha256": "0" * 64,
                }],
                "capacity_snapshot": _artifact(wind_weights),
                "capacity_kind": "annual_location_weights",
            },
        }),
        encoding="utf-8",
    )
    with pytest.raises(FactorBuildInputError, match="generation is required"):
        load_factor_inputs(manifest, "wind")


def test_direct_cli_rejects_unpinned_gcs_inputs(tmp_path: Path) -> None:
    _, wind_weights = _wind_inputs(tmp_path)
    with pytest.raises(SystemExit) as caught:
        main([
            "wind",
            "--weather-partition",
            "gs://example-bucket/latest/data.parquet",
            "--capacity-snapshot",
            str(wind_weights),
            "--capacity-kind",
            "annual_location_weights",
            "--output-dir",
            str(tmp_path / "output"),
        ])
    assert caught.value.code == 2


def test_bad_approved_solar_hash_publishes_neither_output(
    tmp_path: Path,
) -> None:
    solar_weather, solar_weights = _solar_inputs(tmp_path)
    manifest = tmp_path / "bad_approved_output.json"
    manifest.write_text(
        json.dumps({
            "manifest_version": 1,
            "solar": {
                "weather_partitions": [_artifact(solar_weather)],
                "capacity_snapshot": _artifact(solar_weights),
                "capacity_kind": "monthly_location_weights",
                "approved_outputs": {
                    SOLAR_LEAD_OUTPUT_NAME: {
                        "rows": 5,
                        "size_bytes": 1,
                        "sha256": "0" * 64,
                    },
                    SOLAR_SIGNAL_OUTPUT_NAME: {
                        "rows": 1,
                        "size_bytes": 1,
                        "sha256": "0" * 64,
                    },
                },
            },
        }),
        encoding="utf-8",
    )
    output = tmp_path / "bad_approved_output"
    with pytest.raises(SystemExit) as caught:
        main([
            "solar",
            "--input-manifest",
            str(manifest),
            "--output-dir",
            str(output),
        ])
    assert caught.value.code == 2
    assert not (output / SOLAR_LEAD_OUTPUT_NAME).exists()
    assert not (output / SOLAR_SIGNAL_OUTPUT_NAME).exists()


def test_existing_solar_target_prevents_both_writes(tmp_path: Path) -> None:
    solar_weather, solar_weights = _solar_inputs(tmp_path)
    output = tmp_path / "existing_target"
    output.mkdir()
    existing = output / SOLAR_SIGNAL_OUTPUT_NAME
    existing.write_bytes(b"preserve me")
    with pytest.raises(SystemExit) as caught:
        main([
            "solar",
            "--weather-partition",
            str(solar_weather),
            "--capacity-snapshot",
            str(solar_weights),
            "--capacity-kind",
            "monthly_location_weights",
            "--output-dir",
            str(output),
        ])
    assert caught.value.code == 2
    assert existing.read_bytes() == b"preserve me"
    assert not (output / SOLAR_LEAD_OUTPUT_NAME).exists()


def test_official_manifest_is_pinned_and_month_complete() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    manifest = repo_root / "manifests/weather_factor_inputs_2026-07-28.json"
    expected_months = {
        str(period)
        for period in pd.period_range("2016-01", "2026-07", freq="M")
    }
    for component in ("wind", "solar"):
        inputs = load_factor_inputs(manifest, component)
        assert len(inputs.weather_partitions) == 127
        assert all(item.generation for item in inputs.weather_partitions)
        assert len({item.uri for item in inputs.weather_partitions}) == 127
        assert len({item.generation for item in inputs.weather_partitions}) == 127
        observed_months = {
            f"{item.uri.split('/year=')[1][:4]}-"
            f"{item.uri.split('/month=')[1][:2]}"
            for item in inputs.weather_partitions
        }
        assert observed_months == expected_months
        filesystem = ReadOnlyPartitionFileSystem([inputs.capacity_snapshot])
        with filesystem.open(inputs.capacity_snapshot.uri, "rb") as source:
            assert len(source.read()) == inputs.capacity_snapshot.size_bytes


def test_partial_solar_weights_are_rejected(tmp_path: Path) -> None:
    solar_weather, solar_weights = _solar_inputs(tmp_path)
    weights = pd.read_parquet(solar_weights).iloc[:-1]
    weights.to_parquet(solar_weights, index=False)
    output = tmp_path / "partial_weight_output"
    with pytest.raises(SystemExit) as caught:
        main([
            "solar",
            "--weather-partition",
            str(solar_weather),
            "--capacity-snapshot",
            str(solar_weights),
            "--capacity-kind",
            "monthly_location_weights",
            "--output-dir",
            str(output),
        ])
    assert caught.value.code == 2
    assert not (output / SOLAR_LEAD_OUTPUT_NAME).exists()
    assert not (output / SOLAR_SIGNAL_OUTPUT_NAME).exists()


def test_duplicate_solar_rows_are_rejected(tmp_path: Path) -> None:
    solar_weather, solar_weights = _solar_inputs(tmp_path)
    weather = pd.read_parquet(solar_weather)
    pd.concat([weather, weather.iloc[[0]]], ignore_index=True).to_parquet(
        solar_weather,
        index=False,
    )
    output = tmp_path / "duplicate_weather_output"
    with pytest.raises(SystemExit) as caught:
        main([
            "solar",
            "--weather-partition",
            str(solar_weather),
            "--capacity-snapshot",
            str(solar_weights),
            "--capacity-kind",
            "monthly_location_weights",
            "--output-dir",
            str(output),
        ])
    assert caught.value.code == 2
    assert not (output / SOLAR_LEAD_OUTPUT_NAME).exists()
    assert not (output / SOLAR_SIGNAL_OUTPUT_NAME).exists()
