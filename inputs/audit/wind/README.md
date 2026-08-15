# Wind notebook audit inputs

This directory now contains documentation only. The small descriptive
fleet/capacity tables and the frozen score-date input are archived in GCS and
materialized into the ignored `inputs/gcs` cache on demand.

- `wind_annual_location_weights_csv` is a text export of the 308-row
  `selected_annual_location_weights` artifact produced by
  `evaluate_ncar_gdex_complete_wind_factor.py`.
- `wind_annual_fleet_diagnostics_csv` is the corresponding 11-row annual fleet
  summary.

The strict weather-factor rebuild starts from the raw generation-pinned USWTDB
snapshot and checks the derived weights against
`selected_annual_location_weights`. The Parquet artifact preserves the exact
floating-point bit patterns; the CSV is the human-readable notebook
representation.

The CSV generations and hashes are pinned in
`manifests/wind_notebook_audit_inputs_2026-08-15.json`; the selected Parquet
inputs are pinned in `manifests/selected_strategy_inputs_2026-08-14.json`.

`selected_d1_3_storage_amplifier_inputs` is the compact score-date audit input
for the currently selected D1--3 strategy. It contains 1,752 rows from
2019-07-24 through 2026-07-14 and freezes the D1--3 and D1--5 wind-inclusive
scores, the score without wind, wind signals, HDD revision, production-risk
level and revision, Central/Florida firm non-gas shortfalls, South Central
inventory state, every recomputable guard flag, and Florida source-day BA
coverage. It is consumed directly by
`naturalgas/evaluate_model_v03_d1_3_storage_guard.py` and has SHA-256:

```text
d4807aae8bc5401a9bfb533ec64820cedcc6f38352af9a13f460ae1e50befe04
```

The weather-revision guard uses HDD in January--May and September--December,
is disabled in June--August, and has no CDD branch. Recompute the frozen guard
flags and selected score with `naturalgas/rebuild_hdd_guard_seasonality.py`.

This artifact remains the compact downstream audit boundary for EIA-930,
storage, production weather, and guard state. Its two wind columns are also
verified upstream: `python -m naturalgas.pipelines.rebuild_model_v03
--overwrite` reads the exact raw NCAR/GDEX GFS generations declared in
`manifests/weather_factor_inputs_2026-07-28.json`, rebuilds D1/D1--3/D1--5,
and requires bit-for-bit numeric and missing-date parity before running the
strategy. The resulting receipt records the raw-built horizon hash, compact
input hash, issue cycle, matched rows, and missing initialization dates.

Florida now uses one continuous rolling history built from every complete BA
on each source day. Partial-BA observations are retained in the future
reference history. This restores all five previously missing Florida score
dates and retains their corresponding next-session returns; the evaluator no
longer appends a separate eight-row SCEG correction.
