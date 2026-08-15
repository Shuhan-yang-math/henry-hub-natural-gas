import json
from pathlib import Path

import yaml

from naturalgas.evaluate_native_frequency_fundamentals import (
    STRATEGY_START,
    THROUGH_DATE,
)
from naturalgas.evaluate_ncar_gdex_complete_solar_factor import (
    TRANSACTION_COST_BPS,
)
from naturalgas.evaluate_no_consumption_fundamental_weights import (
    POSITION_BOUNDS,
    SIGNAL_LAG_TRADING_SESSIONS,
)
from naturalgas.execution import EARLY_ROLL_TRADING_DAYS


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config/ng_multisignal_panel_2026-07-13.yaml"


def test_declarative_formal_config_matches_executable_policy() -> None:
    """Prevent the documented frozen policy from drifting from source code."""

    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))

    assert config["formal_strategy_cutoff"] == THROUGH_DATE.strftime("%Y-%m-%d")
    assert config["model_v01_evaluator"]["strategy_start"] == (
        STRATEGY_START.strftime("%Y-%m-%d")
    )
    assert config["model_v01_evaluator"]["through_date"] == (
        THROUGH_DATE.strftime("%Y-%m-%d")
    )
    execution = config["execution"]
    assert execution["signal_lag_trading_sessions"] == (
        SIGNAL_LAG_TRADING_SESSIONS
    )
    assert execution["roll_advance_trading_sessions"] == (
        EARLY_ROLL_TRADING_DAYS
    )
    assert execution["transaction_cost_bps_per_unit_turnover"] == (
        TRANSACTION_COST_BPS
    )
    assert tuple(float(value) for value in execution["position_bounds"]) == (
        POSITION_BOUNDS
    )


def test_formal_config_references_the_shipped_contract_files() -> None:
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    declared = {
        config["panel"]["input_manifest"],
        config["panel"]["approved_artifact_manifest"],
        config["panel"]["schema_contract"],
        config["weather_rebuild"]["input_manifest"],
        config["weather_rebuild"]["derived_capacity_parity_manifest"],
        config["wind"]["notebook_audit_inputs"]["manifest"],
    }
    for relative_path in declared:
        assert not Path(relative_path).is_absolute()
        assert (PROJECT_ROOT / relative_path).is_file(), relative_path

    selected_manifest = json.loads(
        (
            PROJECT_ROOT
            / config["weather_rebuild"]["derived_capacity_parity_manifest"]
        ).read_text(encoding="utf-8")
    )
    selected_ids = {entry["id"] for entry in selected_manifest["artifacts"]}
    assert config["weather_rebuild"][
        "frozen_wind_capacity_snapshot_artifact_id"
    ] in selected_ids
    assert config["weather_rebuild"][
        "frozen_solar_capacity_snapshot_artifact_id"
    ] in selected_ids
