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
| Relative volume | Is the move being accepted with unusual participation? | Does it improve H1 OOS conditional expectancy after controlling for RS? |
| Funding + OI | Is persistent strength crowded or supported by fresh positioning? | Test continuation and reversion separately; signs can reverse by regime. |
| Liquidations | Is a move mechanical/forced and therefore likely to mean-revert or continue? | Event study around liquidations, not a generic indicator. |
| Breadth | Is RS isolated or part of a broad alt-beta move? | Does residual breadth predict whether single-name RS persists? |

## Execution is not the next step

The next validation is historical panel construction and event studies, followed by paper observations. A live order module must not be added merely because the scanner has a green row.
