# Wind notebook audit inputs

This directory contains small descriptive fleet/capacity tables and the frozen
score-date input for the selected D1--3 research evaluator. The fleet tables
support `notebooks/02_capacity_weighted_wind.ipynb`; the selected input is
documented separately below.

- `annual_location_weights.csv` is a text export of the 308-row
  `annual_location_weights.parquet` produced by
  `evaluate_ncar_gdex_complete_wind_factor.py`.
- `annual_fleet_diagnostics.csv` is the corresponding 11-row annual fleet
  summary.

The annual-weights Parquet file is also the frozen capacity snapshot consumed by the strict
weather-factor rebuild. It preserves the original floating-point bit patterns
required for byte-exact output. Its size and SHA-256 are pinned in
`manifests/weather_factor_inputs_2026-07-28.json`; the CSV is the
human-readable notebook representation.

The CSV audit-table hashes and formal wind IC hash are recorded in
`config/ng_multisignal_panel_2026-07-13.yaml`. The weather snapshot contract is
enforced by `tests/test_rebuild_weather_factors.py`.

`d1_3_storage_amplifier_inputs.parquet` is the compact score-date audit input
for the currently selected D1--3 strategy. It contains 1,752 rows from
2019-07-24 through 2026-07-14 and freezes the D1--3 and D1--5 wind-inclusive
scores, the score without wind, wind signals, HDD revision, production-risk
level and revision, Central/Florida firm non-gas shortfalls, South Central
inventory state, every recomputable guard flag, and Florida source-day BA
coverage. It is consumed directly by
`naturalgas/evaluate_d1_3_storage_amplified_strategy.py` and has SHA-256:

```text
a476153db3099a61632122b3f4b86e0f33cf657b06a71da61baea84794beb635
```

This file is a derived audit boundary, not a substitute for the raw NCAR/GDEX,
EIA-930, storage, or production-weather source archives.

Florida now uses one continuous rolling history built from every complete BA
on each source day. Partial-BA observations are retained in the future
reference history. This restores all five previously missing Florida score
dates and retains their corresponding next-session returns; the evaluator no
longer appends a separate eight-row SCEG correction.
