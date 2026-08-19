# Sabine nomination-revision intraday overlay — final research artifacts

This directory is the immutable-style output record produced by
`naturalgas/evaluate_sabine_nomination_revision_intraday_overlay_final.py`.
It does not replace the earlier exploratory artifacts and does not modify V03.

## Reproduction contract

The authoritative input contract is
[`manifests/sabine_nomination_overlay_inputs_2026-08-19.json`](../../../manifests/sabine_nomination_overlay_inputs_2026-08-19.json).
It records the exact GCS URI, object generation, SHA-256, byte size, row and
column counts, schema fingerprint, required columns, and local cache path for:

1. the raw 231,679-row all-cycle Sabine OAC archive;
2. the exact assembled nomination research panel; and
3. the exact processed NG entry/settlement execution-window table.

From a repository checkout with private-bucket read access, run the complete
raw-OAC-lineage rebuild:

```bash
python -m naturalgas.pipelines.rebuild_sabine_nomination_overlay --overwrite
```

This writes an atomic verified build and `reproduction_receipt.json` under
`reproduced/experiments/sabine_nomination_revision_intraday_overlay_final/`.
For repeated captures of one gas-day/cycle page, the raw rebuild first keeps
the complete snapshot with the latest native posting timestamp and requires
unique point/direction rows within that snapshot. The receipt then requires
exact raw-to-assembled revision parity, exact execution input parity, exact
output table and daily-path parity, and equality with the shipped result
summary apart from run-local timestamps and paths.

To attach the overlay to a preceding V03 GCS-lineage rebuild:

```bash
python -m naturalgas.pipelines.rebuild_model_v03 --overwrite
python -m naturalgas.pipelines.rebuild_sabine_nomination_overlay \
  --v03-daily reproduced/models/v03_d1_3_storage_guard/strategy/strategy_daily.parquet \
  --overwrite
```

Raw NYMEX trade files are controlled data and are not included. The pinned
execution-window parquet is the reproducible processed trade-price contract;
it does not represent an independent public rebuild from raw ticks.

## Selected research specification

- Signal: dominant absolute causal z-score from the TransCameron LNG I1-to-I3
  delivery revision and Jefferson Island Timely-to-I3 storage-tightness
  revision.
- Position: `0.10 * tanh(selected_revision_z)` added temporarily to V03.
- Entry: held NG contract trade VWAP from I3 posting +5 through +30 minutes.
- Exit: the same contract's settlement-window VWAP.
- Cost: 2.5 bps per unit on entry and 2.5 bps per unit on exit.
- Status: final research overlay; not a formal model version and not part of V03.

## Files

| File | Role |
|---|---|
| `summary.json` | Strategy definition, headline results, bootstrap interval, and SHA-256 input lineage. |
| `daily_strategy_path.parquet` | Score, execution, position, and return path for the selected overlay and next-session comparator. |
| `headline_metrics.csv` | Active-window and full-V03-sample metrics. |
| `annual_attribution.csv` | Event counts and incremental return by calendar year and execution timing. |
| `source_attribution.csv` | LNG and storage contribution. |
| `formulation_comparison.csv` | LNG-only, storage-only, equal, and dominant specifications. |
| `history_sensitivity.csv` | 20-, 60-, and 120-gas-day causal-history comparison. |
| `cost_sensitivity.csv` | Per-leg cost assumptions from 2.5 to 20 bps. |
| `cumulative_net_wealth.png` | Base, selected intraday, and next-session wealth paths. |
| `drawdown_comparison.png` | Common-window drawdown comparison. |
| `annual_incremental_contribution.png` | Annual contribution by execution timing. |
| `signal_and_cost_comparison.png` | Signal formulation and cost sensitivity. |

The narrative interpretation is in
[`reports/sabine_nomination_revision_intraday_overlay_final.md`](../../../reports/sabine_nomination_revision_intraday_overlay_final.md),
and the presentation notebook is
[`notebooks/08_sabine_nomination_revision_intraday_overlay_final.ipynb`](../../../notebooks/08_sabine_nomination_revision_intraday_overlay_final.ipynb).
