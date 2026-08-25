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
from .research import (
    BROAD_ALT_FACTOR_MODEL,
    CURRENT_FACTOR_MODEL,
    FIVE_MODEL_COMPARISON,
    ResearchSpecification,
    chronological_split,
    evaluate,
    generate_point_in_time_event_suite,
)
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
    comparison_suite = bool(getattr(args, "comparison_suite", False))
    if comparison_suite:
        if getattr(args, "event_rule", None) or getattr(args, "factor_model", None):
            raise ValueError(
                "--comparison-suite cannot be combined with --event-rule or "
                "--factor-model"
            )
        specifications = FIVE_MODEL_COMPARISON
    else:
        event_rules = getattr(args, "event_rule", None) or ["confirmed_candidate"]
        factor_models = getattr(args, "factor_model", None) or [CURRENT_FACTOR_MODEL]
        specifications = tuple(
            ResearchSpecification(
                f"custom_{index}", f"{event_rule} with {factor_model}",
                event_rule, factor_model,
            )
            for index, (event_rule, factor_model) in enumerate(
                (
                    (event_rule, factor_model)
                    for factor_model in factor_models
                    for event_rule in event_rules
                ),
                start=1,
            )
        )
    results = []
    factor_models_in_order = tuple(dict.fromkeys(
        specification.factor_model for specification in specifications
    ))
    for factor_model in factor_models_in_order:
        model_specs = tuple(
            specification for specification in specifications
            if specification.factor_model == factor_model
        )
        event_rules = tuple(dict.fromkeys(
            specification.event_rule for specification in model_specs
        ))
        for horizon in horizons:
            events_by_rule = generate_point_in_time_event_suite(
                assets,
                config,
                horizon_bars=horizon,
                event_rules=event_rules,
                universe_size=args.universe,
                factor_model=factor_model,
            )
            for specification in model_specs:
                events = events_by_rule[specification.event_rule]
                train, test = chronological_split(events, args.train_fraction)
                results.append({
                    "spec_id": specification.spec_id,
                    "spec_label": specification.label,
                    "event_rule": specification.event_rule,
                    "factor_model": specification.factor_model,
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
        "schema_version": 3,
        "as_of": as_of.isoformat(),
        "bar_interval_seconds": interval,
        "model_version": "robust_ewma_empirical_discovery_v2",
        "round_trip_cost_bps": args.cost_bps,
        "train_fraction": args.train_fraction,
        "specifications": [asdict(specification) for specification in specifications],
        "comparison_contract": (
            {
                "control": "H1",
                "primary_selection_metric": "asset_return.test.expectancy",
                "required_diagnostics": [
                    "btc_beta_adjusted_return.test.expectancy",
                    "model_factor_adjusted_return.test.expectancy",
                    "event_count",
                ],
                "promotion_rule": (
                    "A challenger cannot replace H1 from this split. It must also pass "
                    "rolling out-of-sample folds and a final untouched holdout."
                ),
            }
            if comparison_suite else None
        ),
        "warning": (
            "Descriptive event study only. Promotion requires rolling folds and an "
            "untouched holdout; context inputs remain excluded from the score."
        ),
        "results": results,
    }
    write_json_atomic(args.output, report)
    counts = ", ".join(
        f"{result['spec_id']}:{result['horizon_bars']} bars={result['event_count']}"
        for result in results
    )
    print(f"wrote point-in-time research report to {args.output}; {counts}")


def require_writable(output: Path) -> None:
    """Refuse to start when the snapshot directory cannot be written.

    A denied write here is not survivable: the loop republishes on every tick,
    so it fails on all of them, and the page keeps serving the last good
    snapshot while looking merely stale. That has now cost real time twice --
    once when macOS revoked a detached process's grant to a protected
    directory, and it is the expected first failure on a container volume,
    which mounts root-owned while the image runs unprivileged. Name the
    directory and stop, rather than reporting it 40 seconds later as an opaque
    PermissionError from inside a thread.
    """
    probe = output / f".writable-probe-{os.getpid()}"
    try:
        output.mkdir(parents=True, exist_ok=True)
        probe.write_text("")
    except OSError as exc:
        raise SystemExit(
            f"cannot write to {output}: {exc}\n"
            "The scanner publishes its snapshot and signal history there, so every "
            "tick would fail. On a mounted volume this usually means the mount is "
            "owned by root while this process runs unprivileged."
        ) from exc
    finally:
        try:
            probe.unlink()
        except OSError:
            pass


def _build_live_scanner(args) -> LiveScanner:
    """Assemble a live scanner from CLI flags, with the config file as the base."""
    require_writable(args.output)
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
        "--event-rule", action="append",
        choices=(
            "confirmed_candidate", "early_discovery", "strong_discovery",
            "h5_discovery_structure", "residual_momentum_baseline",
        ),
        help="event rule to evaluate; repeat to compare predeclared rules",
    )
    backtest.add_argument(
        "--factor-model", action="append",
        choices=(CURRENT_FACTOR_MODEL, BROAD_ALT_FACTOR_MODEL),
        help="factor model to evaluate; repeat for a cross-model comparison",
    )
    backtest.add_argument(
        "--comparison-suite", action="store_true",
        help="run the frozen H1/D1/H5/B1/H6 five-model comparison",
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
