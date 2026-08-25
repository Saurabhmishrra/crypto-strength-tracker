"""Same replay, but emit persistence at several windows and several horizons.

A null result at one window/horizon pair could be an artefact of that pair.
This records the grid so the conclusion is about the statistic, not one cell.
A trailing-momentum control rides along: if nothing at all scores, the harness
is what is broken, and that has to be distinguishable from a real null.
"""
import json
import math
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from terra_cpr.relative_strength import estimate_robust_ewma_factor_model

SP = pathlib.Path(sys.argv[1])
BETA_WINDOW, MIN_POINTS, HALF_LIFE, WINSOR = 720, 240, 168, 6.0
WINDOWS = [12, 24, 48, 72, 168]
HORIZONS = [24, 72, 168]
STEP = 24

raw = json.loads((SP / "history.json").read_text())["candles"]
grid = sorted(set(c["t"] for c in raw["BTC"]) & set(c["t"] for c in raw["ETH"]))
position = {stamp: index for index, stamp in enumerate(grid)}


def aligned(symbol):
    out = [None] * len(grid)
    for candle in raw[symbol]:
        index = position.get(candle["t"])
        if index is not None:
            out[index] = candle["c"]
    return out


closes = {s: aligned(s) for s in raw}


def logret(series):
    return [math.log(b / a) if (a is not None and b is not None) else None
            for a, b in zip(series, series[1:])]


returns = {s: logret(v) for s, v in closes.items()}
assets = sorted(set(returns) - {"BTC", "ETH"})
btc, eth = returns["BTC"], returns["ETH"]
n_returns = len(btc)
max_h = max(HORIZONS)


def persistence(values):
    return (sum(v > 0 for v in values) - sum(v < 0 for v in values)) / len(values)


records = []
points = list(range(BETA_WINDOW, n_returns - max_h, STEP))
print(f"{len(points)} points x {len(assets)} assets", flush=True)

for progress, t in enumerate(points):
    window = slice(t - BETA_WINDOW, t)
    btc_w, eth_w = btc[window], eth[window]
    eth_vs_btc = estimate_robust_ewma_factor_model(
        eth_w, btc_w, MIN_POINTS, HALF_LIFE, WINSOR)
    if eth_vs_btc is None:
        continue
    eth_factor_w = [e - eth_vs_btc.beta * b for e, b in zip(eth_w, btc_w)]

    factor_fwd = {}
    for h in HORIZONS:
        b_fwd = sum(btc[t:t + h])
        factor_fwd[h] = (b_fwd, sum(eth[t:t + h]) - eth_vs_btc.beta * b_fwd)

    for symbol in assets:
        alt_w = returns[symbol][window]
        ahead = returns[symbol][t:t + max_h]
        if any(v is None for v in alt_w) or any(v is None for v in ahead):
            continue
        two = estimate_robust_ewma_factor_model(
            alt_w, btc_w, MIN_POINTS, HALF_LIFE, WINSOR,
            eth_factor_w, "ETH", eth_vs_btc.beta)
        if two is None or two.secondary_beta is None:
            continue
        residuals = [a - two.beta * b - two.secondary_beta * f
                     for a, b, f in zip(alt_w, btc_w, eth_factor_w)]
        record = {"t": t, "symbol": symbol}
        for w in WINDOWS:
            record[f"p{w}"] = persistence(residuals[-w:])
        # Control: trailing factor-adjusted return over the same 24 bars.
        record["mom24"] = sum(residuals[-24:])
        for h in HORIZONS:
            b_fwd, e_fwd = factor_fwd[h]
            record[f"fwd{h}"] = (
                sum(returns[symbol][t:t + h])
                - two.beta * b_fwd - two.secondary_beta * e_fwd
            )
        records.append(record)
    if progress % 25 == 0:
        print(f"  {progress}/{len(points)}", flush=True)

(SP / "records2.json").write_text(json.dumps(records))
print(f"{len(records)} observations", flush=True)
