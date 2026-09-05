"""Offline 40/60-market performance baseline, using production model windows.

Synthetic inputs measure engineering cost, not market performance. The optional
replay probe is bounded to 300 closes; use --replay-bars for a longer run.
"""
import argparse
import json
import math
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from terra_cpr.data import synthetic_demo_assets
from terra_cpr.live import LiveConfig, LiveScanner
from terra_cpr.research import generate_point_in_time_events
from terra_cpr.scanner import ScannerConfig


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--replay-bars', type=int, default=300)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    now, _, demo = synthetic_demo_assets()
    results = []
    for size in (40, 60):
        panel = {s: demo[s] for s in ('BTC', 'ETH')}
        for index in range(size - 2):
            source = demo['SOL']
            bars = []
            for i, bar in enumerate(source.intraday):
                scale = 1 + 0.01 * math.sin(i / (5 + index) + index)
                bars.append(replace(bar, open=bar.open*scale, high=bar.high*scale, low=bar.low*scale, close=bar.close*scale))
            symbol = f'ALT{index:02d}'
            panel[symbol] = replace(source, symbol=symbol, intraday=bars, price=bars[-1].close)

        class Source:
            def fetch_meta_and_contexts(self):
                return {'universe': [{'name': s} for s in panel]}, [{'dayNtlVlm': '1000000'} for _ in panel]

            def fetch_mids(self):
                return {s: a.price for s, a in panel.items()}

            def fetch_candles(self, symbol, interval, start, end):
                return panel[symbol].daily if interval == '1d' else panel[symbol].intraday

        with tempfile.TemporaryDirectory() as directory:
            scanner = LiveScanner(Path(directory), Source(), live_config=LiveConfig(universe_size=size, min_gap_seconds=0), now=lambda: now)
            start = time.perf_counter()
            assert scanner.tick(now, True), scanner.status().last_error
            refresh_seconds = time.perf_counter() - start
            timings = []
            for _ in range(3):
                start = time.perf_counter()
                assert scanner.tick(now, False), scanner.status().last_error
                timings.append(time.perf_counter() - start)
            archive_bytes = sum(p.stat().st_size for p in Path(directory).glob('research_archive.sqlite3*'))
        start = time.perf_counter()
        history = {s: replace(a, intraday=a.intraday[-args.replay_bars:]) for s, a in panel.items()}
        events = generate_point_in_time_events(history, ScannerConfig(), 24, event_rule='strong_discovery')
        result = {'markets': size, 'completed_refresh_seconds': refresh_seconds,
                  'fast_tick_seconds': timings, 'initial_archive_bytes': archive_bytes,
                  'replay_bars': args.replay_bars, 'replay_seconds': time.perf_counter() - start,
                  'replay_events': len(events)}
        results.append(result)
        print(json.dumps(result), flush=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({'synthetic': True, 'python': sys.version, 'results': results}, indent=2))


if __name__ == '__main__':
    main()
