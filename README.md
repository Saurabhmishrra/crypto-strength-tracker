# Strength Tracker

*Factor-adjusted crypto strength, structure, and research replay.*

Strength Tracker is a research-first cryptocurrency relative-strength scanner. It separates broad
BTC and ETH market exposure from token-specific strength using robust factor models, then combines
residual momentum, persistence, CPR, and pivot structure to identify provisional and
completed-bar-confirmed candidates.

The project includes a read-only Hyperliquid market-data loop, an explainable local dashboard, and a
point-in-time historical event generator for out-of-sample research. It contains no account
connectivity or trade-execution functionality.

The immediate objective is to answer a narrower question honestly: *do persistent, beta-adjusted relative-strength regimes add useful information to CPR and pivot context after realistic trading costs?* The scanner can rank and explain candidates; it must not be used as proof of an executable trading edge.

## Safety boundary

- The sibling reference project `two_day_cpr_bot` is never imported, modified, or launched by this project.
- Strength Tracker only has public-market-data interfaces. There is no execution client, private key, or API-secret setting.
- A `LONG_CANDIDATE` or `SHORT_CANDIDATE` is a research label, not an order instruction.
- Adding paper trading is a separate, explicit phase after the hypotheses in [`RESEARCH.md`](RESEARCH.md) pass their gates.

## What is implemented now

- Daily CPR and floor-trader pivot calculations, normalized CPR-width percentile, Wilder ATR, realised volatility, level distances, and price/open location.
- Robust EWMA beta versus BTC, optional orthogonal ETH factor, beta uncertainty, and multi-horizon relative strength standardised against each token's empirical rolling residual distribution.
- Cross-sectional ranking and explainable CPR + RS labels, with live mid-price previews explicitly separated from completed-bar confirmations.
- Fixture-driven scanner CLI that writes an atomic JSON snapshot and lightweight static HTML report.
- A read-only live loop over public Hyperliquid data, and a local research cockpit that draws the structure instead of naming it.
- A point-in-time historical event generator and chronological outcome report for testing the frozen completed-close rule without silently using future data.
- Research-only collection of impact spread, a candle-derived relative-notional proxy, funding, open interest, and market breadth. None enters the score until it improves untouched out-of-sample results.

## Quick start

```bash
cd path/to/Terra_CPR
python -m unittest discover -s tests -v
python -m terra_cpr.cli demo --output output
```

The demo is deliberately synthetic. It validates plumbing only; it conveys no market result.

For a full-history fixture, generate completed-close events and a chronological train/test report with:

```bash
python3 -m terra_cpr.cli backtest --input path/to/history.json --cost-bps 10 \
  --output output/research_report.json
```

The report includes signed asset return (the feasibility/P&L view), event-time
BTC-beta-adjusted forward log return, and the full BTC + orthogonal-ETH model residual
return. It does not prove an edge by itself; rolling folds and a final untouched holdout
remain required.

## Live scanning and the local cockpit

```bash
python3 -m terra_cpr.cli serve --output output --live --universe 60
```

Open `http://127.0.0.1:8765`. See [`DASHBOARD.md`](DASHBOARD.md) for the signal semantics
and what each visual encodes.

`--live` adds a **two-tier public-data loop**. Relative strength cannot change until the
configured bar closes. The structure leg moves continuously, but a mid-price move creates
only a `PROVISIONAL` preview; a `CONFIRMED` candidate requires a completed close:

- **every 20s** — one `allMids` request refreshes prices and provisional structure state.
- **just after each configured bar close** — new bars are merged and completed-close
  confirmation is evaluated. Only confirmed transitions enter alerts and history.

Both tiers call the same `scan_assets`, so they cannot disagree about strategy logic —
only about how fresh their inputs are. A cold start for 60 perps takes roughly 30 seconds.

Run the loop without a dashboard using `strength-tracker live`, or omit `--live` from `serve` to
read whatever a previous scan wrote and touch the network never.

The previous `terra-cpr` command remains as a compatibility alias.

**For anything long-running, write the snapshot outside `~/Documents`:**

```bash
python3 -m terra_cpr.cli serve --output ~/Library/Application\ Support/strength-tracker --live --universe 40
```

`output/` sits under `~/Documents`, which macOS protects with TCC. A server detached from
the shell that launched it loses that grant when its responsible parent process exits, and
from then on every publish fails with `EPERM: Operation not permitted` — while an ordinary
shell in the same directory still writes fine, which makes it look like a code fault when
it is not one. The health rail reports it as `failing` with the exact error, and the page
falls back to `unreachable` once `/api/snapshot` starts answering 503.

Nothing in the loop can retry its way out of this; the write is genuinely denied. Keeping
the launching terminal open also works, but the path above survives however it is started.
`--output` only moves the snapshot and the signal history, both of which are gitignored —
no source or committed file moves with it.

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

Time windows are duration-preserving. Changing `interval_seconds` from 1h to 15m
automatically changes 4/24/168 bars into 16/96/672 bars, keeps persistence at 24h,
and keeps beta estimation at 30 days unless bar-count overrides are explicitly supplied.
With the public Hyperliquid adapter, 15m is the shortest default profile that fits the
provider's 5,000-candle history limit. Faster profiles fail loudly instead of silently
estimating the model on a shorter history.

## Project map

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — reference audit, reuse/discard decisions, and target architecture.
- [`RESEARCH.md`](RESEARCH.md) — hypotheses, anti-overfitting rules, and promotion gates.
- [`terra_cpr/market_structure.py`](terra_cpr/market_structure.py) — CPR/pivots/regime features.
- [`terra_cpr/relative_strength.py`](terra_cpr/relative_strength.py) — persistent beta-adjusted RS.
- [`terra_cpr/scanner.py`](terra_cpr/scanner.py) — ranking and candidate labels.
- [`terra_cpr/data.py`](terra_cpr/data.py) — read-only fixture and public Hyperliquid data adapters.
- [`terra_cpr/research.py`](terra_cpr/research.py) — point-in-time event generation and split-aware evaluation metrics.

## Deliberately deferred

Live execution, portfolio construction, stop management, and a web control plane are intentionally out of scope. Building them before an out-of-sample edge would repeat the main failure mode of the reference system: mature execution around an unproven signal.
