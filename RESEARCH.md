# Research protocol and hypotheses

## Non-negotiable rules

1. A feature may use only bars closed at or before its timestamp. Daily CPR uses the last *completed* daily candle; the incomplete day is never treated as yesterday.
2. Generate all candidate events first, then measure forward outcomes. Do not choose a rule after seeing its outcome table.
3. Include fees, spread/slippage assumptions, funding, and a conservative same-bar stop/target collision rule where a trade simulation is used.
4. Require chronological train/test separation, at least one walk-forward repetition, long/short separation, and cross-asset replication. Cross-sectional dependence means 100 alts on the same BTC day are not 100 independent discoveries.
5. Report all predeclared score buckets and rejected hypotheses. No best-of-grid optimisation is a strategy result.

## Pre-registered first experiments

| ID | Hypothesis | Event at bar close | Outcome | Why it might work | Falsifier |
| --- | --- | --- | --- | --- | --- |
| H1 | Persistent RS is informative conditionally on structure | RS ≥ +3.5, persistence ≥ 0.40, above TC and pivot; symmetric short | 4h, 24h, and 3d beta-adjusted forward return | Cross-sectional leadership may persist when price already accepts prior-session structure | No positive net OOS expectancy in both directions and more than one liquid asset cohort. |
| H2 | CPR width modulates opportunity, not direction | H1 event split by width percentile <25 / 25–75 / >75 | Absolute future range and net directional return | Width may distinguish quiet from expansion sessions | Directional differences do not replicate; then use width only for expected-range/risk estimates. |
| H3 | Relative resilience in BTC stress is a distinct feature | BTC 4h return below its trailing 20th percentile; alt remains above TC with positive residual return | Next 4h/24h residual return | Holding structure during benchmark weakness could reveal genuine demand | No incremental OOS value over RS alone. |
| H4 | Pivot acceptance matters only after RS confirmation | H1, then first completed close across R1/S1 versus no cross | Next 4h/24h residual return, adverse excursion | A level can identify acceptance/failed acceptance, not magical support | Similar or worse outcomes than H1 without pivot condition. |

H1 is the only candidate allowed into a first implementation. H2–H4 are analysis slices, not extra filters, until their incremental value is demonstrated.

### Predeclared discovery tiers

The live scanner may surface persistence-free `WATCH` rows at absolute discovery scores
of **2.5 (early)** and **3.0 (strong)**. The discovery score retains the frozen horizon
and acceleration weights, removes the 15% persistence component, and rescales the
remaining 85% to the same -10 to +10 display range.

These tiers are not candidate rules and do not enter alerts or signal history. H1 remains
frozen at a 3.5 confirmed composite score plus the signed 0.40 persistence and structure
gates. This separation is deliberate: the persistence replay is null, but removing it
from H1 would create an unregistered replacement hypothesis. Point-in-time event output
records both scores so 2.5 and 3.0 can be compared chronologically without tuning them
after seeing outcomes.

## Promotion gates

Scanner label -> paper-trading candidate only if a frozen rule has all of:

- Positive net out-of-sample expectancy after documented costs and funding.
- At least 100 temporally separated events per direction *or* a confidence interval that is convincingly positive with fewer events.
- Profit factor above 1.10, no single asset providing more than 35% of total P&L, and no single calendar month providing more than 25%.
- Positive or neutral results in two rolling out-of-sample folds and no material degradation in a final untouched holdout.
- A benchmark comparison: it must add value versus BTC beta exposure and versus a simple cross-sectional momentum rank.
- An execution feasibility review using Hyperliquid-specific fills, spreads, funding, and capacity.

Failure is an outcome, not an invitation to tune thresholds. A rejected label remains useful scanner context, but is never promoted.

## Data additions only when they answer an independent question

| Data | Question | Test before adding to score |
| --- | --- | --- |
| Impact spread / liquidity | Is the candidate practically tradable at the assumed cost? | First reject infeasible events, then test whether a predeclared liquidity cohort changes H1 OOS expectancy. |
| Relative volume | Is the move being accepted with unusual participation? | Does it improve H1 OOS conditional expectancy after controlling for RS? |
| Funding + OI | Is persistent strength crowded or supported by fresh positioning? | Test continuation and reversion separately; signs can reverse by regime. |
| Liquidations | Is a move mechanical/forced and therefore likely to mean-revert or continue? | Event study around liquidations, not a generic indicator. |
| Breadth | Is RS isolated or part of a broad alt-beta move? | Does residual breadth predict whether single-name RS persists? |

Test order is fixed: (1) impact spread/liquidity and relative volume, (2) funding and OI,
then (3) market breadth. The live adapter and historical event schema collect these as
nullable context fields, but the score does not read them. A field can be promoted only
after it improves rolling out-of-sample results and the final untouched holdout.

## Implemented research mechanics

`generate_point_in_time_events` walks completed benchmark closes and rebuilds the data
panel, volume-ranked universe, daily structure, factors, both RS scores, confirmation state,
and breadth as they were knowable at that time. Candidate events are frozen before fixed
4h/24h/3d outcomes are attached. The `backtest` CLI writes signed asset-return,
event-time BTC-beta-adjusted, and full BTC + orthogonal-ETH residual metrics with a
chronological train/test split.

This mechanism prevents the obvious future-panel leak, but it cannot make a current
metadata value historical. Spread, funding, and OI therefore require a genuinely
timestamped archive or a point-in-time callback; missing historical values remain
`null` and are never backfilled from today's state.

## Result — the persistence gate has nothing to calibrate (2026-08-25)

The gate could not be recalibrated for the two-factor model, because 208 days of
hourly bars say the statistic it gates on is noise. Recorded here so the finding
outlives the session that produced it.

**Setup.** 34 perps (32 plus both factors), 5,000 hourly bars each, replayed with
the production estimator. Beta re-estimated every 24 bars from the trailing 720
only; forward outcomes computed with the beta known at that point. 5,340
observations over 172 sample points. Cross-sectional mean per bar, then a t-stat
across bars, so cross-asset correlation cannot inflate significance the way
pooling would. Costs charged at 10bps round trip.

**Persistence is not one-sided more often than chance.** Under a no-persistence
null, residual signs over 24 bars are Binomial(24, 0.5). Observed frequencies sit
at or slightly below that null at every attainable cut, and fall further in the
tail — 0.89x the null rate at 0.4167, 0.71x at 0.5000. Residual signs are, if
anything, marginally *less* one-sided than coin flips.

**No threshold predicts forward factor-adjusted return.** Sweeping five
persistence windows (12–168 bars) x three horizons (24/72/168 bars) x seven
thresholds gives 75 configurations. The largest |t| among them is **2.35** —
lower than the ~2.6–3.0 one should expect as the maximum of 75 draws from pure
noise. Selecting the best cell of that grid would be fitting the sampling error.

**The one-factor and two-factor distributions are the same.** Mean |persistence|
is 0.1548 one-factor, 0.1576 two-factor, against a 0.1612 null. An earlier
8-symbol single-snapshot probe suggested the ETH factor had compressed
persistence; over 208 days it does no such thing. That probe was one moment, not
a distribution, and it was wrong.

**Read this before relaxing the gate.** A trailing residual-momentum control run
through the same harness is also null at 24h and 72h (t = +0.11, +0.15). That is
the medium-horizon leg of the RS score, so this result is evidence about more
than persistence: it says short-horizon factor-adjusted predictability is absent
in this sample. Removing the persistence gate would stop blocking candidates, but
nothing here establishes that what it was blocking is worth trading. The
promotion gates above still apply, and this sample is one regime.

## Execution is not the next step

The next validation is running the frozen generator on a broad point-in-time archive,
then rolling folds and a final untouched holdout, followed by paper observations. A live
order module must not be added merely because the scanner has a green row.
