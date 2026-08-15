import json
from pathlib import Path

import yaml


def test_shipped_formal_summary() -> None:
    root = Path(__file__).resolve().parents[1]
    path = root / "results/models/v01_south_central_storage/summary.json"
    summary = json.loads(path.read_text(encoding="utf-8"))
    metrics = summary["selected_full_metrics"]
    assert summary["model_id"] == "hh_v01_south_central_storage"
    assert summary["model_sequence"] == 1
    assert summary["lifecycle_state"] == "frozen_formal_baseline"
    assert summary["trading_days"] == 2264
    assert abs(metrics["sharpe_zero_rf"] - 1.667459455270079) < 1e-12
    assert abs(metrics["cagr"] - 0.1458580504328515) < 1e-12


def test_model_registry_is_chronological_and_resolvable() -> None:
    root = Path(__file__).resolve().parents[1]
    registry = yaml.safe_load(
        (root / "config/model_registry.yaml").read_text(encoding="utf-8")
    )
    models = registry["models"]

    assert [model["sequence"] for model in models] == [1, 2, 3]
    assert [model["model_id"] for model in models] == [
        "hh_v01_south_central_storage",
        "hh_v02_eia930_central_florida",
        "hh_v03_d1_3_storage_guard",
    ]
    assert [model["lifecycle_state"] for model in models] == [
        "frozen_formal_baseline",
        "superseded_research",
        "current_selected_research",
    ]
    for model in models:
        assert (root / model["implementation"]).is_file()
        assert (root / model["notebook"]).is_file()
        result_dir = root / model["results"]
        assert (result_dir / "strategy_daily.parquet").is_file()
        summary = json.loads(
            (result_dir / "summary.json").read_text(encoding="utf-8")
        )
        assert summary["model_id"] == model["model_id"]
        assert summary["model_sequence"] == model["sequence"]
        assert summary["lifecycle_state"] == model["lifecycle_state"]
