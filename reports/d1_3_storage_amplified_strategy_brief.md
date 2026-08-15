# Selected D1--3 Wind and Storage-Amplified Guard Strategy

**Selection date:** August 12, 2026; HDD month gate revised August 13, 2026
**Common sample:** July 25, 2019 through July 13, 2026; 1,748 trading days
**Status:** selected research version

## Decision

The selected strategy now uses the capacity-weighted nonlinear GFS wind signal
from forecast days 1--3 instead of days 1--5. It also applies the previously
audited storage-amplified wind direction guard. The selected 40% Central / 60%
Florida EIA-930 sleeve, nine internal fundamental signals, solar factor,
production control, BSEE/Sabine pure short veto, one-session position lag, and
2.5 bp turnover cost remain unchanged.

The selection prioritizes risk-adjusted return and drawdown over the highest
historical cumulative return. On the exact common sample, the selected version
raises net Sharpe from 2.119 to 2.228, raises Sortino from 3.663 to 3.881, and
reduces maximum drawdown from -5.30% to -4.16%. CAGR declines from 19.20% to
19.05%, and the simple sum of paired daily net-return differences is -1.11
percentage points versus the current D1--5 comparator. The separate compounded
final-wealth difference is -2.89 percentage points. Maximum drawdown starts
from initial wealth 1.0, and subperiod turnover inherits the actual position
immediately before the period boundary.

<!-- BEGIN AUTO-GENERATED: metric-conventions -->
The selected D1--3 and EIA-930 tables report zero-risk-free-rate Sharpe and
Sortino ratios from daily log net returns, `g_t = log(1 + r_t)`. Sharpe is
`mean(g_t) / sample_std(g_t) * sqrt(252)`. Sortino is `mean(g_t) * 252`
divided by the zero-target unconditional lower-partial-moment denominator
`sqrt(mean(min(g_t, 0)^2)) * sqrt(252)`; positive-return days therefore enter
the downside average as zeros. This is not the conditional-negative-day
Sortino convention. For the selected strategy, arithmetic-return Sharpe is
2.261 versus the reported 2.228, and conditional-negative-day log Sortino is
2.648 versus the reported 3.881. CAGR uses the actual first settlement
endpoint, maximum drawdown begins from initial wealth 1.0, and all reported
ratios use 252 sessions per year.
<!-- END AUTO-GENERATED: metric-conventions -->

## Rule

`D1--3` describes the wind forecast window, not the holding period. Wind speed
for forecast days 1, 2, and 3 is converted through the same hub-height and
nonlinear turbine power curve and aggregated with the same annual lagged wind-
capacity weights. The trading target and position horizon remain next session.

The guard activates under either condition:

1. a strong fast bullish shock is present; or
2. South Central inventory is at least 1 sigma low and a moderate fast bullish
   shock is present.

Strong fast shocks are:

- outside June--August, five-day HDD forecast revision of at least +1 sigma;
- November--March local production-risk revision of at least +1 trailing-
  quantile scale unit while the freeze-risk level is positive; or
- Central or Florida firm non-gas generation shortfall of at least +2 sigma.

When inventory is low, the moderate thresholds are +0.5 sigma for HDD outside
June--August, +0.5 trailing-quantile scale unit for production revision, and
+1 sigma for firm non-gas generation shortfall. Low storage alone cannot
activate the guard. CDD is not used by the guard.

If the score without wind is positive, the wind signal is bearish, and adding
wind would reverse the score below zero, the guarded score is set to zero. The
guard cannot create or enlarge a long and cannot alter a short already present
without wind.

## Common-sample performance

<!-- BEGIN AUTO-GENERATED: d1-brief-table -->
| Metric | Current D1--5 | D1--3, no guard | **Selected D1--3 + storage amplifier** |
|---|---:|---:|---:|
| Net Sharpe | 2.119 | 2.181 | **2.228** |
| Sortino | 3.663 | 3.787 | **3.881** |
| CAGR | **19.20%** | 18.74% | 19.05% |
| Maximum drawdown | -5.30% | -4.51% | **-4.16%** |
| Total net return | **240.11%** | 231.09% | 237.22% |
| Mean absolute position | 10.69% | 10.43% | **10.20%** |
<!-- END AUTO-GENERATED: d1-brief-table -->

The horizon change provides most of the drawdown reduction. Relative to the
unguarded D1--3 strategy, the storage-amplified guard raises Sharpe by 0.047,
Sortino by 0.094, and the simple sum of daily net-return differences by 1.81
percentage points.
<!-- BEGIN AUTO-GENERATED: d1-drawdown-claim -->
Relative to unguarded D1--3, the guard improves maximum drawdown from -4.51%
to -4.16%, a 0.34 percentage-point reduction in drawdown depth.
<!-- END AUTO-GENERATED: d1-drawdown-claim -->
The compounded final-wealth difference is +6.12 percentage points; it is not
the same metric as the paired daily sum.

This refresh fixes both the NYMEX holiday-session/early-roll path and the EIA
WNGSR holiday release calendar. The D1--3 overlay changes 23 affected score
dates and recomputes the production clamp and storage guard. Florida is
rebuilt as one continuous rolling history from every complete BA
on each source day. An eight-BA observation is compared with the ordinary
preceding history and then remains in the history used by future dates. This
removes the SCEG coupling and retains all five previously omitted returns.

## Intervention behavior

The guard changes 59 held-return dates. It helps on 34 dates and hurts on 25.
It avoids or reduces 6.49 percentage points of losses on helped dates and
sacrifices 4.66 percentage points of profits on hurt dates, for a net gain of
1.81 percentage points in paired daily net-return differences relative to
unguarded D1--3. Comparing compounded final wealth gives +6.12 percentage
points.

The fixed validation-block Sharpe for 2021--2023 rises from 2.232 for
unguarded D1--3 to 2.279 for the selected strategy. The 2024+ first-look block
rises from 1.779 to 1.835. Annual results remain mixed: the guard helps in
2020, 2022, 2023, 2024, and 2025, but hurts in partial 2019, 2021, and 2026
YTD. The rule therefore remains a research selection rather than a claim of
uniform annual improvement.

## Reproduction

Run from `henry-hub-natural-gas/`:

```bash
python naturalgas/evaluate_d1_3_storage_amplified_strategy.py
```

The command above materializes the compact score, WNGSR correction, and event
inputs from the exact GCS generations in
`manifests/selected_strategy_inputs_2026-08-14.json`. To rebuild
the D1--3 wind signal from its immutable GCS source before running the same
evaluator, use:

```bash
python -m naturalgas.pipelines.rebuild_d1_3_strategy --overwrite
```

That pipeline reads the exact GFS object generations in the weather manifest,
reconstructs same-day 00Z D1/D1--3/D1--5 signals with a past-only rolling
reference, requires exact parity with the strategy input, and writes a lineage
receipt together with the reproduced strategy outputs.

The evaluator validates every frozen guard state and recomputes the guarded
score, one-session position, BSEE/Sabine veto, transaction cost, performance
tables, and dashboard. It reads:

- `naturalgas/processed/south_central_storage_strategy/strategy_daily.parquet`;
- GCS artifact `selected_d1_3_storage_amplifier_inputs`;
- GCS artifact `selected_florida_available_ba_signal_history`;
- GCS artifact `selected_wngsr_d1_3_score_corrections`; and
- GCS artifact `selected_event_reports_aligned`.

Outputs are written to `results/experiments/d1_3_storage_amplified/`.
