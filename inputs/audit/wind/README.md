# Wind notebook audit inputs

These small tables support the descriptive fleet and capacity-weighting cells
in `notebooks/02_capacity_weighted_wind.ipynb`. They do not enter the formal
strategy evaluator directly.

- `annual_location_weights.csv` is a text export of the 308-row
  `annual_location_weights.parquet` produced by
  `evaluate_ncar_gdex_complete_wind_factor.py`.
- `annual_fleet_diagnostics.csv` is the corresponding 11-row annual fleet
  summary.

The Parquet file is also the frozen capacity snapshot consumed by the strict
weather-factor rebuild. It preserves the original floating-point bit patterns
required for byte-exact output. Its size and SHA-256 are pinned in
`manifests/weather_factor_inputs_2026-07-28.json`; the CSV is the
human-readable notebook representation.

The CSV audit-table hashes and formal wind IC hash are recorded in
`config/ng_multisignal_panel_2026-07-13.yaml`. The weather snapshot contract is
enforced by `tests/test_rebuild_weather_factors.py`.
