**Reliability and research validation — September 2026**

The Astra review fixes preserve the existing score weights and candidate thresholds.
They change data eligibility, persistence, and research reporting. No order or account
functionality was added.

**Live data and signal history**

- Live caches distinguish forming observations from candles fetched after their close.
  A fast tick or failed request cannot finalize an old partial candle. The separate
  forming daily observation can still supply the current session open.
- Daily structure requires the preceding completed UTC day, UTC-midnight candle opens,
  and uninterrupted daily history. Missing history yields an unavailable row; stale
  levels remain diagnostic and cannot confirm a candidate. Discovery research requires
  usable RS and a completed price; structure-dependent rules also require usable CPR.
- A failed refresh remains due. A captured panel whose durable commit failed is retried
  before processing another panel. Retry delays are bounded at 60 seconds. Shutdown
  interrupts backoff waits and checks cancellation between symbols; an in-flight network
  request remains bounded by the adapter timeout.
- `signal_history.sqlite3` stores confirmed states and transitions in one transaction.
  Only completed refreshes advance those states. Missing prices, unavailable observations,
  and fast ticks cannot clear them. State timestamps prevent duplicate events on retries.
- `signal_history.jsonl` is an atomic compatibility export. A failed export/snapshot
  write is recoverable from SQLite. The dashboard uses indexed recent-event queries.
  Existing JSONL records and the last snapshot are imported when the signal store is
  first created. Previously lost history cannot be reconstructed retroactively.

`/api/status` reports selected and admitted counts, prices available, usable RS rows,
separate refresh/price failures, and candle/archive ages. Selected/admitted/price counts
include BTC; usable RS rows exclude it. Missing mids are listed in `excluded_symbols`.

`/livez` means the HTTP server is alive. `/ready` returns 200 only when a running scanner
has a recent successful candle/archive refresh and a readable published snapshot; otherwise
it returns 503 with details. `/health` remains a compatibility endpoint. The shipped
hosting checks use `/livez`; readiness is available to monitoring/routing integrations.

**Archive schema and migration**

`research_archive.sqlite3` is now schema 2. Opening a schema-1 archive applies an additive
migration, preserving its candles, observations, events, and states. Historical values
that were never recorded remain unknown; the migration does not invent provenance.
Unknown schema versions are rejected.

New scans store:

- The benchmark bar-close time, the engine evaluation time, and actual input availability
  time. Source context retains its own `observed_at`; prices and the latest candle response
  also retain observation timestamps.
- Full scanner, RS, and market settings, a configuration hash, source-content hash, and
  Git revision when available. Container builds can pass `STRENGTH_TRACKER_REVISION` as
  a build argument. The source hash also identifies uncommitted source changes.
- Actual selected/scanned membership, missing-symbol reasons, exact input candle
  timestamps, and factor/daily quality diagnostics for each row.
- Candle revisions with the scan that first observed them. A later correction updates
  the current candle view but cannot rewrite a previous scan's input history.
- Per-model availability counts. Last known active states survive missing data; their
  evaluation timestamps are preserved and `unavailable_states` identifies states that
  have not been reevaluated at the latest scan. They are not new active observations.

Use a separate output directory when changing a model configuration. The archive refuses
to mix configurations into one experiment's state transitions. Keep both SQLite databases
on the persistent volume. For backups of a running process, use SQLite's backup API rather
than copying only the main database file while its WAL may contain committed data.

**Replay the recorded archive**

```bash
python3 -m terra_cpr.cli replay --archive /path/to/research_archive.sqlite3 \
  --horizon-bars 4 --horizon-bars 24 --cost-bps 10 \
  --output output/archive_replay.json
```

Replay reconstructs each panel from its saved membership, configuration, and candle
revisions, then compares regenerated active observations with stored scores/directions.
A mismatch is reported and causes a nonzero exit. Schema-1 scans without provenance
cannot be audited this way: replay refuses them rather than guessing their inputs.

Only recorded activations/changes generate outcomes; initial `seeded` states are counted
separately because their true activation time is unknown. Entries use the first scheduled
completed close at or after actual observation time, so an observation at 05:00:30 cannot
enter at the earlier 05:00 close. Missing entry/exit prices remain missing outcomes; the
entry is never moved to an arbitrary later recovered price. This is a causal event-study
convention, not a fill or execution simulator.

**Calendar partitions and metrics**

Fixture `backtest` and archive `replay` use the same reporting functions:

- A common calendar plan applies to every rule and horizon. Events from one timestamp
  remain together. Training labels that touch/cross the test boundary are purged.
- The final 20% of elapsed evaluation time is reserved by default. Development outcomes
  cannot cross into it. Three expanding-training rolling folds run before that holdout.
  The configuration flags are `--train-fraction`, `--holdout-fraction`, and `--folds`.
- Holdout metrics and events are omitted unless `--include-holdout` explicitly releases
  them. Choosing that flag reveals the results; code cannot prevent a human from rerunning
  or tuning afterward. Freeze rules before release.
- Reports include long/short, per-symbol, and descriptive impact-spread cohorts
  (`<5`, `5–15`, `>=15` bps, plus missing), with missing outcome/funding and factor-coverage
  counts. Cohorts do not enter the score.
- Event expectancy remains available. A separately named `event_mean_stdev_ratio` replaces
  the ambiguous event Sharpe. Legacy `sharpe`, `total_return`, and `max_drawdown` fields are
  null: there is no capital allocation or portfolio equity model to justify them.
- Uncertainty uses cross-sectional means at each timestamp, resampled in calendar blocks
  at least one day and at least the longest holding period. Five blocks are required.
  The 95% interval uses 500 draws with a fixed seed and estimates the timestamp-weighted
  mean, not the event-weighted expectancy. It remains descriptive in small samples.

Fixture backtests retain their idealized completed-close entry convention. Archive replay
uses the availability-aware entry convention above. Do not pool the two as if entry timing
were identical. The fixture callback for external context now requires an `observed_at`
no later than the historical event time.

**Funding inputs**

Both commands accept `--funding-series path/to/funding.json`. Without it, the report
explicitly records a zero-funding assumption. Example contract:

```json
{
  "interval_seconds": 3600,
  "coverage_start": "2026-09-01T00:00:00Z",
  "coverage_end": "2026-09-01T03:00:00Z",
  "rates": {
    "SOL": [
      {"timestamp": "2026-09-01T01:00:00Z", "rate": 0.0001},
      {"timestamp": "2026-09-01T02:00:00Z", "rate": 0.0002},
      {"timestamp": "2026-09-01T03:00:00Z", "rate": -0.0001}
    ]
  }
}
```

Rates are decimal fractions on a fixed UTC settlement grid; positive rates are paid by
longs and received by shorts. Costs sum settlements in `(entry, exit]` on a constant
notional basis. A missing expected settlement or uncovered interval yields unknown funding
and excludes that event from net metrics, with an explicit missing count. Zero-cost periods
must have explicit zero rates.

**Verification and performance**

```bash
python3 -m unittest discover -s tests -v
python3 docs/reviews/reproduce_astra_findings.py
python3.11 scripts/benchmark.py --output /tmp/terra-cpr-benchmark.json
```

The browser test is an optional development dependency; runtime dependencies remain empty.
Using the [Playwright library setup](https://playwright.dev/python/docs/library):

```bash
python3 -m pip install '.[test-browser]'
python3 -m playwright install chromium
python3 tests/browser_smoke.py
```

The CI workflow tests Python 3.9–3.14, runs the browser smoke test on Python 3.11,
and builds/smoke-tests the Python 3.11 container. These jobs run when the repository is
pushed to GitHub; creating the workflow does not imply that hosted CI has already run.

Local verification completed: all 184 unit/integration tests passed on Python 3.9.6
and Python 3.11.15. The isolated Chrome smoke test passed without JavaScript errors;
compilation, the synthetic demo, and the six-panel archive replay integration also
passed. Docker was not installed locally, so the container build is configured for
CI but was not executed on this machine.

Local synthetic measurements on Python 3.11.15, excluding exchange/network latency:

| Markets | Completed refresh + archive | Fast tick | 300-close replay | Initial archive |
| --- | --- | --- | --- | --- |
| 40 | 0.40 s | 0.11 s | 4.15 s | 9.5 MB |
| 60 | 0.59 s | 0.16–0.17 s | 6.29 s | 14.3 MB |

The fast path is comfortably below the default 20-second interval, so no additional
score cache was introduced. Archive growth depends on panel size, observed corrections,
and retained input provenance; these initial sizes are not a retention forecast. The
benchmark is synthetic and supplies no evidence about profitability. The previous
historical market study was not rerun as part of this engineering update.
