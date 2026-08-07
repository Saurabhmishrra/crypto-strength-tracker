"""CLI for fixture scans and a synthetic plumbing demonstration."""
from __future__ import annotations

import argparse
import time
from dataclasses import replace
from pathlib import Path

from .config import load_live_configs, load_scanner_config
from .data import HyperliquidPublicData, load_fixture, synthetic_demo_assets
from .live import LiveConfig, LiveScanner
from .report import render_html, scan_snapshot, write_json_atomic
from .scanner import ScannerConfig, scan_assets
from .signal_history import append_history, signal_transitions
from .dashboard import serve


def _write_scan(output: Path, as_of, assets, interval_seconds: int, config_path: Path | None) -> None:
    config = load_scanner_config(config_path, interval_seconds) if config_path else ScannerConfig(interval_seconds=interval_seconds)
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
    candidates = [row for row in rows if row.setup.label.endswith("CANDIDATE")]
    print(f"wrote {len(rows)} assets to {output}; research candidates={len(candidates)}; transitions={len(events)}")


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
    parser = argparse.ArgumentParser(description="Terra CPR research scanner (no execution capability)")
    subparsers = parser.add_subparsers(dest="command", required=True)
    scan = subparsers.add_parser("scan", help="scan a deterministic JSON candle fixture")
    scan.add_argument("--input", type=Path, required=True)
    scan.add_argument("--output", type=Path, default=Path("output"))
    scan.add_argument("--config", type=Path, help="strict TOML scanner configuration")
    demo = subparsers.add_parser("demo", help="run deterministic synthetic plumbing demo")
    demo.add_argument("--output", type=Path, default=Path("output"))
    demo.add_argument("--config", type=Path, help="strict TOML scanner configuration")
    serve_command = subparsers.add_parser("serve", help="start the loopback-only, read-only scanner dashboard")
    serve_command.add_argument("--output", type=Path, default=Path("output"))
    serve_command.add_argument("--port", type=int, default=8765)
    serve_command.add_argument("--live", action="store_true", help="also run the public-data refresh loop")
    _add_live_flags(serve_command)
    live_command = subparsers.add_parser("live", help="run the public-data refresh loop without a dashboard")
    live_command.add_argument("--output", type=Path, default=Path("output"))
    _add_live_flags(live_command)
    args = parser.parse_args()
    if args.command == "live":
        scanner = _build_live_scanner(args)
        scanner.start()
        print(f"Terra CPR live loop writing to {args.output}. Ctrl-C to stop.")
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
            serve(args.output, args.port, live_scanner=scanner)
        finally:
            if scanner is not None:
                scanner.stop()
    elif args.command == "scan":
        as_of, interval, assets = load_fixture(args.input)
        _write_scan(args.output, as_of, assets, interval, args.config)
    else:
        as_of, interval, assets = synthetic_demo_assets()
        _write_scan(args.output, as_of, assets, interval, args.config)


if __name__ == "__main__":
    main()
