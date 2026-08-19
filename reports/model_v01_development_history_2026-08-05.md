# Development History of a Weather-and-Fundamentals Model for Henry Hub Natural-Gas Futures

**Model-development history and validation record**  
**Report date:** August 5, 2026  
**Current registry identity:** `hh_v01_south_central_storage` (V01 frozen formal baseline)
**Backtest sample:** July 3, 2017 through July 13, 2026  
**Instrument:** NYMEX Henry Hub natural-gas futures  
**Status:** Research model; not a live-performance claim or investment recommendation  
**Structure:** Part I--fundamental research; Part II--quantitative model and validation

---

## Executive overview

This document records the complete development history of a directional Henry
Hub natural-gas futures model built from weather forecasts,
renewable-generation availability, and physical natural-gas fundamentals. It
follows the model from the original economic thesis and candidate feature set
through feature engineering, removal decisions, weight selection, validation,
and the final specification. The purpose is not merely to present the final
backtest, but to preserve why this model class was chosen, which experiments
succeeded or failed, what statistical methods were used, and where the result
remains vulnerable to overfitting or imperfect point-in-time data.

The model was designed around an economic view of Henry Hub as the marginal
price of a national gas system with strong Gulf Coast linkages. Temperature
forecasts affect heating and cooling demand; wind and solar availability
affect gas-fired power burn; storage, production, LNG exports, and trade affect
the physical supply-demand buffer. The final specification is a bounded,
modular score rather than a high-dimensional machine-learning forecast. This
choice reflects the limited effective sample, large structural changes in the
gas market, incomplete historical vintages, and the need to preserve economic
directionality and auditability.

Model development proceeded in stages. The initial broad weather,
fundamental, market, and macro feature set was reduced to signals that had a
clear physical mechanism and acceptable timing. Direct observed weather and
CPC forecast levels were neutralized; only CPC forecast revisions remained.
Linear wind measures were replaced by a nonlinear, capacity-weighted wind
generation proxy. A capacity-weighted PV-availability factor was added at a
conservative weight below its development-grid optimum. Weekly and monthly
fundamentals were standardized at their native release frequencies to prevent
forward-filled observations from creating artificial sample size. Two slow
national consumption factors were removed, and their fixed slots were
reassigned using development-only selection. Finally, South Central Total
storage replaced Lower 48 storage because of its closer relationship to Henry
Hub and Gulf Coast balance.

From July 2017 through July 2026, the formal strategy produced a net
cumulative return of 242.47%, a 14.61% CAGR, an 8.17% annualized volatility,
a zero-rate Sharpe ratio of 1.673, and a maximum drawdown of -6.07% after a
2.5-basis-point turnover charge. These results are economically encouraging
but should not be treated as proof of a persistent 1.67 Sharpe. The final
model was assembled through multiple research stages, several data histories
are revised rather than vintage-pure, and 2025 exposed meaningful wind-signal
persistence, slow-fundamental latency, and jump-risk weaknesses.

The development history is organized in two parts. Part I develops the
physical and fundamental thesis, data, feature definitions, and
feature-selection record. Part II presents execution, statistical methodology,
weight construction, formal results, the 2025 postmortem, and risk analysis.

---


# Part I. Fundamental Research and Physical Model Development

This part explains the economic thesis, physical data, feature engineering,
and the evidence used to retain, remove, or defer each candidate signal.

## 1. Research objective

The objective is to estimate a continuous daily directional exposure to
Henry Hub futures:

```math
P_t \in [-1,1],
```

where positive exposure represents a long natural-gas position and negative
exposure represents a short position. The model seeks to answer:

> Given only information that could reasonably have been available at the
> decision date, do weather-driven demand, renewable-generation substitution,
> and physical supply-demand conditions contain stable directional
> information for the next tradable Henry Hub futures return?

The model is not intended to forecast the exact settlement price or the full
conditional return distribution. It is a directional allocation framework
that converts heterogeneous information into a bounded position.

Three practical requirements shaped the research:

1. **Economic interpretability.** Every retained signal must have a defensible
   mechanism connecting it to gas demand, supply, storage scarcity, or local
   Gulf Coast balance.
2. **Timing auditability.** Forecast issue time, EIA release timing, capacity
   lag, futures settlement, and position lag must be explicit.
3. **Model restraint.** The final rule should remain low-dimensional and
   bounded because approximately 2,269 formal trading days are not enough to
   support an unrestricted high-dimensional model across many structural
   regimes.

## 2. Why this model class was chosen

### 2.1 A physical model rather than a pure price model

Natural gas is unusually suitable for a physically motivated model. Weather
affects residential and commercial heating, summer electricity demand, wind
generation, solar generation, and freeze-off risk. Storage measures the
buffer between supply and demand. Production, LNG exports, imports, and
exports describe slower structural pressure. These relationships exist
independently of the historical correlation observed in the backtest.

Price-only features such as momentum, curve shape, and spot-futures basis can
be useful, but their meaning changes with positioning, liquidity, storage
economics, and contract roll. A physical model was preferred as the core
because its signal signs can be specified before observing returns:

- colder winter forecasts are bullish;
- hotter summer forecasts are bullish through power burn;
- low wind and low solar availability are bullish through thermal
  substitution;
- low storage and weak injections are bullish;
- weaker production is bullish;
- stronger LNG exports are bullish;
- lower domestic net-import availability is bullish.

### 2.2 A modular score rather than an unrestricted regression

The project deliberately uses a modular score instead of fitting one large
linear regression, boosted tree, or neural network. This does not assert that
machine learning is inherently inappropriate. It reflects five constraints:

1. The formal sample begins in 2017, leaving fewer than ten complete years.
2. GFS physics, renewable capacity, LNG export capacity, pipeline topology,
   and gas-market regimes changed materially during the sample.
3. Several EIA and capacity histories are revised rather than archived as
   first-release vintages.
4. Forecast features are autocorrelated and cross-correlated, so apparent
   sample size greatly exceeds effective independent sample size.
5. A modular score permits direct ablation, timing audits, and economic
   interpretation of each source of profit and loss.

The score is linear at the module-allocation level, but the underlying feature
engineering is deliberately nonlinear: causal standardization, turbine power
curves, hyperbolic-tangent compression, daylight scaling, seasonal
allocations, and a one-sided freeze-off control.

### 2.3 Why the model is seasonal

The marginal value of the same physical signal differs by season. Temperature
forecast revisions matter most in peak heating and cooling periods. During
spring and autumn shoulder months, direct temperature demand is weaker and
renewable-generation variation can represent a larger fraction of marginal
gas power burn. The model therefore fixes different top-level weights for:

- **Peak months:** November-February and June-August;
- **Shoulder months:** March-May and September-October.

This seasonal split is intentionally coarse. It provides an economic
structure without fitting month-specific weights for all twelve months.

## 3. Initial feature universe

The original multi-signal panel was intentionally broad. It contained four
families.

### 3.1 Weather and renewable candidates

- CPC seasonal forecast revision;
- CPC forecast level;
- observed HDD/CDD anomalies;
- GFS temperature revision;
- GFS wind speed;
- GFS cloud cover;
- production freeze-off weather;
- later nonlinear capacity-weighted wind;
- later capacity-weighted radiation and PV availability.

### 3.2 Natural-gas fundamental candidates

- storage level relative to seasonal normal;
- one-week storage change surprise;
- four-week storage change surprise;
- dry-gas production year-over-year growth;
- LNG export year-over-year growth;
- total consumption year-over-year growth;
- net-import supply ratio;
- dry-production month-over-month change;
- LNG export month-over-month change;
- consumption month-over-month change;
- net-import-ratio month-over-month change.

### 3.3 Market-price candidates

- futures momentum;
- C2-C1 backwardation or contango;
- C4-C1 long-curve carry;
- front-futures versus Henry Hub spot basis;
- spot momentum.

### 3.4 Macro and risk candidates

- U.S. dollar weakness;
- equity-market growth;
- Treasury term spread;
- geopolitical-risk shock.

This broad panel was useful for hypothesis generation, but the final model
does not assign a nonzero weight to every available variable. Features were
removed when they lacked stable incremental value, duplicated another
mechanism, depended on a problematic timing convention, or reduced economic
clarity.

## 4. Data sources and point-in-time policy

| Module | Primary source | Native frequency | Simulated availability | Main unresolved risk |
|---|---|---|---|---|
| CPC seasonal weather | CPC historical forecasts | Daily issue | Issue mapping plus one-session lag | Archive construction |
| Wind weather | NCAR GDEX / NCEP GFS 0.25 degree | 00Z and six-hour leads | Forecast reference time plus model lag | Missing GRIBs and GFS upgrades |
| Wind capacity | USWTDB | Turbine/project | Commissioning year no later than issue year minus one | Current snapshot reconstructed backward |
| Solar weather | NCAR GDEX / NCEP GFS | 00Z and six-hour radiation | Forecast reference time plus model lag | Field and model-version changes |
| Solar capacity | EIA utility-scale solar | Monthly | Two-month lag | Revised history; incomplete distributed PV |
| Storage | EIA WNGSR | Weekly | Week-ending Friday plus six days | Revised rather than complete first-release archive |
| Production, LNG, trade | EIA | Monthly | Reference month M at start of M+3 | Conservative proxy timing and revised values |
| Futures | C1/C2 settlement panel | Trading day | Settlement-return convention | Not an executable quote |

### 4.1 What “point in time” means in this study

A historical observation can have at least three relevant dates:

1. the **reference date**, which identifies the period being measured;
2. the **publication or forecast-issue date**, when the information first
   became available;
3. the **model-availability date**, when this backtest permits the signal to
   affect a position.

For example, an EIA value describing January production has a January
reference month, may be published substantially later, and is assigned a
still more conservative model-availability date at the start of April
\((M+3)\). The observation must be joined to the trading calendar by the
third date, not by the first.

The “simulated availability” column records the operational rule used by the
backtest. It does not claim that the exact source payload, database contents,
or executable market price has been recovered for every historical day.

Two different forms of look-ahead must therefore be distinguished:

- **date look-ahead:** using an observation before its original publication
  or forecast-issue date;
- **version look-ahead:** assigning a conservative date to a value that was
  subsequently revised, backfilled, or enriched and was not present in that
  form in the database available at the time.

The implementation controls date look-ahead more strongly than version
look-ahead. A one-session lag, \(M+3\) monthly timing, and weekly-release
timing cannot remove future information embedded in a later revised snapshot.

### 4.2 CPC and GFS weather archives

Weather forecasts differ from conventional historical observations because a
forecast has both an issue time and one or more target dates. The usable
forecast must be selected by its original issue timestamp; choosing the most
recently archived file for a historical target date could accidentally use a
later forecast revision.

For CPC seasonal weather, the archive must correctly map:

- forecast issue date;
- target period;
- degree-day season;
- revision relative to the previous issue;
- the model's next eligible trading session.

The unresolved risk called “archive construction” means that the historical
archive may have been assembled from files whose naming, publication
calendar, or revision lineage was not originally designed as a clean
point-in-time panel. The one-session signal lag is useful only after this
issue-to-target mapping is correct.

For GFS wind and solar, each record retains its forecast reference time and
forecast lead. The model uses the 00Z initialization and the appropriate
future six-hour fields, then delays the resulting strategy score by one
trading session. This is a stronger point-in-time design because the GRIB
forecast itself is an ex-ante artifact rather than a later realized-weather
observation.

The remaining GFS risks are different:

- some historical initializations or GRIB fields are missing;
- incomplete initializations may create a non-random sample;
- variable definitions and forecast physics changed across GFS upgrades;
- a reprocessed archive may not be byte-for-byte identical to the file
  received operationally at 00Z;
- download completion time is not the same as forecast reference time.

These issues are primarily archive completeness and model-regime risks.
They are not equivalent to knowingly using realized future weather, but they
can still make the historical signal cleaner than a live feed.

### 4.3 Wind capacity: what “current snapshot reconstructed backward” means

The wind factor requires more than weather. It must estimate how much
generation capacity was exposed to the forecast wind at each historical date.
The project downloads a current USWTDB turbine/project snapshot containing
location, commissioning year, capacity, hub height, and related equipment
fields. It then reconstructs a historical fleet by filtering the current
records according to commissioning year.

For a forecast issued in year \(y\), the current policy permits only turbines
whose recorded commissioning year is no later than \(y-1\):

\[
EligibleWindFleet_y =
\{i: CommissioningYear_i \le y-1\}.
\]

This rule is conservative with respect to physical commissioning. It prevents
a wind farm recorded as entering service in 2024 from affecting a 2022 wind
signal. The additional one-year buffer also reduces the chance that an
incomplete commissioning year is treated as fully operational.

However, commissioning date is not the same as database-availability date.
Suppose the 2026 USWTDB snapshot contains a 300 MW Texas project with a
recorded 2019 commissioning year. The reconstruction includes that project in
the 2020 historical fleet. The project may genuinely have existed in 2020,
but the current snapshot does not establish that its capacity, coordinates,
hub height, or even its database record was available in the USWTDB version
downloadable in 2020. It could have been:

- entered into the database several years later;
- assigned a revised capacity or commissioning year;
- corrected to a different location or turbine specification;
- consolidated with or separated from another project;
- retained or omitted according to later database-cleaning rules.

This creates potential **backfill bias** and **revision bias**. It may also
create a form of survivorship bias if the current snapshot represents active
or well-documented projects more completely than retired or poorly documented
historical equipment.

The wind capacity history is therefore a reconstructed estimate of the
physical fleet, not a preserved sequence of annual USWTDB vintages. The
issue-year-minus-one filter substantially reduces direct future-capacity
leakage, but it cannot prove that every turbine attribute was known in that
historical year.

The strongest future control would be to preserve every downloaded USWTDB
release with:

- retrieval timestamp and source version;
- raw turbine rows;
- checksum;
- change log for commissioning year, capacity, status, and coordinates;
- the exact annual capacity map produced from that vintage.

Until such an archive exists, the correct claim is “capacity-lagged and
economically plausible,” not “fully vintage-pure.”

### 4.4 Solar capacity: a similar but monthly reconstruction

The solar factor also needs a capacity history because one unit of forecast
radiation has a larger gas-market effect after more PV has been installed.
The model forms monthly location weights from EIA utility-scale solar
generator records and applies a two-month lag. A generator appearing in the
source therefore does not immediately influence the same month's signal.

The two-month lag addresses reporting delay, but it does not by itself create
a historical vintage archive. If the generator panel was constructed from a
later EIA snapshot, a plant's earlier monthly existence is inferred from its
reported operating date. Later revisions to capacity, operating month,
retirement status, balancing area, or location can then propagate backward
through the reconstructed weights.

The solar history has an additional coverage problem. EIA utility-scale data
do not fully represent:

- behind-the-meter residential PV;
- small commercial systems;
- revisions to distributed-PV estimates;
- degradation, repowering, curtailment, and temporary outages.

Consequently, the reconstructed series is best understood as lagged
utility-scale nameplate exposure, not the complete point-in-time solar fleet
or realized PV generation.

The wind and solar histories share the same general concern--later equipment
records are used to infer an earlier physical fleet--but their controls are
not identical:

| Capacity history | Reconstruction unit | Timing control | Residual problem |
|---|---|---|---|
| Wind / USWTDB | Turbine or project by commissioning year | Only commission year \(\le y-1\) | Current turbine snapshot may contain later backfills or revisions |
| Solar / EIA | Generator capacity by operating month | Two-month capacity lag | Monthly history may be revised; distributed PV is incomplete |

Neither issue automatically invalidates the weather signal. It means that
the historical capacity weights have more point-in-time uncertainty than the
forecast issue timestamps.

### 4.5 EIA storage and monthly gas fundamentals

Storage for a week ending Friday becomes usable at the actual official EIA
Weekly Natural Gas Storage Report publication timestamp. The normal release is
Thursday at 10:30 a.m. Eastern; the current implementation includes the audited
Wednesday, Friday, and Monday holiday/special exceptions.

The remaining problem is revision lineage. A current EIA historical download
may contain corrected values that differ from the first value published on
the actual release date. Without a complete first-release archive, the backtest
can enforce the correct release day while still using a later revision.

Production, LNG, imports, and exports are treated even more conservatively.
Reference month \(M\) becomes usable only at the beginning of \(M+3\). This
avoids assigning the final monthly balance to the month in which it occurred.
It also makes the signals deliberately slow.

The \(M+3\) convention is a proxy rather than a reconstruction of every
historical EIA release calendar. Different tables may have different
publication days, and later EIA revisions can change the reported history.
The policy therefore trades some timeliness for a lower risk of direct date
look-ahead, but it does not remove version risk. It also explains why the
net-import signal can be economically stale even when it is statistically
causal.

### 4.6 Futures timing and executability

The C1/C2 futures panel is known at settlement, and the strategy return is
defined settlement to settlement. The one-session position lag prevents a
same-date signal from earning an already realized settlement return.

This is a causal research convention, not an execution guarantee. A settlement
is a benchmark price rather than a quote at which the complete desired
position was necessarily tradable. Exact live performance would depend on:

- the time every input file was actually received;
- processing and order-submission latency;
- the bid-ask spread and market depth;
- contract-roll execution;
- price jumps between signal availability and trade completion.

### 4.7 Overall interpretation

The data controls can be summarized in three levels:

| Control level | Meaning | Status in this model |
|---|---|---|
| Release-lag-aware | Signal cannot trade before a conservative availability date | Implemented |
| Revision-aware | First release and subsequent revisions are separately identified | Partial |
| Fully vintage-pure | Exact historical source payload available as it existed at every decision date | Not achieved for all modules |

The appropriate characterization is:

> The backtest is release-lag-aware, but it is not fully vintage-pure.

This statement does not mean that future prices or realized future weather
were deliberately inserted into the model. It means that conservative timing
rules cannot fully compensate for current database snapshots and revised
historical values. Performance should therefore be interpreted with an
explicit vintage-risk discount, and prospective raw-data archiving should be
part of any production process.

## 5. Development chronology

### 5.1 Stage 0: broad multi-signal scaffold

The earliest architecture grouped the large feature universe into weather,
fundamental, market, and macro blocks. This stage established common
directional conventions, a daily panel, a bounded score, and the initial
one-session position lag.

The broad scaffold was not accepted as the final model because:

- several price and macro features were unstable across regimes;
- direct observed weather overlapped with slow physical fundamentals;
- linear wind and cloud features were weak physical approximations;
- weekly and monthly standardization after daily forward-fill created
  frequency distortion;
- equal inclusion of all fundamental factors gave slow national consumption
  variables a direct daily trading role that was difficult to justify.

The broad panel remains useful as a feature archive, but the final strategy is
a narrower physical subset.

### 5.2 Stage 1: execution and roll cleanup

An early-roll convention was introduced to avoid trading too close to the
official expiry switch. The strategy moves to the next contract five complete
trading sessions before the official last-trading-day convention. This change
was retained because it reduces sensitivity to expiry mechanics and makes the
research return series closer to a practicable rolling exposure.

The return construction explicitly avoids treating the C1-to-C2 price gap as
an investment return. During the early-roll window, C2 is compared with its
own previous settlement; after the official switch, it becomes the new C1.

### 5.3 Stage 2: pruning the direct weather block

Three direct CPC/observed-weather slots originally existed:

1. seasonal CPC forecast revision;
2. CPC forecast level;
3. observed weather.

Only the revision signal was retained:

```math
W_t =
\frac{\tanh(CPCRevision_t/2)+0_{level}+0_{observed}}{3}.
```

The denominator remains three. Without that fixed denominator, removing two
features would mechanically triple the remaining CPC revision exposure and
confound feature selection with a weight change.

**Why CPC revision was retained.** Futures prices should react to changes in
expected future demand. Comparing successive forecasts for the same target
dates is closer to new information than using the absolute forecast level.

**Why CPC level was removed.** Absolute cold or heat is often already priced,
its significance changes by season, and its direct directional result was
unstable.

**Why observed weather was removed.** Realized weather helps explain current
demand and storage, but it is less suitable as a fast forward-looking
directional signal. It can behave like lagged price or demand trend following
and overlaps with inventory and consumption information.

### 5.4 Stage 3: replacing linear wind with physical wind generation

The first GFS wind candidates used broad wind-speed summaries. This is
physically incomplete because electricity generation is nonlinear in wind
speed. The improved model:

1. reads GFS 80 m U/V wind components;
2. converts them to wind speed;
3. adjusts to estimated turbine hub height;
4. applies a nonlinear power curve;
5. weights locations by lagged installed wind capacity;
6. aggregates forecast days 1-5 with more weight on nearer days;
7. converts expected generation shortfall into a bullish gas signal.

The hub-height adjustment is

```math
v_h =
v_{80}\left(\frac{h}{80}\right)^{0.14}.
```

The normalized turbine power proxy is

```math
Power(v)=
\begin{cases}
0, & v<3,\\
\dfrac{v^3-3^3}{12^3-3^3}, & 3\le v<12,\\
1, & 12\le v<20,\\
\text{cosine derating}, & 20\le v<25,\\
0, & v\ge25.
\end{cases}
```

This matters because very low wind is bullish gas, ordinary strong wind is
bearish gas, and extreme cut-out-region wind may again become bullish.

Candidate wind specifications produced the following diagnostics:

| Wind version | Pearson IC | Spearman IC | HAC t | Seasonal net Sharpe in wind-stage model |
|---|---:|---:|---:|---:|
| Equal-location nonlinear | 0.0547 | 0.0606 | 2.93 | 0.875 |
| **Capacity-weighted capacity factor** | 0.0636 | **0.0784** | 3.26 | **0.895** |
| Capacity-scaled MW shortfall | **0.0661** | 0.0756 | **3.28** | 0.881 |
| Level plus six-hour revision | 0.0493 | 0.0653 | 2.47 | 0.838 |

The complete IC record for the selected capacity-factor specification is:

| Observations | Pearson IC | Spearman IC | Regression beta | HAC t |
|---:|---:|---:|---:|---:|
| 2,620 | 0.06360 | 0.07843 | 0.002184 | 3.264 |

These statistics use the causally standardized capacity-weighted wind
shortfall at forecast date \(t\) and the roll-adjusted C1/C2 natural-gas
futures return on the next trading day, \(r_{t+1}\). Pearson IC is their
linear correlation; Spearman IC is the correlation of their ranks. The beta
comes from the regression

\[
r_{t+1}=\alpha+\beta\,WindSignal_t+\varepsilon_{t+1}.
\]

HAC t is \(\hat\beta\) divided by a Newey-West heteroskedasticity- and
autocorrelation-consistent standard error with five lags, used because the
wind factor contains overlapping one-to-five-day forecasts. The 2,620
observations are the non-missing signal/next-return pairs. These IC statistics
do not include seasonal portfolio weights, position clipping, transaction
costs, or the performance of the other strategy modules. The reported values
are reproduced from
`results/experiments/wind/complete_wind_factor_ic.csv`,
generated by `naturalgas/evaluate_ncar_gdex_complete_wind_factor.py`.

The capacity-factor version was selected because its portfolio result was
stronger and its first-look behavior was more stable. Pure capacity scaling
slightly improved raw IC but did not improve portfolio robustness. The
level-plus-revision candidate increased turnover and did not survive the
holdout comparison.

### 5.5 Stage 4: giving wind an independent seasonal allocation

Wind was first embedded as a small part of the weather block. The next
experiment separated wind and varied only the shoulder-season allocation,
holding peak weights fixed:

| Candidate family | Peak weather | Peak wind | Peak fundamentals | Shoulder weather | Shoulder wind | Shoulder fundamentals |
|---|---:|---:|---:|---:|---:|---:|
| No wind | 60.0% | 0% | 40.0% | 30.0% | 0% | 70.0% |
| Embedded wind | 45.0% | 15.0% | 40.0% | 22.5% | 7.5% | 70.0% |
| Independent grid | 45.0% | 15.0% | 40.0% | 22.5% | 10%-40% | Residual |

At 2.5 basis points, the wind-stage full-sample Sharpe rose from 0.738 with no
wind to 0.895 with embedded 7.5% shoulder wind and to 1.048 at a 22.5%
shoulder allocation.

The declared development grid itself reached its maximum at 35% shoulder
wind, with a development Sharpe of approximately 1.065. The final downstream
research code later fixed shoulder wind at 22.5%, with peak wind at 15%.
Twenty-two-and-a-half percent is economically interpretable as a conservative
allocation below the 35% development optimum: it preserves material wind
exposure while retaining more fundamental weight and reducing turnover and
drawdown sensitivity.

However, the archived research record does **not** contain a formal
one-standard-error rule, utility function, or predeclared constraint that
uniquely maps the 35% development optimum to 22.5%. Therefore this development record does
not describe 22.5% as the exact statistical optimum. It is a conservative
research choice and a model-governance traceability gap that should be
resolved before production use.

### 5.6 Stage 5: solar factor engineering and selection

Cloud cover was an intuitive initial solar feature, but total cloud fraction
is only an indirect proxy for the energy reaching PV panels. The solar study
tested:

- cloud cover;
- surface downward shortwave radiation;
- fixed PV availability;
- daylight-scaled PV availability.

Six-hour downward shortwave radiation was converted to energy:

```math
E_{6h}=DSWRF\times\frac{6}{1000}
\quad \text{kWh/m}^2.
```

Surface radiation was normalized by extraterrestrial horizontal radiation to
remove deterministic solar geometry:

```math
K_t =
\frac{SW^{surface}_t}{SW^{extra}_t}.
```

A simple temperature adjustment was applied:

```math
T^{cell}=T_{2m}+0.025DSWRF,
```

```math
\eta_T =
\operatorname{clip}\left(1-0.004(T^{cell}-25),0.75,1.10\right),
```

```math
PVAvailability_t = K_t\eta_T.
```

The full-sample information coefficients were:

| Solar candidate | Pearson IC | Spearman IC |
|---|---:|---:|
| **PV daylight** | **0.0431** | **0.0435** |
| PV fixed | 0.0415 | 0.0430 |
| Radiation daylight | 0.0381 | 0.0385 |
| Cloud daylight | 0.0212 | 0.0212 |

PV daylight was chosen because it had the strongest full-sample IC, positive
development/validation/first-look IC, and a more complete physical
interpretation than cloud cover alone.

The solar weight grid gave:

| Nominal solar weight | Full net Sharpe | Full CAGR | First-look Sharpe |
|---:|---:|---:|---:|
| 0% baseline | 1.275 | 9.24% | 1.062 |
| 5% | 1.358 | 9.85% | 1.168 |
| **10%** | **1.406** | **10.44%** | **1.241** |
| 15% development optimum | 1.419 | 11.00% | 1.280 |

The development optimum occurred at the 15% upper grid boundary. A boundary
optimum suggests that the grid may be too narrow or that the development
sample is pushing the model toward excessive allocation. The formal strategy
therefore uses 10%, not 15%, as a conservative intermediate weight.

The effective solar weight is further scaled by daylight:

```math
w^{solar}_{t}
=0.10\times
\operatorname{clip}
\left(
\frac{SW^{extra,5d}_t}{10},
0.25,1
\right).
```

The average effective weight is approximately 7.93%. Solar is funded from the
fundamental block rather than added as leverage.

### 5.7 Stage 6: correcting low-frequency standardization

The original implementation forward-filled weekly and monthly data to daily
frequency before calculating rolling z-scores. That creates pseudo-replication:
one monthly release can appear as roughly twenty repeated observations and
one weekly release as five repeated observations.

The corrected method calculates the z-score on unique releases first and only
then aligns the completed score to trading dates. Holding weights, wind,
solar, execution, and costs fixed:

| Standardization | Full Sharpe | CAGR | Win rate |
|---|---:|---:|---:|
| Legacy daily z-score after forward-fill | 1.552 | 11.84% | 52.93% |
| **Native-frequency causal z-score** | **1.668** | **12.21%** | **53.77%** |

The Sharpe improvement of approximately 0.116 was accepted because it came
from correcting a statistical error rather than searching for a new return
parameter.

### 5.8 Stage 7: removing slow national consumption factors

The original fundamental block had eleven equal slots. Two were national
consumption features:

- consumption year-over-year growth;
- consumption month-over-month change.

Standalone diagnostics were weak:

| Removed feature | Development Sharpe | Validation Sharpe | 2024+ Sharpe | Full Sharpe |
|---|---:|---:|---:|---:|
| Consumption YoY | 0.088 | -0.966 | -0.512 | -0.449 |
| Consumption MoM | -0.360 | -0.350 | -0.675 | -0.450 |

The factors were removed for three related reasons:

1. EIA national monthly consumption is delayed and too slow for a direct
   daily position;
2. consumption strongly overlaps weather, making it partly a delayed
   realization of information already represented in forecasts;
3. national aggregation obscures the regional power-burn and Gulf Coast
   channels most relevant to Henry Hub.

The removal was a model-pruning decision, not a claim that gas consumption is
economically unimportant. Timely regional power burn would be a desirable
future input.

Simply equal-weighting the remaining nine features reduced full Sharpe from
1.668 to 1.557. Therefore the two removed 1/11 slots were not renormalized
equally. Instead, all 45 unordered assignments of the two fixed slots among
the nine remaining factors were tested using development data only.

The selection rule was:

1. calculate 2.5-basis-point net Sharpe for each candidate on development;
2. retain candidates within 0.01 Sharpe of the development maximum;
3. select the lowest-turnover candidate from that shortlist;
4. do not use validation or first-look returns for the selection.

The selected recipients were:

- low storage level;
- LNG export month-over-month growth.

This candidate had a development Sharpe of 1.700, validation Sharpe of 1.812,
and first-look Sharpe of 1.399. Its full Sharpe of 1.633 was lower than the
all-eleven 1.668 model, but it improved first-look behavior relative to the
all-eleven model's 1.326. The decision therefore reflects economic timing,
feature parsimony, and later-period stability rather than full-sample Sharpe
maximization.

### 5.9 Stage 8: replacing Lower 48 with South Central storage

Henry Hub is located in Louisiana and is connected to Gulf Coast production,
LNG terminals, salt storage, and major interstate pipelines. South Central
storage is therefore more local to the marginal Henry Hub balance than Lower
48 aggregate storage.

The experiment held all storage methods and weights fixed and replaced the
source series:

| Storage specification | Full Sharpe | CAGR | Maximum drawdown |
|---|---:|---:|---:|
| Lower 48 baseline | 1.633 | 14.08% | -5.62% |
| South Central salt | 1.657 | 14.26% | -6.13% |
| South Central nonsalt | 1.659 | 14.66% | -6.11% |
| **South Central Total** | **1.673** | **14.61%** | -6.07% |

South Central Total improved Sharpe in all three broad periods:

| Period | Lower 48 Sharpe | South Central Sharpe | Change |
|---|---:|---:|---:|
| Development | 1.700 | 1.721 | +0.021 |
| Validation | 1.812 | 1.878 | +0.065 |
| 2024+ first-look | 1.399 | 1.407 | +0.008 |
| Full | 1.633 | **1.673** | **+0.040** |

The improvement is economically plausible but statistically small. South
Central was promoted as a structural regional substitution, not as proof that
the region has decisively superior alpha.

### 5.10 Stage 9: storage-weight robustness

After South Central promotion, a separate research grid held total storage
weight fixed at 4/11 and redistributed it among level, one-week change, and
four-week change. The candidate that removed the weekly-change slot and moved
it to the four-week signal produced:

- full Sharpe 1.67317 versus 1.67255;
- CAGR improvement of approximately 0.18 percentage point;
- maximum drawdown improvement from -6.07% to -5.30%.

The Sharpe improvement was only 0.0006. Development, validation, and holdout
rankings did not support a decisive change, and the signals are correlated
revised EIA histories. The formal 2:1:1 storage slot allocation was therefore
left unchanged.

---

## 6. Final Feature Set and Economic Rationale

The final model is intentionally small enough to audit but broad enough to
represent several distinct physical channels. It does not assume that every
retained feature is a profitable standalone trading rule. A feature may be
retained because it diversifies another feature, stabilizes the seasonal
portfolio, or represents an economically necessary state variable whose
marginal contribution is more reliable than its standalone Sharpe.

### 6.1 Feature-decision matrix

| Feature family | Final status | Directional interpretation | Why it was retained or removed |
|---|---|---|---|
| CPC forecast revision | Retained | Warmer/cooler forecast revisions mapped through season-specific gas demand | The only CPC component with repeatable incremental value; revisions are closer to news than levels |
| CPC forecast level | Zero-weight slot | Seasonal temperature-demand state | Redundant with climatology and less informative than revisions |
| Observed weather anomaly | Zero-weight slot | Realized HDD/CDD/GDD demand shock | Too late and too correlated with information already incorporated into price |
| Physical wind shortfall | Retained | Lower expected wind raises thermal gas burn | Independent physical construction, positive IC, useful diversification |
| Solar/PV shortfall | Retained | Lower solar raises daytime thermal generation need | Positive daylight-conditioned IC and a different intraday/seasonal channel from wind |
| South Central storage level | Retained | Low inventories are bullish | Directly relevant to Henry Hub geography and persistent scarcity state |
| South Central one-week change | Retained | Weak injection or strong withdrawal is bullish | Captures the latest inventory flow surprise |
| South Central four-week change | Retained | Sustained weak injections/strong withdrawals are bullish | Smoother flow state, less dependent on a single report |
| Dry-gas production YoY and MoM | Retained | Weak supply growth is bullish | Represents domestic supply balance at two time scales |
| LNG feedgas/export YoY and MoM | Retained | Strong export demand is bullish | Large structural demand channel with both trend and acceleration terms |
| Net-import supply level and change | Retained, under review | Lower imports or worsening import balance is bullish | Represents cross-border supply, but the monthly vintage is stale and caused a material 2025 loss |
| Consumption YoY and MoM | Removed | Strong consumption would normally be bullish | Negative or unstable standalone performance across development, validation, and recent windows |
| EBB final receipts | Rejected from production | Higher pipeline receipts can indicate supply availability | Attractive full-sample overlay, but failed development stability and incremental significance tests |
| EBB cycle revisions | Rejected from production | Within-day nomination changes may reveal flow surprises | Too few events and statistically weak incremental return |
| Spot/futures basis and term structure | Rejected from production | Market-implied scarcity and carry | Every tested overlay reduced the formal model's Sharpe |
| Calendar/event flattening | Rejected from production | Holiday/weekend liquidity regime | Improvement did not survive the South Central model revision |
| Macro and geopolitical variables | Deferred | Broad demand, risk, and supply disruption channels | No stable incremental benefit after the physical balance factors were included |

### 6.2 Weather revision

The CPC weather signal uses the model's seasonally appropriate degree-day
concept:

- heating degree days from October through March;
- cooling degree days from May through September;
- growing degree days in April as a shoulder-season bridge.

The raw predictor is the revision for the same forecast target dates, not the
absolute forecast level. The revision is converted to the economically correct
gas-demand sign, standardized causally, and compressed with `tanh`.

The legacy weather block still has three bookkeeping slots--revision, level,
and observed anomaly--but only the revision slot is active. Consequently, a
45% top-level legacy weather block implies approximately 15% direct CPC
revision exposure before interactions with the remaining sleeves. This detail
is essential when interpreting the weight table: the block label is not the
same as effective active-factor exposure.

### 6.3 Physical wind shortfall

The selected wind feature is built from the 00Z GFS forecast:

1. obtain 80-meter U and V wind components at 28 geographically distributed
   locations;
2. aggregate four daily forecast intervals for leads one through five;
3. extrapolate to hub height with a 0.14 power-law exponent where required;
4. convert speed to generation potential using a turbine curve with
   3 m/s cut-in, 12 m/s rated speed, 20 m/s derating threshold, and
   25 m/s cut-out;
5. weight location-level capacity factors by lagged USWTDB installed capacity,
   allowing only projects commissioned by issue year minus one;
6. combine the five forecast horizons with declining 5:4:3:2:1 weights;
7. define low expected wind generation as bullish gas demand;
8. apply a causal 60-session z-score with a 30-session minimum and
   `tanh` compression.

This construction was chosen over raw wind speed because gas demand responds
to displaced power generation, not linearly to meters per second. The lagged
capacity map prevents the model from using later turbine installations to
reconstruct earlier forecasts.

### 6.4 Solar/PV shortfall

The solar sleeve combines forecast downward short-wave radiation, ambient
temperature, deterministic clear-sky geometry, and lagged EIA utility-scale
solar capacity. The result is a PV-output proxy rather than a generic cloud
index. Low forecast PV output is bullish gas because dispatchable thermal
generation must fill more of the daytime residual load.

Solar is active only in proportion to an ex-ante daylight scale. Although its
nominal sleeve weight is 10%, the sample-average effective weight is
approximately 7.93%. This avoids granting a winter night the same solar risk
budget as a long summer day.

### 6.5 South Central storage

The storage family uses EIA South Central Total inventories. Each history is
vintage-controlled and is not made tradable until the weekly release plus six
days. This conservative availability rule is designed to avoid using a report
before the strategy could reasonably have processed and traded it.

Three views of the same inventory state are retained:

- level relative to the same ISO week over the previous five years, with at
  least three prior observations;
- one-week change;
- four-week change.

Storage features use 104 observations with a 52-observation minimum for causal
standardization. The level receives twice the slot weight of either flow term
because it describes the persistent scarcity state, while the changes describe
shorter-lived balance information.

### 6.6 Monthly balance variables

The final monthly family contains:

- production YoY;
- LNG exports/feedgas YoY;
- net-import supply level;
- production MoM;
- LNG exports/feedgas MoM;
- net-import change.

Consumption YoY and MoM remain in the schema at zero weight for auditability
but do not affect the position. Monthly observation \(M\) is made available no
earlier than \(M+3\), a deliberately conservative convention that prevents
publication look-ahead.

Unlike the storage and weather series, these monthly variables retain their
native issue-to-issue standardization rather than being forced into a daily
rolling transform. This preserves the information frequency and prevents
artificially repeated daily observations from dominating the estimated
distribution.

### 6.7 Why both level and change features are present

The model distinguishes state from news:

- levels answer, “Is the system structurally tight or loose?”;
- one-period changes answer, “Is the balance getting tighter or looser now?”;
- multi-period changes answer, “Is the recent movement persistent?”

Using only changes would miss persistent scarcity. Using only levels would
react slowly when the balance turns. The retained mixture therefore encodes
different economic questions rather than mechanically duplicating the same
series.

---


## 7. Prior Attempts, Rejected Features, and Deferred Overlays

A credible development record includes failed ideas. The following experiments
were not hidden or retroactively reclassified; they explain why the final model
is narrower than the initial research universe.

### 7.1 Weather level and observed anomalies

Absolute CPC weather levels and observed temperature anomalies were initially
included alongside forecast revisions. They were ultimately neutralized.
Levels were strongly entangled with predictable seasonality, while observed
weather arrived after much of the price response. Revision information had the
cleaner interpretation: it measured what the forecast market learned, not just
whether the season was hot or cold.

### 7.2 Alternative wind constructions

| Wind specification | Pearson IC | Spearman IC | HAC t-stat | Seasonal net Sharpe |
|---|---:|---:|---:|---:|
| Equal-location nonlinear curve | 0.0547 | 0.0606 | 2.93 | 0.875 |
| **Capacity-weighted capacity factor** | **0.0636** | **0.0784** | **3.26** | **0.895** |
| Installed-MW weighting | 0.0661 | 0.0756 | 3.28 | 0.881 |
| Wind level plus revision | 0.0493 | 0.0653 | 2.47 | 0.838 |

The installed-MW version had a marginally higher Pearson IC, but the selected
capacity-factor construction had the best seasonal portfolio Sharpe and the
highest rank IC. Adding revisions weakened rather than improved the result.

### 7.3 Wind allocation grid and the 22.5% governance gap

At the allocation stage, the relevant full-sample Sharpe sequence was:

| Wind allocation experiment | Full net Sharpe |
|---|---:|
| No wind | 0.738 |
| Embedded wind, 7.5% shoulder allocation | 0.895 |
| Embedded wind, 22.5% shoulder allocation | 1.048 |

The development grid itself reached its highest reported development Sharpe,
1.065, at a 35% shoulder wind weight. The later production implementation uses
22.5%. The archived artifacts do not contain a formal one-standard-error rule,
utility function, or other reproducible calculation that uniquely maps 35% to
22.5%.

The most defensible interpretation is that 22.5% was a conservative choice
designed to preserve fundamentals and reduce concentration in a single
forecast model. It should not be described as the exact mathematical optimum.
This is a documentation and governance gap: the production value is reasonable
and lower-risk, but the final discretionary step should have been recorded
contemporaneously.

### 7.4 Alternative solar features and weights

| Solar signal | Pearson IC | Spearman IC |
|---|---:|---:|
| **PV proxy, daylight-conditioned** | **0.0431** | **0.0435** |
| PV proxy, fixed weighting | 0.0415 | 0.0430 |
| Radiation-only, daylight-conditioned | 0.0381 | 0.0385 |
| Cloud proxy | 0.0212 | 0.0212 |

The weight grid produced:

| Nominal solar weight | Full net Sharpe |
|---:|---:|
| 0% | 1.275 |
| 5% | 1.358 |
| **10%** | **1.406** |
| 15% | 1.419 |

Fifteen percent was the development-grid maximum and sat at the edge of the
tested range. Ten percent was frozen because it captured most of the gain
without selecting the boundary. This is a deliberately conservative
regularization decision, not a claim that 10% is the population optimum.

### 7.5 Daily standardization of monthly data

An earlier implementation treated a monthly observation repeated across many
days as if it were daily-frequency data. Returning each family to its native
information frequency improved net Sharpe from 1.552 to 1.668, CAGR from
11.84% to 12.21%, and win rate from 52.93% to 53.77%. This was both a
statistical improvement and a conceptual correction: repeated stale values
should not create dozens of pseudo-observations.

### 7.6 Consumption features

Consumption failed across independent windows:

| Feature | Development Sharpe | Validation Sharpe | Recent Sharpe | Full Sharpe |
|---|---:|---:|---:|---:|
| Consumption YoY | 0.088 | -0.966 | -0.512 | -0.449 |
| Consumption MoM | -0.360 | -0.350 | -0.675 | -0.450 |

Removing these features was therefore not based only on the full sample.
Consumption was likely too delayed and too overlapping with weather-driven
demand to add clean incremental information.

The complete 11-feature fundamental model had full Sharpe 1.668, development
Sharpe 1.886, validation Sharpe 1.800, and 2024+ holdout Sharpe 1.326. A naive
equal reallocation across the remaining nine features reduced full Sharpe to
1.557. The selected two-slot reallocation recovered full Sharpe to 1.633 and
improved the 2024+ holdout to 1.399, while reducing the development result to a
less extreme 1.700.

### 7.7 Alternative storage regions

South Central salt and nonsalt series both improved full-sample Sharpe versus
Lower 48, but neither clearly dominated South Central Total. The total series
was preferred because it captured the whole regional inventory balance and
avoided selecting a subcomponent on a small backtest difference.

The later storage-weight grid marginally favored dropping the weekly-change
term, but the Sharpe improvement was only 0.0006 and did not survive all
period rankings. It remained a research result rather than a production
change.

### 7.8 Weekend and holiday flattening

A weekend/holiday 18Z flattening rule had improved an older baseline. Once the
South Central model was in place, however, it reduced Sharpe from 1.673 to
1.648. The overlay was rejected because calendar intuition did not compensate
for the lost exposure in the current portfolio.

### 7.9 EBB pipeline receipts

A conservative final-receipt overlay increased full-sample Sharpe from 1.673
to 1.732, and a netted variant reached 1.754. The improvement was not stable:

- development Sharpe fell from 1.721 to 1.464;
- calendar-2020 contribution was approximately -6.15%;
- the full-sample incremental-return test had \(p=0.134\).

The overlay was rejected because a higher full-sample ratio did not outweigh
weaker development behavior and low statistical confidence.

An EBB nomination-cycle revision signal produced approximately 30 events in
2026. Standalone event Sharpe was 2.529 and the hybrid event Sharpe was 2.469,
but the average incremental return was only 1.43 basis points per event with
\(p=0.437\). It moved the full formal Sharpe only from 1.673 to 1.678. This was
insufficient evidence for production.

### 7.10 Market-price overlays

Basis and term-structure features did not improve the physical model:

| Overlay | Full net Sharpe |
|---|---:|
| Formal physical baseline | **1.6726** |
| Add 2.5% basis sleeve | 1.651 |
| Add 5.0% basis sleeve | 1.621 |
| Add 2.5% term sleeve | 1.668 |
| Add 5.0% term sleeve | 1.645 |
| Replace with both market features | 1.611 |

These variables may contain useful information in isolation, but in this model
they appear to repackage price information already expressed through the
instrument. Their inclusion diluted the independent physical signal.

### 7.11 Perfect-information and macro experiments

A no-lag production/consumption experiment combined with robust compression
reached approximately 1.781 Sharpe and -4.40% maximum drawdown. It was not
tradable because it depended on information before its real publication date.
The result is useful only as an upper-bound diagnostic for whether faster
fundamental data might be valuable.

Macro, geopolitical, and broad market variables were also explored. No
archived specification showed a stable incremental improvement after the
physical balance model was included. These variables were deferred rather than
mined until one appeared favorable.

---


# Part II. Quantitative Model, Validation, and Risk Analysis

This part formalizes execution, statistical methods, portfolio weights,
performance, the 2025 loss diagnosis, and the out-of-sample robustness
decision.

## 8. Target, instrument, and execution convention

### 8.1 Futures return

The model trades a research return series constructed from NYMEX Henry Hub C1
and C2 settlements. To reduce expiry distortions, it switches to the next
contract five complete trading sessions before the official last-trading-day
convention used by the source panel.

Let the model's roll-adjusted simple return be `r_fut,t`. The position
is applied settlement-to-settlement. Settlement is a research convention, not
evidence that the full position can be executed at that price.

### 8.2 Signal lag

The complete score is delayed by one trading session:

```math
P_t = \operatorname{clip}(\widetilde S_{t-1},-1,1).
```

This conservative lag avoids using information labeled with date `t` to
earn the already-realized return on the same date. It also permits mixed
sources with different intraday release times to enter one common daily
framework.

### 8.3 Transaction costs

The formal net daily return is

```math
r^{net}_t =
P_t r^{fut}_t
-0.00025|P_t-P_{t-1}|.
```

The 2.5-basis-point cost is charged per unit of position change. Sensitivities
at 0 and 5 basis points were used in several feature experiments. The model
does not yet include dynamic bid-ask spreads, market impact, brokerage fees,
margin financing, or a separate charge for mechanically rolling an unchanged
directional exposure.

## 9. Research design and statistical methods

### 9.1 Development, validation, and first-look periods

The formal research chronology uses:

| Split | Dates | Intended use |
|---|---|---|
| Development | 2017-07-03 to 2020-12-31 | Feature and weight research |
| Validation | 2021-01-01 to 2023-12-31 | Model validation, not primary selection |
| First-look holdout | 2024-01-01 to 2026-07-13 | Later-period stability check |

Some upstream wind and solar experiments start in July 2016 because their
complete common sample is slightly longer. The formal South Central strategy
begins in July 2017 because all required components are available from that
point.

The complete final model is not a pristine one-shot holdout experiment.
Several later structural decisions, especially South Central storage
promotion, were made after earlier validation diagnostics had been viewed.
For this reason, post-2024 results are described as first-look evidence rather
than definitive untouched out-of-sample performance.

### 9.2 Causal standardization

For a release-frequency series `x_t`, the causal z-score is

```math
z_t =
\frac{x_t-\mu_{t-1}}{\sigma_{t-1}},
```

where the mean and standard deviation use only prior observations. The current
observation is excluded with an explicit one-period shift.

The main reference windows are:

| Data type | Window | Minimum observations |
|---|---:|---:|
| Weekly storage releases | 104 weeks | 52 |
| Monthly fundamentals | 60 months | 36 |
| GFS wind and solar issues | 60 issues | 30 |

The critical methodological rule is to standardize at the native publication
frequency before aligning the result to daily trading dates. Forward-filling
one monthly observation across twenty trading days and then calculating a
daily rolling z-score would falsely treat the same release as twenty
independent observations.

### 9.3 Seasonal normalization

Storage levels and changes have strong deterministic seasonality. For storage
week `w`, the normal is the average of the prior five observations from
the same ISO week, with a minimum of three:

```math
Normal_{y,w} =
\frac{1}{K}\sum_{k=1}^{K}Storage_{y-k,w},
\qquad 3\le K\le5.
```

The current year is excluded. Level, one-week change, and four-week change are
each compared with their own same-week history.

### 9.4 Robust compression

Fast weather and weekly fundamental z-scores are generally transformed as

```math
g(z)=\tanh(z/2).
```

This transformation is approximately linear near zero but limits the
influence of extreme standardized observations. It reduces the dependence of
the final position on a single outlier and keeps components on comparable
bounded scales.

The existing monthly issue-z signals remain in their original standardized
units inside the formal fundamental block. This mixed treatment is preserved
for reproducibility and is discussed as a limitation.

### 9.5 Information coefficient

Candidate weather factors were evaluated with both Pearson and Spearman
information coefficients:

```math
IC^{Pearson} = Corr(S_t,r_{t+1}),
```

```math
IC^{Spearman} =
Corr(rank(S_t),rank(r_{t+1})).
```

Pearson IC measures linear directional association; Spearman IC is less
sensitive to outliers and tests monotonic ranking. Both were reported because
natural-gas returns are heavy-tailed and factor-return relationships need not
be linear.

### 9.6 HAC inference

GFS features aggregate overlapping forecast days and are highly
autocorrelated. Ordinary independent-observation t-statistics would overstate
precision. Wind factor regressions therefore use heteroskedasticity- and
autocorrelation-consistent statistics. The 2025 timing audit uses a
Newey-West-style long-run variance with five lags, matching the approximate
overlap of the one-to-five-day forecast window.

HAC statistics are diagnostics, not a complete defense against multiple
testing. Feature families, weights, transformations, and subsets were
examined repeatedly, so reported p-values should be interpreted cautiously.

### 9.7 Portfolio performance statistics

The primary reported statistics are:

- total compounded return;
- CAGR;
- annualized standard deviation of daily log net returns;
- zero-risk-free-rate Sharpe based on daily log net returns;
- maximum peak-to-trough drawdown;
- daily win rate;
- mean absolute position;
- total turnover.

Standalone factor Sharpe is used only as a diagnostic. It is not equal to a
factor's marginal contribution inside the portfolio because the modules are
correlated and interact through seasonal weights and the freeze-off control.

### 9.8 Feature ablation and neutral-slot controls

Feature tests were conducted in two complementary ways:

1. **Ablation or replacement:** hold all other rules fixed and replace one
   feature family;
2. **Neutral-slot control:** remove the candidate signal while preserving the
   amount of capital taken from the funding module.

The neutral-slot solar test was especially important. It separated the value
of PV information from the mechanical benefit or harm of merely reducing the
fundamental weight.

### 9.9 Walk-forward and leave-one-year-out validation

After the 2025 loss review, candidate decay, risk-cap, and timing rules were
tested with:

- **Expanding yearly walk-forward:** select using all years before test year,
  then evaluate the next year;
- **Leave-one-year-out (LOYO):** omit one complete year, select using the other
  complete years, and evaluate the omitted year.

LOYO is a robustness diagnostic rather than a deployable historical trading
simulation because its training set can contain years after the omitted year.
Walk-forward is the more realistic selection test.

## 10. Final Model Specification and Weight Selection

### 10.1 Fundamental subportfolio

Let \(f_{j,t}\) be the signed, causally standardized, and compressed score for
fundamental feature \(j\). The final fundamental score is

\[
F_t = \sum_j \omega_j f_{j,t},
\qquad \sum_j \omega_j = 1.
\]

The production weights are:

| Fundamental feature | Internal weight | Slot interpretation |
|---|---:|---:|
| Low South Central storage level | 18.18% | 2 of 11 slots |
| South Central one-week change | 9.09% | 1 slot |
| South Central four-week change | 9.09% | 1 slot |
| Low production YoY | 9.09% | 1 slot |
| LNG export YoY | 9.09% | 1 slot |
| Consumption YoY | 0.00% | Removed |
| Net-import supply level | 9.09% | 1 slot |
| Production MoM | 9.09% | 1 slot |
| LNG export MoM | 18.18% | 2 slots |
| Consumption MoM | 0.00% | Removed |
| Net-import change | 9.09% | 1 slot |

The weighting procedure began with equal conceptual slots, removed the two
consumption slots after split-sample failure, and tested every unordered
assignment of the freed slots among the remaining factors. There were 45 such
assignments. The chosen allocation added one slot to low storage and one to
LNG MoM. It was not the absolute development-period maximum; it belonged to
the shortlist within 0.01 Sharpe of that maximum and had the lowest turnover.
That rule favored a stable, economical representation over the noisiest point
estimate.

### 10.2 Seasonal top-level allocation

Before solar funding, the top-level weights are:

| Season | Months | Legacy CPC weather | Wind | Fundamentals |
|---|---|---:|---:|---:|
| Peak | Nov-Feb and Jun-Aug | 45.0% | 15.0% | 40.0% |
| Shoulder | Mar-May and Sep-Oct | 22.5% | 22.5% | 55.0% |

Peak periods receive more weather weight because gas demand is most
temperature-sensitive during heating and cooling extremes. Shoulder periods
receive more fundamental weight because storage, production, LNG, and
cross-border balance tend to dominate when temperature loads are less extreme.
Wind receives a larger shoulder allocation because renewable displacement can
be a larger fraction of marginal power-sector gas demand in moderate-load
conditions.

Let \(W_t\) be the legacy CPC weather block and \(G_t\) the wind signal. The
pre-solar baseline is

\[
B_t = a_{s(t)}W_t + b_{s(t)}G_t + c_{s(t)}F_t,
\]

where the coefficients depend only on the predeclared season \(s(t)\).

### 10.3 Solar funding rule

Let \(S_t\) be the solar signal and let \(e_t\) be its effective weight:

\[
e_t = 0.10 \times \text{daylight-scale}_t.
\]

Solar is funded from the fundamental sleeve:

\[
A_t = B_t + e_t(S_t-F_t).
\]

This form makes the risk transfer explicit. Adding solar does not increase
gross top-level exposure; it replaces part of the fundamental score on days
when solar information is physically relevant.

### 10.4 From score to traded position

The active score is passed through the freeze constraint described below,
lagged by one trading day, and bounded:

\[
P_t = \operatorname{clip}(\widetilde A_{t-1},-1,1).
\]

The gross daily strategy return is

\[
r^{gross}_t = P_t r^{NG}_t,
\]

where \(r^{NG}_t\) is the return of the causal continuous natural-gas futures
series. Net return applies 2.5 basis points to absolute position change:

\[
r^{net}_t =
r^{gross}_t
-0.00025\,|P_t-P_{t-1}|.
\]

The one-day lag is part of the production definition. It prevents a forecast
or report timestamp from being treated as if it were known before the modeled
decision point.

### 10.5 Freeze short-control rule

From November through March, if the local freeze-level score is at least 1.0
and the freeze revision is non-negative, the strategy is not allowed to remain
short. The control is asymmetric because sudden winter freeze risk can create
nonlinear upside in gas prices and because a short physical-balance signal can
be invalidated much faster than a long one during an extreme cold event.

This is a guardrail rather than a new alpha sleeve. It changed 2025 performance
by only approximately +0.10%, but it reduces an identifiable tail-risk mode.

### 10.6 Missing-data and neutral-slot policy

The production implementation follows explicit rules:

- absent active inputs are not silently backfilled with future releases;
- a deliberately removed feature contributes exactly zero;
- neutral legacy slots remain visible in output for schema compatibility;
- unavailable solar exposure is reduced through the daylight/availability
  scale rather than replaced with an unrelated feature;
- all rolling statistics are calculated from information available by the
  decision date.

These choices matter because an apparently harmless forward fill can convert a
slow feature into an unintended, persistent directional bet.

---


## 11. Formal Backtest Results

### 11.1 Full-sample performance

The frozen formal run covers 2,269 trading days from 3 July 2017 through
13 July 2026.

| Metric | Before modeled cost | After modeled cost |
|---|---:|---:|
| Cumulative return | 255.55% | **242.47%** |
| CAGR | 15.09% | **14.61%** |
| Annualized volatility | -- | **8.17%** |
| Sharpe ratio | 1.723 | **1.673** |
| Maximum drawdown | -5.69% | **-6.07%** |
| Daily win rate | 52.84% | **52.49%** |
| Average absolute position | -- | **11.39%** |
| Total absolute turnover | -- | **149.94** |

The cost deduction lowers Sharpe by approximately 0.05 and cumulative return
by 13.08 percentage points. The strategy's low average exposure is important:
the return is not generated by remaining permanently long natural gas.

### 11.2 Split performance

| Evaluation window | Net Sharpe | CAGR | Maximum drawdown |
|---|---:|---:|---:|
| Development | 1.721 | 12.55% | -4.39% |
| Validation | 1.878 | 18.64% | -5.12% |
| 2024+ first-look holdout | 1.407 | 12.97% | -6.07% |

The holdout Sharpe is lower than the earlier windows but remains positive. That
decay is more credible than a report in which every later sample improves.

### 11.3 Calendar-year performance

| Year | Net return | Sharpe |
|---|---:|---:|
| 2017 partial | 3.62% | 1.426 |
| 2018 | 1.01% | 0.244 |
| 2019 | 13.28% | 2.120 |
| 2020 | 27.49% | 2.429 |
| 2021 | 12.02% | 1.857 |
| 2022 | 36.89% | 2.749 |
| 2023 | 8.56% | 0.934 |
| 2024 | 27.04% | 2.683 |
| **2025** | **-3.60%** | **-0.533** |
| 2026 through 13 July | 11.12% | 1.849 |

The series is not uniformly profitable. The weak 2018 result and negative 2025
result are economically material and should remain visible in any assessment
of robustness.

### 11.4 Standalone sleeve diagnostics

| Sleeve | Diagnostic standalone Sharpe |
|---|---:|
| Legacy weather block | 1.119 |
| Physical wind | 0.953 |
| Fundamental composite | 0.902 |
| Solar/PV | 0.718 |

These are diagnostic rescalings, not additive decompositions of portfolio
Sharpe. Portfolio value comes partly from combining signals whose errors do
not occur at the same time.

### 11.5 Fundamental-feature diagnostics

| Feature | Development | Validation | Recent | Full |
|---|---:|---:|---:|---:|
| Low storage level | 0.260 | 1.156 | -0.391 | 0.462 |
| One-week storage change | 0.220 | 0.554 | -1.093 | -0.132 |
| Four-week storage change | -0.080 | 0.787 | -0.377 | 0.153 |
| Low production YoY | -0.161 | -0.271 | 0.025 | -0.128 |
| LNG export YoY | 0.380 | 0.170 | -0.014 | 0.092 |
| Net-import supply | -0.607 | 0.115 | 0.053 | -0.168 |
| Production MoM | 0.293 | -0.447 | -0.665 | -0.118 |
| LNG export MoM | 0.860 | -0.223 | 1.045 | 0.462 |
| Net-import change | -0.319 | 0.010 | 0.554 | 0.023 |

Several retained features have weak or negative standalone Sharpe. Removing
them merely because of the full-sample column would repeat the same
multiple-testing error that the project is designed to avoid. Retention is
based on physical role, marginal portfolio behavior, split stability, and
diversification. The table instead identifies which factors deserve continued
monitoring and where future evidence could justify a preregistered removal.

---

## 12. Why the Strategy Lost Money in 2025

### 12.1 The result was not a small cost artifact

The formal 2025 return was -3.60%, with a Sharpe ratio of -0.533 and a
maximum drawdown that ultimately reached -6.07%. The gross arithmetic daily
contribution was approximately -2.97%, while the continuous turnover charge
was approximately -0.46%. These arithmetic diagnostics do not exactly sum to
the compounded calendar return, but they show that transaction cost was not
the primary cause. The strategy lost before costs.

Directional accuracy fell to approximately 48.6%. The asymmetry was more
revealing:

- on 147 long-position days, gross P&L was approximately +0.11%;
- on 103 short-position days, gross P&L was approximately -3.08%.

The model did not fail because every signal became useless. It failed mainly
because it held short exposure during a small number of rapid upside price
moves.

### 12.2 Exact factor contribution

Holding the implemented seasonal weights and interactions fixed, the 2025
gross arithmetic contributions were approximately:

| Factor or family | 2025 gross contribution |
|---|---:|
| Wind | **-1.96%** |
| Net-import family | **-1.12%** |
| CPC weather | **-0.99%** |
| LNG family | -0.15% |
| Production family | -0.01% |
| South Central storage family | +0.54% |
| Solar/PV | +0.62% |
| Freeze short-control | +0.10% |

Within net imports, the change feature contributed approximately -1.24% and
the level feature approximately +0.12%. Within storage, the level and weekly
change contributed approximately -0.17% and -0.32%, while the four-week
change contributed approximately +1.04%.

These are arithmetic contribution estimates and need not sum exactly to the
compounded net return because of costs, compounding, position clipping, and
control interactions. They nevertheless identify the economic source of the
loss: wind, slow net-import change, and CPC weather were on the wrong side of
several important moves, while solar and storage partly offset them.

### 12.3 Wind in February, March, and October

The user's requested months show three different phenomena:

| Month | Approximate wind contribution | Interpretation |
|---|---:|---|
| February 2025 | **-2.14%** | Low-wind bullish exposure persisted into fast downside reversals or remained short before upside jumps |
| March 2025 | **-1.88%** | The same multi-day forecast state adapted too slowly as price direction changed |
| October 2025 | +0.39% for the month | The month was positive in aggregate, but one extreme day exposed severe short-jump risk |

October is important because a monthly total can conceal the tail event. On
20 October, the futures return was approximately +12.93% while the strategy
position was approximately -22.46%. The day's net loss was approximately
-2.91%, and wind alone contributed approximately -2.74%. On 3 February, the
futures return was approximately +10.12% against a position near -12.68%,
creating a loss of about -1.29%.

The ten worst net trading days of 2025 lost approximately 12.69% in aggregate;
the rest of the year offset most, but not all, of those losses. This is the
signature of jump concentration rather than uniform daily decay.

### 12.4 Why the wind signal was vulnerable

The wind feature averages five overlapping forecast horizons. That is useful
for estimating the physical renewable state, but it creates persistence. The
measured wind-score autocorrelations were approximately:

| Lag | Autocorrelation |
|---:|---:|
| 1 day | 0.768 |
| 2 days | 0.522 |
| 3 days | 0.307 |

The factor's predictive value, however, was concentrated at one to two days.
The signal could therefore remain materially long or short after the
price-relevant information horizon had largely passed. In an ordinary market,
the score's smoothness controls turnover. During a rapid reversal, it becomes
adaptation lag.

This diagnosis is more precise than saying that “wind stopped working.”
Across the full sample and high-volatility subsamples, the next-day wind
relationship remained positive. The weakness was the mismatch between a
sticky five-day state variable and a much shorter return horizon, combined
with exposure to discontinuous price moves.

### 12.5 Net-import staleness

The net-import features are monthly and are not tradable until observation
month plus three months. That is conservative for look-ahead control, but it
also means the economic information can already be three to four months old
when first used. Once released, a monthly score remains unchanged until the
next issue.

The 2025 loss from net-import change is therefore consistent with a slow
balance direction persisting after the market regime turned. A conventional
daily “age since release” decay only addresses the second form of staleness.
It cannot recover the information already lost in the underlying publication
delay. This distinction explains why simple exponential decay looked helpful
in 2025 but was unstable across other years.

### 12.6 The one-day weather lag was not the main failure

One plausible hypothesis was that delaying weather by a day turns it into a
contrarian indicator during high-volatility reversals. The audit does not
support that hypothesis.

The full strategy performed best at the production one-day lag:

| Weather lag | Full cumulative return | Full Sharpe | Maximum drawdown |
|---:|---:|---:|---:|
| 0 days, noncausal diagnostic | 185.82% | 1.351 | -7.61% |
| **1 day, production baseline** | **242.47%** | **1.673** | **-6.07%** |
| 2 days | 194.55% | 1.447 | -6.33% |
| 3 days | 148.73% | 1.221 | -7.80% |
| 5 days | 207.40% | 1.500 | -7.48% |

Within the causal high-volatility regime, strategy Sharpes for lags zero,
one, two, three, and five were approximately 1.853, 2.289, 2.032, 1.518, and
2.003. The one-day lag remained the strongest. Eight of eight usable
leave-one-year-out folds selected or preserved the one-day convention.

A two-day lag would have improved 2025 to approximately +1.14%, but selecting
that lag because it fixes 2025 would be hindsight. A walk-forward model trained
only through 2024 selected the one-day lag for 2025.

### 12.7 South Central storage was not the cause

In 2025, the South Central strategy returned -3.60% versus approximately
-3.40% for the otherwise comparable Lower 48 storage version. Daily positions
had correlation of approximately 0.996, and the performance difference was
only about -20 basis points. Replacing the storage geography would not have
solved the drawdown.

### 12.8 Drawdown path and recovery

The formal maximum-drawdown episode ran from 3 January through 26 November
2025 and recovered by 21 January 2026. The recovery provides some evidence
that the full model was not permanently broken, but it does not invalidate the
2025 diagnosis. A strategy can recover and still contain an unacceptable
concentration or latency risk.

The correct conclusion is:

> 2025 was primarily a fast-reversal and jump-risk failure concentrated in
> short positions, amplified by a persistent wind state and a stale monthly
> net-import direction. It was not primarily a South Central storage error,
> a transaction-cost problem, or evidence that the one-day weather lag became
> systematically contrarian.

---


## 13. Interpretation of Model Selection

### 13.1 The model was selected by constrained evidence, not one optimizer

There was no single regression that estimated all final weights. Selection was
a sequence of constrained decisions:

1. specify an economic direction before testing;
2. enforce causal availability and native-frequency transforms;
3. compare economically adjacent feature constructions;
4. require development and validation evidence rather than full-sample Sharpe
   alone;
5. use simple slot reallocations and coarse grids;
6. prefer an interior or lower-concentration weight when the numerical
   optimum was at a boundary;
7. reject overlays whose improvement was small, unstable, or dependent on
   revised/non-tradable information;
8. freeze the surviving rule and report later-year failures.

This is closer to structured model selection with economic regularization than
to parameter estimation in a single statistical model.

### 13.2 Why equal conceptual slots were useful

With a small effective sample and correlated physical variables, precise
continuous weight estimates would be unstable. Equal conceptual slots reduce
degrees of freedom, make reallocations auditable, and prevent a feature with a
temporarily high Sharpe from receiving an implausibly large allocation.

The 2:1:1 storage allocation and double LNG-MoM slot are therefore best viewed
as coarse portfolio priors. They express relative importance without claiming
that the true optimal ratio is exactly 18.18% versus 9.09%.

### 13.3 Why the final model is not simply the highest-Sharpe backtest

Several examples demonstrate the distinction:

- 35% shoulder wind beat 22.5% in the development grid, but was not used;
- 15% solar beat 10% at the development boundary, but was not used;
- a storage reallocation raised full Sharpe by 0.0006, but was not promoted;
- EBB final receipts raised full Sharpe, but damaged development stability;
- a two-day weather lag fixed 2025, but lost the full sample and walk-forward
  decision.

The organizing principle is not “maximize the reported Sharpe.” It is
“retain economically defensible effects that survive timing and stability
tests with limited model complexity.”

---

## 14. Limitations and Threats to Validity

### 14.1 The final result is research, not a pristine prospective trial

The strategy evolved through multiple experiments. Although the development,
validation, and first-look labels impose discipline, later structural choices
were made after earlier results were known. The 1.673 full-sample Sharpe
therefore contains researcher degrees of freedom and should be discounted
relative to a truly frozen prospective result.

### 14.2 Incomplete vintage purity

The implementation applies conservative release dates, capacity lags, and
signal lags. That is necessary but not identical to possessing every original
historical data vintage. Some EIA histories may reflect later revisions, and
the archived forecast universe is not guaranteed to reproduce every file that
would have been observable in real time.

The safest interpretation is that the backtest is point-in-time controlled
where metadata permit, but not fully vintage-pure. Production deployment
should archive the exact received payload and timestamp for every new release.

### 14.3 Small effective sample and structural change

There are 2,269 daily observations, but the effective sample is much smaller:

- monthly signals change only about twelve times per year;
- weekly storage signals change roughly fifty-two times per year;
- wind forecasts overlap across five horizons;
- seasonal regimes reduce the number of comparable observations;
- LNG export capacity, renewable penetration, production geography, and
  market liquidity changed materially between 2017 and 2026.

As a result, conventional daily standard errors can convey false precision.
HAC statistics help with autocorrelation but do not solve regime change or
multiple testing.

### 14.4 Backtest execution is simplified

The return series uses settlement-to-settlement futures returns and a fixed
turnover cost. It does not model:

- intraday forecast receipt and order-routing delay;
- bid-ask spreads that widen during gas-price jumps;
- market impact or position-size capacity;
- margin and collateral return;
- exchange, clearing, and brokerage charges;
- separate slippage caused by contract rolling;
- failed or delayed data delivery.

The observed average absolute position is low, which helps plausibility, but a
live implementation should be evaluated with timestamped executable prices.

### 14.5 Risk is bounded but not fully budgeted

The final total position is clipped to \([-1,1]\), seasonal top-level weights
limit nominal concentration, and the freeze rule controls one winter short
tail. However, the production model has no explicit ex-ante marginal risk
contribution limit for the wind sleeve. The concentrated 2025 wind losses show
that this remains a material limitation.

### 14.6 Monthly staleness is only partially measurable

Age since publication is observable, but economic age begins at the reference
month, not the publication date. A value first used at \(M+3\) is already
stale before the post-release decay clock starts. This makes a simple
half-life parameter an incomplete solution to monthly latency.

### 14.7 Model interactions complicate attribution

Solar is funded from fundamentals, seasonal weights change across the year,
the score is clipped, and freeze control is nonlinear. A factor's daily
arithmetic contribution is informative but not a Shapley decomposition and
cannot be interpreted as an independent strategy return. Similarly,
standalone Sharpe does not determine marginal value in the combined model.

### 14.8 Weight documentation was not perfect

Most weight changes have archived grids and selection rules. The move from a
35% development-optimal shoulder wind allocation to the 22.5% production
allocation does not. The report records the likely concentration-control
rationale but does not invent a missing rule. Future discretionary changes
should require a signed decision record containing:

- candidate menu;
- training window;
- selection statistic;
- tie-breaker;
- expected risk effect;
- effective date;
- next review or promotion criterion.

---

## 15. August 11, 2026 Selected Enhancement

The next development step retained the complete nine-factor fundamental block
and the forward GFS wind and solar signals. Two bounded additions were made
without overwriting the approved full-history artifact.

First, a pure BSEE/Sabine event controller was added after the core score. A
worsening offshore shut-in accompanied by recent relevant Sabine operating
context can cancel a conflicting short. The controller cannot create a long,
amplify an existing position, or trade without a conflicting core position.

Second, a continuous EIA-930 total non-gas generation-shortfall signal was
given a fixed 10% top-level allocation funded from fundamentals. The signal
aggregates ERCOT, MISO, and SPP wind, solar, coal, nuclear, hydro, and other
reported non-gas generation. This design preserves the distinction between
forward GFS renewable availability and the realized multi-fuel power-system
state.

The resulting selected allocation is:

| Season | CPC weather | Wind | Solar | EIA-930 | Fundamentals after funding |
|---|---:|---:|---:|---:|---:|
| Peak demand | 45.0% | 15.0% | 0--10.0% | 10.0% | 20.0--30.0% |
| Shoulder | 22.5% | 22.5% | 0--10.0% | 10.0% | 35.0--45.0% |

On the exact EIA-930 common overlap from June 5, 2019 through July 13, 2026,
the matched weather/fundamental/event-veto baseline has 1.842 net Sharpe,
3.132 Sortino, 17.65% CAGR, and -6.07% maximum drawdown. The selected EIA-930
version has 1.930 net Sharpe, 3.213 Sortino, 18.65% CAGR, and -6.07% maximum
drawdown. Its cumulative incremental net return is 6.05 percentage points.

The approved 2017-start formal result remains unchanged. The new evaluator,
generation-pinned GCS inputs, annual results, and dashboard are identified by:

- `naturalgas/evaluate_model_v02_eia930_central_florida.py`;
- `manifests/selected_strategy_inputs_2026-08-14.json` artifacts
  `selected_eia930_overlay_inputs` and `selected_event_reports_aligned`;
- `results/models/v02_eia930_central_florida/`.

## 16. Conclusion

The South Central storage strategy was developed as an interpretable physical
model of Henry Hub rather than a generic price predictor. The chosen feature
set represents demand shocks through CPC forecast revisions, renewable
substitution through nonlinear wind and PV proxies, and the gas balance
through regional storage, production, LNG exports, and net imports.
Consumption, weather levels, observed anomalies, pipeline overlays,
calendar rules, curve features, and broad macro variables were removed,
rejected, or deferred when timing, stability, or incremental evidence was
insufficient.

Weights were selected with coarse seasonal modules and conceptual slots.
Development grids informed the allocations, but the final rule generally
favored lower concentration, interior choices, turnover control, and
validation stability over the largest point estimate. The one poorly
documented exception--the exact rationale for choosing 22.5% rather than the
35% development-optimal shoulder wind allocation--is disclosed as a
governance gap.

The formal result--242.47% cumulative net return, 14.61% CAGR, 1.673 Sharpe,
and -6.07% maximum drawdown--is strong enough to justify continued research,
but not blind confidence. The negative 2025 year showed exactly where the
model is fragile: persistent wind exposure, slow monthly net-import
information, and shorts held through rapid upside jumps.

---

## Appendix A. Compact Algorithm

For every decision date:

1. Load only the latest source vintage that satisfies its declared availability
   rule.
2. Construct season-specific CPC revision, capacity-weighted wind shortfall,
   daylight-scaled PV shortfall, South Central storage, production, LNG, and
   net-import features.
3. Standardize each feature causally at its native issue frequency.
4. Apply the declared economic sign and robust compression.
5. Combine fundamental slots using the fixed internal weights.
6. Apply peak or shoulder top-level weights.
7. Fund effective solar exposure from the fundamental sleeve.
8. In the selected version, fund a fixed 10% EIA-930 non-gas shortfall sleeve
   from the fundamental sleeve.
9. Apply the winter freeze short-control.
10. Apply the BSEE/Sabine pure short veto when its event state is active.
11. Lag the complete score by one trading session and clip to \([-1,1]\).
12. Apply the position to the causal rolled C1/C2 futures return.
13. Deduct 2.5 basis points per unit of absolute position change.
14. Store inputs, component scores, controls, position, return, and costs.

In compact notation:

\[
\begin{aligned}
F_t &= \sum_j \omega_j f_{j,t},\\
B_t &= a_{s(t)}W_t+b_{s(t)}G_t+c_{s(t)}F_t,\\
A_t &= B_t+e_t(S_t-F_t),\\
\widetilde A_t &= FreezeControl(A_t + 0.10EIA930_t),\\
P_t &= EventVeto\!\left(clip(\widetilde A_{t-1},-1,1)\right),\\
r^{net}_t &= P_t r^{NG}_t
            -0.00025|P_t-P_{t-1}|.
\end{aligned}
\]

---

## Appendix B. Reproducibility Map

Primary formal artifacts:

- `results/models/v01_south_central_storage/summary.json`:
  headline performance and formal configuration;
- `results/models/v01_south_central_storage/strategy_daily.parquet`:
  daily component scores, positions, and returns;
- `results/models/v01_south_central_storage/strategy_weights.csv`:
  final internal weights;
- `results/models/v01_south_central_storage/period_comparison.csv`
  and `annual_comparison.csv`: split and calendar results;
- `naturalgas/evaluate_model_v01_south_central_storage.py`:
  formal strategy evaluation.
- `naturalgas/evaluate_model_v02_eia930_central_florida.py`:
  selected EIA-930 and event-controller evaluation.
- `results/models/v02_eia930_central_florida/`:
  selected daily series, annual metrics, summary, and dashboard.

Feature-development artifacts:

- `naturalgas/processed/ncar_gdex_complete_wind_factor/`:
  wind IC, standalone, annual, and construction comparisons;
- `naturalgas/processed/ncar_gdex_independent_wind_weights/`:
  independent wind-allocation grid and attribution;
- `naturalgas/processed/ncar_gdex_complete_solar_factor/`:
  solar IC, weight grid, costs, and selected daily series;
- `naturalgas/processed/native_frequency_fundamentals/`:
  native-frequency correction results;
- `naturalgas/processed/no_consumption_fundamental_weights/`:
  consumption removal, candidate reallocations, and selected slots;
- `naturalgas/processed/south_central_storage/`:
  Lower 48, salt, nonsalt, and total storage comparisons;
- `naturalgas/processed/south_central_storage_weights/`:
  storage allocation robustness.

Rejected-overlay artifacts:

- `naturalgas/processed/sabine_hh_final_history_factor/` and
  `naturalgas/processed/sabine_hh_overlay_full_strategy/`:
  EBB final-receipt tests;
- `naturalgas/processed/sabine_hh_cycle_revision_local_balance/`:
  cycle-revision event tests;
- `naturalgas/processed/basis_term_spread_overlay/`:
  basis and term-structure comparisons.

Context and prior reports:

- `reports/comprehensive_strategy_report.md`;
- `reports/model_v01_development_history_2026-08-05.md`
  (this model-development history).

---

## Appendix C. Feature Retirement and Promotion Rules

A new feature should be promoted only if it:

1. has a predeclared economic sign and information timestamp;
2. improves a fixed baseline after costs;
3. is not dependent on an unavailable historical vintage;
4. survives development and at least one independent validation scheme;
5. has acceptable turnover, drawdown, and factor concentration;
6. is not merely a proxy for an existing feature;
7. passes a preregistered prospective or walk-forward test.

An existing feature should be retired or reduced only if:

1. its economic mechanism is no longer valid, or data quality has degraded;
2. weakness appears across multiple independent periods rather than one year;
3. portfolio ablation shows improvement without hidden risk transfer;
4. the decision rule was specified before inspecting the decisive evaluation
   period.

These rules explain why consumption was removed, why several overlays were
rejected, and why wind or net imports were not mechanically reweighted after
2025 despite clear diagnostic concerns.
