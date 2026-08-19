from __future__ import annotations

import re
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MATH_DOCUMENTS = (
    Path("MODEL_CARD.md"),
    Path("reports/comprehensive_strategy_report.md"),
    Path("reports/henry_hub_strategy_report_rewrite.md"),
    Path("reports/model_v01_development_history_2026-08-05.md"),
    Path("reports/model_v02_eia930_central_florida_brief.md"),
    Path("reports/sabine_nomination_revision_intraday_overlay_final.md"),
)


def _math_blocks(text: str) -> list[str]:
    lines = text.splitlines()
    blocks: list[str] = []
    index = 0
    while index < len(lines):
        if lines[index].strip() != "```math":
            index += 1
            continue
        start = index + 1
        index = start
        while index < len(lines) and lines[index].strip() != "```":
            index += 1
        assert index < len(lines), f"unclosed math fence beginning on line {start}"
        blocks.append("\n".join(lines[start:index]))
        index += 1
    return blocks


@pytest.mark.parametrize("relative_path", MATH_DOCUMENTS, ids=str)
def test_markdown_uses_github_safe_math_syntax(relative_path: Path) -> None:
    text = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")

    assert "\\operatorname" not in text
    assert "$$" not in text
    assert not re.search(r"(?m)^\\\[$|^\\\]$", text)
    assert not re.search(r"\\\([^\n]+\\\)", text)
    assert text.count("$`") == text.count("`$")

    blocks = _math_blocks(text)
    assert blocks, f"no GitHub math blocks found in {relative_path}"

    for block in blocks:
        for environment in ("cases", "aligned"):
            assert block.count(rf"\begin{{{environment}}}") == block.count(
                rf"\end{{{environment}}}"
            )

        for match in re.finditer(
            r"\\begin\{cases\}(.*?)\\end\{cases\}", block, flags=re.DOTALL
        ):
            rows = [line.strip() for line in match.group(1).splitlines() if line.strip()]
            assert all("&" in row for row in rows), (
                "keep each cases branch on one physical line for GitHub MathJax: "
                f"{rows}"
            )
