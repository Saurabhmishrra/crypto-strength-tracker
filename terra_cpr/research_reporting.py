"""Shared calendar partitions, descriptive cohorts, and funding inputs."""
from __future__ import annotations

import json
import math
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from .data import parse_timestamp
from .research import chronological_split, evaluate, feature_buckets


def calendar_plan(start, end, train_fraction=0.65, holdout_fraction=0.20, folds=3):
    if end <= start:
        raise ValueError("history must contain a positive evaluation interval after warmup")
    if not 0 < train_fraction < 1 or not 0 < holdout_fraction < 1:
        raise ValueError("train and holdout fractions must be between zero and one")
    if type(folds) is not int or folds < 1:
        raise ValueError("folds must be a positive integer")
    holdout_start = end - (end - start) * holdout_fraction
    boundary = start + (holdout_start - start) * train_fraction
    stride = (holdout_start - boundary) / folds
    return {
        "start": start, "end": end, "test_start": boundary,
        "holdout_start": holdout_start,
        "folds": [{"train_start": start, "test_start": boundary + stride * index,
                   "test_end": boundary + stride * (index + 1)} for index in range(folds)],
    }


def _exit(event, interval):
    return event.exit_timestamp or event.timestamp + timedelta(seconds=(event.horizon_bars or 0) * interval)


def partition(events, plan, interval):
    # No development label may contain returns from the reserved holdout.
    development = [e for e in events if plan["start"] <= e.timestamp < plan["holdout_start"] and _exit(e, interval) < plan["holdout_start"]]
    train, test = chronological_split(development, boundary=plan["test_start"], interval_seconds=interval)
    holdout = [e for e in events if plan["holdout_start"] <= e.timestamp <= plan["end"]]
    return train, test, holdout


def summarize_events(events, plan, interval, cost, include_holdout=False):
    train, test, holdout = partition(events, plan, interval)
    outcomes = {"asset_return": "asset", "btc_beta_adjusted_return": "btc_beta_adjusted", "model_factor_adjusted_return": "model_factor_adjusted"}
    result = {
        "event_count": len(train) + len(test),
        "purged_development_events": sum(plan["start"] <= e.timestamp < plan["holdout_start"] for e in events) - len(train) - len(test),
        "holdout": {"released": include_holdout},
    }
    for label, outcome in outcomes.items():
        result[label] = {name: asdict(evaluate(values, cost, outcome=outcome)) for name, values in (("all", train + test), ("train", train), ("test", test))}
        if include_holdout:
            result[label]["holdout"] = asdict(evaluate(holdout, cost, outcome=outcome))
    result["rolling_folds"] = []
    for fold in plan["folds"]:
        fold_events = [e for e in events if fold["train_start"] <= e.timestamp < fold["test_end"] and _exit(e, interval) < fold["test_end"]]
        fold_train, fold_test = chronological_split(fold_events, boundary=fold["test_start"], interval_seconds=interval)
        result["rolling_folds"].append({
            **fold, "train_count": len(fold_train), "test_count": len(fold_test),
            "metrics": {label: asdict(evaluate(fold_test, cost, outcome=outcome)) for label, outcome in outcomes.items()},
        })
    result["test_by_direction"] = {direction: asdict(evaluate([e for e in test if e.direction == direction], cost)) for direction in ("LONG", "SHORT")}
    result["test_by_symbol"] = {symbol: asdict(evaluate([e for e in test if e.symbol == symbol], cost)) for symbol in sorted({e.symbol for e in test})}
    result["test_liquidity_cohorts"] = {label: asdict(evaluate(values, cost)) for label, values in feature_buckets(test, "impact_spread_bps", [5.0, 15.0]).items()}
    visible = sorted(train + test + (holdout if include_holdout else []), key=lambda e: (e.timestamp, e.symbol))
    result["events"] = [asdict(event) for event in visible]
    result["missing_outcomes"] = {
        "asset": sum(e.gross_forward_return is None for e in visible),
        "btc_factor": sum(e.btc_beta_adjusted_forward_log_return is None for e in visible),
        "full_factor": sum(e.model_factor_adjusted_forward_log_return is None for e in visible),
        "funding": sum(e.funding_cost is None for e in visible),
    }
    return result


def load_funding_series(path: Path):
    """Load explicit signed rates at scheduled settlement timestamps.

    Contract: {interval_seconds, coverage_start, coverage_end, rates:
    {SYMBOL: [{timestamp, rate}, ...]}}. Missing expected settlements produce
    None, not a zero-cost assumption. Positive rates are paid by longs.
    """
    raw = json.loads(path.read_text())
    step = raw["interval_seconds"]
    if type(step) is not int or step <= 0:
        raise ValueError("funding interval_seconds must be a positive integer")
    start, end = parse_timestamp(raw["coverage_start"]), parse_timestamp(raw["coverage_end"])
    if end <= start:
        raise ValueError("funding coverage_end must follow coverage_start")
    rates = {}
    for symbol, rows in raw["rates"].items():
        values = {}
        for row in rows:
            timestamp, rate = parse_timestamp(row["timestamp"]), float(row["rate"])
            if not math.isfinite(rate) or timestamp.timestamp() % step or timestamp in values:
                raise ValueError(f"invalid or duplicate funding observation for {symbol}")
            values[timestamp] = rate
        rates[symbol] = values

    def funding(symbol, entry, exit_time, direction):
        if entry < start or exit_time > end:
            return None
        epoch = (int(entry.timestamp()) // step + 1) * step
        total = 0.0
        while epoch <= exit_time.timestamp():
            timestamp = datetime.fromtimestamp(epoch, tz=entry.tzinfo)
            rate = rates.get(symbol, {}).get(timestamp)
            if rate is None:
                return None
            total += rate
            epoch += step
        return total if direction == "LONG" else -total

    return funding
