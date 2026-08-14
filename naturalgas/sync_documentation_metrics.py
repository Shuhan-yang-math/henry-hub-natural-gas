"""Synchronize published strategy metrics with checked-in result artifacts.

The generated blocks deliberately live in human-facing Markdown files. Run
this module after rebuilding results, or use ``--check`` in CI to fail when a
document has drifted from its canonical CSV/JSON source.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
D1_RESULTS = Path("results/experiments/d1_3_storage_amplified")
EIA_RESULTS = Path("results/experiments/eia930_selected")

BEGIN = "<!-- BEGIN AUTO-GENERATED: {name} -->"
END = "<!-- END AUTO-GENERATED: {name} -->"


def _metric_conventions(root: Path) -> tuple[str, str]:
    metrics = pd.read_csv(root / D1_RESULTS / "strategy_metrics.csv").set_index(
        "variant"
    )
    selected = metrics.loc["d1_3_storage_amplified"]
    daily = pd.read_parquet(
        root / D1_RESULTS / "selected_strategy_daily.parquet",
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

    paragraph = f"""The selected D1--3 and EIA-930 tables report zero-risk-free-rate Sharpe and
Sortino ratios from daily log net returns, `g_t = log(1 + r_t)`. Sharpe is
`mean(g_t) / sample_std(g_t) * sqrt(252)`. Sortino is `mean(g_t) * 252`
divided by the zero-target unconditional lower-partial-moment denominator
`sqrt(mean(min(g_t, 0)^2)) * sqrt(252)`; positive-return days therefore enter
the downside average as zeros. This is not the conditional-negative-day
Sortino convention. For the selected strategy, arithmetic-return Sharpe is
{arithmetic_sharpe:.3f} versus the reported {selected['sharpe']:.3f}, and conditional-negative-day log Sortino is
{conditional_sortino:.3f} versus the reported {selected['sortino']:.3f}. CAGR uses the actual first settlement
endpoint, maximum drawdown begins from initial wealth 1.0, and all reported
ratios use 252 sessions per year."""

    bullets = f"""- Risk-free rate: zero in the reported Sharpe and Sortino ratios.
- Return basis: daily log net return, `g_t = log(1 + r_t)`.
- Sharpe: `mean(g_t) / sample_std(g_t) * sqrt(252)`.
- Sortino: `mean(g_t) * 252` divided by
  `sqrt(mean(min(g_t, 0)^2)) * sqrt(252)`. This is a zero-target,
  unconditional lower-partial-moment definition: positive-return days enter
  the downside average as zeros, rather than being removed.
- CAGR: compound every included return over the calendar span beginning at
  the first return interval's actual prior settlement endpoint.

For comparison, the selected strategy's arithmetic-return Sharpe is {arithmetic_sharpe:.3f}
versus the reported log-return Sharpe of {selected['sharpe']:.3f}. A conditional-negative-day log
Sortino is {conditional_sortino:.3f} versus the reported unconditional-LPM value of {selected['sortino']:.3f}. These
alternatives are sensitivities, not the shipped metric definitions."""
    return paragraph, bullets


def _d1_blocks(root: Path) -> dict[str, str]:
    metrics = pd.read_csv(root / D1_RESULTS / "strategy_metrics.csv").set_index(
        "variant"
    )
    current = metrics.loc["d1_5_current"]
    no_guard = metrics.loc["d1_3_no_guard"]
    selected = metrics.loc["d1_3_storage_amplified"]
    rows = (current, no_guard, selected)
    sample = f"{selected['start']} to {selected['end']}"
    dates = f"{selected['start']}–{selected['end']}"

    def values(field: str, formatter: str) -> tuple[str, str, str]:
        formatted = tuple(format(float(row[field]), formatter) for row in rows)
        return formatted[0], formatted[1], formatted[2]

    sharpe = values("sharpe", ".3f")
    sortino = values("sortino", ".3f")
    cagr = values("cagr", ".2%")
    drawdown = values("maximum_drawdown", ".2%")
    total_return = values("total_return", ".2%")
    position = values("mean_absolute_position", ".2%")
    improvement = (
        selected["maximum_drawdown"] - no_guard["maximum_drawdown"]
    ) * 100.0

    full_table = f"""| Common-overlap metric | Current D1--5 | D1--3, no guard | Selected D1--3 + storage amplifier |
|---|---:|---:|---:|
| Sample | {sample} | same | same |
| Trading days | {int(selected['trading_days']):,} | {int(selected['trading_days']):,} | {int(selected['trading_days']):,} |
| Net Sharpe | {sharpe[0]} | {sharpe[1]} | **{sharpe[2]}** |
| Net Sortino | {sortino[0]} | {sortino[1]} | **{sortino[2]}** |
| Net CAGR | **{cagr[0]}** | {cagr[1]} | {cagr[2]} |
| Maximum drawdown | {drawdown[0]} | {drawdown[1]} | **{drawdown[2]}** |
| Total net return | **{total_return[0]}** | {total_return[1]} | {total_return[2]} |"""
    model_table = f"""| Metric | Current D1--5 | D1--3, no guard | Selected D1--3 + storage amplifier |
|---|---:|---:|---:|
| Dates | {dates} | same | same |
| Net Sharpe | {sharpe[0]} | {sharpe[1]} | **{sharpe[2]}** |
| Net Sortino | {sortino[0]} | {sortino[1]} | **{sortino[2]}** |
| Net CAGR | **{cagr[0]}** | {cagr[1]} | {cagr[2]} |
| Maximum drawdown | {drawdown[0]} | {drawdown[1]} | **{drawdown[2]}** |
| Total net return | **{total_return[0]}** | {total_return[1]} | {total_return[2]} |"""
    brief_table = f"""| Metric | Current D1--5 | D1--3, no guard | **Selected D1--3 + storage amplifier** |
|---|---:|---:|---:|
| Net Sharpe | {sharpe[0]} | {sharpe[1]} | **{sharpe[2]}** |
| Sortino | {sortino[0]} | {sortino[1]} | **{sortino[2]}** |
| CAGR | **{cagr[0]}** | {cagr[1]} | {cagr[2]} |
| Maximum drawdown | {drawdown[0]} | {drawdown[1]} | **{drawdown[2]}** |
| Total net return | **{total_return[0]}** | {total_return[1]} | {total_return[2]} |
| Mean absolute position | {position[0]} | {position[1]} | **{position[2]}** |"""
    drawdown_claim = f"""Relative to unguarded D1--3, the guard improves maximum drawdown from {drawdown[1]}
to {drawdown[2]}, a {improvement:.2f} percentage-point reduction in drawdown depth."""
    return {
        "d1-full-table": full_table,
        "d1-model-table": model_table,
        "d1-brief-table": brief_table,
        "d1-drawdown-claim": drawdown_claim,
    }


def _eia_blocks(root: Path) -> dict[str, str]:
    summary = json.loads((root / EIA_RESULTS / "summary.json").read_text())
    baseline = summary["baseline_metrics"]
    central = summary["current_central_metrics"]
    selected = summary["selected_metrics"]
    rows = (baseline, central, selected)

    def values(field: str, formatter: str) -> tuple[str, str, str]:
        formatted = tuple(format(float(row[field]), formatter) for row in rows)
        return formatted[0], formatted[1], formatted[2]

    sharpe = values("sharpe", ".3f")
    sortino = values("sortino", ".3f")
    cagr = values("cagr", ".2%")
    drawdown = values("maximum_drawdown", ".2%")
    total_return = values("total_return", ".2%")
    position = values("mean_absolute_position", ".2%")
    sample = f"{str(selected['start'])[:10]} to {str(selected['end'])[:10]}"
    delta = summary["change_vs_current_central"]
    simple_pp = delta["cumulative_incremental_net_return"] * 100.0
    wealth_pp = (selected["total_return"] - central["total_return"]) * 100.0
    drawdown_pp = (
        selected["maximum_drawdown"] - central["maximum_drawdown"]
    ) * 100.0

    full_table = f"""| Common-overlap metric | Weather, fundamentals, and event veto | Previous 10% Central sleeve | Selected Central 40% / Florida 60% |
|---|---:|---:|---:|
| Sample | {sample} | same | same |
| Trading days | {int(selected['trading_days']):,} | {int(selected['trading_days']):,} | {int(selected['trading_days']):,} |
| Net Sharpe | {sharpe[0]} | {sharpe[1]} | **{sharpe[2]}** |
| Net Sortino | {sortino[0]} | {sortino[1]} | **{sortino[2]}** |
| Net CAGR | {cagr[0]} | {cagr[1]} | **{cagr[2]}** |
| Maximum drawdown | {drawdown[0]} | {drawdown[1]} | **{drawdown[2]}** |
| Total net return | {total_return[0]} | {total_return[1]} | **{total_return[2]}** |"""
    brief_table = f"""| Metric | Core weather, fundamentals, and veto | Previous 10% Central sleeve | Selected Central 40% / Florida 60% |
|---|---:|---:|---:|
| Net Sharpe | {sharpe[0]} | {sharpe[1]} | **{sharpe[2]}** |
| Sortino | {sortino[0]} | {sortino[1]} | **{sortino[2]}** |
| CAGR | {cagr[0]} | {cagr[1]} | **{cagr[2]}** |
| Maximum drawdown | {drawdown[0]} | {drawdown[1]} | **{drawdown[2]}** |
| Mean absolute position | {position[0]} | {position[1]} | **{position[2]}** |
| Final cumulative return | {total_return[0]} | {total_return[1]} | **{total_return[2]}** |"""
    readme_claim = f"""Relative to the Central sleeve, the selected blend improves Sharpe by {delta['sharpe']:.3f},
Sortino by {delta['sortino']:.3f}, and maximum drawdown by {drawdown_pp:.2f} percentage points. Its simple
sum of daily incremental net returns is +{simple_pp:.2f} percentage points; the distinct
compounded final-wealth difference is +{wealth_pp:.2f} percentage points. The benefit is
downside diversification rather than a large unconditional daily-return
increment. It is an incremental research enhancement, not a rewrite of the
approved 2017 full-history baseline."""
    brief_claim = f"""Relative to the Central sleeve, the selected version raises Sharpe by {delta['sharpe']:.3f}
and Sortino by {delta['sortino']:.3f}. The simple sum of daily incremental net returns is
+{simple_pp:.2f} percentage points, while the distinct compounded final-wealth level is
{wealth_pp:.2f} percentage points higher."""
    return {
        "eia-full-table": full_table,
        "eia-brief-table": brief_table,
        "eia-readme-claim": readme_claim,
        "eia-brief-claim": brief_claim,
    }


def _replace_block(text: str, name: str, body: str, path: Path) -> str:
    begin = BEGIN.format(name=name)
    end = END.format(name=name)
    pattern = re.compile(
        rf"{re.escape(begin)}\n.*?\n{re.escape(end)}", re.DOTALL
    )
    replacement = f"{begin}\n{body.rstrip()}\n{end}"
    updated, count = pattern.subn(replacement, text)
    if count != 1:
        raise ValueError(f"expected one generated block {name!r} in {path}")
    return updated


def render_documents(root: Path = PROJECT_ROOT) -> dict[Path, str]:
    paragraph, bullets = _metric_conventions(root)
    blocks = _d1_blocks(root) | _eia_blocks(root) | {
        "metric-conventions": paragraph,
        "metric-conventions-bullets": bullets,
    }
    assignments = {
        Path("README.md"): (
            "d1-full-table",
            "d1-drawdown-claim",
            "metric-conventions",
            "eia-full-table",
            "eia-readme-claim",
        ),
        Path("MODEL_CARD.md"): (
            "metric-conventions-bullets",
            "d1-model-table",
            "d1-drawdown-claim",
        ),
        Path("reports/d1_3_storage_amplified_strategy_brief.md"): (
            "metric-conventions",
            "d1-brief-table",
            "d1-drawdown-claim",
        ),
        Path("reports/eia930_central_florida_40_60_brief.md"): (
            "metric-conventions",
            "eia-brief-table",
            "eia-brief-claim",
        ),
    }
    rendered: dict[Path, str] = {}
    for relative_path, names in assignments.items():
        path = root / relative_path
        text = path.read_text(encoding="utf-8")
        for name in names:
            text = _replace_block(text, name, blocks[name], path)
        rendered[relative_path] = text
    return rendered


def stale_documents(root: Path = PROJECT_ROOT) -> tuple[Path, ...]:
    """Return generated documents that differ from their canonical sources."""
    return tuple(
        path
        for path, text in render_documents(root).items()
        if (root / path).read_text(encoding="utf-8") != text
    )


def synchronize_documents(root: Path = PROJECT_ROOT) -> tuple[Path, ...]:
    """Render stale generated blocks and return the updated relative paths."""
    rendered = render_documents(root)
    stale = tuple(
        path
        for path, text in rendered.items()
        if (root / path).read_text(encoding="utf-8") != text
    )
    for path in stale:
        (root / path).write_text(rendered[path], encoding="utf-8")
    return stale


def synchronize_after_canonical_result(
    *,
    output_dir: Path,
    canonical_output_dir: Path,
    root: Path = PROJECT_ROOT,
) -> tuple[Path, ...]:
    """Synchronize only when an evaluator published to its official target."""
    if output_dir.expanduser().resolve() != canonical_output_dir.resolve():
        return ()
    return synchronize_documents(root)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail instead of writing when generated documentation is stale",
    )
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    args = parser.parse_args()

    if args.check:
        stale = stale_documents(args.root)
        if stale:
            print("stale generated documentation:")
            for path in stale:
                print(f"- {path}")
            return 1
        return 0

    for path in synchronize_documents(args.root):
        print(f"updated {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
