"""Read-only data adapters: fixture replay and public Hyperliquid candles only."""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .models import Candle
from .scanner import AssetInput

HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"


def parse_timestamp(value: Any) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)
    if not isinstance(value, str):
        raise ValueError("timestamp must be an ISO UTC string or Unix milliseconds")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def parse_candle(row: Mapping[str, Any]) -> Candle:
    def value(short: str, long: str) -> Any:
        if short in row:
            return row[short]
        if long in row:
            return row[long]
        raise ValueError(f"candle is missing {long}")

    return Candle(
        timestamp=parse_timestamp(value("t", "timestamp")),
        open=float(value("o", "open")), high=float(value("h", "high")),
        low=float(value("l", "low")), close=float(value("c", "close")),
        volume=float(row.get("v", row.get("volume", 0.0))),
    )


def completed_bars(candles: Sequence[Candle], interval_seconds: int, as_of: datetime) -> list[Candle]:
    """Drop a forming bar based on its end time; never assume the last row closes."""
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    if len({candle.timestamp for candle in candles}) != len(candles):
        raise ValueError("duplicate candle timestamps are not safe to reconcile")
    completed = [
        candle for candle in candles
        if candle.timestamp + timedelta(seconds=interval_seconds) <= as_of
    ]
    return sorted(completed, key=lambda c: c.timestamp)


def load_fixture(path: Path) -> tuple[datetime, int, dict[str, AssetInput]]:
    raw = json.loads(path.read_text())
    as_of = parse_timestamp(raw["as_of"])
    interval_seconds = int(raw.get("interval_seconds", 3600))
    assets: dict[str, AssetInput] = {}
    for symbol, values in raw["assets"].items():
        daily = completed_bars([parse_candle(row) for row in values.get("daily", [])], 86_400, as_of)
        intraday = completed_bars([parse_candle(row) for row in values.get("intraday", [])], interval_seconds, as_of)
        assets[symbol] = AssetInput(
            symbol=symbol, price=float(values["price"]), session_open=(float(values["session_open"]) if values.get("session_open") is not None else None),
            prior_price=(float(values["prior_price"]) if values.get("prior_price") is not None else None),
            daily=daily, intraday=intraday,
        )
    return as_of, interval_seconds, assets


class CandleSource(Protocol):
    def fetch_candles(self, symbol: str, interval: str, start: datetime, end: datetime) -> list[Candle]: ...


@dataclass
class HyperliquidPublicData:
    """Minimal public-data adapter. It has no private endpoint or signing code."""

    timeout_seconds: float = 10.0
    retries: int = 2
    endpoint: str = HYPERLIQUID_INFO_URL

    def _post(self, payload: Mapping[str, Any]) -> Any:
        body = json.dumps(payload).encode("utf-8")
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                request = Request(self.endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST")
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    return json.loads(response.read().decode("utf-8"))
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt == self.retries:
                    break
                time.sleep(0.4 * (2 ** attempt))
        raise RuntimeError(f"Hyperliquid public-data request failed: {last_error}")

    def fetch_candles(self, symbol: str, interval: str, start: datetime, end: datetime) -> list[Candle]:
        """Fetch raw public candles; callers still decide the as-of completion gate."""
        payload = {
            "type": "candleSnapshot",
            "req": {
                "coin": symbol, "interval": interval,
                "startTime": int(start.timestamp() * 1000), "endTime": int(end.timestamp() * 1000),
            },
        }
        rows = self._post(payload)
        if not isinstance(rows, list):
            raise RuntimeError(f"unexpected candle response for {symbol}: {type(rows).__name__}")
        for row in rows:
            if not isinstance(row, Mapping):
                raise RuntimeError(f"unexpected candle row for {symbol}: {type(row).__name__}")
            if row.get("s", symbol) != symbol:
                raise RuntimeError(
                    f"candle response symbol mismatch: requested {symbol}, got {row.get('s')}"
                )
            if row.get("i", interval) != interval:
                raise RuntimeError(
                    f"candle response interval mismatch: requested {interval}, got {row.get('i')}"
                )
        return sorted((parse_candle(row) for row in rows), key=lambda candle: candle.timestamp)

    def fetch_mids(self) -> dict[str, float]:
        rows = self._post({"type": "allMids"})
        if not isinstance(rows, dict):
            raise RuntimeError("unexpected allMids response")
        mids = {symbol: float(price) for symbol, price in rows.items()}
        invalid = [
            symbol for symbol, price in mids.items()
            if not math.isfinite(price) or price <= 0
        ]
        if invalid:
            raise RuntimeError(f"invalid allMids price(s): {', '.join(sorted(invalid))}")
        return mids

    def fetch_meta_and_contexts(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Return perp metadata and its positionally aligned market contexts."""
        payload = self._post({"type": "metaAndAssetCtxs"})
        if not isinstance(payload, list) or len(payload) != 2:
            raise RuntimeError("unexpected metaAndAssetCtxs response shape")
        meta, contexts = payload
        if not isinstance(meta, dict) or not isinstance(contexts, list):
            raise RuntimeError("metaAndAssetCtxs did not return (meta, contexts)")
        return meta, contexts


def synthetic_demo_assets() -> tuple[datetime, int, dict[str, AssetInput]]:
    """Deterministic synthetic panel for a plumbing demonstration, never research."""
    from math import exp, sin

    interval = 3600
    as_of = datetime(2026, 8, 7, 5, 0, tzinfo=timezone.utc)
    start = as_of - timedelta(hours=1001)
    btc_prices, sol_prices, eth_prices = [], [], []
    btc_log = math_log(95_000.0)
    sol_log = math_log(160.0)
    eth_log = math_log(3_000.0)
    for index in range(1001):
        btc_return = 0.00015 + 0.003 * sin(index / 13.0)
        btc_log += btc_return
        sol_log += 1.7 * btc_return + 0.0016 + 0.0008 * sin(index / 5.0)
        eth_log += 1.1 * btc_return - 0.0005 + 0.0007 * sin(index / 7.0)
        btc_prices.append(exp(btc_log))
        sol_prices.append(exp(sol_log))
        eth_prices.append(exp(eth_log))

    def bars(prices: Sequence[float]) -> list[Candle]:
        output = []
        for index, price in enumerate(prices):
            prior = prices[index - 1] if index else price
            output.append(Candle(
                timestamp=start + timedelta(hours=index), open=prior,
                high=max(prior, price) * 1.001, low=min(prior, price) * 0.999,
                close=price, volume=1_000_000.0 + index * 10.0,
            ))
        return output

    def daily_from_hourly(hourly: Sequence[Candle]) -> list[Candle]:
        daily = []
        # Days are intentionally fully completed through the session before as_of.
        for index in range(0, len(hourly) - 24, 24):
            group = hourly[index:index + 24]
            daily.append(Candle(group[0].timestamp, group[0].open, max(c.high for c in group), min(c.low for c in group), group[-1].close, sum(c.volume for c in group)))
        return daily

    btc, sol, eth = bars(btc_prices), bars(sol_prices), bars(eth_prices)
    return as_of, interval, {
        "BTC": AssetInput("BTC", btc[-1].close, daily_from_hourly(btc), btc),
        "SOL": AssetInput("SOL", sol[-1].close, daily_from_hourly(sol), sol, session_open=sol[-6].open, prior_price=sol[-2].close),
        "ETH": AssetInput("ETH", eth[-1].close, daily_from_hourly(eth), eth, session_open=eth[-6].open, prior_price=eth[-2].close),
    }


def math_log(value: float) -> float:
    # Kept as a tiny helper so the demo's import list does not look like a market-data dependency.
    import math
    return math.log(value)
