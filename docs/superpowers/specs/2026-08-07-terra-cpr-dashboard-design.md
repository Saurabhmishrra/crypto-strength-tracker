# Terra CPR live research cockpit — design

Date: 2026-08-07
Status: approved for planning

## Problem

Terra CPR computes a defensible research signal but cannot currently show it.

Two concrete gaps:

1. **No live data path.** `HyperliquidPublicData` exists in `terra_cpr/data.py` but no CLI
   command ever calls it. `scan` replays a fixture; `demo` generates synthetic sine-wave
   prices. The only snapshot on disk holds two assets. Beta-adjusted *cross-sectional*
   relative strength over two assets carries no information — the ranking is the point,
   and there is nothing to rank.
2. **The existing dashboard renders structure as text.** `above_tc`, `tight`, `r1_to_r2`
   are categorical strings in a table. The scanner already computes every level distance
   in ATR units and a normalised CPR width percentile; none of it is drawn. A trader
   cannot see where price sits, how wide the balance area is, or which assets are queued
   just outside the candidate gate.

This design covers a live two-tier data loop and a rebuilt dashboard that renders the
strategy's own geometry.

## Non-goals

Unchanged from `README.md` and `ARCHITECTURE.md`, restated because a cockpit invites
scope creep:

- No order placement, account data, position data, or credential loading.
- No configuration or control routes. The HTTP surface stays `GET`-only.
- No new strategy logic. The dashboard **must not** recompute or reinterpret a label;
  it renders `scanner_latest.json` and nothing else.
- No RS time-series sparklines. `signal_history.jsonl` stores transitions only, and a
  rolling per-asset score store is a separate feature with its own retention question.

## Constraints

- **Zero dependencies.** `pyproject.toml` declares `dependencies = []`. Everything is
  stdlib Python and one self-contained HTML/CSS/JS document. No build step, no CDN,
  no node_modules. Charts are hand-rolled SVG.
- **Loopback only.** `serve()` binds `127.0.0.1` and gains no bind-address option.
- **This project is self-contained.** All work lands under `Terra_CPR/`. Nothing is
  written to, committed to, or read-modified in `two_day_cpr_bot`
  or any sibling directory.

---

## Part 1 — Live data loop

### 1.1 Why two tiers

RS is computed from hourly bars, so a new RS score exists only when an hourly bar closes.
The *structure* leg of the decision rule (`price_cpr_position == "above_tc"` and
`price > pivots.pivot`) moves continuously — and it is the leg that flips `WATCH` into
`LONG_CANDIDATE`. A naive fixed-interval full rescan is therefore both wasteful and less
responsive than a split schedule.

| Tier | Cadence | Network | Effect |
| --- | --- | --- | --- |
| Fast | every `fast_interval` (default 20s) | one `allMids` POST | fresh price → structure legs, labels, transitions |
| Slow | hourly close + 30s settle | delta candle fetch per symbol | fresh RS, CPR, pivots, ATR |
| Cold | first tick only | full window per symbol | populate cache |

A naive 5-minute full rescan over 60 assets is ~120 POSTs per cycle. The two-tier design
is one POST per 20 seconds plus a bounded hourly top-up.

Both tiers call the **same** `scan_assets()` with the same `ScannerConfig`. The tiers
differ only in input freshness, so the two cannot disagree about strategy logic.

### 1.2 Hyperliquid hazards to design around

**Empty-list load shedding.** The `/info` endpoint sheds load by returning HTTP 200 with
an empty JSON array rather than 429. An empty `candleSnapshot` response therefore means
*retry with backoff*, *not* "this market has no data". Treating it as the latter silently
drops assets from the universe and corrupts the cross-sectional ranking. The fetcher
treats an empty array for a symbol known to be in the universe as a retryable condition,
with exponential backoff and a bounded retry count, then surfaces the failure in status
rather than writing a truncated panel.

**Rate limiting.** Requests are throttled with a configurable minimum gap (default 120ms)
between candle requests, applied to the cold fetch and the hourly top-up.

**Field names are unverified.** `metaAndAssetCtxs` is expected to return `[meta, contexts]`
with a per-asset 24h notional volume field and a delisting marker. The exact key names
are confirmed against a live response during implementation, not assumed here. If the
expected shape is absent, universe selection fails loudly rather than falling back to an
arbitrary alphabetical slice.

### 1.3 Universe selection

Default: top **60** perps by 24h notional volume, excluding delisted markets and any
symbol that fails the history requirement. `BTC` is always included as the benchmark
regardless of rank. Size and the exclusion rules are CLI-configurable.

An asset that survives selection but lacks history is **not** dropped — it is scanned and
surfaces as `INSUFFICIENT_DATA` with its blocker text. Silent disappearance is worse than
a visible failed row.

### 1.4 Candle cache

Per-symbol, in-memory, keyed by bar-open timestamp.

- Cold fetch pulls `beta_window` (720) hourly bars plus roughly 140 daily bars —
  enough for `width_history` (120) plus `atr_period` (14) plus headroom.
- Top-up fetches from `last_stored_open - 2 × interval` to now, then merges by timestamp,
  de-duplicating and keeping the newest value for a given open. Overlap is deliberate:
  it repairs a bar that was fetched while still forming.
- Bars older than the window are evicted.
- **Gaps are never forward-filled.** A missing bar simply ends the contiguous suffix that
  `_contiguous_aligned_closes` will use, which surfaces as reduced usable history and, if
  severe, a `stale_or_misaligned` quality flag. This is existing engine behaviour and
  needs no special handling — only a guarantee that the cache does not paper over it.

### 1.5 `as_of`, completed bars, and session open

- `as_of` is **wall-clock UTC**, not the last bar timestamp. The staleness gate in
  `compute_relative_strength` measures `as_of` against the last completed bar's close, so
  a wall-clock `as_of` is what makes a stalled feed detectable. A frozen `as_of` would
  hide exactly the failure the gate exists to catch.
- Hourly and daily candles are filtered through the existing `completed_bars()` helper
  before reaching any engine.
- **`session_open` comes from the forming daily bar.** The daily fetch deliberately
  includes the incomplete current day; its `open` becomes `session_open`, and only the
  completed days are passed to `build_market_structure`. This is the one place a forming
  bar is read, it is read for a single field, and it is never treated as a closed session.
- `prior_price` is the previous fast tick's price for that symbol. This makes the
  `fresh R1 acceptance` / `fresh S1 acceptance` reasons in `assess_setup` functional for
  the first time outside tests — currently only the synthetic demo populates the field.

Consequence to accept: between hourly closes the wall-clock `as_of` drifts up to one
interval past the last bar's close, which is inside the gate's tolerance. If the slow
tick fails to run promptly after an hourly close, assets legitimately flag as stale. That
is correct behaviour and the UI shows it rather than hiding it.

### 1.6 Threading and process shape

`LiveScanner` is a daemon `threading.Thread` driven by a `threading.Event` for shutdown.
Each completed tick writes the snapshot through the existing `write_json_atomic` and
appends transitions through `append_history`, so an HTTP reader can never observe a torn
file.

**The HTTP layer remains side-effect-free.** No route triggers a fetch or a scan. The
timer lives in the process, not in a request handler. This preserves the read-only
property that `dashboard.py` currently guarantees structurally rather than by convention.

Shared status lives in a lock-protected dataclass: last successful tick times per tier,
next scheduled tick, universe size, consecutive failure count, and last error string.

### 1.7 CLI

```
terra-cpr live  [--output DIR] [--universe N] [--fast-interval S] [--slow-interval S] [--config FILE]
terra-cpr serve [--output DIR] [--port P] [--live] [--universe N] [--fast-interval S] [--config FILE]
```

`live` runs the loop headless. `serve --live` runs the loop and the dashboard in one
process. `serve` without `--live` keeps today's behaviour exactly: read whatever snapshot
exists, touch the network never.

### 1.8 New read-only route

`GET /api/status` returns the live-loop status dataclass. The health rail depends on it
to distinguish "snapshot is old because the loop is off" from "snapshot is old because
the loop is failing" — two very different situations that a timestamp alone conflates.

---

## Part 2 — Dashboard

### 2.1 Principle

Every visual encodes a quantity the scanner already computes. No decorative chart, no
derived metric invented at render time. If a number is not in `scanner_latest.json`, it
is not on the screen.

### 2.2 Signature element — the CPR / pivot ladder

Replaces the categorical strings with the actual geometry.

- **Vertical axis in ATR units, not price.** `level_distances_atr` gives `(price - level) / atr`
  for `BC, P, TC, S1, S2, S3, R1, R2, R3`. Plotting price at zero and each level at
  `-distance` makes a SOL ladder and a BTC ladder directly comparable. This is the only
  scaling under which "near R1" means the same thing across assets, which is precisely
  why the engine computes it.
- **The CPR band is drawn at its true height.** A `tight` regime is a hairline; `wide`
  is a visible slab. Width percentile appears as a small gauge beside the band.
- **Two markers: price and session open.** `price_cpr_position` and
  `opening_cpr_position` are both in the payload. The distance between the markers is
  the intra-session acceptance story, and no arrangement of text conveys it.
- **Marker colour maps to the structure leg only**: green above TC, red below BC, neutral
  inside the CPR.
- A micro variant (~120×28px) renders in every universe-table row.

Fallback: when `atr` is `None`, every distance is `None`. The ladder switches to raw
price scaling and is visibly marked as unscaled rather than silently rendering a
misleading geometry.

### 2.3 The cross-section map

A single scatter that expresses the decision rule directly.

- **x** — RS score, −10…+10, with vertical rails at ±`candidate_rs_score` (3.5).
- **y** — signed distance from the CPR band in ATR units: `level_distances_atr.TC` when
  above, `level_distances_atr.BC` when below, zero inside. This turns the three-way
  categorical position into a continuous axis.
- The `LONG_CANDIDATE` region is a literal shaded box in the top-right; `SHORT_CANDIDATE`
  bottom-left.
- **Dot size** — |persistence|. **Dot opacity** — beta `r_squared`.

The payoff: `WATCH` assets are visibly *queued outside the box* — strong RS whose
structure has not confirmed. That relationship is the most decision-relevant fact the
current dashboard cannot express.

### 2.4 RS horizon stack

- Diverging bars for `horizon_z.short` / `.medium` / `.long` on a shared ±3σ axis,
  matching the `/3.0` normalisation the score itself uses.
- Persistence as a meter across −1…+1 with the `candidate_persistence` gate (0.40) marked.
- Acceleration (`short − medium`) as a delta caret.
- **When `low_beta_fit` is flagged (`r_squared < 0.05`), the horizon bars desaturate.**
  An unreliable beta must look unreliable. The engine publishes beta quality deliberately;
  the design honours that instead of burying it in a tooltip.

### 2.5 Honest degradation

The same principle at page level. When `/api/status` reports consecutive failures or the
snapshot exceeds an age threshold, the entire interface desaturates and the age becomes
prominent. A cockpit that silently presents 40-minute-old data as current is worse than
no cockpit.

### 2.6 Layout

```
HEALTH RAIL      snapshot age · next tick · universe · aligned · flagged · loop state
ROW 1            candidate queue (cards with micro-ladder)   |  transition feed
ROW 2            cross-section map                            |  RS leaderboard
ROW 3            universe table (sortable, micro-ladders, quality dots)
DETAIL DRAWER    slides over full-width: large ladder, horizon stack, beta fit,
                 reasons, blockers, raw levels
```

The detail view is a drawer rather than a side panel so the ladder gets full width at
quant density.

### 2.7 Visual system

- **Warm graphite**, not the conventional navy-black: background `#0a0a0b`, surfaces
  `#141416`, hairlines `#232327`.
- **Strict colour semantics.** Green and red mean *direction* and nothing else. Amber
  means blocked or data-quality. One cool accent is reserved exclusively for *structure* —
  levels, rails, axes. Structure and direction never share a hue.
- System font stacks only (zero-dep): `ui-monospace, SFMono-Regular, Menlo` for numerics
  with `font-variant-numeric: tabular-nums`, `system-ui` for labels.
- Responsive down to a single column; the map and ladders keep their aspect ratio.

### 2.8 Rendering edge cases

| Condition | Behaviour |
| --- | --- |
| No snapshot | 503 from the API; "awaiting first scan" state, not an error dump |
| `rs.score` is `None` | Row present and greyed with its blocker; excluded from map and leaderboard |
| `beta` is `None` | Horizon stack replaced by the blocker reason |
| `cpr_width_percentile` is `None` | Regime reads `unknown`; gauge indeterminate |
| `atr` is `None` | Ladder falls back to price scaling, marked unscaled |
| Zero candidates | Queue states this plainly and points at the map |

### 2.9 No schema change

Every value above already exists in `scan_snapshot()` output: `level_distances_atr`,
`horizon_z`, `persistence`, `acceleration`, `beta.r_squared`, `cpr_width_percentile`,
`previous_cpr`, `opening_cpr_position`, `quality_flags`, `reasons`, `blockers`.
`schema_version` stays `1`.

---

## Part 3 — Files and testing

### 3.1 Files

| File | Change |
| --- | --- |
| `terra_cpr/live.py` | **New.** Universe selection, candle cache, two-tier loop, status. |
| `terra_cpr/dashboard.py` | Rewrite the HTML document; add `/api/status`; accept an optional live scanner. |
| `terra_cpr/cli.py` | Add `live`; add `--live` and tier flags to `serve`. |
| `terra_cpr/config.py` | Extend TOML loading for live settings. |
| `config.example.toml` | Document the `[live]` section. |
| `tests/test_live.py` | **New.** |
| `tests/test_dashboard.py` | **New.** |
| `DASHBOARD.md` | Update to describe the rebuilt cockpit and the live loop. |
| `README.md` | Document the `live` command and its read-only boundary. |

**Untouched:** `market_structure.py`, `relative_strength.py`, `scanner.py`, `models.py`,
`research.py`, `report.py`, `signal_history.py`. The pure engines carry the strategy and
this work must not perturb them.

### 3.2 Tests

`tests/test_live.py`
- Universe selection ranks by notional volume, excludes delisted, always retains the benchmark.
- A missing or renamed volume field raises rather than silently degrading.
- Empty-array candle response triggers retry with backoff, not a dropped asset.
- Retry exhaustion surfaces in status and does not write a truncated panel.
- Cache merge de-duplicates by timestamp, prefers the newer bar, evicts beyond the window.
- Cache never forward-fills across a gap.
- `session_open` is taken from the forming daily bar; only completed days reach the engine.
- `prior_price` carries the previous fast tick's price.
- Throttle enforces the minimum inter-request gap.
- The scanner thread stops promptly on its stop event.

All network behaviour is tested against a fake transport. **No test performs real network I/O.**

`tests/test_dashboard.py`
- `GET /`, `/api/snapshot`, `/api/signals`, `/api/status`, `/health` respond as specified.
- `POST`, `PUT`, `DELETE` return 405 on every route.
- Unknown paths return 404.
- `/api/snapshot` returns 503 when no snapshot exists.
- `/api/status` reports loop-off and loop-failing distinctly.
- `serve()` binds loopback and exposes no bind-address parameter.

### 3.3 Verification

`python -m unittest discover -s tests -v` passes, including the five existing suites.
A live smoke run is performed manually against the public endpoint and its result
reported honestly — including the observed universe size and any assets that failed
quality gates.

## Implementation notes — where this design was wrong

Recorded rather than quietly edited, because the reasoning matters more than the plan.

1. **The snapshot schema did change, to version 2.** §2.9 claimed no change was needed.
   It was wrong: the dashboard must draw the gate thresholds, and those live in
   `ScannerConfig`, not in the snapshot. Hardcoding 3.5 and 0.40 in the page would have
   silently drawn the wrong gates under any custom configuration. `scan_snapshot` now
   records a `gates` block, so the rule that produced a label travels with it — which is
   also better research hygiene than the original design.
2. **The micro-ladder became a horizontal track.** §2.2 put an upright ladder in a table
   row. Measured against real data that was unreadable: a typical CPR band is ~0.15 ATR,
   which over a ±2.5 ATR window in a 34px row renders as a 1.2px hairline, identical for
   every asset. A table row is landscape; laying the same information on its side gives
   112px of resolution instead of 34 and matches the tracks beside it. The upright ladder
   survives in the drawer, where it has the height to earn its geometry.
3. **The drawer ladder fits its levels instead of centring on price.** Pivots sit
   asymmetrically around price, and centring spent about a third of the canvas on empty
   space above R3. The scale stays linear, so nothing is distorted.
4. **The detail view states all three gates explicitly.** Not in the original design.
   `assess_setup` only records a blocker for the structure leg, so an asset held back
   purely by persistence arrives with an empty `blockers` list and no visible explanation
   — which is the common case, since persistence binds far more often than the other two.

## Observations about the strategy, not changed

- **The pivot leg of the candidate rule is mathematically redundant.** `TC = 2P - BC`
  makes P the exact midpoint of the CPR band, so `price > TC` already implies
  `price > pivot`, and symmetrically below BC. Verified algebraically and against all 39
  rows of a live scan. `scanner.py` is untouched; the redundancy is harmless, and it is
  why the cross-section's y-axis can express the whole structure gate on one dimension.
- **`alignment_ratio` conflates "newly listed" with "stale feed."** A recent listing has
  genuinely fewer aligned bars and trips `stale_or_misaligned` at the 0.95 threshold. The
  dashboard surfaces the flag as published; separating the two causes would be an engine
  change and belongs to its own decision.

## Open risks

- Hyperliquid response field names are unverified until implementation; universe
  selection fails loudly if the expected shape is absent.
- 60 symbols × ~860 bars is a meaningful cold fetch. If the throttle makes startup slow,
  the fix is a smaller default universe, not a faster throttle.
- The design assumes the hourly interval from `ScannerConfig.interval_seconds`. A
  non-hourly configuration changes the slow tier's boundary arithmetic; the loop derives
  its schedule from the configured interval rather than hard-coding an hour.
