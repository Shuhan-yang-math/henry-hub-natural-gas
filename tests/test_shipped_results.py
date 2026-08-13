import json
from pathlib import Path


def test_shipped_formal_summary() -> None:
    root = Path(__file__).resolve().parents[1]
    path = root / "naturalgas/processed/south_central_storage_strategy/summary.json"
    summary = json.loads(path.read_text(encoding="utf-8"))
    metrics = summary["selected_full_metrics"]
    assert summary["strategy_version"] == "south_central_total_storage"
    assert summary["trading_days"] == 2264
    assert abs(metrics["sharpe_zero_rf"] - 1.667459455270079) < 1e-12
    assert abs(metrics["cagr"] - 0.1458580504328515) < 1e-12
