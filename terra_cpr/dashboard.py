"""Loopback-only, read-only dashboard for Terra CPR scanner snapshots."""
from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .signal_history import read_history


def _read_snapshot(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def dashboard_html() -> str:
    """Single-file dashboard; all data comes from same-origin read-only APIs."""
    return r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Terra CPR Scanner</title><style>
:root{--bg:#080d18;--panel:#111a2c;--line:#24334d;--text:#e8edf7;--muted:#9aabc4;--green:#4ade80;--red:#fb7185;--amber:#fbbf24;--blue:#7dd3fc}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px Inter,ui-sans-serif,system-ui,sans-serif}main{max-width:1600px;margin:auto;padding:24px}header{display:flex;justify-content:space-between;gap:18px;align-items:flex-start;margin-bottom:20px}h1{font-size:25px;margin:0 0 5px}.sub{color:var(--muted);margin:0}.badge{font-size:12px;border:1px solid var(--line);border-radius:999px;padding:6px 9px;color:var(--blue);white-space:nowrap}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:16px 0}.card,.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px}.card{padding:14px}.label{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.06em}.value{font-size:25px;font-weight:700;margin-top:5px}.layout{display:grid;grid-template-columns:1.7fr 1fr;gap:14px}.panel{padding:16px;overflow:auto}.panel h2{font-size:15px;margin:0 0 12px}.signal{border-left:3px solid var(--line);padding:10px 12px;margin:8px 0;background:#0c1424}.long{border-left-color:var(--green)}.short{border-left-color:var(--red)}.watch{border-left-color:var(--amber)}.muted{color:var(--muted)}.green{color:var(--green)}.red{color:var(--red)}.amber{color:var(--amber)}button{background:#17243c;border:1px solid var(--line);border-radius:6px;color:var(--text);padding:7px 10px;cursor:pointer}button.active{background:#1f4564;border-color:var(--blue)}button:hover{border-color:var(--blue)}.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px}input{background:#0c1424;border:1px solid var(--line);color:var(--text);padding:7px 9px;border-radius:6px;min-width:180px}table{width:100%;border-collapse:collapse}th,td{padding:10px 8px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}th{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}tr.data-row{cursor:pointer}tr.data-row:hover{background:#17243c}.detail{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.kv{background:#0c1424;padding:9px;border-radius:6px}.kv b{display:block;font-size:12px;color:var(--muted);font-weight:500;margin-bottom:3px}.reason{margin:5px 0;padding:7px 8px;background:#0c1424;border-radius:5px}.feed-item{padding:8px 0;border-bottom:1px solid var(--line)}.feed-item:last-child{border:0}@media(max-width:1000px){.grid{grid-template-columns:repeat(2,1fr)}.layout{grid-template-columns:1fr}}@media(max-width:560px){main{padding:14px}.grid{grid-template-columns:1fr}header{display:block}.badge{display:inline-block;margin-top:8px}th:nth-child(4),td:nth-child(4),th:nth-child(5),td:nth-child(5){display:none}}
</style></head><body><main>
<header><div><h1>Terra CPR scanner</h1><p class="sub">Market structure + persistent beta-adjusted RS. Research signals only — never orders.</p></div><div id="health" class="badge">Loading snapshot…</div></header>
<section class="grid"><div class="card"><div class="label">Assets scanned</div><div class="value" id="count">—</div></div><div class="card"><div class="label">Long candidates</div><div class="value green" id="longs">—</div></div><div class="card"><div class="label">Short candidates</div><div class="value red" id="shorts">—</div></div><div class="card"><div class="label">Data flags</div><div class="value amber" id="issues">—</div></div></section>
<section class="layout"><div class="panel"><h2>Active signal queue</h2><div id="queue" class="muted">No snapshot loaded.</div></div><div class="panel"><h2>RS leaders</h2><div id="leaders" class="muted">No snapshot loaded.</div><h2 style="margin-top:18px">Recent signal transitions</h2><div id="feed" class="muted">No signal history yet.</div></div></section>
<section class="layout" style="margin-top:14px"><div class="panel"><h2>Market map</h2><div class="toolbar"><input id="search" placeholder="Filter asset / setup"><button data-filter="ALL" class="active">All</button><button data-filter="LONG_CANDIDATE">Longs</button><button data-filter="SHORT_CANDIDATE">Shorts</button><button data-filter="WATCH">Watch</button></div><table><thead><tr><th>Asset</th><th>RS</th><th>Persist.</th><th>CPR</th><th>Width</th><th>Pivot</th><th>Setup</th></tr></thead><tbody id="map"></tbody></table></div><div class="panel"><h2 id="detail-title">Asset detail</h2><div id="detail" class="muted">Click an asset to inspect the calculation and its exact reasons.</div></div></section>
</main><script>
let snapshot=null, activeFilter='ALL', selected=null;
const $=id=>document.getElementById(id); const n=(v,d=2)=>v==null?'—':Number(v).toFixed(d); const clean=s=>(s||'').replaceAll('_',' ');
function esc(v){const d=document.createElement('div');d.textContent=v??'';return d.innerHTML}
function rowClass(label){return label==='LONG_CANDIDATE'?'long':label==='SHORT_CANDIDATE'?'short':label==='WATCH'?'watch':''}
function render(){if(!snapshot)return;const rows=snapshot.rows||[]; const longs=rows.filter(r=>r.setup.label==='LONG_CANDIDATE'), shorts=rows.filter(r=>r.setup.label==='SHORT_CANDIDATE'), issues=rows.filter(r=>(r.rs.quality_flags||[]).length).length;
$('health').textContent='Snapshot '+new Date(snapshot.as_of).toLocaleString();$('count').textContent=rows.length;$('longs').textContent=longs.length;$('shorts').textContent=shorts.length;$('issues').textContent=issues;
const signals=[...longs,...shorts];$('queue').innerHTML=signals.length?signals.map(r=>`<div class="signal ${rowClass(r.setup.label)}"><b>${esc(r.symbol)} · ${clean(r.setup.label)}</b> <span class="muted">RS ${n(r.rs.score)} · strength ${n(r.setup.strength,0)}</span><div class="muted">${esc((r.setup.reasons||[]).join(' · '))}</div></div>`).join(''):'<span class="muted">No active candidate. WATCH labels remain visible in the market map.</span>';
const ranked=rows.filter(r=>r.rs.score!=null).sort((a,b)=>b.rs.score-a.rs.score), top=ranked.slice(0,3), bottom=ranked.slice(-3).reverse();$('leaders').innerHTML=`<div class="detail"><div><div class="label">Strongest</div>${top.map(r=>`<div class="reason green"><b>${esc(r.symbol)}</b> · ${n(r.rs.score)} <span class="muted">${clean(r.market.price_cpr_position)}</span></div>`).join('')||'—'}</div><div><div class="label">Weakest</div>${bottom.map(r=>`<div class="reason red"><b>${esc(r.symbol)}</b> · ${n(r.rs.score)} <span class="muted">${clean(r.market.price_cpr_position)}</span></div>`).join('')||'—'}</div></div>`;
const query=$('search').value.toLowerCase();const filtered=rows.filter(r=>(activeFilter==='ALL'||r.setup.label===activeFilter)&&(`${r.symbol} ${r.setup.label}`).toLowerCase().includes(query));$('map').innerHTML=filtered.map(r=>`<tr class="data-row" data-symbol="${esc(r.symbol)}"><td><b>${esc(r.symbol)}</b><br><span class="muted">#${r.strong_rank??'—'}</span></td><td class="${r.rs.score>0?'green':r.rs.score<0?'red':''}">${n(r.rs.score)}</td><td>${n(r.rs.persistence)}</td><td>${clean(r.market.price_cpr_position)}</td><td>${esc(r.market.cpr_regime)}</td><td>${clean(r.market.pivot_position)}</td><td class="${rowClass(r.setup.label)}">${clean(r.setup.label)}</td></tr>`).join('')||'<tr><td colspan="7" class="muted">No matching assets.</td></tr>';
document.querySelectorAll('[data-symbol]').forEach(el=>el.onclick=()=>show(el.dataset.symbol)); if(selected)show(selected);
}
function show(symbol){selected=symbol;const r=(snapshot?.rows||[]).find(x=>x.symbol===symbol);if(!r)return;const c=r.market.active_cpr,p=r.market.pivots,b=r.rs;$('detail-title').textContent=symbol+' detail';const reasons=(r.setup.reasons||[]).map(x=>`<div class="reason green">${esc(x)}</div>`).join('');const blockers=(r.setup.blockers||[]).map(x=>`<div class="reason amber">${esc(x)}</div>`).join('');$('detail').innerHTML=`<div class="detail"><div class="kv"><b>Setup</b>${clean(r.setup.label)} (${n(r.setup.strength,0)})</div><div class="kv"><b>RS score / persistence</b>${n(b.score)} / ${n(b.persistence)}</div><div class="kv"><b>Beta / R²</b>${n(b.beta?.beta)} / ${n(b.beta?.r_squared)}</div><div class="kv"><b>RS horizons (S/M/L)</b>${n(b.horizon_z?.short)} / ${n(b.horizon_z?.medium)} / ${n(b.horizon_z?.long)}</div><div class="kv"><b>CPR BC / P / TC</b>${n(c.bottom)} / ${n(c.pivot)} / ${n(c.top)}</div><div class="kv"><b>Pivot S1 / R1</b>${n(p.s1)} / ${n(p.r1)}</div><div class="kv"><b>ATR / realised vol</b>${n(r.market.atr)} / ${n(r.market.realized_volatility)}</div><div class="kv"><b>Width percentile</b>${r.market.cpr_width_percentile==null?'—':n(100*r.market.cpr_width_percentile,0)+'%'}</div></div><h3>Why it is labelled this way</h3>${reasons||blockers||'<div class="muted">No active condition.</div>'}<h3>Data quality</h3><div class="muted">${esc((b.quality_flags||[]).join(', ')||'No flags')}</div>`;}
function renderFeed(events){$('feed').innerHTML=events.length?events.map(e=>{const state=e.current||e.previous||{};return `<div class="feed-item"><b>${esc(e.symbol)} · ${esc(e.event)}</b> <span class="${state.direction==='LONG'?'green':'red'}">${clean(state.label||'')}</span><br><span class="muted">${new Date(e.timestamp).toLocaleString()} · RS ${n(state.score)}</span></div>`}).join(''):'<span class="muted">No candidate transition has been recorded yet.</span>';}
async function refresh(){try{const [s,h]=await Promise.all([fetch('/api/snapshot'),fetch('/api/signals?limit=50')]);if(!s.ok)throw new Error('snapshot unavailable');snapshot=await s.json();render();renderFeed(h.ok?await h.json():[])}catch(e){$('health').textContent='Awaiting a scanner snapshot';}}
$('search').oninput=render;document.querySelectorAll('[data-filter]').forEach(b=>b.onclick=()=>{activeFilter=b.dataset.filter;document.querySelectorAll('[data-filter]').forEach(x=>x.classList.toggle('active',x===b));render()});refresh();setInterval(refresh,15000);
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    output_dir: Path

    def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802 - required handler method
        request = urlparse(self.path)
        if request.path == "/":
            self._html(dashboard_html())
            return
        if request.path == "/api/snapshot":
            snapshot = _read_snapshot(self.output_dir / "scanner_latest.json")
            if snapshot is None:
                self._json({"error": "No scanner snapshot has been generated yet."}, HTTPStatus.SERVICE_UNAVAILABLE)
            else:
                self._json(snapshot)
            return
        if request.path == "/api/signals":
            raw_limit = parse_qs(request.query).get("limit", ["50"])[0]
            try:
                limit = max(1, min(int(raw_limit), 500))
            except ValueError:
                limit = 50
            self._json(read_history(self.output_dir / "signal_history.jsonl", limit))
            return
        if request.path == "/health":
            self._json({"ok": _read_snapshot(self.output_dir / "scanner_latest.json") is not None})
            return
        self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802 - required handler method
        self._json({"error": "This dashboard is read-only."}, HTTPStatus.METHOD_NOT_ALLOWED)

    do_PUT = do_POST
    do_DELETE = do_POST

    def log_message(self, *_: Any) -> None:
        """Avoid request noise; this is a local visualisation, not an audit log."""


def serve(output_dir: Path, port: int = 8765) -> None:
    """Serve only on loopback. Do not add a non-local bind option here."""
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    handler = type("ScannerDashboardHandler", (_Handler,), {"output_dir": output_dir})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    print(f"Terra CPR dashboard: http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        server.server_close()
