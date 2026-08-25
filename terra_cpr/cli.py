"""CLI for fixture scans and a synthetic plumbing demonstration."""
from __future__ import annotations

import argparse
import math
import os
import time
from dataclasses import asdict, replace
from pathlib import Path

from .config import load_live_configs, load_scanner_config
from .data import HyperliquidPublicData, load_fixture, synthetic_demo_assets
from .live import LiveConfig, LiveScanner
from .relative_strength import RSConfig
from .report import render_html, scan_snapshot, write_json_atomic
from .research import chronological_split, evaluate, generate_point_in_time_events
from .scanner import ScannerConfig, scan_assets
from .signal_history import append_history, signal_transitions
from .dashboard import serve


def _write_scan(output: Path, as_of, assets, interval_seconds: int, config_path: Path | None) -> None:
    config = (
        load_scanner_config(config_path, interval_seconds)
        if config_path else ScannerConfig(
            interval_seconds=interval_seconds,
            rs=RSConfig.for_interval(interval_seconds),
        )
    )
    rows = scan_assets(assets, as_of, config)
    snapshot = scan_snapshot(as_of, rows, config)
    previous_path = output / "scanner_latest.json"
    history_path = output / "signal_history.jsonl"
    try:
        import json
        # A newly enabled history starts with the current active candidates even
        # if an older snapshot already existed before this feature was added.
        previous = json.loads(previous_path.read_text()) if previous_path.exists() and history_path.exists() else None
    except (OSError, json.JSONDecodeError):
        previous = None
    events = signal_transitions(previous, snapshot)
    write_json_atomic(output / "scanner_latest.json", snapshot)
    (output / "scanner_latest.html").write_text(render_html(snapshot))
    append_history(history_path, events)
    confirmed = [
        row for row in rows
        if row.setup.label.endswith("CANDIDATE") and row.setup.confirmation == "CONFIRMED"
    ]
    provisional = [
        row for row in rows
        if row.setup.label.endswith("CANDIDATE") and row.setup.confirmation == "PROVISIONAL"
    ]
    print(
        f"wrote {len(rows)} assets to {output}; confirmed={len(confirmed)}; "
        f"provisional={len(provisional)}; transitions={len(events)}"
    )


def _write_backtest(args) -> None:
    as_of, interval, assets = load_fixture(args.input)
    config = (
        load_scanner_config(args.config, interval)
        if args.config else ScannerConfig(
            interval_seconds=interval,
            rs=RSConfig.for_interval(interval),
        )
    )
    horizons = args.horizon_bars or sorted({
        config.rs.short_horizon_bars,
        config.rs.medium_horizon_bars,
        math.ceil(3 * 86_400 / interval),
    })
    results = []
    for horizon in horizons:
        events = generate_point_in_time_events(
            assets,
            config,
            horizon_bars=horizon,
            universe_size=args.universe,
        )
        train, test = chronological_split(events, args.train_fraction)
        results.append({
            "horizon_bars": horizon,
            "horizon_seconds": horizon * interval,
            "event_count": len(events),
            "asset_return": {
                "all": asdict(evaluate(events, args.cost_bps)),
                "train": asdict(evaluate(train, args.cost_bps)),
                "test": asdict(evaluate(test, args.cost_bps)),
            },
            "btc_beta_adjusted_return": {
                "all": asdict(evaluate(
                    events, args.cost_bps, outcome="btc_beta_adjusted"
                )),
                "train": asdict(evaluate(
                    train, args.cost_bps, outcome="btc_beta_adjusted"
                )),
                "test": asdict(evaluate(
                    test, args.cost_bps, outcome="btc_beta_adjusted"
                )),
            },
            "model_factor_adjusted_return": {
                "all": asdict(evaluate(
                    events, args.cost_bps, outcome="model_factor_adjusted"
                )),
                "train": asdict(evaluate(
                    train, args.cost_bps, outcome="model_factor_adjusted"
                )),
                "test": asdict(evaluate(
                    test, args.cost_bps, outcome="model_factor_adjusted"
                )),
            },
            "events": [asdict(event) for event in events],
        })
    report = {
        "schema_version": 1,
        "as_of": as_of.isoformat(),
        "bar_interval_seconds": interval,
        "model_version": "robust_ewma_empirical_v1",
        "round_trip_cost_bps": args.cost_bps,
        "train_fraction": args.train_fraction,
        "warning": (
            "Descriptive event study only. Promotion requires rolling folds and an "
            "untouched holdout; context inputs remain excluded from the score."
        ),
        "results": results,
    }
    write_json_atomic(args.output, report)
    counts = ", ".join(
        f"{result['horizon_bars']} bars={result['event_count']}"
        for result in results
    )
    print(f"wrote point-in-time research report to {args.output}; {counts}")


def _build_live_scanner(args) -> LiveScanner:
    """Assemble a live scanner from CLI flags, with the config file as the base."""
    scanner_config, live_config = (
        load_live_configs(args.config) if args.config else (ScannerConfig(), LiveConfig())
    )
    overrides = {}
    if args.universe is not None:
        overrides["universe_size"] = args.universe
    if args.fast_interval is not None:
        overrides["fast_interval_seconds"] = args.fast_interval
    if overrides:
        live_config = replace(live_config, **overrides)
    return LiveScanner(
        output_dir=args.output, source=HyperliquidPublicData(),
        scanner_config=scanner_config, live_config=live_config,
    )


def _add_live_flags(command) -> None:
    command.add_argument("--universe", type=int, help="number of perps to scan, ranked by 24h notional volume")
    command.add_argument("--fast-interval", type=float, help="seconds between mid-price refreshes")
    command.add_argument("--config", type=Path, help="strict TOML scanner configuration")


def main() -> None:
    parser = argparse.ArgumentParser(description="Strength Tracker research scanner (no execution capability)")
    subparsers = parser.add_subparsers(dest="command", required=True)
    scan = subparsers.add_parser("scan", help="scan a deterministic JSON candle fixture")
    scan.add_argument("--input", type=Path, required=True)
    scan.add_argument("--output", type=Path, default=Path("output"))
    scan.add_argument("--config", type=Path, help="strict TOML scanner configuration")
    backtest = subparsers.add_parser(
        "backtest",
        help="generate completed-close events and chronological outcome reports",
    )
    backtest.add_argument("--input", type=Path, required=True)
    backtest.add_argument(
        "--output", type=Path, default=Path("output/research_report.json")
    )
    backtest.add_argument("--config", type=Path, help="strict TOML scanner configuration")
    backtest.add_argument(
        "--horizon-bars", type=int, action="append",
        help="forward horizon in bars; repeat for several horizons",
    )
    backtest.add_argument(
        "--universe", type=int,
        help="point-in-time notional-volume universe size",
    )
    backtest.add_argument(
        "--cost-bps", type=float, default=10.0,
        help="round-trip fee plus spread/slippage assumption",
    )
    backtest.add_argument("--train-fraction", type=float, default=0.65)
    demo = subparsers.add_parser("demo", help="run deterministic synthetic plumbing demo")
    demo.add_argument("--output", type=Path, default=Path("output"))
    demo.add_argument("--config", type=Path, help="strict TOML scanner configuration")
    serve_command = subparsers.add_parser("serve", help="start the read-only scanner dashboard")
    serve_command.add_argument("--output", type=Path, default=Path("output"))
    # Hosting platforms hand the port to the process rather than the operator.
    serve_command.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8765)))
    serve_command.add_argument(
        "--host", default="127.0.0.1",
        help="bind address; 0.0.0.0 exposes the dashboard beyond this machine "
             "and assumes a TLS-terminating proxy in front of it",
    )
    serve_command.add_argument("--live", action="store_true", help="also run the public-data refresh loop")
    _add_live_flags(serve_command)
    live_command = subparsers.add_parser("live", help="run the public-data refresh loop without a dashboard")
    live_command.add_argument("--output", type=Path, default=Path("output"))
    _add_live_flags(live_command)
    args = parser.parse_args()
    if args.command == "live":
        scanner = _build_live_scanner(args)
        scanner.start()
        print(f"Strength Tracker live loop writing to {args.output}. Ctrl-C to stop.")
        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            print("\nStopping live loop.")
        finally:
            scanner.stop()
    elif args.command == "serve":
        scanner = _build_live_scanner(args) if args.live else None
        if scanner is not None:
            scanner.start()
        try:
            serve(args.output, args.port, live_scanner=scanner, host=args.host)
        finally:
            if scanner is not None:
                scanner.stop()
    elif args.command == "scan":
        as_of, interval, assets = load_fixture(args.input)
        _write_scan(args.output, as_of, assets, interval, args.config)
    elif args.command == "backtest":
        _write_backtest(args)
    else:
        as_of, interval, assets = synthetic_demo_assets()
        _write_scan(args.output, as_of, assets, interval, args.config)


if __name__ == "__main__":
    main()
