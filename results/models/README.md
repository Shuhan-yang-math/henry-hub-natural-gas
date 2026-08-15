# Canonical model results

These directories are ordered model releases, not a collection of unrelated
experiments. The stable order is:

| Directory | Lifecycle state | Meaning |
|---|---|---|
| `v01_south_central_storage/` | Frozen formal baseline | Historical full-sample provenance baseline |
| `v02_eia930_central_florida/` | Superseded research | Prior selected EIA-930 Central/Florida model |
| `v03_d1_3_storage_guard/` | Current selected research | Current D1--3 wind and storage-guard model |

Every directory uses `strategy_daily.parquet` for its canonical daily result
and `summary.json` for model identity, lifecycle state, and headline metrics.
Factor-development outputs remain under `results/experiments/`. The complete
version metadata and source paths are in `config/model_registry.yaml`.
