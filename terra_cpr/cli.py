"""CLI for fixture scans and a synthetic plumbing demonstration."""
from __future__ import annotations

import argparse
from pathlib import Path

from .config import load_scanner_config
from .data import load_fixture, synthetic_demo_assets
from .report import render_html, scan_snapshot, write_json_atomic
from .scanner import ScannerConfig, scan_assets
from .signal_history import append_history, signal_transitions
from .dashboard import serve


def _write_scan(output: Path, as_of, assets, interval_seconds: int, config_path: Path | None) -> None:
    config = load_scanner_config(config_path, interval_seconds) if config_path else ScannerConfig(interval_seconds=interval_seconds)
    rows = scan_assets(assets, as_of, config)
    snapshot = scan_snapshot(as_of, rows)
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
    args = parser.parse_args()
    if args.command == "serve":
        serve(args.output, args.port)
    elif args.command == "scan":
        as_of, interval, assets = load_fixture(args.input)
        _write_scan(args.output, as_of, assets, interval, args.config)
    else:
        as_of, interval, assets = synthetic_demo_assets()
        _write_scan(args.output, as_of, assets, interval, args.config)


if __name__ == "__main__":
    main()
