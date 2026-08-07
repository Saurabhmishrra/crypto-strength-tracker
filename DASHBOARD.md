# Terra CPR research cockpit

The dashboard is a **local research cockpit**, not a trading control plane. It binds only
to `127.0.0.1`, exposes read-only `GET` endpoints, and has no account, execution, or
configuration routes. Tests assert each of those properties rather than trusting them.

```bash
python3 -m terra_cpr.cli serve --output output              # read whatever a scan last wrote
python3 -m terra_cpr.cli serve --output output --live       # and keep it current
```

## What counts as a pushed signal

Only `LONG_CANDIDATE` and `SHORT_CANDIDATE` states create an alert event. `WATCH` means
the relative-strength regime exists but the CPR/pivot structure has not confirmed;
`NEUTRAL` and `INSUFFICIENT_DATA` never alert.

An event is recorded only when a candidate first appears, changes side/type, or clears.
Re-running a scan with an unchanged candidate does not spam the history. A "signal" is
therefore a state transition in a frozen research rule, not a trade or a recommendation.

## The visual grammar: everything is a gate

A candidate must clear three conditions, so the interface draws one primitive — a track
with its threshold marked — at every scale.

| Element | What it encodes |
| --- | --- |
| **Cross-section** | relative strength (x) against ATR distance from the CPR band (y). The shaded corners are the candidate regions; the dashed rails are the thresholds. |
| **Structure track** | signed ATR distance from the band. The aqua block at the centre is the band drawn at its true ATR width, so a `tight` regime is a sliver and a `wide` one a slab. The hollow ring is the session open. |
| **Pivot ladder** | all nine levels on an ATR axis, with each level's price. Upright only in the detail drawer, where it has the height to earn the geometry. |
| **Horizon bars** | residual z at 4h / 24h / 168h on a shared ±3σ axis, matching the `/3` normalisation the score itself applies. |

Two encodings carry meaning beyond colour:

- **A hollow dot has not cleared the persistence gate.** In practice persistence is the
  binding constraint far more often than relative strength or structure, so it gets its
  own visual channel rather than being buried in a column.
- **Faded means the beta fit is weak.** When `r_squared` drops below 0.05 the engine
  raises `low_beta_fit`, and the horizon bars desaturate. An unreliable beta should look
  unreliable.

Colour is strict: green and red mean **direction** and nothing else, amber means blocked
or a data-quality problem, and aqua is reserved for **structure** — levels, rails, axes.
Structure and direction never share a hue.

## Honest degradation

If the refresh loop is failing, or the snapshot is more than 90 minutes old, the entire
interface desaturates and the age becomes prominent. A cockpit that silently presents
stale data as current is worse than no cockpit.

`/api/status` distinguishes `off` (no loop running) from `failing` (a loop that is
erroring), because a timestamp alone conflates two situations that call for opposite
reactions.

## Why a label is what it is

`assess_setup` records a blocker for the structure leg only, so an asset held back purely
by persistence arrives with an empty `blockers` list. The detail drawer therefore shows
all three gates explicitly, with each published number against the threshold it must
clear. Those thresholds are read from the snapshot's `gates` block — the rule that
produced a label travels with it — never hardcoded in the page.

The dashboard never recomputes or reinterprets a label. Every value is read from
`scanner_latest.json` as the scanner wrote it.

## Routes

| Route | Returns |
| --- | --- |
| `GET /` | the cockpit, one self-contained document with no external requests |
| `GET /api/snapshot` | the latest scan, or 503 before the first one |
| `GET /api/signals?limit=N` | candidate transitions, newest first |
| `GET /api/status` | live-loop state, universe size, failure count, last error |
| `GET /health` | whether a snapshot exists |

`POST`, `PUT`, and `DELETE` return 405 on every route. No route starts, stops, or steps
the refresh loop: the timer lives in the process, never in a request handler.

The dashboard deliberately does not show order buttons, account data, position data, or
buy/sell controls. Research has not earned those interfaces yet.
