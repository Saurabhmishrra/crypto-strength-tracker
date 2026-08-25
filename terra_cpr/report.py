"""Atomic scanner snapshot and a dependency-free, read-only HTML report."""
from __future__ import annotations

import html
import json
import os
import statistics
import tempfile
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from .models import ScanRow
from .scanner import ScannerConfig


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"cannot serialise {type(value).__name__}")


def scan_snapshot(
    as_of: datetime, rows: Sequence[ScanRow], config: ScannerConfig = ScannerConfig()
) -> dict[str, Any]:
    payload_rows = []
    for row in rows:
        payload = asdict(row)
        for key in ("active_cpr", "previous_cpr"):
            cpr = payload["market"][key]
            cpr["bottom"] = min(cpr["bc"], cpr["tc"])
            cpr["top"] = max(cpr["bc"], cpr["tc"])
            cpr["width"] = cpr["top"] - cpr["bottom"]
        payload_rows.append(payload)
    usable_scores = [float(row.rs.score) for row in rows if row.rs.is_usable]
    discovery_scores = [
        float(row.rs.discovery_score) for row in rows
        if row.rs.is_usable and row.rs.discovery_score is not None
    ]
    confirmed = [
        row for row in rows
        if row.setup.label.endswith("CANDIDATE")
        and row.setup.confirmation == "CONFIRMED"
    ]
    provisional = [
        row for row in rows
        if row.setup.label.endswith("CANDIDATE")
        and row.setup.confirmation == "PROVISIONAL"
    ]
    breadth = {
        "usable_assets": len(usable_scores),
        "positive_rs_ratio": (
            sum(score > 0 for score in usable_scores) / len(usable_scores)
            if usable_scores else None
        ),
        "strong_rs_ratio": (
            sum(abs(score) >= config.candidate_rs_score for score in usable_scores)
            / len(usable_scores)
            if usable_scores else None
        ),
        "median_rs_score": statistics.median(usable_scores) if usable_scores else None,
        "confirmed_candidates": len(confirmed),
        "provisional_candidates": len(provisional),
        "early_discoveries": sum(
            row.setup.discovery_tier == "EARLY_DISCOVERY" for row in rows
        ),
        "strong_discoveries": sum(
            row.setup.discovery_tier == "STRONG_DISCOVERY" for row in rows
        ),
        "median_discovery_score": (
            statistics.median(discovery_scores) if discovery_scores else None
        ),
    }
    # The thresholds travel with the labels they produced. A snapshot that records
    # WATCH without recording the rule that made it WATCH cannot be audited later,
    # and a reader has no way to draw the gate it missed.
    return {
        "schema_version": 4,
        "as_of": as_of.isoformat(),
        "gates": {
            "candidate_rs_score": config.candidate_rs_score,
            "candidate_persistence": config.candidate_persistence,
            "early_discovery_score": config.early_discovery_score,
            "strong_discovery_score": config.strong_discovery_score,
            "bar_interval_seconds": config.interval_seconds,
            "short_horizon_seconds": (
                config.rs.short_horizon_bars * config.interval_seconds
            ),
            "medium_horizon_seconds": (
                config.rs.medium_horizon_bars * config.interval_seconds
            ),
            "long_horizon_seconds": (
                config.rs.long_horizon_bars * config.interval_seconds
            ),
        },
        "model_version": (
            rows[0].rs.model_version if rows else "robust_ewma_empirical_discovery_v2"
        ),
        "breadth": breadth,
        "rows": payload_rows,
    }


def write_json_atomic(path: Path, payload: Any) -> None:
    """Publish a snapshot in one indivisible step, leaving nothing behind.

    The temporary is unique per write and is removed when the rename fails.
    A single fixed ``<name>.tmp`` reused across writes is a trap: on macOS the
    abandoned file keeps the ``com.apple.macl`` tag TCC stamped on it, so every
    later ``os.replace`` onto the real snapshot is denied as well, and one
    transient permission error becomes a permanently frozen dashboard that
    survives a process restart. A unique name also stops two writers from
    clobbering each other's half-written file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w") as stream:
            stream.write(json.dumps(payload, indent=2, default=_json_default, sort_keys=True))
        # mkstemp creates 0600 and os.replace preserves the source mode, which
        # would silently tighten the published snapshot. Restore the ordinary
        # mode a plain write would have produced.
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        # The cleanup runs on the failure path, where the filesystem is often
        # exactly what is refusing us. A denied unlink must not replace the
        # error that explains why the write failed.
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _number(value: Any, digits: int = 2) -> str:
    return "—" if value is None else f"{float(value):.{digits}f}"


def render_html(snapshot: dict[str, Any]) -> str:
    """A static scanner report; no control endpoints and no external scripts."""
    rows = snapshot["rows"]
    table_rows = []
    for row in rows:
        market, rs, setup = row["market"], row["rs"], row["setup"]
        score = rs.get("score")
        score_class = "positive" if score is not None and score > 0 else "negative" if score is not None and score < 0 else "muted"
        reasons = "; ".join(setup.get("reasons") or setup.get("blockers") or [])
        table_rows.append(
            "<tr>"
            f"<td><strong>{html.escape(row['symbol'])}</strong></td>"
            f"<td class='{score_class}'>{_number(score)}</td>"
            f"<td>{html.escape(market['price_cpr_position'].replace('_', ' '))}</td>"
            f"<td>{html.escape(market['cpr_regime'])}</td>"
            f"<td>{html.escape(market['pivot_position'].replace('_', ' '))}</td>"
            f"<td>{html.escape(setup['label'].replace('_', ' '))}</td>"
            f"<td>{html.escape(setup.get('confirmation', 'NONE').lower())}</td>"
            f"<td>{html.escape(reasons)}</td>"
            "</tr>"
        )
    generated = html.escape(snapshot["as_of"])
    return f"""<!doctype html>
<html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Strength Tracker</title><style>
body{{background:#0b1020;color:#e5e7eb;font:14px system-ui,sans-serif;margin:0;padding:28px}} h1{{margin:0 0 6px}} p{{color:#94a3b8}} table{{width:100%;border-collapse:collapse;margin-top:22px;background:#111827}} th,td{{padding:11px;border-bottom:1px solid #243044;text-align:left}} th{{color:#93c5fd;font-size:12px;text-transform:uppercase}} .positive{{color:#4ade80;font-weight:700}} .negative{{color:#fb7185;font-weight:700}} .muted{{color:#94a3b8}}
</style></head><body><h1>Strength Tracker</h1><p>CPR + beta-adjusted relative strength. As of {generated}. Research labels only — never order instructions.</p>
<table><thead><tr><th>Asset</th><th>RS score</th><th>CPR position</th><th>CPR width</th><th>Pivot position</th><th>Setup</th><th>Confirmation</th><th>Why / blocker</th></tr></thead>
<tbody>{''.join(table_rows)}</tbody></table></body></html>"""
