# Solar factor capacity snapshot

`monthly_location_weights.parquet` is the frozen monthly utility-scale solar
capacity mapping consumed by the strict weather-factor rebuild. It is a direct
input to the solar factor builder, not one of the seven direct inputs to the
formal evaluator.

Its byte size and SHA-256 are pinned in
`manifests/weather_factor_inputs_2026-07-28.json` and validated before factor
construction. Keeping this historical snapshot avoids silently substituting a
later revised capacity history.
