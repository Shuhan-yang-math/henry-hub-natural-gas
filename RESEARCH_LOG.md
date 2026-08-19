# Research log and model decisions

This document separates accepted model changes from informative but rejected
experiments. It is intended to prevent full-sample discoveries from being
mistaken for predeclared live rules.

The model chronology is V01 frozen formal baseline → V02 superseded EIA-930
research model → V03 current selected D1--3 research model. Stable ids,
lifecycle states, promotion dates, implementations, notebooks, and result
directories are registered in `config/model_registry.yaml`.

## Accepted research steps

| Research question | Decision | Evidence retained here |
|---|---|---|
| Should weekly/monthly variables be standardized after daily forward-fill? | No. Standardize at native frequency first. | Full Sharpe increased from 1.552 to 1.668 with fixed top-level policy. |
| Is nonlinear capacity-weighted wind useful? | Yes, as a separate seasonal component. | Shoulder wind allocation fixed at 22.5%; peak wind at 15%. |
| Is shortwave/PV availability more useful than cloud alone? | Yes. | Use the capacity-weighted PV-availability signal with 10% nominal daylight-scaled weight. |
| Should consumption variables directly drive the daily position? | No in the current version. | Both consumption slots are zero; freed slots went to low storage and LNG MoM. |
| Is regional storage preferable to Lower 48? | Use South Central Total, cautiously. | After the NYMEX holiday-calendar correction, full Sharpe is 1.667 versus 1.627 for Lower 48; holdout improvement remains small. |
| Should the strategy roll at official expiry? | No. | Use the fixed five-trading-session early roll. |
| Should realized power data replace the GFS wind and solar forecasts? | No. | Keep GFS for future availability; use realized EIA-930 Central and Florida states in a separate 10% sleeve. |
| How should offshore shut-ins enter the strategy? | Use a one-sided event veto. | The controller can cancel a conflicting short but cannot create or amplify exposure. |
| Does the selected EIA-930 sleeve improve the matched sample? | Yes, as downside diversification. | On the 1,748-day overlap, the 40/60 blend reaches 2.084 Sharpe and 3.576 Sortino versus 1.951 and 3.252 for Central only. |
| What Central/Florida mix should be retained? | 40% Central / 60% Florida inside the fixed 10% slot. | It has the highest 2021–2023 validation Sharpe and lies inside a stable 60%–80% Florida plateau; the ex-post 80% Florida full-sample maximum was not selected. |
| Which wind horizon and direction guard should be retained? | Select forecast days 1--3 with the storage-amplified fast-shock guard; disable its HDD branch in June--August and do not add CDD. | On the calendar-corrected, daily-available-BA Florida 1,748-day sample, Sharpe is 2.228 and Sortino is 3.881 versus 2.119 and 3.663 for D1--5; maximum drawdown improves from -5.30% to -4.16%, with lower CAGR and total return. |

## Deliberately excluded experiments

- **Observed weather direct factor:** removed from direct positioning; it is
  better viewed as an explanatory input to demand/inventory models.
- **CPC forecast level:** direct level signal was unstable and is fixed at
  zero; only forecast revision remains active.
- **Weekend/holiday 18Z reopen trading:** in the pre-calendar-correction audit,
  the risk-only flattening rule reduced South Central full Sharpe from 1.673 to
  1.648. It is not formal and those historical comparison values are retained
  only as experiment provenance.
- **Earlier daily EBB Henry Hub/Sabine pipeline overlay:** short-history results
  were regime dependent, especially around 2020. No EBB factor is in the
  formal daily model. The separately timed I3-to-settlement overlay is recorded
  below and remains outside V03.
- **Macro/geopolitical/market-price overlays:** no stable incremental signal
  was established, so they are excluded.
- **No-lag EIA production and consumption:** aligning final month-M values to
  month M is a perfect-information upper bound. With robust `tanh(z/2)`
  compression it raised current South Central full Sharpe to approximately
  1.781, but it is impossible to trade with the current delayed EIA source.

## Parameter-selection discipline

- Wind shoulder weights were evaluated on a declared grid and development
  period; validation and first-look periods were reported separately.
- The solar development optimum reached the 15% grid boundary. To reduce
  overfitting risk, the formal research version uses 10%, not the boundary.
- Consumption-slot reallocation was selected from development-period
  candidates within 0.01 Sharpe of the best, using lower turnover as the
  tiebreaker.
- South Central replacement was a later research decision and should be
  monitored as a model change, not treated as an untouched holdout result.

## Notebook map

| Notebook | Purpose |
|---|---|
| `01_model_v01_south_central_storage.ipynb` | V01 frozen formal baseline and historical performance |
| `02_capacity_weighted_wind.ipynb` | wind power curve, capacity weighting, and seasonal allocation |
| `03_capacity_weighted_solar.ipynb` | radiation/PV factor, weight grid, and controls |
| `04_native_frequency_fundamentals.ipynb` | weekly/monthly causal standardization correction |
| `05_fundamental_weight_selection.ipynb` | remove consumption and reallocate fixed slots |
| `06_model_v02_eia930_central_florida.ipynb` | V02 superseded EIA-930 regional blend, stability, and loss-day attribution |
| `07_model_v03_d1_3_storage_guard.ipynb` | V03 current selected wind horizon, guard logic, performance, and intervention audit |
| `08_sabine_nomination_revision_intraday_overlay_final.ipynb` | Final isolated I3 nomination-revision overlay, execution timing, rejected next-session comparator, and robustness |

## V02 — August 11, 2026 superseded EIA-930 research model

V02 retained the nine-factor fundamental block and the
GFS wind/solar forecasts.  It allocates a fixed 10% EIA-930 sleeve 40% to the
ERCOT/MISO/SPP total non-gas shortfall and 60% to Florida firm non-gas
generation relative to demand.  It also keeps the BSEE/Sabine controller as a
pure short veto.

On the calendar-corrected daily-available-BA 1,748-day EIA-930 overlap, the
selected version records 2.084 net Sharpe, 3.576 Sortino, 19.24% CAGR, and
-5.29% maximum drawdown. The previous Central-only sleeve records 1.951
Sharpe, 3.252 Sortino, 19.07% CAGR, and
-6.07% maximum drawdown. The simple daily incremental net return versus
Central is +0.66 percentage points, while the distinct compounded final-wealth
difference is +3.26 percentage points: the improvement is primarily a
smoother loss path, not a large unconditional daily-return increment.

The Central sleeve has 808 loss days. The selected blend improves 544 of
them, turns 74 nonnegative, and recovers 32.00 percentage points on those
dates. It gives back 31.35 points on Central non-loss days, which explains why
Sharpe and Sortino improve while unconditional cumulative return is nearly
unchanged.

The implementation and output are isolated from the approved full-history
artifact:

- `naturalgas/evaluate_model_v02_eia930_central_florida.py`
- `results/models/v02_eia930_central_florida/`
- GCS artifacts `selected_eia930_overlay_inputs` and
  `selected_event_reports_aligned` pinned in
  `manifests/selected_strategy_inputs_2026-08-14.json`
- `notebooks/06_model_v02_eia930_central_florida.ipynb`
- `reports/model_v02_eia930_central_florida_brief.md`

## V03 — August 12--14, 2026 current selected research model

The current selected research version keeps the 40% Central / 60% Florida
EIA-930 sleeve and replaces the days 1--5 wind average with days 1--3. It also
adds a one-sided storage-amplified fast-shock guard. Low storage cannot trigger
the guard by itself; it only allows moderate HDD, production-risk revision, or
firm non-gas shortfall shocks to prevent wind from reversing an otherwise
bullish score into a short.

On August 13 the weather-revision branch was narrowed after reviewing the
historical alternatives: HDD remains active in January--May and September--
December, is disabled in June--August, and CDD is not used. These historical
figures are retrospective validation, not an untouched holdout.

On the calendar-corrected daily-available-BA 1,748-day overlap, current D1--5
records 2.119 net Sharpe, 3.663 Sortino, 19.20% CAGR, and -5.30% maximum
drawdown. The selected D1--3 version records 2.228 Sharpe, 3.881 Sortino,
19.05% CAGR, and -4.16% maximum drawdown. Its simple sum of paired daily net-
return differences versus D1--5 is -1.11 percentage
points, while its compounded final-wealth difference is -2.89 percentage
points. The decision therefore prioritizes risk-adjusted performance and
drawdown over maximum cumulative return.

The guard changes 59 held-return dates, helps 34, hurts 25, and adds 1.81
percentage points to the sum of paired daily net-return differences relative
to unguarded D1--3; the corresponding compounded final-wealth difference is
+6.12 percentage points. Its effect is not positive in every year, so the
frozen rule remains subject to prospective monitoring.

- `naturalgas/evaluate_model_v03_d1_3_storage_guard.py`
- GCS artifacts `selected_d1_3_storage_amplifier_inputs` and
  `selected_wngsr_d1_3_score_corrections` pinned in
  `manifests/selected_strategy_inputs_2026-08-14.json`
- `results/models/v03_d1_3_storage_guard/`
- `notebooks/07_model_v03_d1_3_storage_guard.ipynb`
- `reports/model_v03_d1_3_storage_guard_brief.md`

## August 19, 2026 — final Sabine nomination-revision intraday overlay

The pipeline-nomination work is retained as a separate intraday research
overlay rather than a V03 score change. The final name is **Sabine dominant
nomination-revision intraday overlay**.

The signal compares two causal scheduled-quantity revisions: TransCameron LNG
delivery from Intraday 1 to Intraday 3, and Jefferson Island injection minus
withdrawal from Timely to Intraday 3. Each uses an expanding z-score based on
strictly earlier gas days with a 60-day minimum. The larger absolute revision
sets a temporary position of `0.10 * tanh(z)` while preserving its sign.

Execution is fixed at the held NG contract's trade VWAP from I3 posting +5
through +30 minutes, followed by a complete exit at the same contract's
settlement-window VWAP. The overlay pays 2.5 bps per unit on both entry and
exit. It does not change the stored V03 daily position.

On the 2023-10-23--2026-07-13 active window, base V03 records 1.960 Sharpe,
47.36% total return, and -3.94% maximum drawdown. The selected intraday overlay
records 2.454 Sharpe, 65.44% total return, and -3.10% maximum drawdown. The
simple sum of incremental net returns is +1,162.9 bps across 635 eligible I3
observations.

The same signal was also shifted to the next confirmed session's normal
settlement-to-settlement position without an I3-to-settlement trade. That
comparator records 1.332 Sharpe, 30.06% total return, -7.86% maximum drawdown,
and a -1,250.4 bps simple incremental net-return sum on the same active dates.
It is rejected. This contrast makes execution timing part of the retained
economic hypothesis rather than an implementation detail.

Final artifacts:

- `naturalgas/evaluate_sabine_nomination_revision_intraday_overlay_final.py`
- `naturalgas/pipelines/rebuild_sabine_nomination_overlay.py`
- `manifests/sabine_nomination_overlay_inputs_2026-08-19.json`
- `notebooks/08_sabine_nomination_revision_intraday_overlay_final.ipynb`
- `reports/sabine_nomination_revision_intraday_overlay_final.md`
- `results/experiments/sabine_nomination_revision_intraday_overlay_final/`

The reproduction pipeline downloads three exact GCS generations, rebuilds the
retained revisions and 20/60/120-gas-day causal z-scores from the 231,679-row
raw all-cycle OAC archive, requires exact parity with the assembled research
panel, validates the processed execution-window contract, and verifies the
reproduced daily path and result tables against the shipped artifacts. Raw
NYMEX ticks remain controlled and are not redistributed; the pinned execution
window is the exact processed price-input contract.

Earlier exploratory scripts and processed outputs remain unchanged.
