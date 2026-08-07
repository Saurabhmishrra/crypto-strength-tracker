"""Atomic scanner snapshot and a dependency-free, read-only HTML report."""
from __future__ import annotations

import html
import json
import os
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from .models import ScanRow


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"cannot serialise {type(value).__name__}")


def scan_snapshot(as_of: datetime, rows: Sequence[ScanRow]) -> dict[str, Any]:
    payload_rows = []
    for row in rows:
        payload = asdict(row)
        for key in ("active_cpr", "previous_cpr"):
            cpr = payload["market"][key]
            cpr["bottom"] = min(cpr["bc"], cpr["tc"])
            cpr["top"] = max(cpr["bc"], cpr["tc"])
            cpr["width"] = cpr["top"] - cpr["bottom"]
        payload_rows.append(payload)
    return {"schema_version": 1, "as_of": as_of.isoformat(), "rows": payload_rows}


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, default=_json_default, sort_keys=True))
    os.replace(temporary, path)


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
            f"<td>{html.escape(reasons)}</td>"
            "</tr>"
        )
    generated = html.escape(snapshot["as_of"])
    return f"""<!doctype html>
<html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Terra CPR Scanner</title><style>
body{{background:#0b1020;color:#e5e7eb;font:14px system-ui,sans-serif;margin:0;padding:28px}} h1{{margin:0 0 6px}} p{{color:#94a3b8}} table{{width:100%;border-collapse:collapse;margin-top:22px;background:#111827}} th,td{{padding:11px;border-bottom:1px solid #243044;text-align:left}} th{{color:#93c5fd;font-size:12px;text-transform:uppercase}} .positive{{color:#4ade80;font-weight:700}} .negative{{color:#fb7185;font-weight:700}} .muted{{color:#94a3b8}}
</style></head><body><h1>Terra CPR market scanner</h1><p>As of {generated}. Research labels only — never order instructions.</p>
<table><thead><tr><th>Asset</th><th>RS score</th><th>CPR position</th><th>CPR width</th><th>Pivot position</th><th>Setup</th><th>Why / blocker</th></tr></thead>
<tbody>{''.join(table_rows)}</tbody></table></body></html>"""
