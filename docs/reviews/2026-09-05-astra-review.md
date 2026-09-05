**Terra_CPR — Astra code review, 5 September 2026**

Implementation follow-up: the findings below describe the original reviewed revision. The fixes and operational changes are documented in [validation.md](</Users/mishy/Documents/New project/Terra_CPR/docs/validation.md>). The reproduction command now runs the corrected-behavior regression suite.

Reviewed revision: `368f2ca` (`research: persist five-model completed-bar archive`). The working tree was clean at the start. Production code was not modified. This review adds this document and an offline reproduction script.

The project has a sensible architecture for a research scanner, but several timing and persistence defects undermine its completed-bar and point-in-time guarantees. Correct those before treating the archive or the comparison report as reliable validation evidence. A rewrite is unnecessary; the main work is at the boundaries between otherwise well-separated components.

**What the codebase does**

Strength Tracker, implemented in the `terra_cpr` package, consumes public Hyperliquid data or local fixtures. It combines robust EWMA BTC/ETH factor adjustment, empirical multi-horizon residual strength, persistence, and daily CPR/pivot context. It distinguishes discovery watches, provisional mid-price candidates, and confirmed close-based candidates. The live loop publishes a dashboard snapshot and signal history, while SQLite records five research specifications: H1, D1, H5, B1, and H6. Historical replay attaches forward outcomes to frozen event rules.

The source contains no order client, account connection, or signing path. The separation between research labels and execution is reflected in the implementation, not just the README.

**What I ran**

| Check | Result |
| --- | --- |
| `python3 -m unittest discover -s tests -v` | 147 tests discovered: 133 passed; 14 dashboard tests initially blocked at local socket binding by the execution sandbox. |
| `python3 -m unittest discover -s tests -p test_dashboard.py -v`, with local socket access | All 27 dashboard tests passed, including all 14 previously blocked tests. Across these runs, all 147 existing tests passed. |
| `python3 -m terra_cpr.cli demo --output /tmp/terra-cpr-astra-demo` | Two asset rows, one confirmed candidate, zero provisional candidates, one transition. Synthetic plumbing only. |
| `python3 -m compileall -q terra_cpr` | Passed. |
| `python3 docs/reviews/reproduce_astra_findings.py` | All nine defect reproductions completed successfully. |

Runtime tested: Python 3.9.6 on macOS. The deployment image specifies Python 3.11; I did not build the image or run a second interpreter. I did not fetch live exchange data, run a browser interaction test, or rerun the historical profitability study. The local `output/` contains old snapshots/history, but no SQLite research archive or full-history fixture with which to reproduce that study. Existing user outputs were left untouched.

The reproduction script intentionally asserts the observed faulty behavior. Its successful exit confirms the findings, not correct application behavior. Convert the checks to assertions of the desired behavior when implementing fixes.

**Prioritized findings**

P1 means fix before relying on the relevant signal or research output. P2 means a material reliability or reporting correction.

**1. [P1] A cached forming candle becomes “completed” without a final fetch.**

Location: [live.py:309](</Users/mishy/Documents/New project/Terra_CPR/terra_cpr/live.py:309>) and [data.py:54](</Users/mishy/Documents/New project/Terra_CPR/terra_cpr/data.py:54>).

The cache stores forming candles, and `build_panel` decides completion solely from the current clock. Suppose the 05:00 candle was fetched at 05:00:30. At 06:00:10, before the scheduled 06:00:30 refresh, a fast tick accepts that old partial OHLC as the completed 05:00 candle. The same happens if its refresh fails. This can change scores and confirmations using an unfinished close; the daily cache has the same vulnerability at midnight.

Reproduction: a candle excluded at 05:00:30 is admitted at 06:00:10 without any intervening fetch, and the resulting RS is marked usable. If archived on a failed top-up, the partial value can also persist because candle inserts use `INSERT OR IGNORE`.

Fix: track whether a bar was actually observed after its close, or maintain separate forming and verified-completed stores. Time passing must not finalize a cached partial bar. Test the pre-settlement fast tick, failed top-up, midnight, and later corrected candle.

**2. [P1] Discovery research rules accept rows explicitly marked stale.**

Location: [research.py:239](</Users/mishy/Documents/New project/Terra_CPR/terra_cpr/research.py:239>).

The discovery branch checks only score existence, magnitude, and optional structure. It never checks `row.rs.is_usable`. H1 is indirectly protected by `assess_setup`, and B1 by the percentile calculation, but D1/H5 and eligible H6 rows can retain invalid states. H5 can also fall back to the current mid when an insufficient-data setup has no confirmation price.

Reproduction: move `as_of` three hours beyond the demo history. All rows become unusable, yet D1 emits ETH and SOL triggers and H5 emits SOL. Thus a research evaluation can report zero usable rows alongside active discovery observations.

Fix: enforce a shared research eligibility check before evaluating every rule, and require a valid completed price for structure-dependent rules. Represent unavailable observations separately from a valid inactive state.

**3. [P1] Train/test splitting does not separate timestamps or outcome windows.**

Location: [research.py:751](</Users/mishy/Documents/New project/Terra_CPR/terra_cpr/research.py:751>) and [cli.py:129](</Users/mishy/Documents/New project/Terra_CPR/terra_cpr/cli.py:129>).

`chronological_split` sorts events and cuts by event count. Events across assets at the same close can land on opposite sides. A training event's 24h/72h forward outcome can also extend into the test period. Moreover, the CLI chooses a separate event-count cutoff for each rule and horizon, so the five models can be compared over different market periods.

Reproduction: four simultaneous events split 50/50 share the same timestamp across train and test. With four hourly events and 24h outcomes, the last training outcome ends 23 hours after the first test event starts.

Fix: choose shared calendar boundaries for all specifications, keep each timestamp together, and purge training labels whose outcome intervals cross the boundary. Persist boundaries and purged counts in the report, then build rolling folds and a final holdout on that contract. The current metrics are descriptive; they do not supply an independent test partition for model selection.

**4. [P1] A failed archive refresh is skipped, then hidden by a successful fast tick.**

Location: [live.py:614](</Users/mishy/Documents/New project/Terra_CPR/terra_cpr/live.py:614>) and [live.py:565](</Users/mishy/Documents/New project/Terra_CPR/terra_cpr/live.py:565>).

`tick` catches a refresh error and returns without a success result. `_run` nevertheless advances the scheduled candle refresh. If symbols were already admitted, the next tick takes the fast path, publishes from cache without retrying the archive, and resets the shared failure counter. This defeats the documented guarantee that an archive failure prevents publication of a snapshot with a research-history hole.

Reproduction: inject an archive failure and execute two loop iterations. The archive is attempted once, no completed refresh succeeds, but the second iteration publishes a snapshot and reports zero failures.

Fix: return an explicit refresh result and advance the completed-bar checkpoint only after durable commit. Retry the pending close with bounded backoff. Track candle/archive health separately from mid-price health so a working price endpoint cannot hide a failed research refresh.

**5. [P1] Archived context is timestamped earlier than it was observed.**

Location: [research_archive.py:312](</Users/mishy/Documents/New project/Terra_CPR/terra_cpr/research_archive.py:312>) and [research_archive.py:333](</Users/mishy/Documents/New project/Terra_CPR/terra_cpr/research_archive.py:333>).

The archive derives `scans.as_of` from the last BTC candle close. Prices, volume-ranked membership, funding, OI, and spread are fetched later, but their observation times are not stored. On a cold start at 05:59, data observed at 05:59 is associated with a 05:00 scan. Normal settlement delays cause the same issue on a smaller scale. These values cannot safely be interpreted as features known at 05:00.

Reproduction: supply rows and context observed at 05:59. The archive stores the scan at 05:00 and has no panel-row observation-time column.

Fix: retain both bar-close time and actual observation/availability times, ideally per source response. Historical joins must use availability time. Decide explicitly whether a research event belongs to the theoretical close or actual decision time; record both instead of discarding the distinction. This is currently a provenance defect and creates leakage risk for future archive-based context studies, even though funding/OI/spread do not yet enter the live score.

**6. [P2] A history write failure permanently loses an activation.**

Location: [live.py:579](</Users/mishy/Documents/New project/Terra_CPR/terra_cpr/live.py:579>). The fixture CLI has the same ordering pattern.

The code replaces the snapshot before appending its transitions. If the append fails, the next attempt reads the already-updated snapshot as its previous state and generates no activation. Retrying does not recover the lost event.

Reproduction: fail `append_history` once, then republish the same confirmed snapshot. The snapshot exists, but history contains zero activation events.

Fix: commit transitions and their checkpoint together in SQLite, then publish the snapshot as a derived view. An outbox with stable event IDs is another option. Simply reversing the writes replaces lost events with potential duplicates and is insufficient.

**7. [P2] Stale daily structure can still produce confirmed candidates.**

Location: [market_structure.py:145](</Users/mishy/Documents/New project/Terra_CPR/terra_cpr/market_structure.py:145>) and [live.py:497](</Users/mishy/Documents/New project/Terra_CPR/terra_cpr/live.py:497>).

Daily validation checks duplicates and a two-bar minimum, but not freshness, UTC session boundaries, or continuity. Live admission uses the same count-only criterion. If hourly updates succeed while daily requests fail, current RS can be combined with old CPR/pivot levels indefinitely without a daily-data quality blocker.

Reproduction: remove the ten latest daily candles while preserving intraday history. SOL remains a confirmed candidate and the RS rows remain usable.

Fix: require the expected last completed session for active CPR and validate daily cadence. Keep stale levels available for diagnostics, but block structure confirmation. Missing daily history should degrade one asset rather than abort an entire fixture scan.

**8. [P2] Missing mids silently remove assets and generate false clearing events.**

Location: [live.py:304](</Users/mishy/Documents/New project/Terra_CPR/terra_cpr/live.py:304>) and [signal_history.py:45](</Users/mishy/Documents/New project/Terra_CPR/terra_cpr/signal_history.py:45>).

`build_panel` skips a non-benchmark asset with no mid. That omission is not added to `excluded_symbols`, and status retains the admitted universe size. Comparing the reduced snapshot with the previous snapshot then treats the missing candidate as cleared. A returning mid can manufacture another activation even though no new completed-bar decision occurred.

Reproduction: remove only SOL from the fast-tick mid response. SOL disappears, exclusions remain empty, failures remain zero, and signal history records a SOL clearing.

Fix: represent temporary price unavailability explicitly and preserve the last confirmed state through it. Only an eligible completed-bar evaluation should clear a confirmed signal. Report selected, admitted, price-available, and score-usable counts separately.

**9. [P2] Event-study returns are compounded as if all events were sequential trades.**

Location: [research.py:731](</Users/mishy/Documents/New project/Terra_CPR/terra_cpr/research.py:731>).

`evaluate` multiplies `1 + return` for each event, including simultaneous assets and overlapping holding periods. There is no portfolio allocation, shared-capital constraint, or mark-to-market series. Consequently `total_return` and `max_drawdown` are not portfolio metrics; drawdown can also depend on within-timestamp ordering. The reported Sharpe is a per-event mean/stdev ratio by default, not a portfolio or annualized Sharpe.

Reproduction: two simultaneous +10% observations produce a reported total return of +21%. An equal-weight portfolio over that period would return +10%; the generator does not specify any allocation that justifies its compounded result.

Fix: keep event expectancy and distribution summaries, but remove or explicitly rename the hypothetical compounded fields. Produce portfolio return/drawdown only from a separate chronological exposure model. For research uncertainty, use time-grouped results and time-block resampling rather than treating correlated events as independent observations.

**What is worth retaining**

- Immutable models and pure calculation functions make focused testing practical.
- OHLCV finite-value validation, exact timestamp alignment, no price forward-fill, and explicit RS quality flags establish useful data contracts.
- CPR and Wilder ATR calculations have focused tests; factor fitting has tests for beta recovery, outliers, secondary-factor behavior, and horizon scaling.
- Candidate/discovery separation and shared trigger evaluation reduce strategy drift between paths.
- Snapshot replacement uses unique temporary files; archive transactions, uniqueness constraints, and restart-aware states are sound foundations.
- The documentation records negative research results and keeps score changes separate from execution features.

**Further improvements after the defects**

1. Make the archive reproducible: store the full effective configuration, code revision/configuration hash, factor mode and fallback reason, source availability times, and per-row input cutoff. Presently the archive stores four decision gates and model strings, not all beta/window/market settings. Add a replay command that consumes the actual archived membership and observations; the current CLI accepts fixtures and reconstructs a volume-ranked universe.
2. Expand research reporting: common calendar folds, long/short and cohort separation, missing-outcome counts, factor-availability counts in backtests, and reproducible uncertainty estimates. Do not silently treat missing future prices as representative observations. The CLI currently has no funding-series input even though the generator has a callback, so its default cost study does not include realized funding.
3. Add configuration validation at startup. `LiveConfig` accepts zero/negative/non-finite polling values, and unknown TOML sections can be ignored. Validate types, ranges, allowed sections, and finite numeric values so configuration mistakes fail before the worker starts.
4. Add CI across the supported Python range and the deployment interpreter. Prioritize regression coverage for these reproduced boundary failures, plus midnight, restart, partial-panel recovery, and future-data perturbation checks. Add a small browser smoke test for actual filtering, drawer interaction, and stale-health behavior; most current UI tests inspect HTML or HTTP routes.
5. Improve long-running operation without changing the strategy: separate liveness from scanner readiness, make shutdown interrupt retries promptly, monitor candle/archive age, and query recent signal events without rereading the entire growing JSONL file on every request. Benchmark realistic 40–60 asset panels before optimizing the repeated fast-tick factor computation or full-history replay.

The next useful milestone is a verified completed-bar archive plus reproducible common-calendar replay. New indicators, threshold relaxation, and execution functionality would not resolve the current evidence gaps. The negative findings in `RESEARCH.md` remain project-reported historical results; this review neither independently confirms them nor establishes a trading edge.
