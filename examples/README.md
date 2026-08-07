# Fixture format

Use a fixture for deterministic scanner development and research replay. A fixture must contain a coherent `as_of` timestamp and exact-bar-aligned BTC plus asset histories. Daily candles are filtered to completed sessions before feature construction; the current-session price is supplied separately.

```json
{
  "as_of": "2026-08-07T05:00:00Z",
  "interval_seconds": 3600,
  "assets": {
    "BTC": {"price": 100000, "daily": [{"t": "...", "o": 1, "h": 1, "l": 1, "c": 1, "v": 0}], "intraday": []},
    "SOL": {"price": 180, "session_open": 176, "prior_price": 175, "daily": [], "intraday": []}
  }
}
```

Timestamps are ISO-8601 UTC strings or Unix milliseconds. Values may use either compact exchange keys (`t/o/h/l/c/v`) or descriptive keys (`timestamp/open/high/low/close/volume`). Missing bars are not forward-filled; an asset will be flagged as stale or insufficient instead.
