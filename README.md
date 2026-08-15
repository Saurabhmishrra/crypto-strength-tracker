# Terra CPR

Terra CPR is a separate, **research-first market-structure and relative-strength scanner**. It is not connected to an exchange account and contains no order-placement code or credential loading.

The immediate objective is to answer a narrower question honestly: *do persistent, beta-adjusted relative-strength regimes add useful information to CPR and pivot context after realistic trading costs?* The scanner can rank and explain candidates; it must not be used as proof of an executable trading edge.

## Safety boundary

- The reference project at `two_day_cpr_bot` is never imported, modified, or launched by this project.
- Terra CPR only has public-market-data interfaces. There is no execution client, private key, or API-secret setting.
- A `LONG_CANDIDATE` or `SHORT_CANDIDATE` is a research label, not an order instruction.
- Adding paper trading is a separate, explicit phase after the hypotheses in [`RESEARCH.md`](RESEARCH.md) pass their gates.

## What is implemented now

- Daily CPR and floor-trader pivot calculations, normalized CPR-width percentile, ATR, realised volatility, level distances, and price/open location.
- Multi-horizon, beta-adjusted relative strength versus BTC with residual volatility normalisation, beta quality, persistence, and acceleration.
- Cross-sectional ranking and explainable CPR + RS candidate labels.
- Fixture-driven scanner CLI that writes an atomic JSON snapshot and lightweight static HTML report.
- A read-only live loop over public Hyperliquid data, and a local research cockpit that draws the structure instead of naming it.
- An event-study / chronological split metrics module for testing candidate rules without silently using future data.

## Quick start

```bash
cd path/to/Terra_CPR
python -m unittest discover -s tests -v
python -m terra_cpr.cli demo --output output
```

The demo is deliberately synthetic. It validates plumbing only; it conveys no market result.

## Live scanning and the local cockpit

```bash
python3 -m terra_cpr.cli serve --output output --live --universe 60
```

Open `http://127.0.0.1:8765`. See [`DASHBOARD.md`](DASHBOARD.md) for the signal semantics
and what each visual encodes.

`--live` adds a **two-tier public-data loop**. Relative strength cannot change until an
hourly bar closes, but the structure leg of the rule moves continuously and is what flips
`WATCH` into a candidate — so the tiers are split:

- **every 20s** — one `allMids` request refreshes prices, and the panel is rescanned from
  cached bars.
- **just after each hourly close** — only the new bars are fetched per symbol and merged.

Both tiers call the same `scan_assets`, so they cannot disagree about strategy logic —
only about how fresh their inputs are. A cold start for 60 perps takes roughly 30 seconds.

Run the loop without a dashboard using `terra-cpr live`, or omit `--live` from `serve` to
read whatever a previous scan wrote and touch the network never.

**Run it from a terminal you keep open.** `output/` sits under `~/Documents`, which macOS
protects with TCC. A server detached from the shell that launched it loses that grant when
its responsible parent process exits, and from then on every publish fails with
`EPERM: Operation not permitted` on the snapshot rename — while an ordinary shell in the
same directory still writes fine. The health rail reports it as `failing` with the exact
error. Nothing in the loop can retry its way out of it; either keep the launching terminal
alive, or put the snapshot outside the protected tree:

```bash
python3 -m terra_cpr.cli serve --output ~/Library/Application\ Support/terra-cpr --live --universe 40
```

The loop is public-data only: it selects a universe from `metaAndAssetCtxs` and reads
candles. There is no private endpoint, no signing code, and no order path. Note that the
public `/info` endpoint sheds load by answering HTTP 200 with an **empty array** rather
than a 429, so an empty candle window is treated as retryable, never as "this market has
no data" — reading it the other way would quietly shrink the panel and corrupt every
cross-sectional rank computed from it.

## Data contract

`scan` reads a JSON document containing one BTC benchmark and assets with daily and intraday candles. Timestamps must be ISO-8601 UTC or Unix milliseconds. Daily candles must contain **completed sessions only**; the current session price/open is supplied separately.

```json
{
  "as_of": "2026-08-07T05:00:00Z",
  "interval_seconds": 3600,
  "assets": {
    "BTC": {"price": 100000, "daily": [], "intraday": []},
    "SOL": {"price": 180, "session_open": 176, "daily": [], "intraday": []}
  }
}
```

The scanner rejects or marks an asset insufficient when timestamps do not align, data is stale, or the required history is unavailable. It never fills gaps with a previous price.

## Project map

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — reference audit, reuse/discard decisions, and target architecture.
- [`RESEARCH.md`](RESEARCH.md) — hypotheses, anti-overfitting rules, and promotion gates.
- [`terra_cpr/market_structure.py`](terra_cpr/market_structure.py) — CPR/pivots/regime features.
- [`terra_cpr/relative_strength.py`](terra_cpr/relative_strength.py) — persistent beta-adjusted RS.
- [`terra_cpr/scanner.py`](terra_cpr/scanner.py) — ranking and candidate labels.
- [`terra_cpr/data.py`](terra_cpr/data.py) — read-only fixture and public Hyperliquid data adapters.
- [`terra_cpr/research.py`](terra_cpr/research.py) — split-aware evaluation metrics.

## Deliberately deferred

Live execution, portfolio construction, stop management, and a web control plane are intentionally out of scope. Building them before an out-of-sample edge would repeat the main failure mode of the reference system: mature execution around an unproven signal.
