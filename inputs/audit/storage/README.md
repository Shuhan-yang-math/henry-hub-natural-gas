# EIA WNGSR release-calendar audit

The two derived data files documented below are not tracked in Git. Artifacts
`selected_legacy_wngsr_formal_scores` and
`selected_wngsr_d1_3_score_corrections` are downloaded from the exact GCS
generations pinned in `manifests/selected_strategy_inputs_2026-08-14.json` into
the ignored `inputs/gcs` cache.

Weekly storage values come from the generation-pinned EIA bulk artifact in
`manifests/input_artifacts_2026-07-13.json`. That artifact is a revised history:
its `week_ending` field is an observation date and does not preserve the first
publication timestamp.

The strategy therefore aligns weekly values with the official EIA Weekly
Natural Gas Storage Report schedule implemented in
`naturalgas/eia_storage_release_calendar.py`. The normal release is Thursday at
10:30 a.m. Eastern. The registry contains all 31 holiday/special exceptions
whose report weeks fall in 2017--2025. No 2026 exception occurs before the
audited storage cutoff, week ending 2026-07-03.

The current official schedule is:

```text
https://ir.eia.gov/ngs/schedule.html
```

Historical final-year schedule pages were checked through the Internet Archive
using the official EIA URL. The retrieved source-page SHA-256 values were:

| Schedule year | Source-page SHA-256 |
|---:|---|
| 2017 | `413aa2c6eaabb6f59f48bfae086f01b2e7750c011a8bb0a2d7cf180316042a43` |
| 2018 | `6fb7df903f22cfd5f404f214ada3951bc27638698e9058e1d65d7b3b655f6ad4` |
| 2019 | `2355c13bb9de4d0a463dea0ea03a115d8653ca2d1139e9948e45d17e25d31887` |
| 2020 | `13afd611a15d762261d642b453d52ac5dce049218d6b19c8d2f9bc1366d35760` |
| 2021 | `6cdbfc282605c9f9ec411dd0a8b383ddbfc19c1e75f2cb029a1860ffe7617592` |
| 2022 | `098894ef6d5df56e39bbce12f6d5076642ce12f0726346344fd20c47bae36f3f` |
| 2023 | `14a9c73a9739e722489923bc44e761f3d79dab99e06441f271efe352b2b35390` |
| 2024 | `433ae924f159336efe760b06944a6657ef48085e7d3cd833cb7dd3a441a049f2` |
| 2025/current | `b54595ec6784c11c95ab448fa21d3f115b03a2bcc02960cab589fb0ad4cc1e04` |

Every audited exception was at or before noon Eastern and therefore before the
strategy's 2:30 p.m. Eastern information cutoff. The exact UTC publication
timestamp is retained by the calendar function and tested across daylight and
standard time. This calendar fixes publication timing only; it does not turn
the revised EIA bulk values into first-release vintages.

## D1--3 correction boundary

The selected D1--3 evaluator historically consumed a frozen derived score
input. To avoid mixing this calendar correction with unrelated model-version
differences, the fix is applied as a narrow overlay:

| Artifact | Rows | SHA-256 |
|---|---:|---|
| `selected_legacy_wngsr_formal_scores` | 2,264 | `ba0e107f9380075931cbf29d84ac6d2d135f77e4c2a7a373f049f0fbae2c8a0b` |
| `selected_wngsr_d1_3_score_corrections` | 23 | `b68fe58589f8337be69e57a14011eefa83436fdd32a0b0b1d5c58e5af76b8a4a` |

The first artifact retains only the date, formal core score, and formal core
fundamental from the pre-fix `week_ending + 6` run. The builder differences it
against the corrected formal artifact, verifies that the same 23 dates have a
changed South Central storage state, and writes the second file. Rebuild it
with:

```bash
python naturalgas/build_wngsr_d1_3_corrections.py \
  --corrected-formal \
    results/models/v01_south_central_storage/strategy_daily.parquet
```

The command writes its regenerated intermediate under `reproduced/audit/`; it
does not overwrite the immutable GCS input or write into `inputs/audit`.

The evaluator adds the before-production-control score delta to the frozen
D1--5, D1--3, and no-wind scores, reapplies the production clamp, replaces the
South Central level state, and recomputes every guard flag. Scores outside the
23 affected dates are required to remain bit-for-bit unchanged by tests.
