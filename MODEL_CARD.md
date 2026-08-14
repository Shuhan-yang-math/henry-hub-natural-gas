# Model card

## Objective

Estimate a bounded daily directional exposure to NYMEX Henry Hub natural-gas
futures from weather-driven power-sector substitution and slower natural-gas
fundamentals. The model is a research framework rather than a production
trading system.

## Target and execution convention

- Instrument: NYMEX Henry Hub natural-gas futures.
- Return: settlement-to-settlement front-month return.
- Roll: switch five trading sessions before the official last-trading-day
  convention used by the source panel.
- Signal timing: final composite score is delayed by one trading session.
- Position: continuous and clipped to `[-1, 1]`.
- Cost: 2.5 bps multiplied by absolute daily position change.
- Risk-free rate: zero in the reported Sharpe ratio.

## Top-level score

The selected wind allocation is fixed by season.

| Season | CPC weather block | Wind | Fundamentals before solar funding |
|---|---:|---:|---:|
| Peak demand: Nov–Feb, Jun–Aug | 45.0% | 15.0% | 40.0% |
| Shoulder: Mar–May, Sep–Oct | 22.5% | 22.5% | 55.0% |

Solar is a nominal 10% slot funded from the fundamental allocation and scaled
by deterministic daylight. Missing wind or solar values occupy a neutral zero
slot rather than causing the remaining components to be renormalized.

The selected EIA-930 version funds a fixed 10% regional power-system sleeve
from the fundamental allocation.  Forty percent of that slot is Central and
60% is Florida.  The resulting top-level budgets are:

| Season | CPC weather | Wind | Solar | EIA-930 | Fundamentals after funding |
|---|---:|---:|---:|---:|---:|
| Peak demand | 45.0% | 15.0% | 0–10.0% | 10.0% | 20.0–30.0% |
| Shoulder | 22.5% | 22.5% | 0–10.0% | 10.0% | 35.0–45.0% |

Solar remains daylight-scaled. Its unused nominal weight returns to the
fundamental sleeve. The EIA-930 sleeve does not change the nine internal
fundamental signals or create a tenth fundamental factor.

Only `sig_cpc_seasonal_revision` remains active in the three-slot legacy
weather block. The direct CPC-level and observed-weather slots are fixed at
zero. This preserves the historical allocation without pretending those two
discarded signals still add information.

## Wind factor

The wind dataset starts from GFS 80 m wind. The current selected research
version aggregates forecast leads 1--3; the prior comparator uses leads 1--5.
Locations are weighted by lagged U.S. wind capacity. Wind is converted to a
capacity-factor proxy through hub-height adjustment and a nonlinear turbine
power curve with approximate cut-in, rated, and cut-out speeds of 3, 12, and
25 m/s.

The directional signal is a causal 60-issue z-score of wind-generation
shortfall, transformed with `tanh(z / 2)`. Low wind is bullish natural gas,
ordinary high wind is bearish, and extreme cut-out-region wind can become
bullish again.

The D1--3 wind calculation is reproducible from the raw NCAR/GDEX point
archive retained in GCS. The checked-in weather manifest fixes all 127 monthly
objects by generation and SHA-256; the builder retains same-day 00Z issues,
requires the complete lead/location/hour inventory, applies the frozen annual
capacity snapshot, and excludes the current issue from its rolling reference.
`python -m naturalgas.pipelines.rebuild_d1_3_strategy --overwrite` requires
the rebuilt daily D1--3 and D1--5 values to match the compact strategy input
exactly before it will run the selected backtest.

## Solar factor

The solar factor uses GFS downward shortwave radiation, temperature, clear-sky
geometry, and lagged utility-scale solar capacity. It creates a simple
capacity-weighted PV-availability index for forecast leads 1–5. Low expected
PV availability is bullish natural gas because thermal generation may need to
replace unavailable solar output.

The active signal is `tanh(sig_solar_pv / 2)`, with a nominal 10% weight scaled
by daylight. It is not a full plane-of-array or plant-dispatch model.

## Fundamental block

The current internal weights are:

| Component | Internal weight | Directional interpretation |
|---|---:|---|
| South Central storage level | 18.18% | low versus same-week normal is bullish |
| South Central one-week change | 9.09% | weak injection/strong withdrawal is bullish |
| South Central four-week change | 9.09% | sustained tightening is bullish |
| Low production growth | 9.09% | slower dry-gas growth is bullish |
| LNG export YoY growth | 9.09% | higher structural export demand is bullish |
| Net-import supply | 9.09% | less domestic net supply is bullish |
| Dry-production MoM | 9.09% | falling production is bullish |
| LNG export MoM | 18.18% | rising export demand is bullish |
| Net-import-ratio MoM change | 9.09% | declining net import availability is bullish |
| Consumption YoY / MoM | 0% | removed from the direct daily position model |

Weekly and monthly z-scores are calculated at their native release frequency
before being aligned to trading days. The current observation is excluded from
its own rolling reference distribution. Weekly storage uses 104 releases with
a 52-release minimum; monthly factors use 60 observations with a 36-month
minimum.

Weekly EIA storage becomes available at its actual WNGSR publication timestamp.
The normal release is Thursday at 10:30 a.m. Eastern; the implementation uses
the audited EIA holiday/special schedule, including Wednesday, Friday, and
Monday exceptions. All releases in the backtest were before the 2:30 p.m.
Eastern information cutoff. Monthly EIA values use the conservative research
convention that reference month M becomes usable at the start of M+3.

## Freeze-off control

In November–March, when both the local freeze level and revision scores meet
the fixed freeze condition, the final raw score cannot be negative. This is a
one-sided safety control against shorting during severe production disruption.

## Storage-amplified wind direction guard

The selected D1--3 version adds a one-sided score guard. Strong new bullish
shocks can activate it directly: outside June--August, HDD forecast revision
at or above +1 sigma;
November--March local production-risk revision at or above +1 trailing-
quantile scale unit while the risk level is positive; or Central/Florida firm
non-gas generation shortfall at or above +2 sigma.

Low South Central inventory at or above +1 sigma cannot activate the guard by
itself. It only lowers the corresponding fast-shock thresholds to +0.5 sigma
for HDD outside June--August, +0.5 trailing-quantile scale unit for production
revision, and +1 sigma for firm non-gas generation shortfall. There is no CDD
guard branch.

When the score without wind is positive, D1--3 wind is bearish, and wind would
reverse the score below zero, the score is set to zero. The guard cannot create
or amplify a long and cannot change a short already present without wind.

## EIA-930 regional power-system sleeve

The Central component measures realized total non-gas generation shortfall
across ERCOT, MISO, and SPP.  It aggregates wind, solar, coal, nuclear, hydro,
and other reported non-gas generation relative to demand.  The Florida
component measures coal, nuclear, and water generation relative to Florida
demand.  Positive values indicate that less non-gas supply was available and
more gas-fired generation may have been required; negative values indicate
unusually abundant firm non-gas supply.

The selected signal is:

\[
S_t^{selected}=0.40S_t^{Central}+0.60S_t^{Florida}.
\]

Both components are bounded continuous `tanh(z/2)` signals.  Their blend
receives one fixed 10% top-level allocation funded from fundamentals.  It
complements rather than replaces the GFS wind and solar forecasts: GFS
represents expected future renewable availability, whereas EIA-930 represents
the realized multi-fuel power-system state.

## BSEE/Sabine event veto

The event controller is applied after the core score. A worsening BSEE
offshore shut-in report accompanied by recent relevant Sabine operating
context can set a conflicting short position to zero. It cannot create a long,
increase an existing long, or alter a non-conflicting position. The selected
D1--3 overlap contains six actual event-veto dates.

## Research splits

| Split | Dates | Used for |
|---|---|---|
| Development | 2017-07-03–2020-12-31 | predeclared factor/weight research |
| Validation | 2021-01-01–2023-12-31 | validation, not selection |
| First-look holdout | 2024-01-01–2026-07-13 | later-period evaluation |

The final South Central substitution was promoted after research comparison;
its approximately +0.04 full-sample Sharpe improvement over Lower 48 is small
and not statistically decisive.

## Performance by period

| Period | Sharpe | CAGR | Maximum drawdown |
|---|---:|---:|---:|
| Development | 1.693 | 12.41% | -4.39% |
| Validation | 1.880 | 18.66% | -5.15% |
| First-look holdout | 1.415 | 13.05% | -6.14% |
| Full | **1.667** | **14.59%** | **-6.14%** |

## Selected common-overlap performance

| Metric | Current D1--5 | D1--3, no guard | Selected D1--3 + storage amplifier |
|---|---:|---:|---:|
| Dates | 2019-07-25–2026-07-13 | same | same |
| Net Sharpe | 2.119 | 2.181 | **2.228** |
| Net Sortino | 3.663 | 3.787 | **3.881** |
| Net CAGR | **19.20%** | 18.75% | 19.06% |
| Maximum drawdown | -5.30% | **-4.16%** | **-4.16%** |
| Total net return | **240.11%** | 231.09% | 237.22% |

Selected minus unguarded D1--3 is +1.81 percentage points when paired daily
net-return differences are simply summed, but +6.12 percentage points when
the two final compounded wealth levels are compared. Selected minus D1--5 is
-1.11 and -2.89 percentage points on those respective definitions.

Maximum drawdown is measured from initial wealth 1.0, so a loss on the first
reported day is included. Period and annual turnover inherit the actual
position immediately before the reporting boundary, consistent with the
transaction cost already embedded in the continuous full-path net returns.

These are 1,748-day common-overlap results for the already-selected 40%
Central / 60% Florida sleeve. The D1--3 choice raises risk-adjusted performance
and reduces drawdown while accepting lower CAGR and cumulative return than the
D1--5 comparator. The 1.667 Sharpe above remains the approved 2017-start
historical baseline and should not be compared directly with the shorter
sample without aligning dates.

## Principal limitations

- EIA histories are revised series rather than a complete first-release
  vintage archive.
- Capacity histories are also revised and have publication lags.
- GFS nodes approximate plant fleets and do not model wake effects,
  curtailment, outages, congestion, snow, tracking geometry, or all
  behind-the-meter solar.
- Settlement backtests do not prove executable intraday liquidity.
- The model has one complete losing calendar year, 2025, and its edge is not
  stable across every subperiod.
- Results are sensitive to the chosen data-availability and roll conventions.
- Florida uses every complete BA on each source day in one continuous rolling
  history. This removes the SCEG cross-region coupling and retains the five
  returns previously lost after partial Florida BA outages.
