# EIA-930 selected Central / Florida overlay input

The data files documented here are not tracked in Git. Their exact GCS
generations are pinned in
`manifests/selected_strategy_inputs_2026-08-14.json` and are materialized into
the ignored `inputs/gcs` cache by `python -m naturalgas.audit_inputs` or by the
evaluator on first use.

`selected_eia930_overlay_inputs` is the compact, frozen input for the selected
10% EIA-930 enhancement. The slot remains fixed at 40% Central and 60%
Florida, equivalent to 4% and 6% of the total strategy score.

| Property | Value |
|---|---|
| Rows | 1,751 strategy score dates |
| Score-date range | 2019-07-24 through 2026-07-13 |
| Central balancing authorities | ERCOT (`ERCO`), MISO, and SPP (`SWPP`) |
| Florida eligible balancing authorities | FMPP, FPC, FPL, GVL, HST, JEA, SEC, TAL, and TEC |
| Florida daily policy | Aggregate every BA whose daily demand and required generation fields are complete |
| Signal transform | One continuous past-only same-weekday history, prior innovation scale, `tanh(z/2)` |
| SHA-256 | `80118666e3c63062c87441435c78f729676560b08b77cfcee1c9afe8b969f155` |

The pinned daily source artifact is
`selected_eia930_southeast_daily_multifuel` (2,751 days by 18 respondents,
SHA-256 `332bbf025b5f9536adf5148aa40be09cd80f596d49a39f058f1d7eea132542e4`).
`selected_florida_available_ba_signal_history` is its deterministic Florida
score-date build (1,752 rows through 2026-07-14, SHA-256
`c34597ae140a9251c07e670649f2f8d5a1fd6d8ea8a80d4c8dc7e4b84616189b`).
It is rebuilt by `naturalgas/build_eia930_florida_available_ba.py`.

For source day t, the Florida physical input is:

```text
sum(coal + nuclear + hydro + pumped storage over complete Florida BAs)
------------------------------------------------------------------------
               sum(demand over the same complete BAs)
```

That raw share enters one continuous history. Its expected value is the mean
of the prior eight observations for the same weekday (`shift(1)`), and its
innovation is scaled by the prior 252 innovations with a 126-observation
minimum (`shift(1)`). A low firm-non-gas share is bullish natural gas, and the
result is compressed with `tanh(z/2)`.

If only eight BAs are complete on t, the eight-BA share is compared with the
ordinary preceding history, which normally contains nine-BA observations. The
eight-BA raw share and innovation then remain in that same history and can
affect later rolling means and volatility. There is no separate same-subset
normalization and no reweighting of the fixed 40/60 Central/Florida slot.

Sixteen score dates use fewer than nine Florida BAs; the minimum is six.
This unified rule also removes the old accidental Carolinas/SCEG coupling and
restores the five score dates 2020-02-07, 2020-09-15, 2023-11-01,
2023-11-02, and 2026-05-19. Every corresponding NYMEX return is retained.

Each source gas day maps to the first strictly later strategy score date. The
evaluator then keeps the existing one-session position lag. The common return
sample contains 1,748 trading dates from 2019-07-25 through 2026-07-13.

The source is a frozen revised EIA-930 bulk history, not a first-vintage
archive. Reproducibility of this artifact does not prove historical
first-publication availability.
