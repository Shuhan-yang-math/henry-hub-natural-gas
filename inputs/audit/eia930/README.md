# EIA-930 selected Central / Florida overlay input

`selected_overlay_inputs.parquet` is the compact, frozen input for the selected
10% EIA-930 enhancement.  The slot is fixed at 40% Central and 60% Florida,
equivalent to 4% and 6% of the total strategy score.

| Property | Value |
|---|---|
| Rows | 1,738 strategy score dates |
| Score-date range | 2019-07-24 through 2026-07-13 |
| Central source gas-day range | 2019-07-23 through 2026-07-12 |
| Florida source gas-day range | 2019-07-23 through 2026-07-12 |
| Central balancing authorities | ERCOT (`ERCO`), MISO, and SPP (`SWPP`) |
| Florida balancing authorities | FMPP, FPC, FPL, GVL, HST, JEA, SEC, TAL, and TEC |
| Signal transform | Past-only same-weekday anomaly, prior volatility scale, continuous `tanh(z/2)` |
| SHA-256 | `bbaa1b948df815842feaa6b11a42fdc7d92d099b5f001eeb26adb6bc2daa3fee` |

Both inputs start from hourly EIA-930 demand and generation by fuel.  The
Central signal divides total non-gas generation by Central demand.  The
Florida signal divides firm non-gas generation (coal, nuclear, and water) by
Florida demand.  Each share is compared with a past-only eight-observation
same-weekday mean, scaled by prior 252-day innovation volatility with a
126-day minimum, oriented so a shortfall is bullish natural gas, and
compressed with `tanh(z/2)`.

Each source gas day maps to the first strictly later strategy score date.  The
evaluator then retains the strategy's existing one-session position lag.  The
common return sample is therefore 1,737 trading dates from 2019-07-25 through
2026-07-13.

`production_short_block_active` preserves the existing cold-season production
safety state on each score date. The selected evaluator uses it only to prevent
the new allocation from creating a conflicting short.
