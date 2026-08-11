# EIA-930 Central 40% / Florida 60% Strategy Brief

**Research date:** August 11, 2026  
**Common sample:** July 25, 2019 through July 13, 2026; 1,737 trading days  
**Status:** selected research version; not yet production-approved

## Decision

The selected EIA-930 sleeve is now **40% Central and 60% Florida inside the
existing fixed 10% slot**.  In total-strategy terms, 4% of the score uses the
Central signal and 6% uses the Florida signal.  The change does not add
leverage and leaves the GFS wind and solar factors, production-risk state,
BSEE/Sabine short veto, and 2.5 bp turnover cost unchanged.

The 40/60 version reaches net Sharpe 2.115 and Sortino 3.643, versus 1.992 and
3.329 for the previous 100% Central sleeve on the exact common sample.  Its
maximum drawdown is -5.27%, compared with -6.07% for Central.  The improvement
is primarily a smoother loss and drawdown profile rather than higher
cumulative return.

## Signals and physical interpretation

The Central signal uses total non-gas generation relative to demand across
ERCOT, MISO, and SPP.  The Florida signal uses firm non-gas generation relative
to demand:

\[
S^{FL}_t =
-\tanh\left[
\frac{1}{2}z_t\left(
\frac{Coal_t+Nuclear_t+Water_t}{Demand_t}
\right)
\right].
\]

A positive Florida signal indicates that coal, nuclear, and water generation
supply an unusually small share of Florida demand, increasing the probability
that gas-fired generation must meet the residual requirement.

Florida represents approximately 29.2% of demand in the audited Southeast
footprint but 42.6% of its gas generation.  Reported Florida gas generation is
approximately 67.4% of local demand.  The Florida and Central signals have a
correlation of only approximately 0.010, so the blend adds a distinct regional
power-system state rather than duplicating the existing Central exposure.

## Common-sample results

| Metric | Core weather, fundamentals, and veto | Previous 10% Central sleeve | Selected Central 40% / Florida 60% |
|---|---:|---:|---:|
| Net Sharpe | 1.875 | 1.992 | **2.115** |
| Sortino | 3.195 | 3.329 | **3.643** |
| CAGR | 18.03% | 19.36% | **19.38%** |
| Maximum drawdown | -5.59% | -6.07% | **-5.27%** |
| Mean absolute position | 11.50% | 11.73% | **10.80%** |
| Final cumulative return | 217.35% | 243.11% | **243.70%** |

Relative to the Central sleeve, the selected version raises Sharpe by 0.122
and Sortino by 0.314.  The simple sum of daily incremental net returns is
-0.13 percentage points, while compounded final wealth is slightly higher.
This combination confirms that the improvement comes from path and tail-risk
diversification rather than a material increase in unconditional return.

## Weight stability

The slot weight was evaluated on a fixed grid from 0% to 100% Florida in
10-point increments.

| Central / Florida | Full Sharpe | Sortino | Development Sharpe | Validation Sharpe | 2024+ Sharpe | Maximum drawdown |
|---|---:|---:|---:|---:|---:|---:|
| 100% / 0% | 1.992 | 3.329 | 2.785 | 2.030 | 1.536 | -6.07% |
| 50% / 50% | 2.104 | 3.607 | 2.784 | 2.117 | 1.714 | -5.29% |
| **40% / 60%** | **2.115** | **3.643** | 2.770 | **2.120** | 1.745 | -5.27% |
| 20% / 80% | 2.121 | 3.682 | 2.730 | 2.109 | 1.796 | -5.23% |
| 0% / 100% | 2.108 | 3.676 | 2.672 | 2.079 | **1.831** | **-5.18%** |

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
| Previous Central loss days | 801 |
| Loss days improved by 40/60 | 541 (67.5%) |
| Central loss days turned nonnegative | 73 |
| Central loss-day net return | -240.88 pp |
| Selected return on the same dates | -209.00 pp |
| Loss-day improvement | **+31.88 pp** |
| Giveback on Central non-loss days | -32.01 pp |

On Central loss days, mean absolute position falls from 10.93% to 10.10%.
Reducing a same-direction position contributes +38.32 percentage points, and
flipping direction contributes +10.92 points.  Increasing a same-direction
position costs -17.69 points.  Gross position changes account for nearly all
of the improvement; transaction-cost differences contribute only about +0.07
points.  The Florida signal therefore behaves like regional downside
diversification, not a cost artifact.

## Timing and limitations

Each EIA-930 source day maps to the first strictly later strategy score date,
and the position retains the existing one-session lag.  Both Central and
Florida position-source dates strictly precede the return date in the frozen
daily output.

The historical EIA-930 bulk files can be revised, and generation by fuel is a
next-day realized observation rather than a forecast.  Florida was identified
in a second-stage geographic audit, and its effect on Henry Hub remains
conditional on pipeline capacity, regional basis, and the marginal generation
fleet.  The 40/60 version should remain in shadow deployment until prospective
first-vintage capture confirms the historical result.

## Reproducible artifacts

- `notebooks/06_eia930_central_florida_40_60.ipynb`
- `naturalgas/evaluate_eia930_selected_enhancement.py`
- `inputs/audit/eia930/selected_overlay_inputs.parquet`
- `results/experiments/eia930_selected/summary.json`
- `results/experiments/eia930_selected/selected_strategy_daily.parquet`
- `results/experiments/eia930_selected/central_florida_weight_sweep.csv`
- `results/experiments/eia930_selected/loss_day_yearly.csv`
- `results/experiments/eia930_selected/latest_strategy_dashboard.png`
- `results/experiments/eia930_selected/central_florida_weight_sweep.png`

