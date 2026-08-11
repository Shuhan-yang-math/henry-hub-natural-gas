# Data manifest

Large model inputs are intentionally excluded from Git. Access requires
Braeswood read credentials for bucket `bcli-natgas-data-497807`.

The authoritative machine-readable inventory is
[`manifests/input_artifacts_2026-07-13.json`](manifests/input_artifacts_2026-07-13.json).
It pins every object by GCS generation and SHA-256 and also records byte size,
row/column counts, date coverage, local path, and an Arrow schema fingerprint.
Exact schemas are retained in
[`schemas/input_schemas_2026-07-13.json`](schemas/input_schemas_2026-07-13.json).

## Supported full rebuild

From `henry-hub-natural-gas/`, install the Python dependencies and run:

```bash
python -m naturalgas.pipelines.rebuild_all --overwrite
```

The default command performs five steps:

1. validates and downloads the 72 generation-pinned direct inputs used by the
   155-column master-panel builder;
2. validates and downloads 127 wind plus 127 solar NCAR/GDEX partitions and
   reads two checked-in capacity-weight snapshots;
3. rebuilds the master panel and the three selected wind/solar artifacts, then
   verifies every derived parquet against its approved SHA-256;
4. routes the three EIA reads through immutable local snapshots rather than
   mutable live GCS keys; and
5. rebuilds the formal result and verifies its headline metrics and complete
   summary-file SHA-256 against the shipped summary.

For a quicker audit that rebuilds the master panel but downloads the three
approved wind/solar artifacts, run:

```bash
python -m naturalgas.pipelines.rebuild_all \
  --use-approved-weather-artifacts --overwrite
```

The narrow seven-object processed-input rebuild remains available as:

```bash
python -m naturalgas.pipelines.rebuild_final_backtest --overwrite
```

Once the seven formal inputs exist locally and validate, that narrow build can
run without a network request:

```bash
python -m naturalgas.pipelines.rebuild_final_backtest --offline --overwrite
```

## Pinned formal inputs

| Artifact id | GCS generation | Rows × columns | Formal use |
|---|---:|---:|---|
| `ng_multisignal_panel` | `1785435608499104` | 8,149 × 155 | futures, legacy weather, and precomputed signal panel |
| `capacity_weighted_wind_features_daily` | `1785904479037568` | 3,857 × 43 | selected nonlinear capacity-weighted **00Z-only** wind signal |
| `capacity_weighted_solar_signals` | `1785904479034390` | 15,448 × 17 | four-cycle solar signals; evaluator selects 00Z |
| `capacity_weighted_location_leads` | `1785904479072012` | 77,225 × 14 | solar issue × lead daylight/capacity context |
| `storage_weekly` | `1784674812651746` | 862 × 9 | Lower 48 and regional weekly storage snapshots |
| `fundamentals_monthly` | `1784674813059617` | 640 × 5 | dry production, consumption, exports, and imports |
| `eia_country_monthly` | `1784765115754980` | 21,735 × 20 | U.S. aggregate LNG export history |

Do not replace these with unqualified `gs://` downloads when reproducing the
2026-07-13 result. A bucket key can be overwritten while retaining the same
name; the generation and content hash are the identity of an approved input.

## Local paths

The downloader materializes the four model files at the paths already used by
the evaluators:

```text
naturalgas/processed/ng_multisignal_score/ng_multisignal_panel.parquet
naturalgas/processed/ncar_gdex_complete_wind_factor/capacity_weighted_wind_features_daily.parquet
naturalgas/processed/ncar_gdex_capacity_weighted_solar/capacity_weighted_solar_signals.parquet
naturalgas/processed/ncar_gdex_capacity_weighted_solar/capacity_weighted_location_leads.parquet
```

The three EIA objects are downloaded to `inputs/gcs/` and exposed to the
legacy factor functions through a read-only local filesystem adapter. No GCS
object is modified.

Notebook 02 also uses two small, checked-in audit tables:

```text
inputs/audit/wind/annual_location_weights.csv
inputs/audit/wind/annual_fleet_diagnostics.csv
```

These tables explain the historical capacity weighting and fleet diagnostics;
they are not additional formal evaluator inputs.

## Selected EIA-930 enhancement inputs

The selected enhancement uses two compact, checked-in audit inputs. They are
separate from the seven pinned inputs of the approved formal rebuild.

| Path | Rows | Coverage | SHA-256 | Use |
|---|---:|---|---|---|
| `inputs/audit/eia930/selected_overlay_inputs.parquet` | 1,738 | 2019-07-24–2026-07-13 score dates | `bbaa1b948df815842feaa6b11a42fdc7d92d099b5f001eeb26adb6bc2daa3fee` | Central total non-gas and Florida firm non-gas share shortfalls, production short-block state, and lineage |
| `inputs/audit/events/event_reports_aligned.parquet` | 101 | 2017-08-24–2024-09-29 | `f1a99a286c1a2a5b7b03990edfec08786aa9a56e0b7f5ad88417450fb984fb1b` | BSEE/Sabine event-controller registry |

The selected evaluator reads these inputs together with
`naturalgas/processed/south_central_storage_strategy/strategy_daily.parquet`
and writes:

```text
results/experiments/eia930_selected/selected_strategy_daily.parquet
results/experiments/eia930_selected/annual_metrics.csv
results/experiments/eia930_selected/central_florida_weight_sweep.csv
results/experiments/eia930_selected/loss_day_yearly.csv
results/experiments/eia930_selected/event_report_registry.parquet
results/experiments/eia930_selected/latest_strategy_dashboard.png
results/experiments/eia930_selected/central_florida_weight_sweep.png
results/experiments/eia930_selected/summary.json
```

Rebuild those artifacts with:

```bash
python naturalgas/evaluate_eia930_selected_enhancement.py
```

The byte-exact weather rebuild additionally uses the checked-in frozen
capacity snapshots below. The Parquet wind snapshot preserves the original
floating-point values used by the builder; the CSV remains a human-readable
notebook audit table.

```text
inputs/audit/wind/annual_location_weights.parquet
inputs/audit/solar/monthly_location_weights.parquet
```

## Wind source correction

The selected wind artifact contains one row per 00Z initialization. Although
the upstream raw archive contains the 00Z, 06Z, 12Z, and 18Z cycles, the
complete factor builder filters `forecast_cycle_hour_utc == 0` before writing
`capacity_weighted_wind_features_daily.parquet`.

The direct source for this capacity-weighted build is:

```text
gs://bcli-natgas-data-497807/raw/weather/ncar_gdex/d084001/
  wind_points/model=ncep_gfs_0p25/cycle=all/year=*/month=*/data.parquet
```

It contains point-level 80 m wind fields for the 28 representative locations,
forecast days 1–5, and valid hours 00/06/12/18 UTC. The
`processed/weather/.../wind_daily/` tree is a separate intermediate used by an
earlier equal-location path; it is not the direct source of the selected
capacity-weighted artifact.

## Reproduction boundary

This handoff supports reproduction of the formal 2017-07-03 through 2026-07-13
backtest from immutable internal base objects. The panel object itself extends
to 2026-07-17, but the frozen configuration applies the 2026-07-13 cutoff.

The three inventories have different roles:

- [`manifests/master_panel_inputs_2026-07-13.json`](manifests/master_panel_inputs_2026-07-13.json)
  pins 72 direct master-panel inputs;
- [`manifests/weather_factor_inputs_2026-07-28.json`](manifests/weather_factor_inputs_2026-07-28.json)
  pins 254 weather partitions and the frozen capacity snapshots; and
- [`manifests/input_artifacts_2026-07-13.json`](manifests/input_artifacts_2026-07-13.json)
  pins the seven approved processed artifacts used by the narrow rebuild and
  as parity targets for the broader build.

This is not a guarantee that re-querying current public APIs reproduces the
same history. Upstream CPC, futures, EIA, USWTDB, Open-Meteo, FRED, and related
sources can revise or lack complete first-release archives. A present-day API
download is therefore a data refresh, not a bit-exact rebuild of this model.

## Revision and vintage caveats

- EIA weekly/monthly files may reflect later revisions; they are not guaranteed
  first-release historical vintages.
- GFS files preserve forecast issue/reference time and therefore support
  vintage-aware weather analysis.
- Installed wind/solar capacity histories are lagged in the factor build but
  are revised historical datasets.
- A future refresh must create a new dated manifest/config rather than mutate
  the 2026-07-13 declarations.
