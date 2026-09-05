"""Read-only replay of actual archived membership and candle revisions."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from contextlib import closing
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

from .data import parse_timestamp
from .market_structure import MarketStructureConfig
from .models import Candle, MarketContext
from .relative_strength import RSConfig
from .report import write_json_atomic
from .research import (
    BROAD_ALT_FACTOR_MODEL, FIVE_MODEL_COMPARISON, EventOutcome,
    _broad_alt_forward_log_return, active_research_triggers, config_for_factor_model,
)
from .research_reporting import calendar_plan, load_funding_series, summarize_events
from .scanner import AssetInput, ScannerConfig, scan_assets


def config_from_dict(raw):
    raw = dict(raw)
    raw["rs"] = RSConfig(**raw["rs"])
    raw["market"] = MarketStructureConfig(**raw["market"])
    return ScannerConfig(**raw)


def archived_panels(path: Path):
    """Yield exact per-scan inputs; never select a new universe during replay."""
    with closing(sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        version = db.execute("SELECT value FROM archive_metadata WHERE key='schema_version'").fetchone()
        if version is None or int(version[0]) != 2:
            raise ValueError("replay requires archive schema 2 with recorded provenance")
        for scan in db.execute("SELECT * FROM scans ORDER BY id"):
            if not scan["config_json"] or not scan["observed_at"]:
                raise ValueError(f"scan {scan['id']} predates input provenance; cannot reconstruct its availability")
            if hashlib.sha256(scan["config_json"].encode()).hexdigest() != scan["config_hash"]:
                raise ValueError(f"configuration hash mismatch in scan {scan['id']}")
            config = config_from_dict(json.loads(scan["config_json"]))
            panel = {}
            for row in db.execute("SELECT * FROM panel_rows WHERE scan_id=?", (scan["id"],)):
                inputs = json.loads(row["input_json"])
                candles = {}
                for bar in db.execute("""SELECT * FROM candle_versions WHERE symbol=? AND first_scan_id<=?
                    ORDER BY first_scan_id""", (row["symbol"], scan["id"])):
                    candles[(bar["interval_seconds"], bar["timestamp"])] = Candle(
                        parse_timestamp(bar["timestamp"]), bar["open"], bar["high"], bar["low"], bar["close"], bar["volume"])
                context = json.loads(row["context_json"]) if row["context_json"] else None
                if context and context.get("observed_at"):
                    context["observed_at"] = parse_timestamp(context["observed_at"])
                panel[row["symbol"]] = AssetInput(
                    symbol=row["symbol"], price=row["price"], prior_price=row["prior_price"],
                    session_open=row["session_open"],
                    intraday=[candles[(config.interval_seconds, stamp)] for stamp in inputs["intraday_timestamps"]],
                    daily=[candles[(86400, stamp)] for stamp in inputs["daily_timestamps"]],
                    context=MarketContext(**context) if context else None,
                    price_observed_at=parse_timestamp(row["observed_at"]),
                    candles_observed_at=parse_timestamp(inputs["candles_observed_at"]) if inputs.get("candles_observed_at") else None,
                )
            yield dict(scan), config, panel


def replay_archive(path: Path, horizons, funding=None):
    if not horizons or any(type(h) is not int or h <= 0 for h in horizons):
        raise ValueError("replay horizons must be positive integers")
    with closing(sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)) as db:
        # Forward outcomes use the first archived completed observation of a
        # candle; later corrections must not rewrite the historical experiment.
        closes = {}
        for symbol, interval, timestamp, price in db.execute("""
            SELECT v.symbol,v.interval_seconds,v.timestamp,v.close FROM candle_versions v
            WHERE first_scan_id=(SELECT MIN(first_scan_id) FROM candle_versions w
              WHERE w.symbol=v.symbol AND w.interval_seconds=v.interval_seconds AND w.timestamp=v.timestamp)
        """):
            closes.setdefault(interval, {}).setdefault(symbol, {})[parse_timestamp(timestamp)] = price
        transitions = {}
        for as_of, spec, symbol, kind in db.execute("SELECT as_of,spec_id,symbol,event_type FROM research_events ORDER BY id"):
            transitions[(as_of, spec, symbol)] = kind
        observations = {}
        for scan_id, spec, symbol, score, direction in db.execute("SELECT scan_id,spec_id,symbol,score,direction FROM research_observations"):
            observations[(scan_id, spec, symbol)] = (score, direction)
    output = {(spec.spec_id, horizon): [] for spec in FIVE_MODEL_COMPARISON for horizon in horizons}
    audit = {"scans": 0, "verified_observations": 0, "mismatches": [], "seeded_states_excluded": 0,
             "entry_policy": "first completed candle at or after actual observation time; forward outcomes start there",
             "coverage": {spec.spec_id: {"evaluated_rows": 0, "usable_rows": 0, "factor_available_rows": 0} for spec in FIVE_MODEL_COMPARISON}}
    times, intervals, hashes = [], set(), set()
    for scan, config, panel in archived_panels(path):
        bar_close, observed = parse_timestamp(scan["as_of"]), parse_timestamp(scan["observed_at"])
        # Intraday scores and levels refer to the recorded close. Context retains
        # its actual availability; entries cannot precede that availability.
        evaluated_at = parse_timestamp(scan["evaluated_at"] or scan["as_of"])
        rows_by_model = {model: scan_assets(panel, evaluated_at, config_for_factor_model(config, model)) for model in {s.factor_model for s in FIVE_MODEL_COMPARISON}}
        audit["scans"] += 1
        times.append(observed)
        intervals.add(config.interval_seconds)
        hashes.add(scan["config_hash"])
        market_closes = closes.get(config.interval_seconds, {})
        for spec in FIVE_MODEL_COMPARISON:
            rows = rows_by_model[spec.factor_model]
            coverage = audit["coverage"][spec.spec_id]
            coverage["evaluated_rows"] += len(rows)
            coverage["usable_rows"] += sum(row.rs.is_usable for row in rows)
            coverage["factor_available_rows"] += sum(row.rs.is_usable and row.rs.beta is not None and row.rs.beta.secondary_beta is not None for row in rows)
            triggers = active_research_triggers(rows, config, [spec.event_rule], spec.factor_model)[spec.event_rule]
            stored_symbols = {symbol for scan_id, spec_id, symbol in observations if scan_id == scan["id"] and spec_id == spec.spec_id}
            if stored_symbols != set(triggers):
                audit["mismatches"].append({"scan_id": scan["id"], "spec": spec.spec_id, "reason": "active symbol set differs"})
            for symbol, trigger in triggers.items():
                stored = observations.get((scan["id"], spec.spec_id, symbol))
                if stored is None or abs(stored[0] - trigger.score) > 1e-8 or stored[1] != trigger.direction:
                    audit["mismatches"].append({"scan_id": scan["id"], "spec": spec.spec_id, "symbol": symbol, "reason": "score or direction differs"})
                else:
                    audit["verified_observations"] += 1
                kind = transitions.get((scan["as_of"], spec.spec_id, symbol))
                if kind == "seeded":
                    audit["seeded_states_excluded"] += 1
                if kind not in {"activated", "changed"}:
                    continue
                step = config.interval_seconds
                # Require the next grid close, not an arbitrary later recovered
                # price after a gap (which would silently change the horizon).
                entry_close = observed.replace(microsecond=0)
                seconds = math.ceil(observed.timestamp() / step) * step
                entry_close = type(observed).fromtimestamp(seconds, tz=observed.tzinfo)
                entry_open = entry_close - timedelta(seconds=step)
                entry_price = market_closes.get(symbol, {}).get(entry_open)
                for horizon in horizons:
                    exit_open = entry_open + timedelta(seconds=step * horizon)
                    exit_time = exit_open + timedelta(seconds=step)
                    exit_price = market_closes.get(symbol, {}).get(exit_open)
                    btc_entry = market_closes.get(config.benchmark, {}).get(entry_open)
                    btc_exit = market_closes.get(config.benchmark, {}).get(exit_open)
                    gross = btc_forward = adjusted = model_adjusted = None
                    missing = None
                    if entry_price and exit_price and btc_entry and btc_exit:
                        gross = exit_price / entry_price - 1
                        btc_forward = btc_exit / btc_entry - 1
                        btc_log = math.log(btc_exit / btc_entry)
                        features = trigger.features
                        adjusted = math.log(exit_price / entry_price) - features["beta"] * btc_log
                        model_adjusted = adjusted
                        secondary_beta = features.get("secondary_beta")
                        if secondary_beta is not None:
                            if spec.factor_model == BROAD_ALT_FACTOR_MODEL:
                                secondary = _broad_alt_forward_log_return(market_closes, [s for s in panel if s != config.benchmark], symbol, entry_open, exit_open, step, config.rs.broad_alt_min_constituents)
                            else:
                                e0, e1 = market_closes.get("ETH", {}).get(entry_open), market_closes.get("ETH", {}).get(exit_open)
                                secondary = math.log(e1 / e0) if e0 and e1 else None
                            model_adjusted = adjusted - secondary_beta * (secondary - features["secondary_primary_beta"] * btc_log) if secondary is not None else None
                    else:
                        missing = "missing_forward_price"
                    output[(spec.spec_id, horizon)].append(EventOutcome(
                        observed, symbol, trigger.direction, gross,
                        funding_cost=funding(symbol, entry_close, exit_time, trigger.direction) if funding else 0,
                        score=trigger.score, entry_price=entry_price, exit_price=exit_price,
                        horizon_bars=horizon, benchmark_forward_return=btc_forward,
                        btc_beta_adjusted_forward_log_return=adjusted,
                        model_factor_adjusted_forward_log_return=model_adjusted,
                        features={**trigger.features, "entry_delay_seconds": (entry_close - observed).total_seconds()},
                        event_rule=spec.event_rule, factor_model=spec.factor_model,
                        exit_timestamp=exit_time, missing_outcome_reason=missing,
                    ))
    if not times:
        raise ValueError("archive has no replayable scans")
    if len(intervals) != 1 or len(hashes) != 1:
        raise ValueError("archive contains multiple configurations; replay separate configuration cohorts")
    return output, audit, min(times), max(times), intervals.pop()


def write_replay(args):
    funding = load_funding_series(args.funding_series) if args.funding_series else None
    horizons = args.horizon_bars or [4, 24, 72]
    events, audit, start, end, interval = replay_archive(args.archive, horizons, funding)
    plan = calendar_plan(start, end, args.train_fraction, args.holdout_fraction, args.folds)
    report = {
        "schema_version": 1, "source": str(args.archive), "audit": audit,
        "calendar_partitions": plan,
        "funding": str(args.funding_series) if args.funding_series else "not supplied; zero assumption",
        "results": [{"spec_id": spec.spec_id, "horizon_bars": horizon,
                     **summarize_events(events[(spec.spec_id, horizon)], plan, interval, args.cost_bps, args.include_holdout)}
                    for spec in FIVE_MODEL_COMPARISON for horizon in horizons],
    }
    write_json_atomic(args.output, report)
    if audit["mismatches"]:
        raise ValueError(f"replay differs from archived observations; inspect {args.output}")
    print(f"verified {audit['scans']} archived panels; wrote {args.output}")
