# Strength Tracker design

## Read-only reference assessment

The existing `two_day_cpr_bot` was inspected as reference material only. Its CPR formula is conventional and its public Hyperliquid data conventions are useful. Its research notes contain the more important result: raw directional Two-Day CPR, a width-gated breakout, and the CPR/EMA/pivot variant all failed their out-of-sample edge gates. CPR width did show a repeatable relationship to *next-session range*, not direction.

The inherited RS idea — log-price residual versus a beta-scaled BTC benchmark — was a sound starting point, but the original single-window approximate z-score lacked robust beta diagnostics, multi-horizon persistence, reliable stale-data gates, and point-in-time outcomes. Strength Tracker addresses those implementation gaps; predictive value is still an empirical question, not alpha evidence.

## Reuse versus discard

| Reference component | Strength Tracker decision | Reason |
| --- | --- | --- |
| CPR calculation | Reimplement as a pure function | Conventional, correct, and simple. A fresh implementation avoids coupling to a live package. |
| Floor-trader pivot formula | Reimplement | Useful structural coordinate system; never assumed predictive on its own. |
| CPR-width research result | Retain as a volatility/regime feature | Width predicts range, not a direction or a trade. |
| Beta-adjusted BTC residual | Retain and strengthen | It removes much of the high-beta-altcoin illusion. |
| Single-threshold RS alerts | Discard | Binary crossings confuse an observation with a signal. |
| Existing live runner/order code/state model | Do not copy | It has no place in an unproven strategy and would carry execution risk into a new project. |
| Legacy CPR backtests | Do not use as a strategy oracle | Their strategy timing and live timing differ; use only as an audit trail. |

## Target architecture

```text
Read-only public data
        |
        v
Validated candle panel ----> MarketStructureEngine
        |                         | CPR, pivots, width percentile, ATR, regime
        |                         v
        +--------------------> RelativeStrengthEngine
                                  | beta-adjusted residual returns, persistence,
                                  | acceleration, quality
                                  v
                           Scanner / DecisionEngine
                                  | ranked, explainable research candidates
                                  v
                       JSON snapshot + static HTML report
                                  |
                                  v
                    Research event study / walk-forward reports

Future, separate promotion only:
approved research rule -> paper broker -> risk gate -> execution adapter
```

The first six boxes are pure or read-only. They can run from the same candle snapshot and therefore cannot disagree because of duplicated strategy logic. A future execution adapter receives an immutable, versioned candidate intent only after an independent risk gate approves it; it must not recompute the strategy.

## Market Structure Engine

For an `as_of` timestamp, the engine accepts *only completed daily candles*. The latest completed day generates the active session's CPR and pivots. It reports:

- `BC`, `P`, and `TC` (normalised into an ordered CPR band), plus `R1..R3` and `S1..S3`.
- Normalised width (`CPR width / |pivot|`) and its historical percentile. This prevents a $1 width on a $10 asset from being compared with a $1 width on BTC.
- Current-price and session-open location: below BC, inside CPR, above TC; and interval relative to pivots.
- Level distances expressed in ATR units, so “near R1” has a consistent meaning across assets.
- ATR, realised volatility, and a descriptive `tight/normal/wide` CPR regime.

No field is a prediction. The feature layer answers “where are we, and how unusual is it?”

## Relative Strength Engine

For aligned completed bars, the engine estimates a robust exponentially weighted factor model:

`r_alt = alpha + beta_BTC × r_BTC + beta_ETH × orthogonal_ETH + residual`

BTC is always the primary factor. When ETH is available, its return is first residualised against BTC so the second coefficient measures ETH/alt-market behaviour rather than counting BTC exposure twice. Coefficients use EWMA weights and MAD winsorisation; the engine publishes approximate coefficient uncertainty, effective observations, `R²`, residual volatility, history count, and quality flags.

The research path also supports a leave-one-out broad-alt challenger. It takes the
equal-weight log return of the point-in-time selected alt panel, excludes the target and
BTC, requires at least five constituents, and then orthogonalises that factor to BTC.
This is not the live default. It exists to test whether ETH is an inadequate proxy for
common alt movement without mechanically including the token in its own benchmark.

For each 4h/24h/7d horizon, the current factor-adjusted cumulative return is divided by the robust scale of that token's prior rolling horizon sums. The scale is `1.4826 × MAD`, with a standard-deviation fallback only when MAD degenerates. This empirical distribution reflects observed clustering, autocorrelation, and tails much better than `one-bar sigma × sqrt(horizon)`. Because rolling windows overlap, the result is a descriptive robust unit, not a p-value or an independent Gaussian z-score. The bounded `-10..+10` composite is also not a z-score: `3.5` never means `3.5σ`.

The bar interval changes sampling resolution, not the intended economic horizons. `RSConfig.for_interval` preserves 4h/24h/7d horizons, 24h persistence, a 30-day beta window, and a 7-day EWMA half-life when switching between supported intervals.

This design explicitly answers the failure modes of a simple “alt return minus BTC return” screen:

- A high-beta asset must exceed its *own expected BTC response* to rank highly.
- A single spike cannot dominate because medium/long relative returns and persistence are separate terms.
- Low-quality or uncertain beta fits remain visible, rather than being silently treated as precise.
- It does not treat equal raw returns as equal RS when an alt normally moves 2–3× BTC.

Impact spread, a candle-derived relative-notional proxy, funding, open interest, and cross-sectional breadth are collected as **research context**, not score terms. They are plausible independent forces, but their incremental forward-return value must first be tested in the declared order. Liquidations remain unimplemented because the current public adapter has no clean point-in-time history for them. Adding any field as a decorative confirmation would be indicator soup.

## CPR + RS decision engine

Strength Tracker emits research labels, never automatic trades. A long candidate requires persistent positive RS plus price acceptance above TC and the daily pivot; a short is symmetrical below BC and the pivot. Otherwise the result is `WATCH` or `NEUTRAL`.

In live mode a current public mid beyond structure is labelled `PROVISIONAL`. It is visible as an early heads-up but creates no durable history event. `CONFIRMED` requires the last completed bar to close beyond structure; only confirmed transitions enter alerts and historical evaluation. A confirmed state remains tied to that close even if the next live mid temporarily retreats, with the retreat shown as a blocker.

## Point-in-time research path

`generate_point_in_time_events` rebuilds the eligible volume-ranked universe, completed intraday history, completed daily structure, factor fit, RS values, and market breadth at every historical close. It records only new rule activations, then attaches fixed-horizon outcomes after event generation. The CLI `backtest --comparison-suite` evaluates the frozen H1, persistence-free discovery, H5 structure challenger, a simple residual-momentum rank, and the broad-alt factor challenger. It keeps tradable asset return separate from event-time BTC-adjusted and selected full-model residual returns, and applies a chronological train/test split.

This closes the tooling gap; it does not fill the evidence gap. Real point-in-time history, declared costs, rolling out-of-sample folds, and a final untouched holdout are still required before an input or label can be promoted.

This is a transparent hypothesis generator, not a conclusion that those combinations work. The labels are designed to produce a finite, timestamped event set for the tests below.
