# Henry Hub Natural-Gas Futures Strategy: Comprehensive Research Report

**Report date:** August 13, 2026
**Historical formal version:** `south_central_total_storage`
**Selected research version:** `d1_3_wind_storage_amplified_guard`
**Historical formal sample:** July 3, 2017 through July 13, 2026; 2,269 trading days
**Selected common sample:** July 25, 2019 through July 13, 2026; 1,737 trading days
**Instrument:** NYMEX Henry Hub natural-gas futures
**Purpose:** Research documentation, causality audit, and risk review. This is not investment advice or a live-performance claim.

---

## 1. Executive summary

The model treats Henry Hub futures as a market-clearing expression of expected
U.S. natural-gas supply and demand. The selected version combines five
information groups:

1. **Forward weather revisions:** CPC seasonal degree-day forecast changes;
2. **Wind availability:** GFS 80 m wind converted through a nonlinear turbine
   curve and weighted by lagged installed wind capacity;
3. **Solar availability:** GFS surface downward shortwave radiation and
   temperature converted into a capacity-weighted PV proxy;
4. **Gas fundamentals:** South Central storage, dry-gas production, LNG
   exports, and net-import availability;
5. **Realized power and event context:** a fixed EIA-930 Central 40% / Florida
   60% regional shortfall sleeve plus a one-sided BSEE/Sabine short veto.

Each component is oriented so that a positive score is bullish natural gas.
The complete score is delayed by one trading session, clipped to `[-1, 1]`,
and applied to a futures return series that rolls five trading sessions before
the official last-trading-day convention. The backtest charges 2.5 bps per
unit of position change.

### 1.1 Historical formal baseline—not the current selected version

The 2017-start artifact remains the approved historical baseline. It predates
the EIA-930 regional sleeve, event veto, D1--3 wind horizon, and
storage-amplified direction guard. Its figures are retained for provenance and
must not be presented as the current selected strategy.

| Metric | Net of costs | Before costs |
|---|---:|---:|
| Total return | 242.47% | 255.55% |
| CAGR | 14.61% | 15.09% |
| Annualized volatility | 8.17% | 8.17% |
| Sharpe, zero risk-free rate | **1.673** | **1.723** |
| Maximum drawdown | -6.07% | -5.69% |
| Daily win rate | 52.49% | 52.84% |

### 1.2 Selected D1--3 common-overlap performance

The selected enhancement begins on July 25, 2019, when the checked-in D1--3
wind and EIA-930 signals are available to the held position. The comparison
below aligns all versions to the same 1,737 trading days. Every version retains
the selected Central 40% / Florida 60% sleeve and event veto.

| Metric | Current D1--5 | D1--3, no guard | Selected D1--3 + storage amplifier |
|---|---:|---:|---:|
| Net Sharpe | 2.149 | 2.198 | **2.245** |
| Net Sortino | 3.726 | 3.827 | **3.922** |
| Net CAGR | **19.33%** | 18.76% | 19.07% |
| Maximum drawdown | -5.29% | **-4.15%** | **-4.15%** |
| Daily win rate | **54.06%** | 53.66% | 52.22% |
| Total net return | **242.70%** | 231.35% | 237.44% |

The selected version improves Sharpe by 0.096 and Sortino by 0.196 versus the
D1--5 comparator while reducing maximum drawdown by 1.14 percentage points.
Its simple sum of daily incremental net returns is -1.80 percentage points, so
the choice explicitly prioritizes risk-adjusted performance and drawdown over
maximum cumulative return. The approved 2017-start formal artifact remains
unchanged and is reported separately because its history is longer.

![Selected D1--3 strategy dashboard](../results/experiments/d1_3_storage_amplified/latest_strategy_dashboard.png)

The principal conclusions are:

- No single module explains the complete result; weather, renewables, and
  slower fundamentals provide diversification.
- Wind cannot be modeled as a linear wind-speed signal. Very low wind is
  bullish gas, ordinary high wind is bearish, and extreme cut-out-region wind
  can become bullish again.
- South Central Total storage is economically closer to Henry Hub than Lower
  48 storage, but its full-sample Sharpe improvement is only about 0.04 and is
  not decisive statistical evidence.
- The historical formal baseline loses 3.60% in 2025, but the selected
  storage-amplified D1--3 version earns 2.40% with a 0.347 Sharpe. The year
  remains the weakest complete year in the selected overlap.
- Mechanical date look-ahead is substantially controlled, but revised EIA and
  installed-capacity histories remain the largest unresolved vintage risk.

## 2. Model-version boundary

The approved historical strategy contains only:

```text
South Central Total storage fundamentals
+ CPC seasonal forecast revision
+ capacity-weighted nonlinear wind shortfall
+ capacity-weighted PV-availability shortfall
+ production-freeze safety control
+ one-session signal lag
+ five-trading-session early futures roll
+ 2.5 bps position-turnover cost
```

The selected research version adds bounded overlays without changing the
nine internal fundamental signals:

```text
+ fixed 10% EIA-930 sleeve: 40% Central total non-gas shortfall and
  60% Florida firm non-gas shortfall relative to demand, funded from
  the fundamental sleeve
+ BSEE/Sabine pure short veto, applied after the score
+ replace the wind average from forecast days 1--5 with days 1--3
+ apply a one-sided fast-shock guard; low storage acts only as an amplifier
```

The event controller can cancel a conflicting short but cannot create a long,
increase an existing position, or trade by itself.

The formal performance excludes:

- Sabine/Henry Hub EBB receipt or cycle-revision overlays;
- local pipeline headroom and constraint measures;
- weekend or holiday reopen transactions;
- perfect-information production, consumption, or LNG experiments;
- direct observed-weather positioning;
- direct CPC forecast-level positioning;
- geopolitical, dollar, equity, curve, or other market-price overlays.

Several excluded experiments have higher in-sample Sharpe than the formal
version. They are excluded because they depend on short samples, hypothetical
publication timing, revised values, unstable regimes, or execution prices that
have not passed a complete audit.

## 3. Economic mechanism

### 3.1 Temperature and gas demand

Cold winter weather raises residential and commercial heating demand. Hot
summer weather raises electricity demand and, in many regions, gas-fired power
burn. Mild conditions reduce both effects. Futures prices react to expected
demand before physical consumption occurs, so forecast changes are generally
more relevant than already-realized weather.

The active CPC factor is therefore a revision measure: how the forecast for
the same future target dates changed across successive issues.

### 3.2 Wind displacement of gas generation

In ERCOT, SPP, MISO, and other wind-heavy systems, low-marginal-cost wind often
displaces gas-fired generation. The directional mechanism is:

```text
expected wind shortfall rises
        -> gas-fired generation rises
        -> natural-gas demand rises
        -> bullish Henry Hub pressure
```

The reverse holds when expected wind generation is unusually strong, subject
to transmission, curtailment, and unit constraints.

### 3.3 Solar displacement of gas generation

Surface downward shortwave radiation is a direct physical input to PV output.
Weak radiation or heat-related module-efficiency loss can increase daytime
thermal generation requirements. Radiation is preferable to cloud fraction
alone because it incorporates solar geometry, cloud optical depth, atmospheric
transmission, and the energy reaching the surface.

### 3.4 Storage, production, LNG, and trade

- Storage below its same-week seasonal normal indicates a thinner buffer and
  is usually bullish.
- Stronger-than-normal injection or weaker-than-normal withdrawal is usually
  bearish.
- Slower dry-gas production growth is bullish supply tightening.
- Rising LNG exports represent structural U.S. demand and are bullish.
- Lower net-import supply or greater net exports leave less gas domestically
  and are bullish.

Henry Hub is located in Louisiana, so South Central storage and Gulf Coast LNG
activity are economically closer to the marginal hub balance than aggregate
U.S. consumption.

## 4. Daily model architecture

The calculation order is:

```text
forecast issues, EIA fundamentals, and installed capacity
    -> point-in-time raw features at native publication frequency
    -> causal z-scores whose reference windows exclude the current value
    -> CPC weather, wind, solar, and fundamental modules
    -> peak/shoulder seasonal allocation and daylight scaling
    -> winter production-freeze safety control
    -> one-session delay
    -> position clipped to [-1, 1]
    -> five-session early-roll futures return
    -> 2.5 bps x absolute position change
```

Let the contemporaneous component score be

$$
S_t = w_{W,t}W_t + w_{wind,t}Wind_t
      + w_{F,t}F_t + w_{solar,t}Solar_t.
$$

After the freeze control produces $\widetilde S_t$, the traded position is

$$
P_t = \operatorname{clip}(\widetilde S_{t-1},-1,1).
$$

Net daily return is

$$
r^{net}_t = P_t r^{fut}_t
 - 0.00025\left|P_t-P_{t-1}\right|.
$$

## 5. Data inventory and availability assumptions

| Module | Primary source | Native frequency | Simulated availability | Main vintage risk |
|---|---|---|---|---|
| CPC seasonal weather | CPC historical forecast panel | daily issue | mapped issue availability plus one-session model lag | historical panel construction |
| Wind weather | NCAR GDEX / NCEP GFS 0.25 degree | 00Z; six-hour forecast intervals | after issue plus one-session lag | missing GRIB files and model upgrades |
| Wind capacity | USWTDB | turbine/project | only commissioning years before issue year | current snapshot reconstructed backward |
| Solar weather | NCAR GDEX / NCEP GFS | 00Z; six-hour radiation intervals | after issue plus one-session lag | field definitions and model upgrades |
| Solar capacity | EIA utility-scale solar | monthly | two-month lag in factor build | revised history; incomplete distributed PV |
| South Central storage | EIA WNGSR | weekly | week-ending Friday plus six days | revised history, incomplete first-release archive |
| Production, LNG, trade | EIA | monthly | reference month M at start of M+3 | conservative proxy timing; revised values |
| EIA-930 power generation | EIA balancing-authority fuel-type data | hourly observations aggregated daily | source gas day aligned to the strategy score date | reporting coverage and BA-to-pipeline mapping |
| BSEE/Sabine events | BSEE shut-in reports and Sabine operating notices | event | first eligible strategy date after the event record | sparse event history and event classification |
| Futures | C1/C2 settlements | trading day | settlement-return research convention | settlement is not an execution quote |

Exact GCS objects and local destinations are listed in `DATA_MANIFEST.md`.

## 6. Native-frequency standardization

Forward-filling one monthly observation across roughly 20 trading days and
then calculating a 252-day z-score incorrectly treats one release as roughly
20 independent observations. Months with different numbers of trading days
receive different implicit weights. Weekly data suffer the same issue.

The corrected procedure is:

1. Calculate weekly storage statistics only on unique weekly releases;
2. Calculate monthly production, LNG, and trade statistics only on unique
   monthly observations;
3. Exclude the current observation from its own rolling mean and standard
   deviation;
4. Carry the completed release-level score forward to trading dates.

Weekly signals use a 104-release window with at least 52 releases. Monthly
signals use a 60-month window with at least 36 months. Holding all other model
rules fixed, this correction increased full-sample Sharpe from approximately
1.552 to 1.668 in the Lower 48 comparison stage.

## 7. CPC weather module

The seasonal weather variable is HDD from October through March, CDD from May
through September, and GDD in April. The active feature compares forecasts for
the same future target dates across successive issues.

Only `sig_cpc_seasonal_revision` remains active. The legacy weather block
retains three equal nominal slots:

$$
W_t = \frac{\tanh(CPCRevision_t/2)+0_{level}+0_{observed}}{3}.
$$

Keeping the denominator at three prevents removal of CPC level and observed
weather from mechanically tripling the surviving revision signal.

Direct CPC level was unstable because absolute cold or warmth is often already
priced and its relationship changes across seasons and regimes. Realized
weather is more appropriate for explaining demand or inventory than for fast
directional positioning and can create lagged trend-following behavior.

## 8. Capacity-weighted nonlinear wind module

### 8.1 Weather input

The factor uses NCEP GFS 0.25 degree forecasts from the 00Z cycle, 28
representative U.S. locations, four daily valid intervals, and 80 m U/V wind
components. The selected version aggregates forecast days 1--3; forecast days
1--5 remain the prior comparator. Wind speed is

$$
v_{80}=\sqrt{u_{80}^2+v_{80}^2}.
$$

### 8.2 Hub-height adjustment

Wind is adjusted from 80 m to estimated hub height $h$ using

$$
v_h=v_{80}\left(\frac{h}{80}\right)^{0.14}.
$$

The exponent is a neutral approximation and does not fully represent
stability, roughness, low-level jets, or complex terrain.

### 8.3 Power curve

The normalized power proxy is approximately:

$$
P(v)=
\begin{cases}
0, & v<3,\\
\dfrac{v^3-3^3}{12^3-3^3}, & 3\le v<12,\\
1, & 12\le v<20,\\
\text{cosine derating}, & 20\le v<25,\\
0, & v\ge25.
\end{cases}
$$

Thus low wind and extreme cut-out wind can both be bullish gas, while ordinary
high wind is bearish.

### 8.4 Capacity aggregation and signal

USWTDB turbines are mapped to representative weather locations using rated
capacity and commissioning year. For issue year $y$, the point-in-time proxy
includes only turbines with commissioning year no later than $y-1$.

Power curves are evaluated before aggregation:

$$
EstimatedWindCF_t =
\frac{\sum_i Capacity_{i,t}P(v_{i,t})}
{\sum_i Capacity_{i,t}}.
$$

The raw gas-direction feature is wind shortfall, $1-EstimatedWindCF_t$. The
selected signal is a causal 60-issue z-score with a 30-issue minimum and is
compressed as

$$
Wind_t=\tanh\left(\frac{Z^{causal}_{60}(1-EstimatedWindCF_t)}{2}\right).
$$

Positive values indicate weak expected wind and bullish power-burn pressure.

### 8.5 Storage-amplified direction guard

Strong fast bullish shocks can prevent bearish D1--3 wind from reversing an
otherwise positive score below zero. The strong triggers are HDD revision of
at least +1 sigma, winter production-risk revision of at least +1 trailing-
quantile scale unit while the risk level is positive, or Central/Florida firm
non-gas generation shortfall of at least +2 sigma.

Low South Central inventory cannot trigger the guard alone. When inventory is
at least 1 sigma low, the corresponding moderate thresholds are +0.5 sigma for
HDD, +0.5 trailing-quantile scale unit for production revision, and +1 sigma
for firm non-gas shortfall. The guard sets only a wind-flipped negative score
to zero; it cannot create or amplify exposure.

## 9. Capacity-weighted solar module

### 9.1 Radiation conversion

The main GFS fields are downward shortwave radiation (`DSWRF`), total cloud
cover (`TCDC`) for diagnostics, and 2 m temperature. A six-hour average
radiation flux converts to energy as

$$
E_{6h}=DSWRF\times\frac{6}{1000}
\quad\text{kWh/m}^2.
$$

Four intervals form daily surface energy.

### 9.2 Clear-sky geometry and temperature

Surface energy is normalized by deterministic extraterrestrial horizontal
radiation to remove much of the mechanical day-length and solar-angle effect:

$$
K_t=\frac{SW^{surface}_t}{SW^{extra}_t}.
$$

A simple cell-temperature proxy and efficiency adjustment are

$$
T^{cell}=T_{2m}+0.025\,DSWRF,
$$

$$
\eta_T=\operatorname{clip}\left[1-0.004(T^{cell}-25),0.75,1.10\right].
$$

The PV-availability proxy is $K_t\eta_T$.

### 9.3 Capacity weighting and final signal

EIA utility-scale operating solar capacity is mapped to the nearest weather
location and lagged two months. The factor aggregates days 1--5, takes the
negative of expected PV availability so that low solar is bullish gas,
calculates a causal 60-issue z-score, and applies `tanh(z/2)`.

The nominal 10% solar allocation is multiplied by deterministic daylight:

$$
w^{effective}_{solar,t}=0.10\times
\operatorname{clip}\left(\frac{SW^{extra,5d}_t}{10},0.25,1\right).
$$

The average effective weight is approximately 7.93%, with lower winter and
higher summer exposure.

This remains an availability proxy rather than a full hourly PV model. It does
not explicitly model panel orientation, tracking, DC/AC ratio, snow, soiling,
curtailment, outages, distributed solar, or net-load ramps.

## 10. South Central storage module

The formal model uses South Central Total storage for all three storage
features. Henry Hub's Louisiana location and its connection to Gulf Coast
production, LNG terminals, salt storage, and major pipelines provide the
economic rationale. Salt and nonsalt inventories were studied but are not
given separate formal weights.

For each ISO week, the seasonal normal is the mean of the prior five same-week
observations, requiring at least three. The level deviation is

$$
Dev_t=\frac{Storage_t}{Normal_t}-1.
$$

Low storage is bullish, so the signal is a negative causal z-score. One-week
and four-week changes are each compared with their prior-five-year same-week
normal changes and also receive a negative causal z-score. All three scores are
computed on weekly releases before alignment to trading days.

## 11. Monthly fundamental factors

Each monthly factor uses up to 60 independent months with a 36-month minimum.
The active components are:

- **Low production YoY growth:** negative z-score of dry-production YoY;
- **LNG export YoY growth:** positive z-score of export growth;
- **Net-import supply:** negative z-score of `(imports-exports)/consumption`;
- **Dry-production MoM:** negative z-score of the daily-rate monthly change;
- **LNG export MoM:** positive z-score of daily-rate monthly change;
- **Net-import-ratio MoM change:** negative z-score of the ratio change.

`sig_consumption_growth` and `sig_consumption_mom` have zero formal weight.
This does not imply consumption is unimportant. It means delayed nationwide
monthly consumption is too slow, aggregated, and weather-overlapping to serve
as a reliable direct daily-position driver. Regional power burn, pipeline
deliveries, and LNG feedgas are more promising future inputs.

## 12. Final fundamental weights

| Component | Internal weight |
|---|---:|
| South Central low storage | **18.18%** |
| South Central one-week storage change | 9.09% |
| South Central four-week storage change | 9.09% |
| Low production YoY growth | 9.09% |
| LNG export YoY growth | 9.09% |
| Consumption YoY growth | **0%** |
| Net-import supply | 9.09% |
| Dry-production MoM | 9.09% |
| LNG export MoM | **18.18%** |
| Consumption MoM | **0%** |
| Net-import-ratio MoM change | 9.09% |

The two removed consumption slots were assigned to low storage and LNG export
MoM. Candidate selection used development data only, retaining allocations
within 0.01 Sharpe of the development maximum and choosing the lowest-turnover
candidate from that shortlist.

## 13. Seasonal top-level allocation

| Season | Legacy CPC weather | Wind | Fundamentals before solar funding |
|---|---:|---:|---:|
| Peak: Nov--Feb and Jun--Aug | 45.0% | 15.0% | 40.0% |
| Shoulder: Mar--May and Sep--Oct | 22.5% | 22.5% | 55.0% |

Solar receives a nominal 10% daylight-scaled allocation funded from the
fundamental block. Missing wind or solar occupies a neutral fixed slot rather
than causing dynamic renormalization.

For the current selected research version, an additional fixed 10% EIA-930
allocation is funded from the same fundamental sleeve:

| Season | Legacy CPC weather | Wind | Solar | EIA-930 | Fundamentals after funding |
|---|---:|---:|---:|---:|---:|
| Peak: Nov--Feb and Jun--Aug | 45.0% | 15.0% | 0--10.0% | 10.0% | 20.0--30.0% |
| Shoulder: Mar--May and Sep--Oct | 22.5% | 22.5% | 0--10.0% | 10.0% | 35.0--45.0% |

The solar range reflects deterministic daylight scaling; unused solar weight
returns to fundamentals.

## 14. Production-freeze safety control

During November--March, if both the local freeze level score is at least 1.0
and the local revision score is nonnegative, the composite raw score cannot be
negative. The control prevents the model from shorting natural gas during a
severe estimated production disruption. It is a one-sided safety rule, not an
additional alpha factor.

### 14.1 EIA-930 Central / Florida regional signal

The Central component aggregates realized non-gas generation across ERCOT,
MISO, and SPP.  It includes wind, solar, coal, nuclear, hydro, and other
reported non-gas fuels relative to demand.  The Florida component measures
coal, nuclear, and water generation relative to Florida demand.  A positive
shortfall means the power system received less non-gas generation than
expected and may require more gas-fired output; a negative value means firm
non-gas generation was unusually abundant.

The selected continuous signal is 40% Central and 60% Florida inside one fixed
10% allocation.  It does not replace the GFS wind or solar factors.  GFS
expresses expected future renewable availability, while EIA-930 summarizes
the realized multi-fuel system state.

### 14.2 BSEE/Sabine pure short veto

A worsening BSEE offshore shut-in accompanied by recent Sabine operational
context can set a conflicting core short to zero. This design treats the event
information as a risk constraint rather than a standalone bullish forecast.
There are six actual event-veto dates in the selected D1--3 overlap.

## 15. Futures construction and costs

The source panel contains C1 and C2 settlements. For each delivery month, the
formal strategy switches to the contemporaneous C2 five full trading sessions
before the official last-trading-day convention. After the official switch,
that same contract becomes the new C1.

The daily 2.5 bps charge applies to absolute position change. It does not
separately model bid/ask spread, market impact, or both legs of a mechanical
contract roll. Settlement returns are a research convention, not proof of
intraday executability.

## 16. Research splits and performance

### 16.1 Historical formal baseline

This table belongs only to the unchanged 2017-start historical artifact.

| Period | Dates | Sharpe | CAGR | Maximum drawdown |
|---|---|---:|---:|---:|
| Development | 2017-07-03--2020-12-31 | 1.721 | 12.55% | -4.39% |
| Validation | 2021-01-01--2023-12-31 | 1.878 | 18.64% | -5.12% |
| First-look holdout | 2024-01-01--2026-07-13 | 1.407 | 12.97% | -6.07% |
| Full | 2017-07-03--2026-07-13 | **1.673** | **14.61%** | **-6.07%** |

Its calendar-year results are:

| Year | Net return | Sharpe |
|---:|---:|---:|
| 2017 partial | 3.62% | 1.426 |
| 2018 | 1.01% | 0.244 |
| 2019 | 13.28% | 2.120 |
| 2020 | 27.49% | 2.429 |
| 2021 | 12.02% | 1.857 |
| 2022 | 36.89% | 2.749 |
| 2023 | 8.56% | 0.934 |
| 2024 | 27.04% | 2.683 |
| 2025 | -3.60% | -0.533 |
| 2026 partial | 11.12% | 1.849 |

### 16.2 Current selected strategy on its common sample

The current selected strategy is the D1--3 wind version with the
storage-amplified fast-shock guard, 40% Central / 60% Florida EIA-930 sleeve,
and BSEE/Sabine pure short veto. The table compares it with D1--5 on exactly
the same dates and inputs.

| Period | Dates | D1--5 Sharpe | Selected Sharpe | D1--5 CAGR | Selected CAGR | D1--5 max DD | Selected max DD |
|---|---|---:|---:|---:|---:|---:|---:|
| Development overlap | 2019-07-25--2020-12-31 | **2.788** | 2.742 | **24.31%** | 23.60% | **-2.97%** | -3.62% |
| Validation | 2021-01-04--2023-12-29 | 2.137 | **2.270** | **20.62%** | 20.59% | -5.29% | **-4.15%** |
| First-look | 2024-01-02--2026-07-13 | 1.811 | **1.919** | **15.30%** | 15.01% | -4.35% | **-3.69%** |
| Full common sample | 2019-07-25--2026-07-13 | 2.149 | **2.245** | **19.33%** | 19.07% | -5.29% | **-4.15%** |

The selected version improves the fixed validation and first-look Sharpe and
drawdown. It is weaker than D1--5 in the short development overlap and accepts
slightly lower CAGR over the full common sample.

### 16.3 Current selected calendar-year results

| Year | Net return | Sharpe |
|---:|---:|---:|
| 2019 partial | 7.87% | 2.802 |
| 2020 | 25.71% | 2.755 |
| 2021 | 10.00% | 1.689 |
| 2022 | 41.58% | 3.280 |
| 2023 | 12.22% | 1.530 |
| 2024 | 26.90% | 3.217 |
| 2025 | 2.40% | 0.347 |
| 2026 partial | 9.58% | 2.229 |

All selected calendar years are positive in this revised-history backtest,
but 2021 and 2026 YTD have lower Sharpe than the D1--5 comparator. The result
is therefore not a claim of uniform annual dominance.

## 17. Interpretation of the development path

The final Sharpe did not result from one unrestricted optimization. Major
research changes were layered sequentially:

1. Nonlinear capacity-weighted wind separated from the legacy weather block;
2. Observed weather and CPC level set to neutral zero;
3. A conservative 10% capacity-weighted solar slot added;
4. Weekly and monthly factors standardized at native frequency;
5. Consumption factors removed and their slots reassigned under a fixed
   development-only rule;
6. South Central Total replaced Lower 48 for all storage components.
7. A pure BSEE/Sabine short veto was added as an event-risk controller.
8. A continuous 10% EIA-930 sleeve was funded from fundamentals, with 40% of
   the slot assigned to Central and 60% to Florida, while retaining the GFS
   wind and solar forecasts.
9. The selected research version shortened the wind forecast window from
   days 1--5 to days 1--3 and added the storage-amplified fast-shock direction
   guard described in Section 8.5.

Because some later decisions were made after viewing validation diagnostics,
the complete final model cannot claim a pristine untouched holdout. The report
therefore preserves the chronology and labels post-2024 performance as
first-look rather than definitive out-of-sample proof.

## 18. 2025 weakness review

The historical formal model's 2025 result is -3.60% with a -0.533 Sharpe.
The main pattern was not constant small losses. Several large market moves
occurred while slow fundamental values and daily weather signals retained the
prior direction. The model's low average position limited drawdown, but it did
not adapt quickly enough to every reversal.

On the current common-sample calculation, D1--5 earns 0.36% with a 0.050
Sharpe in 2025. Shortening wind to D1--3 without the guard raises return to
1.85% and Sharpe to 0.265. The selected storage-amplified version reaches
2.40% and a 0.347 Sharpe, with a -3.69% maximum drawdown versus -4.35% for
D1--5. The latest version therefore repairs enough of the old loss to make the
year modestly positive, but 2025 remains a weak result rather than strong
evidence of edge.

This suggests that the next improvement should come from timelier regional
balance data and explicit event-risk controls rather than more full-sample
weight tuning.

## 19. Look-ahead and publication-time audit

Controls already implemented include:

- GFS signals retain `forecast_reference_time_utc` and target-date lineage;
- causal rolling windows use `shift(1)` so the current value does not enter its
  own reference distribution;
- weekly and monthly factors are standardized before forward-fill;
- weekly storage becomes available no earlier than the following Thursday;
- monthly production, trade, and LNG use a conservative M+3 convention;
- capacity histories are lagged;
- the final score is delayed by one trading session.

Remaining risks include:

- EIA histories may contain later revisions rather than first releases;
- USWTDB and EIA capacity data are reconstructed from current snapshots;
- exact intraday publication timestamps have not been archived for every
  series;
- settlement prices do not establish execution at the assumed decision time.

The formal backtest should therefore be described as **release-lag-aware but
not fully vintage-pure**.

## 20. Perfect-information production and consumption experiment

An explanatory upper bound aligns final month-M production and consumption
values to month M, removes their actual reporting delay, and compresses all
fundamental z-scores with `tanh(z/2)`. Under the current South Central framework
this raises full-sample Sharpe from 1.673 to approximately 1.781 and improves
maximum drawdown from -6.07% to about -4.40%.

This is not a tradable result. It shows that timely, granular production and
consumption estimates may have incremental value and motivates research into
pipeline flows, regional power burn, dry-gas estimates, and LNG feedgas.

## 21. Rejected or deferred overlays

- **Weekend/holiday reopen trading:** a risk-only flattening rule improved an
  older baseline but reduced current South Central full Sharpe to 1.648.
- **EBB core receipts and revisions:** some recent explanatory value appeared,
  but the sample is short and direction was unstable around 2020.
- **Market and macro overlays:** geopolitical, dollar, equity, curve, and price
  factors did not demonstrate stable incremental value under the tested rules.
- **Observed weather and CPC level:** both remain neutral direct slots.

These findings are documented to prevent rejected ideas from being silently
reintroduced into the formal score.

## 22. Principal physical and operational limitations

### Wind

- generic rather than OEM-specific turbine curves;
- approximate hub-height and no explicit air-density correction;
- no wake loss, icing, maintenance, curtailment, congestion, or plant outage;
- only 28 representative weather locations;
- no direct ISO dispatch constraint.

### Solar

- utility-scale capacity does not capture all behind-the-meter PV;
- no complete panel orientation or tracker model;
- no snow, soiling, degradation, clipping, inverter, or curtailment model;
- daily rather than hourly net-load value.

### Fundamentals and execution

- revised histories rather than full first-release archives;
- national monthly production and trade remain slow proxies;
- no complete regional power-burn or Gulf Coast dry-production history;
- no bid/ask, market-impact, or liquidity model at the intended trade time;
- no dynamic volatility targeting or event-risk sizing.

## 23. Recommended next research priorities

Without changing the formal strategy in advance, the highest-value work is:

1. Archive true release timestamps and first-release vintages for weekly and
   monthly EIA data;
2. Freeze USWTDB and EIA solar-capacity snapshots by acquisition date;
3. Split wind and solar capacity into ERCOT, SPP, MISO, PJM, and CAISO and
   estimate their marginal relationship with regional gas power burn;
4. Validate forecasted wind and PV availability against EIA-930 and ISO actual
   generation;
5. Pre-register tests of successive GFS-run revisions;
6. Acquire timely regional power burn, LNG feedgas, and Gulf Coast dry-gas
   estimates;
7. Extend EBB history before reconsidering a local-balance overlay;
8. Reconstruct realistic executable prices and roll costs;
9. Monitor the fixed model on new data rather than repeatedly optimizing the
   existing sample.

## 24. Reproducibility

Primary entry points are:

- Final notebook: `notebooks/01_final_south_central_strategy.ipynb`
- Formal evaluator: `naturalgas/evaluate_south_central_storage_strategy.py`
- Data manifest: `DATA_MANIFEST.md`
- Formal metrics: `results/formal/summary.json`
- Weights: `results/formal/strategy_weights.csv`
- Period results: `results/formal/period_comparison.csv`
- Annual results: `results/formal/annual_comparison.csv`
- Selected evaluator: `naturalgas/evaluate_d1_3_storage_amplified_strategy.py`
- Selected notebook: `notebooks/07_d1_3_storage_amplified_strategy.ipynb`
- Selected brief: `reports/d1_3_storage_amplified_strategy_brief.md`
- Selected summary: `results/experiments/d1_3_storage_amplified/summary.json`
- Selected dashboard:
  `results/experiments/d1_3_storage_amplified/latest_strategy_dashboard.png`

The repository now includes the factor builders, master-panel builder, pinned
generation manifests, and strict rebuild pipelines. Large source objects
remain in GCS, while the supported build reconstructs the approved result from
their immutable archived generations. This guarantee does not extend to
re-querying current public APIs, whose revised responses may differ from the
historical snapshots.

## 25. Final assessment

The project has developed from a basic weather/fundamental score into a
coherent framework combining forecast revisions, renewable-generation
substitution, realized multi-fuel generation, regional storage, structural gas
supply/demand variables, and bounded event-risk control.
Its economic mechanism is plausible, the major timing assumptions are
explicit, and results remain positive across development, validation, and the
post-2024 period.

The current selected storage-amplified D1--3 version records 2.245 net Sharpe,
3.922 Sortino, 19.07% CAGR, and -4.15% maximum drawdown on the matched
2019--2026 sample. These figures supersede the earlier EIA-only selected
version in this report. The 1.673 Sharpe remains only the longer 2017-start
historical formal baseline.

The research direction deserves continued development, but the next stage
should focus on point-in-time data, regional physical validation, Gulf Coast
local-balance information, and realistic execution rather than adding more
parameters to maximize historical Sharpe.
