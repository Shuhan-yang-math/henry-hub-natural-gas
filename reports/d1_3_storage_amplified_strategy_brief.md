# Selected D1--3 Wind and Storage-Amplified Guard Strategy

**Selection date:** August 12, 2026
**Common sample:** July 25, 2019 through July 13, 2026; 1,737 trading days
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
raises net Sharpe from 2.149 to 2.245, raises Sortino from 3.726 to 3.922, and
reduces maximum drawdown from -5.29% to -4.15%. CAGR declines from 19.33% to
19.07%, and the simple sum of incremental daily net return is -1.80 percentage
points versus the current D1--5 comparator.

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

- five-day HDD forecast revision of at least +1 sigma;
- November--March local production-risk revision of at least +1 trailing-
  quantile scale unit while the freeze-risk level is positive; or
- Central or Florida firm non-gas generation shortfall of at least +2 sigma.

When inventory is low, the moderate thresholds are +0.5 sigma for HDD, +0.5
trailing-quantile scale unit for production revision, and +1 sigma for firm
non-gas generation shortfall. Low storage alone cannot activate the guard.

If the score without wind is positive, the wind signal is bearish, and adding
wind would reverse the score below zero, the guarded score is set to zero. The
guard cannot create or enlarge a long and cannot alter a short already present
without wind.

## Common-sample performance

| Metric | Current D1--5 | D1--3, no guard | **Selected D1--3 + storage amplifier** |
|---|---:|---:|---:|
| Net Sharpe | 2.149 | 2.198 | **2.245** |
| Sortino | 3.726 | 3.827 | **3.922** |
| CAGR | **19.33%** | 18.76% | 19.07% |
| Maximum drawdown | -5.29% | **-4.15%** | **-4.15%** |
| Total net return | **242.70%** | 231.35% | 237.44% |
| Mean absolute position | 10.68% | 10.41% | **10.19%** |

The horizon change provides most of the drawdown reduction. Relative to the
unguarded D1--3 strategy, the storage-amplified guard raises Sharpe by 0.047,
Sortino by 0.095, and cumulative net return by 1.80 percentage points.

## Intervention behavior

The guard changes 60 held-return dates. It helps on 35 dates and hurts on 25.
It avoids or reduces 6.62 percentage points of losses on helped dates and
sacrifices 4.80 percentage points of profits on hurt dates, for a net gain of
1.80 percentage points relative to unguarded D1--3.

The fixed validation-block Sharpe for 2021--2023 rises from 2.223 for
unguarded D1--3 to 2.270 for the selected strategy. The 2024+ first-look block
rises from 1.859 to 1.919. Annual results remain mixed: the guard helps in
2020, 2022, 2023, 2024, and 2025, but hurts in partial 2019, 2021, and 2026
YTD. The rule therefore remains a research selection rather than a claim of
uniform annual improvement.

## Reproduction

Run from `henry-hub-natural-gas/`:

```bash
python naturalgas/evaluate_d1_3_storage_amplified_strategy.py
```

The evaluator validates every frozen guard state and recomputes the guarded
score, one-session position, BSEE/Sabine veto, transaction cost, performance
tables, and dashboard. It reads:

- `naturalgas/processed/south_central_storage_strategy/strategy_daily.parquet`;
- `inputs/audit/wind/d1_3_storage_amplifier_inputs.parquet`; and
- `inputs/audit/events/event_reports_aligned.parquet`.

Outputs are written to `results/experiments/d1_3_storage_amplified/`.
