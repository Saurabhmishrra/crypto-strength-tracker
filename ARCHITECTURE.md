# Terra CPR design

## Read-only reference assessment

The existing `two_day_cpr_bot` was inspected as reference material only. Its CPR formula is conventional and its public Hyperliquid data conventions are useful. Its research notes contain the more important result: raw directional Two-Day CPR, a width-gated breakout, and the CPR/EMA/pivot variant all failed their out-of-sample edge gates. CPR width did show a repeatable relationship to *next-session range*, not direction.

The current RS screener has a sound starting idea — log-price residual versus a beta-scaled BTC benchmark — but it is a single 24-hour z-score alert with provisional weights. It lacks beta-quality diagnostics, multi-horizon persistence, reliable stale-data gates, and outcome research. It is a discovery view, not alpha evidence.

## Reuse versus discard

| Reference component | Terra decision | Reason |
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

For aligned completed bars, the engine estimates

`r_alt = alpha + beta × r_BTC + residual`

on a trailing window. It then measures beta-adjusted relative returns across short, medium, and long horizons, each divided by residual volatility for that horizon. The score is a bounded signed composite of those normalised returns, residual-return persistence, and acceleration. It also publishes `beta`, `R²`, residual volatility, history count, and data-quality flags.

This design explicitly answers the failure modes of a simple “alt return minus BTC return” screen:

- A high-beta asset must exceed its *own expected BTC response* to rank highly.
- A single spike cannot dominate because medium/long relative returns and persistence are separate terms.
- Low-quality beta fits remain visible, rather than being silently treated as precise.
- It does not treat equal raw returns as equal RS when an alt normally moves 2–3× BTC.

Volume, funding, OI, and liquidation data are intentionally **not** score terms yet. They are plausible *independent forces*, but their data definitions and incremental forward-return value must first be tested. Adding them as decorative confirmations would be indicator soup.

## CPR + RS decision engine

Terra emits research labels, never automatic trades. A long candidate currently requires persistent positive RS plus price above TC and the daily pivot; a short is symmetrical below BC and the pivot. An R1/S1 cross can increase the structural score but is not required. Otherwise the result is `WATCH` or `NEUTRAL`.

This is a transparent hypothesis generator, not a conclusion that those combinations work. The labels are designed to produce a finite, timestamped event set for the tests below.
