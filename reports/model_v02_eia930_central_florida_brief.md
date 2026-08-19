# Model V02 — EIA-930 Central/Florida Strategy Brief

**Research date:** August 11, 2026
**Common sample:** July 25, 2019 through July 13, 2026; 1,748 trading days
**Status:** superseded research model; retained for chronology and attribution

## Decision

V02 set the EIA-930 sleeve to **40% Central and 60% Florida inside the
existing fixed 10% slot**.  In total-strategy terms, 4% of the score uses the
Central signal and 6% uses the Florida signal.  The change does not add
leverage and leaves the GFS wind and solar factors, production-risk state,
BSEE/Sabine short veto, and 2.5 bp turnover cost unchanged.

The 40/60 version reaches net Sharpe 2.084 and Sortino 3.576, versus 1.951 and
3.252 for the previous 100% Central sleeve on the exact common sample. Its
maximum drawdown is -5.29%, compared with -6.07% for Central. The improvement
is primarily a smoother loss and drawdown profile rather than higher
cumulative return.

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

## Signals and physical interpretation

The Central signal uses total non-gas generation relative to demand across
ERCOT, MISO, and SPP.  The Florida signal uses firm non-gas generation relative
to demand:

```math
S^{FL}_t =
-\tanh\left[
\frac{1}{2}z_t\left(
\frac{Coal_t+Nuclear_t+Water_t}{Demand_t}
\right)
\right].
```

A positive Florida signal indicates that coal, nuclear, and water generation
supply an unusually small share of Florida demand, increasing the probability
that gas-fired generation must meet the residual requirement.

Florida represents approximately 29.2% of demand in the audited Southeast
footprint but 42.6% of its gas generation.  Reported Florida gas generation is
approximately 67.4% of local demand.  The Florida and Central signals have a
correlation of only approximately 0.010, so the blend adds a distinct regional
power-system state rather than duplicating the existing Central exposure.

## Common-sample results

<!-- BEGIN AUTO-GENERATED: eia-brief-table -->
| Metric | Core weather, fundamentals, and veto | Previous 10% Central sleeve | Selected Central 40% / Florida 60% |
|---|---:|---:|---:|
| Net Sharpe | 1.856 | 1.951 | **2.084** |
| Sortino | 3.157 | 3.252 | **3.576** |
| CAGR | 17.99% | 19.07% | **19.24%** |
| Maximum drawdown | -6.14% | -6.07% | **-5.29%** |
| Mean absolute position | 11.51% | 11.73% | **10.81%** |
| Final cumulative return | 216.61% | 237.47% | **240.73%** |
<!-- END AUTO-GENERATED: eia-brief-table -->

<!-- BEGIN AUTO-GENERATED: eia-brief-claim -->
Relative to the Central sleeve, the selected version raises Sharpe by 0.132
and Sortino by 0.324. The simple sum of daily incremental net returns is
+0.66 percentage points, while the distinct compounded final-wealth level is
3.26 percentage points higher.
<!-- END AUTO-GENERATED: eia-brief-claim -->
This combination confirms that the improvement comes from path and tail-risk
diversification rather than a material increase in unconditional return.

## Weight stability

The slot weight was evaluated on a fixed grid from 0% to 100% Florida in
10-point increments.

| Central / Florida | Full Sharpe | Sortino | Development Sharpe | Validation Sharpe | 2024+ Sharpe | Maximum drawdown |
|---|---:|---:|---:|---:|---:|---:|
| 100% / 0% | 1.951 | 3.252 | 2.776 | 2.025 | 1.438 | -6.07% |
| 50% / 50% | 2.071 | 3.538 | 2.776 | 2.124 | 1.618 | -5.38% |
| **40% / 60%** | **2.084** | **3.576** | 2.762 | **2.129** | 1.651 | -5.29% |
| 20% / 80% | 2.096 | 3.624 | 2.723 | 2.123 | 1.707 | -5.24% |
| 0% / 100% | 2.088 | 3.629 | 2.667 | 2.099 | **1.747** | **-5.20%** |

The 80% Florida row has the highest ex-post full-sample Sharpe, and 90%
Florida has the highest full-sample Sortino.  Neither is selected.  The 60%
Florida row has the highest 2021-2023 validation Sharpe and lies in the middle
of the broad 60%-80% Florida plateau, retaining more of the previously selected
Central information.

## Loss-day behavior

Loss days are defined once using the previous Central sleeve's net return,
then the selected strategy is compared on those same dates.

| Diagnostic | Result |
|---|---:|
| Previous Central loss days | 808 |
| Loss days improved by 40/60 | 544 (67.3%) |
| Central loss days turned nonnegative | 74 |
| Central loss-day net return | -244.00 pp |
| Selected return on the same dates | -212.00 pp |
| Loss-day improvement | **+32.00 pp** |
| Giveback on Central non-loss days | -31.35 pp |

On Central loss days, mean absolute position falls from 10.92% to 10.08%.
Reducing a same-direction position contributes +38.58 percentage points, and
flipping direction contributes +10.92 points.  Increasing a same-direction
position costs -17.89 points.  Gross position changes account for nearly all
of the improvement; transaction-cost differences contribute only about +0.07
points.  The Florida signal therefore behaves like regional downside
diversification, not a cost artifact.

## Timing and limitations

Each EIA-930 source day maps to the first strictly later strategy score date,
and the position retains the existing one-session lag.  Both Central and
Florida position-source dates strictly precede the return date in the frozen
daily output.

The Florida signal is explicitly independent of the Carolinas group. On every
source day it aggregates the Florida BAs whose daily inputs are complete. A
partial-BA observation enters the same rolling history as ordinary nine-BA
observations and remains available to future rolling references. This retains
all five returns previously omitted after partial Florida outages.

The historical EIA-930 bulk files can be revised, and generation by fuel is a
next-day realized observation rather than a forecast.  Florida was identified
in a second-stage geographic audit, and its effect on Henry Hub remains
conditional on pipeline capacity, regional basis, and the marginal generation
fleet.  The 40/60 version should remain in shadow deployment until prospective
first-vintage capture confirms the historical result.

## Reproducible artifacts

- `notebooks/06_model_v02_eia930_central_florida.ipynb`
- `naturalgas/evaluate_model_v02_eia930_central_florida.py`
- GCS artifact `selected_eia930_overlay_inputs` in
  `manifests/selected_strategy_inputs_2026-08-14.json`
- GCS artifact `selected_florida_available_ba_signal_history` in the same
  manifest
- `results/models/v02_eia930_central_florida/summary.json`
- `results/models/v02_eia930_central_florida/strategy_daily.parquet`
- `results/models/v02_eia930_central_florida/central_florida_weight_sweep.csv`
- `results/models/v02_eia930_central_florida/loss_day_yearly.csv`
- `results/models/v02_eia930_central_florida/dashboard.png`
- `results/models/v02_eia930_central_florida/central_florida_weight_sweep.png`
