from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
import pandas as pd

import naturalgas.sync_documentation_metrics as documentation_sync
from naturalgas.sync_documentation_metrics import (
    render_documents,
    stale_documents,
    synchronize_documents,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
D1_RESULTS = PROJECT_ROOT / "results/experiments/d1_3_storage_amplified"
EIA_RESULTS = PROJECT_ROOT / "results/experiments/eia930_selected"


def _read(relative_path: str) -> str:
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def _normalized(relative_path: str) -> str:
    return " ".join(_read(relative_path).split())


def test_generated_documentation_is_current() -> None:
    for relative_path, rendered in render_documents(PROJECT_ROOT).items():
        assert _read(str(relative_path)) == rendered


def test_automatic_sync_is_limited_to_canonical_output(
    monkeypatch,
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "results/canonical"
    calls: list[Path] = []

    def record_sync(root: Path) -> tuple[Path, ...]:
        calls.append(root)
        return (Path("README.md"),)

    monkeypatch.setattr(
        documentation_sync,
        "synchronize_documents",
        record_sync,
    )

    assert documentation_sync.synchronize_after_canonical_result(
        output_dir=tmp_path / "reproduced/strategy",
        canonical_output_dir=canonical,
        root=tmp_path,
    ) == ()
    assert calls == []

    assert documentation_sync.synchronize_after_canonical_result(
        output_dir=canonical,
        canonical_output_dir=canonical,
        root=tmp_path,
    ) == (Path("README.md"),)
    assert calls == [tmp_path]


def test_synchronize_documents_repairs_a_stale_generated_block(
    tmp_path: Path,
) -> None:
    required_files = (
        "README.md",
        "MODEL_CARD.md",
        "reports/d1_3_storage_amplified_strategy_brief.md",
        "reports/eia930_central_florida_40_60_brief.md",
        "results/experiments/d1_3_storage_amplified/strategy_metrics.csv",
        "results/experiments/d1_3_storage_amplified/"
        "selected_strategy_daily.parquet",
        "results/experiments/eia930_selected/summary.json",
    )
    for relative_path in required_files:
        target = tmp_path / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PROJECT_ROOT / relative_path, target)

    readme = tmp_path / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8").replace("2.228", "9.999", 1),
        encoding="utf-8",
    )

    assert stale_documents(tmp_path) == (Path("README.md"),)
    assert synchronize_documents(tmp_path) == (Path("README.md"),)
    assert stale_documents(tmp_path) == ()
    assert "9.999" not in readme.read_text(encoding="utf-8")


def test_selected_strategy_documentation_matches_metrics_csv() -> None:
    metrics = pd.read_csv(D1_RESULTS / "strategy_metrics.csv").set_index(
        "variant"
    )
    current = metrics.loc["d1_5_current"]
    no_guard = metrics.loc["d1_3_no_guard"]
    selected = metrics.loc["d1_3_storage_amplified"]
    rows = (current, no_guard, selected)

    sharpe_values = tuple(f"{row['sharpe']:.3f}" for row in rows)
    sortino_values = tuple(f"{row['sortino']:.3f}" for row in rows)
    cagr_values = tuple(f"{row['cagr']:.2%}" for row in rows)
    drawdown_values = tuple(
        f"{row['maximum_drawdown']:.2%}" for row in rows
    )
    return_values = tuple(f"{row['total_return']:.2%}" for row in rows)
    position_values = tuple(
        f"{row['mean_absolute_position']:.2%}" for row in rows
    )
    drawdown_improvement_pp = (
        selected["maximum_drawdown"] - no_guard["maximum_drawdown"]
    ) * 100.0
    markdown_sharpe = (
        f"| Net Sharpe | {sharpe_values[0]} | {sharpe_values[1]} | "
        f"**{sharpe_values[2]}** |"
    )
    markdown_sortino = (
        f"| Net Sortino | {sortino_values[0]} | {sortino_values[1]} | "
        f"**{sortino_values[2]}** |"
    )
    markdown_cagr = (
        f"| Net CAGR | **{cagr_values[0]}** | {cagr_values[1]} | "
        f"{cagr_values[2]} |"
    )
    markdown_drawdown = (
        f"| Maximum drawdown | {drawdown_values[0]} | "
        f"{drawdown_values[1]} | **{drawdown_values[2]}** |"
    )
    markdown_return = (
        f"| Total net return | **{return_values[0]}** | "
        f"{return_values[1]} | {return_values[2]} |"
    )

    for path in ("README.md", "MODEL_CARD.md"):
        document = _read(path)
        for row in (
            markdown_sharpe,
            markdown_sortino,
            markdown_cagr,
            markdown_drawdown,
            markdown_return,
        ):
            assert row in document
        normalized = " ".join(document.split())
        assert (
            "improves maximum drawdown from "
            f"{drawdown_values[1]} to {drawdown_values[2]}, a "
            f"{drawdown_improvement_pp:.2f} percentage-point reduction in "
            "drawdown depth"
        ) in normalized

    brief = _read("reports/d1_3_storage_amplified_strategy_brief.md")
    assert markdown_sharpe in brief
    assert (
        f"| Sortino | {sortino_values[0]} | {sortino_values[1]} | "
        f"**{sortino_values[2]}** |"
    ) in brief
    assert (
        f"| CAGR | **{cagr_values[0]}** | {cagr_values[1]} | "
        f"{cagr_values[2]} |"
    ) in brief
    assert markdown_drawdown in brief
    assert markdown_return in brief
    assert (
        f"| Mean absolute position | {position_values[0]} | "
        f"{position_values[1]} | **{position_values[2]}** |"
    ) in brief
    assert (
        "improves maximum drawdown from "
        f"{drawdown_values[1]} to {drawdown_values[2]}, a "
        f"{drawdown_improvement_pp:.2f} percentage-point reduction in "
        "drawdown depth"
    ) in " ".join(brief.split())

    comprehensive = _read("reports/comprehensive_strategy_report.md")
    for row in (
        markdown_sharpe,
        markdown_sortino,
        markdown_cagr,
        markdown_drawdown,
        markdown_return,
    ):
        assert row in comprehensive
    assert (
        "improves maximum drawdown from "
        f"{drawdown_values[1]} to {drawdown_values[2]}, a "
        f"{drawdown_improvement_pp:.2f} percentage-point reduction in "
        "drawdown depth"
    ) in " ".join(comprehensive.split())
    assert (
        f"| D1--3, no guard | 2.757 | 2.232 | 1.779 | 2.181 | "
        f"{cagr_values[1]} | {drawdown_values[1]} |"
    ) in comprehensive
    assert (
        f"| **Selected D1--3 + storage amplifier** | 2.782 | **2.279** | "
        f"**1.835** | **2.228** | {cagr_values[2]} | "
        f"**{drawdown_values[2]}** |"
    ) in comprehensive

    tex = _read("reports/comprehensive_strategy_report.tex")
    assert (
        f"Net CAGR & \\textbf{{{cagr_values[0][:-1]}\\%}} & "
        f"{cagr_values[1][:-1]}\\% & {cagr_values[2][:-1]}\\%"
    ) in tex
    assert (
        "improves maximum drawdown from "
        f"{drawdown_values[1][:-1]}\\% to "
        f"{drawdown_values[2][:-1]}\\%, a "
        f"{drawdown_improvement_pp:.2f} percentage-point reduction in "
        "drawdown depth"
    ) in " ".join(tex.split())

    research_log = _read("RESEARCH_LOG.md")
    assert f"{cagr_values[2]} CAGR, and {drawdown_values[2]}" in research_log

    notebook = _read("notebooks/07_d1_3_storage_amplified_strategy.ipynb")
    assert f">{cagr_values[1]}</td>" in notebook
    assert f">{cagr_values[2]}</td>" in notebook


def test_eia_documentation_matches_summary_json() -> None:
    summary = json.loads((EIA_RESULTS / "summary.json").read_text())
    result_rows = (
        summary["baseline_metrics"],
        summary["current_central_metrics"],
        summary["selected_metrics"],
    )
    sharpe_values = tuple(f"{row['sharpe']:.3f}" for row in result_rows)
    sortino_values = tuple(f"{row['sortino']:.3f}" for row in result_rows)
    cagr_values = tuple(f"{row['cagr']:.2%}" for row in result_rows)
    drawdown_values = tuple(
        f"{row['maximum_drawdown']:.2%}" for row in result_rows
    )
    position_values = tuple(
        f"{row['mean_absolute_position']:.2%}" for row in result_rows
    )
    return_values = tuple(f"{row['total_return']:.2%}" for row in result_rows)
    delta_sharpe = summary["change_vs_current_central"]["sharpe"]
    delta_sortino = summary["change_vs_current_central"]["sortino"]
    simple_return_pp = (
        summary["change_vs_current_central"][
            "cumulative_incremental_net_return"
        ]
        * 100.0
    )
    final_wealth_pp = (
        summary["selected_metrics"]["total_return"]
        - summary["current_central_metrics"]["total_return"]
    ) * 100.0

    brief = _normalized("reports/eia930_central_florida_40_60_brief.md")
    assert (
        f"raises Sharpe by {delta_sharpe:.3f} and Sortino by "
        f"{delta_sortino:.3f}."
    ) in brief

    brief_raw = _read("reports/eia930_central_florida_40_60_brief.md")
    for row in (
        f"| Net Sharpe | {sharpe_values[0]} | {sharpe_values[1]} | "
        f"**{sharpe_values[2]}** |",
        f"| Sortino | {sortino_values[0]} | {sortino_values[1]} | "
        f"**{sortino_values[2]}** |",
        f"| CAGR | {cagr_values[0]} | {cagr_values[1]} | "
        f"**{cagr_values[2]}** |",
        f"| Maximum drawdown | {drawdown_values[0]} | "
        f"{drawdown_values[1]} | **{drawdown_values[2]}** |",
        f"| Mean absolute position | {position_values[0]} | "
        f"{position_values[1]} | **{position_values[2]}** |",
        f"| Final cumulative return | {return_values[0]} | "
        f"{return_values[1]} | **{return_values[2]}** |",
    ):
        assert row in brief_raw
    assert (
        f"simple sum of daily incremental net returns is "
        f"+{simple_return_pp:.2f} percentage points"
    ) in brief
    assert (
        f"compounded final-wealth level is {final_wealth_pp:.2f} "
        f"percentage points higher"
    ) in brief

    research_log = _normalized("RESEARCH_LOG.md")
    assert f"Central is +{simple_return_pp:.2f} percentage points" in research_log
    assert (
        f"compounded final-wealth difference is +{final_wealth_pp:.2f} "
        f"percentage points"
    ) in research_log

    readme = _normalized("README.md")
    assert f"improves Sharpe by {delta_sharpe:.3f}" in readme
    assert f"Sortino by {delta_sortino:.3f}" in readme
    assert f"final-wealth difference is +{final_wealth_pp:.2f}" in readme

    readme_raw = _read("README.md")
    for row in (
        f"| Net Sharpe | {sharpe_values[0]} | {sharpe_values[1]} | "
        f"**{sharpe_values[2]}** |",
        f"| Net Sortino | {sortino_values[0]} | {sortino_values[1]} | "
        f"**{sortino_values[2]}** |",
        f"| Net CAGR | {cagr_values[0]} | {cagr_values[1]} | "
        f"**{cagr_values[2]}** |",
        f"| Maximum drawdown | {drawdown_values[0]} | "
        f"{drawdown_values[1]} | **{drawdown_values[2]}** |",
        f"| Total net return | {return_values[0]} | {return_values[1]} | "
        f"**{return_values[2]}** |",
    ):
        assert row in readme_raw


def test_metric_definitions_and_sensitivities_are_disclosed() -> None:
    daily = pd.read_parquet(
        D1_RESULTS / "selected_strategy_daily.parquet",
        columns=["net_return__d1_3_storage_amplified"],
    )
    net = daily["net_return__d1_3_storage_amplified"]
    log_return = np.log1p(net)
    arithmetic_sharpe = float(net.mean() / net.std(ddof=1) * np.sqrt(252.0))
    negative = log_return.loc[log_return.lt(0.0)]
    conditional_sortino = float(
        log_return.mean()
        * 252.0
        / (np.sqrt(np.square(negative).mean()) * np.sqrt(252.0))
    )

    for path in (
        "README.md",
        "MODEL_CARD.md",
        "reports/d1_3_storage_amplified_strategy_brief.md",
        "reports/comprehensive_strategy_report.md",
    ):
        document = _read(path)
        assert "daily log net return" in document
        assert "unconditional lower-partial-moment" in document
        assert f"{arithmetic_sharpe:.3f}" in document
        assert f"{conditional_sortino:.3f}" in document


def test_known_stale_metric_claims_are_absent() -> None:
    documents = "\n".join(
        _read(path)
        for path in (
            "README.md",
            "MODEL_CARD.md",
            "RESEARCH_LOG.md",
            "reports/d1_3_storage_amplified_strategy_brief.md",
            "reports/eia930_central_florida_40_60_brief.md",
            "reports/comprehensive_strategy_report.md",
            "reports/comprehensive_strategy_report.tex",
            "notebooks/07_d1_3_storage_amplified_strategy.ipynb",
        )
    )
    stale_drawdown = (
        "| Maximum drawdown | -5.30% | **-4.16%** | **-4.16%** |"
    )
    assert stale_drawdown not in documents
    assert "18.75%" not in documents
    assert "19.06%" not in documents
    assert "raises Sharpe by 0.125" not in documents
    assert "Sortino by 0.313" not in documents
    assert "Central is +0.14 percentage points" not in documents
    assert "compounded final wealth is 1.51" not in documents
