# Data manifest

Large model inputs are intentionally excluded from Git. Access requires
Braeswood read credentials for bucket `bcli-natgas-data-497807`.

The authoritative machine-readable inventories are listed under
[Reproduction boundary](#reproduction-boundary). They pin every object by GCS
generation and SHA-256 and record byte size, row/column counts, and local
paths; the formal inventory additionally retains Arrow schema fingerprints.
Exact schemas are retained in
[`schemas/input_schemas_2026-07-13.json`](schemas/input_schemas_2026-07-13.json).

## Supported full rebuild

From `henry-hub-natural-gas/`, install the Python dependencies and run:

```bash
python -m naturalgas.pipelines.rebuild_all --overwrite
```

The default command performs six steps:

1. validates and downloads the 72 generation-pinned direct inputs used by the
   155-column master-panel builder;
2. validates and downloads 127 wind plus 127 solar NCAR/GDEX partitions and
   two generation-pinned raw capacity snapshots (USWTDB and EIA-860M);
3. rebuilds the master panel with the audited NYMEX session filter (removing
   the five 2019 settlement carry rows on January 21, February 18, May 27,
   September 2, and December 25) and the actual WNGSR holiday release calendar,
   verifies its corrected SHA-256, and rebuilds the three selected wind/solar
   artifacts plus the D1/D1--3/D1--5 horizon lineage byte-for-byte;
4. routes the three EIA reads through immutable local snapshots rather than
   mutable live GCS keys; and
5. rebuilds V01 from the corrected panel and verifies its headline metrics and
   complete summary-file SHA-256 against the shipped summary; and
6. downloads and validates all 14 objects in the selected-strategy archive,
   rebuilds Central/Florida EIA-930 signals, fundamentals, production controls,
   no-wind/D1--3/D1--5 scores, the WNGSR correction, and the guard, then verifies
   the selected result. The frozen compact contract is comparison evidence only.

For a quicker formal-only audit that rebuilds the master panel but downloads
the three approved wind/solar artifacts and skips the selected D1--3 raw-wind
lineage, run:

```bash
python -m naturalgas.pipelines.rebuild_all \
  --use-approved-weather-artifacts --overwrite
```

The narrow seven-object processed-input rebuild remains available as:

```bash
python -m naturalgas.pipelines.rebuild_model_v01 --overwrite
```

Once the seven formal inputs exist locally and validate, that narrow build can
run without a network request:

```bash
python -m naturalgas.pipelines.rebuild_model_v01 --offline --overwrite
```

This older seven-object manifest predates the five-row NYMEX holiday panel
correction. The narrow receipt therefore verifies every V01 field except the
legacy Lower 48 comparison delta and records both summary hashes. The primary
`rebuild_all` path reconstructs the corrected panel and requires the complete
canonical V01 summary to match byte-for-byte.

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

Notebook 02 also uses two small audit tables archived in GCS:

```text
gs://bcli-natgas-data-497807/research/henry_hub_strategy/v2/inputs/wind/annual_location_weights.csv
gs://bcli-natgas-data-497807/research/henry_hub_strategy/v2/inputs/wind/annual_fleet_diagnostics.csv
```

Their generations and hashes are pinned in
`manifests/wind_notebook_audit_inputs_2026-08-15.json`. Notebook 02 downloads
them into the ignored `inputs/gcs` cache. They explain the historical capacity
weighting and fleet diagnostics; they are not additional formal evaluator
inputs.

## Selected strategy enhancement inputs

The selected enhancement keeps intermediate audit data out of Git. Compact
parity targets, EIA-930 source/rolling tables, event inputs, and capacity
snapshots are archived at immutable GCS generations.
[`manifests/selected_strategy_inputs_2026-08-14.json`](manifests/selected_strategy_inputs_2026-08-14.json)
pins all 14 objects by generation, SHA-256, size, dimensions, and required
columns. They remain separate from the seven formal processed inputs.

| Manifest artifact id | Rows | Coverage | SHA-256 | Use |
|---|---:|---|---|---|
| `selected_eia930_central_daily_multifuel` | 8,253 | revised respondent-days | `e275a32768822d152a369f9e5b42730fdc67bda7ba5e39a2282d072acc81987a` | Rebuild Central total- and firm-non-gas signals from ERCO/MISO/SWPP source rows |
| `selected_eia930_southeast_daily_multifuel` | 49,518 | 2019-01-01–2026-07-13 respondent-days | `332bbf025b5f9536adf5148aa40be09cd80f596d49a39f058f1d7eea132542e4` | Frozen revised EIA-930 daily BA demand and generation source |
| `selected_florida_available_ba_signal_history` | 1,752 | 2019-07-24–2026-07-14 score dates | `c34597ae140a9251c07e670649f2f8d5a1fd6d8ea8a80d4c8dc7e4b84616189b` | Deterministic daily-available-BA Florida signal and rolling lineage |
| `selected_eia930_overlay_inputs` | 1,751 | 2019-07-24–2026-07-13 score dates | `80118666e3c63062c87441435c78f729676560b08b77cfcee1c9afe8b969f155` | Central total non-gas and daily-available-BA Florida shortfalls, production short-block state, and lineage |
| `selected_event_reports_aligned` | 101 | 2017-08-24–2024-09-29 | `f1a99a286c1a2a5b7b03990edfec08786aa9a56e0b7f5ad88417450fb984fb1b` | BSEE/Sabine event-controller registry |
| `selected_d1_3_storage_amplifier_inputs` | 1,752 | 2019-07-24–2026-07-14 score dates | `d4807aae8bc5401a9bfb533ec64820cedcc6f38352af9a13f460ae1e50befe04` | D1--3/D1--5 scores, score without wind, fast-shock inputs, storage state, Florida BA coverage, and HDD guard flags with June--August disabled |
| `selected_legacy_wngsr_formal_scores` | 2,264 | 2017-07-03–2026-07-13 | `ba0e107f9380075931cbf29d84ac6d2d135f77e4c2a7a373f049f0fbae2c8a0b` | Narrow pre-fix score baseline used only to isolate the release-calendar delta |
| `selected_wngsr_d1_3_score_corrections` | 23 | 2019-11-27–2025-12-31 affected score dates | `b68fe58589f8337be69e57a14011eefa83436fdd32a0b0b1d5c58e5af76b8a4a` | Actual-release-date score delta, corrected South Central state, and production-clamp audit fields |
| `selected_annual_location_weights` | 308 | 2016–2026 issue years | `f7ea18d461edbea3386046e528859294e8346393f80f215131451189373dee60` | Wind-capacity parity target |
| `selected_monthly_location_weights` | 3,532 | Historical monthly capacity periods | `f415446ae98a8233318f7f812066f5ebafb77f1c3af15b478fdaf79239f16002` | Solar-capacity parity target |

Florida is rebuilt from the BAs that are complete on each source gas day into
one continuous past-only rolling history. Partial-BA observations remain in
the reference history used by later dates. This removes the accidental SCEG
coupling and retains all five previously omitted Florida-outage return dates.

The selected evaluator materializes the required objects into the ignored
`inputs/gcs` cache, reads them together with
`results/models/v01_south_central_storage/strategy_daily.parquet`
and writes:

```text
results/models/v02_eia930_central_florida/strategy_daily.parquet
results/models/v02_eia930_central_florida/annual_metrics.csv
results/models/v02_eia930_central_florida/central_florida_weight_sweep.csv
results/models/v02_eia930_central_florida/loss_day_yearly.csv
results/models/v02_eia930_central_florida/event_report_registry.parquet
results/models/v02_eia930_central_florida/dashboard.png
results/models/v02_eia930_central_florida/central_florida_weight_sweep.png
results/models/v02_eia930_central_florida/summary.json
```

Rebuild those artifacts with:

```bash
python naturalgas/evaluate_model_v02_eia930_central_florida.py
```

The fast downstream evaluator can still read the compact wind/guard contract.
The supported strict pipelines do not: they rebuild wind, solar, fundamentals,
Central/Florida EIA-930 signals, production controls, all pre-guard scores, the
WNGSR correction, and the final guard. The compact object is retained only for
explicit parity reporting. The strategy writes:

```text
results/models/v03_d1_3_storage_guard/strategy_daily.parquet
results/models/v03_d1_3_storage_guard/strategy_metrics.csv
results/models/v03_d1_3_storage_guard/period_metrics.csv
results/models/v03_d1_3_storage_guard/annual_metrics.csv
results/models/v03_d1_3_storage_guard/event_report_registry.parquet
results/models/v03_d1_3_storage_guard/dashboard.png
results/models/v03_d1_3_storage_guard/summary.json
```

Rebuild the selected strategy artifacts with:

```bash
python naturalgas/evaluate_model_v03_d1_3_storage_guard.py
```

That is the fast downstream path: missing audit inputs are downloaded from
their exact GCS generations. The strict processed-upstream-to-result path is:

```bash
python -m naturalgas.pipelines.rebuild_model_v03 --overwrite
```

It writes rebuilt wind/solar artifacts, `model_v03_score_inputs.parquet`, both
regional EIA-930 histories, the WNGSR correction, selected-strategy artifacts,
and a lineage receipt under
`reproduced/models/v03_d1_3_storage_guard/`. For the
formal master panel and selected strategy in one transaction, use
`python -m naturalgas.pipelines.rebuild_all --overwrite`.

The source-built score contract has 1,750 rows. The frozen compact parity
target has two additional legacy rows, September 2 and December 25, 2019,
which are not confirmed NYMEX sessions in the corrected master panel. All
1,750 shared dates match exactly for the rebuilt upstream fields and all three
pre-guard scores; the receipt lists the two frozen-only dates explicitly.

The byte-exact weather rebuild starts from generation-pinned raw USWTDB and
EIA-860M snapshots in GCS. The builder has been checked to regenerate the wind
and solar derived-weight artifacts exactly; both parity targets also remain in
GCS and are pinned in `manifests/selected_strategy_inputs_2026-08-14.json`.

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

The manifest pins all 127 monthly objects by GCS generation, byte size, and
SHA-256. `rebuild_weather_factors wind-horizons` reads those immutable object
versions, keeps only 00Z, requires all 28 locations × 4 valid hours for every
requested lead, applies the frozen issue-year-minus-one annual fleet weights,
and computes each rolling z-score from prior initializations only. The approved
3,857-row horizon parquet is itself pinned by SHA-256 in the same manifest.

## Sabine nomination-overlay archive

The isolated Sabine nomination-revision overlay has a separate three-object
contract in
[`manifests/sabine_nomination_overlay_inputs_2026-08-19.json`](manifests/sabine_nomination_overlay_inputs_2026-08-19.json):

| Artifact | Rows × columns | Reproduction role |
|---|---:|---|
| Raw all-cycle Sabine OAC archive | 231,679 × 26 | Keep the latest complete gas-day/cycle snapshot, then rebuild TransCameron Intraday-1-to-3 and Jefferson Island Timely-to-Intraday-3 revisions and their 20/60/120-day causal histories. |
| Assembled nomination research panel | 1,748 × 78 | Exact mapped factor contract and parity target consumed by the final evaluator. |
| Processed NG execution windows | 2,250 × 18 | Exact native-posting, entry-VWAP, settlement-VWAP, held-contract, volume, trade-count, and settlement-method contract. |

Each object is pinned by GCS generation, SHA-256, byte size, Parquet dimensions,
schema fingerprint, and required columns. The strict rebuild is:

```bash
python -m naturalgas.pipelines.rebuild_sabine_nomination_overlay --overwrite
```

It downloads all three objects, verifies the raw nomination lineage exactly,
runs the final overlay against V03, and writes a reproduction receipt under
`reproduced/experiments/sabine_nomination_revision_intraday_overlay_final/`.
Raw NYMEX tick files are controlled and are not redistributed, so the execution
window is a pinned processed-input boundary rather than a public raw-tick
rebuild.

## Reproduction boundary

This handoff supports reproduction of the formal 2017-07-03 through 2026-07-13
backtest from immutable internal base objects. The panel object itself extends
to 2026-07-17, but the frozen configuration applies the 2026-07-13 cutoff.

The five inventories have different roles:

- [`manifests/master_panel_inputs_2026-07-13.json`](manifests/master_panel_inputs_2026-07-13.json)
  pins 72 direct master-panel inputs;
- [`manifests/weather_factor_inputs_2026-07-28.json`](manifests/weather_factor_inputs_2026-07-28.json)
  pins 254 weather partitions and raw USWTDB/EIA-860M capacity snapshots;
- [`manifests/selected_strategy_inputs_2026-08-14.json`](manifests/selected_strategy_inputs_2026-08-14.json)
  pins 14 exact selected-strategy, EIA-930/event audit, storage-correction, and
  raw/derived capacity objects; and
- [`manifests/input_artifacts_2026-07-13.json`](manifests/input_artifacts_2026-07-13.json)
  pins the seven approved processed artifacts used by the narrow rebuild and
  as parity targets for the broader build; and
- [`manifests/sabine_nomination_overlay_inputs_2026-08-19.json`](manifests/sabine_nomination_overlay_inputs_2026-08-19.json)
  pins the raw Sabine nomination archive, assembled overlay panel, and
  processed execution-window contract used by the isolated intraday study.

This is not a guarantee that re-querying current public APIs reproduces the
same history. Upstream CPC, futures, EIA, USWTDB, Open-Meteo, FRED, and related
sources can revise or lack complete first-release archives. A present-day API
download is therefore a data refresh, not a bit-exact rebuild of this model.

## Revision and vintage caveats

- EIA weekly/monthly files may reflect later revisions; they are not guaranteed
  first-release historical vintages.
- The archived EIA-930 respondent-day source is the exact revised-history input
  used by the model, not a historical first-publication payload archive.
- GFS files preserve forecast issue/reference time and therefore support
  vintage-aware weather analysis.
- Installed wind/solar capacity histories are lagged in the factor build but
  are revised historical datasets.
- A future refresh must create a new dated manifest/config rather than mutate
  the 2026-07-13 declarations.
