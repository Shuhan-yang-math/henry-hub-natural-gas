# Solar factor capacity snapshot

No data file is tracked in this directory. Artifact
`selected_monthly_location_weights` is the frozen monthly utility-scale solar
capacity parity target archived in GCS and pinned in
`manifests/selected_strategy_inputs_2026-08-14.json`. The strict weather-factor
rebuild starts from the raw generation-pinned EIA-860M generator snapshot and
requires the rebuilt weights to match this target.

Its byte size, SHA-256, schema, and generation are validated when it is
materialized into the ignored `inputs/gcs` cache. Keeping this historical
snapshot in GCS avoids silently substituting a later revised capacity history
without keeping intermediate data in Git.
