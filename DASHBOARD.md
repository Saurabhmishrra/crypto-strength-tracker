# Terra CPR scanner dashboard

The dashboard is a **local research cockpit**, not a trading control plane. It binds only to `127.0.0.1`, exposes read-only `GET` endpoints, and has no account, execution, or configuration routes.

## What counts as a pushed signal

Only `LONG_CANDIDATE` and `SHORT_CANDIDATE` states create an alert event. `WATCH` means the relative-strength regime exists but the CPR/pivot structure has not confirmed; `NEUTRAL` and `INSUFFICIENT_DATA` never alert.

An event is recorded only when a candidate first appears, changes side/type, or clears. Re-running a scan with an unchanged candidate does not spam the history. A “signal” is therefore a state transition in a frozen research rule, not a trade or a recommendation.

## Layout

1. Health strip: snapshot timestamp, freshness, asset count, and data-quality failures.
2. Signal queue: active long/short candidates, showing the exact reasons they qualify.
3. RS leaders: strongest and weakest beta-adjusted residual trends.
4. Market map: every asset with RS score, persistence, CPR position/width, pivot position, and label.
5. Detail panel: click any asset to inspect its CPR, pivots, ATR-normalised distances, beta fit, RS horizons, reasons, and blockers.
6. Alert feed: candidate activation/change/clear history.

The dashboard deliberately does not show order buttons, account data, position data, or “buy/sell” controls. Research has not earned those interfaces yet.
