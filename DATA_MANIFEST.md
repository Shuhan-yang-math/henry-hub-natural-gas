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
3. rebuilds the master panel with the audited NYMEX session filter (removing
   the five 2019 settlement carry rows on January 21, February 18, May 27,
   September 2, and December 25) and the actual WNGSR holiday release calendar,
   verifies its corrected SHA-256, and rebuilds the three selected wind/solar
   artifacts byte-for-byte;
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

## Selected strategy enhancement inputs

The selected enhancement uses compact, checked-in audit inputs. They are
separate from the seven pinned inputs of the approved formal rebuild.

| Path | Rows | Coverage | SHA-256 | Use |
|---|---:|---|---|---|
| `inputs/audit/eia930/eia930_southeast_daily_multifuel.parquet` | 49,518 | 2019-01-01–2026-07-13 respondent-days | `332bbf025b5f9536adf5148aa40be09cd80f596d49a39f058f1d7eea132542e4` | Frozen revised EIA-930 daily BA demand and generation source |
| `inputs/audit/eia930/florida_available_ba_signal_history.parquet` | 1,752 | 2019-07-24–2026-07-14 score dates | `c34597ae140a9251c07e670649f2f8d5a1fd6d8ea8a80d4c8dc7e4b84616189b` | Deterministic daily-available-BA Florida signal and rolling lineage |
| `inputs/audit/eia930/selected_overlay_inputs.parquet` | 1,751 | 2019-07-24–2026-07-13 score dates | `80118666e3c63062c87441435c78f729676560b08b77cfcee1c9afe8b969f155` | Central total non-gas and daily-available-BA Florida shortfalls, production short-block state, and lineage |
| `inputs/audit/events/event_reports_aligned.parquet` | 101 | 2017-08-24–2024-09-29 | `f1a99a286c1a2a5b7b03990edfec08786aa9a56e0b7f5ad88417450fb984fb1b` | BSEE/Sabine event-controller registry |
| `inputs/audit/wind/d1_3_storage_amplifier_inputs.parquet` | 1,752 | 2019-07-24–2026-07-14 score dates | `d4807aae8bc5401a9bfb533ec64820cedcc6f38352af9a13f460ae1e50befe04` | D1--3/D1--5 scores, score without wind, fast-shock inputs, storage state, Florida BA coverage, and HDD guard flags with June--August disabled |
| `inputs/audit/storage/legacy_week_ending_plus_six_formal_scores.parquet` | 2,264 | 2017-07-03–2026-07-13 | `ba0e107f9380075931cbf29d84ac6d2d135f77e4c2a7a373f049f0fbae2c8a0b` | Narrow pre-fix score baseline used only to isolate the release-calendar delta |
| `inputs/audit/storage/wngsr_d1_3_score_corrections.parquet` | 23 | 2019-11-27–2025-12-31 affected score dates | `b68fe58589f8337be69e57a14011eefa83436fdd32a0b0b1d5c58e5af76b8a4a` | Actual-release-date score delta, corrected South Central state, and production-clamp audit fields |

Florida is rebuilt from the BAs that are complete on each source gas day into
one continuous past-only rolling history. Partial-BA observations remain in
the reference history used by later dates. This removes the accidental SCEG
coupling and retains all five previously omitted Florida-outage return dates.

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

The current selected D1--3 strategy additionally reads the frozen wind/guard
input with embedded Florida BA coverage and the narrow WNGSR correction
overlay above and writes:

```text
results/experiments/d1_3_storage_amplified/selected_strategy_daily.parquet
results/experiments/d1_3_storage_amplified/strategy_metrics.csv
results/experiments/d1_3_storage_amplified/period_metrics.csv
results/experiments/d1_3_storage_amplified/annual_metrics.csv
results/experiments/d1_3_storage_amplified/event_report_registry.parquet
results/experiments/d1_3_storage_amplified/latest_strategy_dashboard.png
results/experiments/d1_3_storage_amplified/summary.json
```

Rebuild the selected strategy artifacts with:

```bash
python naturalgas/build_wngsr_d1_3_corrections.py \
  --corrected-formal \
  naturalgas/processed/south_central_storage_strategy/strategy_daily.parquet
python naturalgas/rebuild_hdd_guard_seasonality.py
python naturalgas/evaluate_d1_3_storage_amplified_strategy.py
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
