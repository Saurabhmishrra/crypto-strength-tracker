"""Loopback-only, read-only dashboard for Strength Tracker snapshots."""
from __future__ import annotations

import json
from dataclasses import asdict
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
    """Single-file cockpit; all data comes from same-origin read-only APIs.

    The visual grammar is one idea repeated at every scale: a *gate*. Relative
    strength, persistence, and beta quality each render as a track with its
    threshold marked, and the CPR ladder is the same primitive stood upright with
    real geometry. Nothing here recomputes a label — every value is read from the
    snapshot the scanner wrote.
    """
    return r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Strength Tracker</title><style>
:root{
  --void:#08090C; --panel:#0F1116; --panel2:#14171E; --rail:#1E222B; --rail2:#2A2F3A;
  --ink:#E6E8EE; --dim:#838A9B; --faint:#565D6E;
  --structure:#6BE3D4; --long:#45C08A; --short:#F2615C; --blocked:#E0A93B;
  --mono:ui-monospace,"SF Mono",SFMono-Regular,Menlo,Consolas,monospace;
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{
  background:var(--void); color:var(--ink);
  font:12px/1.45 var(--mono); font-variant-numeric:tabular-nums;
  -webkit-font-smoothing:antialiased;
}
body.degraded{filter:saturate(.18) brightness(.86)}
body.degraded .rail{filter:none}
main{max-width:1560px;margin:0 auto;padding:0 18px 72px}

/* ---- health rail ---- */
.rail{
  position:sticky; top:0; z-index:40; display:flex; flex-wrap:wrap; gap:0;
  align-items:stretch; background:var(--panel); border-bottom:1px solid var(--rail);
  margin-bottom:18px;
}
.rail-in{max-width:1560px;margin:0 auto;padding:0 18px;display:flex;flex-wrap:wrap;align-items:stretch;width:100%}
.brand{display:flex;flex-direction:column;justify-content:center;padding:11px 22px 11px 0;margin-right:22px;border-right:1px solid var(--rail)}
.brand b{font-size:14px;font-weight:600;letter-spacing:.16em;text-transform:uppercase}
.brand span{color:var(--faint);font-size:10px;letter-spacing:.1em;text-transform:uppercase}
.stat{display:flex;flex-direction:column;justify-content:center;padding:11px 20px 11px 0;margin-right:20px;min-width:76px}
.stat u{font-style:normal;text-decoration:none;color:var(--faint);font-size:9.5px;letter-spacing:.13em;text-transform:uppercase}
.stat b{font-size:15px;font-weight:600;margin-top:2px}
.loop{display:flex;align-items:center;gap:7px}
.dot{width:7px;height:7px;border-radius:50%;background:var(--faint);flex:none}
.dot.live{background:var(--long);box-shadow:0 0 0 3px rgba(69,192,138,.16)}
.dot.failing{background:var(--short);box-shadow:0 0 0 3px rgba(242,97,92,.16)}
.dot.off{background:var(--faint)}
.dot.unreachable{background:var(--short);box-shadow:0 0 0 3px rgba(242,97,92,.16)}
.rail .warn{color:var(--blocked)}
.fault{
  width:100%;background:#2A1416;border-top:1px solid var(--short);
  color:#FFC9C6;font-size:11.5px;padding:9px 18px;letter-spacing:.01em;
}
body.degraded .fault{filter:none}

/* ---- panels ---- */
.board{display:grid;grid-template-columns:minmax(0,1.55fr) minmax(300px,1fr);gap:14px;margin-bottom:14px}
.panel{background:var(--panel);border:1px solid var(--rail);border-radius:3px;padding:14px 16px 16px}
h2{margin:0 0 2px;font-size:11px;font-weight:600;letter-spacing:.15em;text-transform:uppercase;color:var(--ink)}
.hint{margin:0 0 14px;color:var(--faint);font-size:10.5px;letter-spacing:.02em}
.prose{font-family:var(--sans);letter-spacing:0}

/* ---- gate funnel ---- */
.funnel{display:grid;gap:9px;margin-top:4px}
.gate{display:grid;grid-template-columns:82px 1fr 40px;gap:10px;align-items:center}
.gate u{font-style:normal;text-decoration:none;color:var(--dim);font-size:10px;letter-spacing:.08em;text-transform:uppercase}
.gate .bar{height:6px;background:var(--rail);border-radius:1px;overflow:hidden}
.gate .bar i{display:block;height:100%;background:var(--structure);opacity:.75}
.gate .bar i.bind{background:var(--blocked)}
.gate b{text-align:right;font-size:12px;font-weight:600}
.binding{margin-top:11px;padding-top:10px;border-top:1px solid var(--rail);color:var(--dim);font-size:10.5px}
.binding em{font-style:normal;color:var(--blocked)}

/* ---- queue ---- */
.card{border:1px solid var(--rail);border-left-width:2px;border-radius:2px;padding:9px 11px;margin-bottom:8px;background:var(--panel2)}
.card.LONG{border-left-color:var(--long)} .card.SHORT{border-left-color:var(--short)}
.card-h{display:flex;justify-content:space-between;align-items:baseline;gap:10px}
.card-h b{font-size:14px;letter-spacing:.04em}
.tag{font-size:9.5px;letter-spacing:.1em;text-transform:uppercase;padding:2px 5px;border-radius:2px;border:1px solid currentColor}
.long{color:var(--long)} .short{color:var(--short)} .blocked{color:var(--blocked)} .dimmed{color:var(--dim)}
.why{margin:6px 0 0;padding:0;list-style:none;color:var(--dim);font-size:11px}
.why li{padding-left:11px;position:relative}
.why li:before{content:"";position:absolute;left:0;top:6px;width:4px;height:1px;background:var(--structure)}
.why li.block:before{background:var(--blocked)}
.empty{color:var(--faint);font-size:11px;padding:10px 0}

/* ---- feed ---- */
.feed{max-height:216px;overflow-y:auto;margin-top:6px}
.ev{display:grid;grid-template-columns:1fr auto;gap:8px;padding:7px 0;border-bottom:1px solid var(--rail);font-size:11px}
.ev:last-child{border-bottom:0}
.ev time{color:var(--faint);font-size:10px}

/* ---- universe table ---- */
.tools{display:flex;gap:7px;align-items:center;flex-wrap:wrap;margin-bottom:11px}
input[type=search]{background:var(--void);border:1px solid var(--rail);color:var(--ink);padding:6px 9px;border-radius:2px;font:11px var(--mono);min-width:150px}
input[type=search]:focus-visible,button:focus-visible,tr:focus-visible{outline:2px solid var(--structure);outline-offset:1px}
button{background:transparent;border:1px solid var(--rail);color:var(--dim);padding:6px 10px;border-radius:2px;cursor:pointer;font:10px var(--mono);letter-spacing:.09em;text-transform:uppercase}
button:hover{border-color:var(--rail2);color:var(--ink)}
button[aria-pressed=true]{border-color:var(--structure);color:var(--structure)}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;min-width:920px}
th{text-align:left;font-size:9.5px;letter-spacing:.12em;text-transform:uppercase;color:var(--faint);font-weight:500;padding:0 10px 8px 0;white-space:nowrap}
th.sortable{cursor:pointer} th.sortable:hover{color:var(--dim)}
th[aria-sort] {color:var(--structure)}
td{padding:7px 10px 7px 0;border-top:1px solid var(--rail);vertical-align:middle;white-space:nowrap}
tbody tr{cursor:pointer}
tbody tr:hover{background:var(--panel2)}
tbody tr[aria-selected=true]{background:var(--panel2);box-shadow:inset 2px 0 0 var(--structure)}
.sym{font-size:13px;font-weight:600;letter-spacing:.03em}
.rank{color:var(--faint);font-size:10px}
.num{text-align:right}

/* ---- drawer ---- */
.drawer{position:fixed;inset:0 0 0 auto;width:min(620px,100%);background:var(--panel);border-left:1px solid var(--rail2);z-index:60;overflow-y:auto;padding:18px 20px 60px;transform:translateX(100%);transition:transform .22s ease}
.drawer[data-open=true]{transform:none}
.scrim{position:fixed;inset:0;background:rgba(4,5,7,.62);z-index:50;opacity:0;pointer-events:none;transition:opacity .22s ease}
.scrim[data-open=true]{opacity:1;pointer-events:auto}
.drawer-h{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:16px}
.drawer-h h3{margin:0;font-size:22px;letter-spacing:.05em;font-weight:600}
.close{border:1px solid var(--rail);background:transparent;color:var(--dim);width:28px;height:28px;border-radius:2px;font-size:13px;padding:0;cursor:pointer}
.dgrid{display:grid;grid-template-columns:300px minmax(0,1fr);gap:18px}
.kv{display:grid;grid-template-columns:1fr auto;gap:8px;padding:5px 0;border-bottom:1px solid var(--rail);font-size:11px}
.kv:last-child{border-bottom:0}
.kv span{color:var(--dim)}
.sub{margin:18px 0 8px;font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--faint)}
.flag{display:inline-block;border:1px solid var(--blocked);color:var(--blocked);padding:2px 6px;border-radius:2px;font-size:10px;margin:0 5px 5px 0}

/* ---- chart bits ---- */
.legend{display:flex;flex-wrap:wrap;gap:12px;margin-top:10px;font-size:10px;color:var(--dim)}
.legend i{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px;vertical-align:-1px}
.legend .hollow{background:transparent;border:1.5px solid var(--dim)}
figure{margin:0}
svg{display:block;max-width:100%;height:auto;overflow:visible}
.tip{position:fixed;z-index:80;background:var(--panel2);border:1px solid var(--rail2);border-radius:2px;padding:7px 9px;font-size:10.5px;pointer-events:none;opacity:0;transition:opacity .1s;box-shadow:0 6px 22px rgba(0,0,0,.55);white-space:nowrap}
.tip[data-show=true]{opacity:1}
.tip b{font-size:12px;letter-spacing:.04em}
.tip div{color:var(--dim);margin-top:2px}

@media(max-width:1080px){.board{grid-template-columns:1fr}.dgrid{grid-template-columns:1fr}}
@media(prefers-reduced-motion:reduce){*{transition:none!important}}
</style></head><body>
<header class="rail"><div class="rail-in">
  <div class="brand"><b>Strength Tracker</b><span>CPR + beta-adjusted RS</span></div>
  <div class="stat"><u>Feed</u><b class="loop"><i class="dot off" id="loopDot"></i><span id="loopText">—</span></b></div>
  <div class="stat"><u>Snapshot</u><b id="age">—</b></div>
  <div class="stat"><u>Universe</u><b id="count">—</b></div>
  <div class="stat"><u>Confirmed / provisional</u><b id="cands">—</b></div>
  <div class="stat"><u>Quality flags</u><b id="flags">—</b></div>
  <div class="stat"><u>Next candles</u><b id="nextRefresh">—</b></div>
</div><div class="fault" id="fault" role="alert" hidden></div></header>

<main>
  <section class="board">
    <div class="panel">
      <h2>Cross-section</h2>
      <p class="hint">Relative strength against current price location. Shaded corners are the candidate regions; a hollow dot has not cleared the persistence gate.</p>
      <figure><div id="map"></div></figure>
      <div class="legend">
        <span><i style="background:var(--long)"></i>Long candidate</span>
        <span><i style="background:var(--short)"></i>Short candidate</span>
        <span><i style="background:var(--blocked)"></i>Watch</span>
        <span><i class="hollow"></i>Persistence gate not cleared</span>
        <span>Faded = weak beta fit</span>
      </div>
    </div>
    <div class="panel">
      <h2>Gates</h2>
      <p class="hint">How many assets clear each condition on its own.</p>
      <div class="funnel" id="funnel"></div>
      <p class="binding" id="binding"></p>
      <h2 style="margin-top:22px">Signal queue</h2>
      <p class="hint">Only completed-bar confirmations enter alerts and history.</p>
      <div id="queue"></div>
      <h2 style="margin-top:18px">Transitions</h2>
      <p class="hint">Activated, changed, or cleared — never a refresh.</p>
      <div class="feed" id="feed"></div>
    </div>
  </section>

  <section class="panel">
    <h2>Universe</h2>
    <p class="hint">Select an asset to inspect its full calculation.</p>
    <div class="tools">
      <input type="search" id="q" placeholder="Filter symbol" aria-label="Filter by symbol">
      <button data-filter="ALL" aria-pressed="true">All</button>
      <button data-filter="LONG_CANDIDATE" aria-pressed="false">Long</button>
      <button data-filter="SHORT_CANDIDATE" aria-pressed="false">Short</button>
      <button data-filter="CONFIRMED" aria-pressed="false">Confirmed</button>
      <button data-filter="PROVISIONAL" aria-pressed="false">Provisional</button>
      <button data-filter="WATCH" aria-pressed="false">Watch</button>
      <button data-filter="FLAGGED" aria-pressed="false">Flagged</button>
    </div>
    <div class="scroll"><table>
      <thead><tr>
        <th class="sortable" data-sort="symbol">Asset</th>
        <th class="sortable" data-sort="structure">Structure &plusmn;1.5 ATR</th>
        <th class="sortable" data-sort="score">Relative strength</th>
        <th class="sortable" data-sort="persistence">Persistence</th>
        <th class="sortable" data-sort="width">CPR width</th>
        <th>Setup</th>
      </tr></thead>
      <tbody id="rows"></tbody>
    </table></div>
  </section>
</main>

<div class="scrim" id="scrim"></div>
<aside class="drawer" id="drawer" role="dialog" aria-modal="true" aria-labelledby="dTitle" data-open="false"></aside>
<div class="tip" id="tip"></div>

<script>
const $ = id => document.getElementById(id);
let snap = null, status = null, events = [], filter = 'ALL', sort = 'score', dir = -1, selected = null;
let reachable = true, unreachableWhy = '';

/* ---------- formatting ---------- */
const esc = v => { const d = document.createElement('div'); d.textContent = v == null ? '' : v; return d.innerHTML; };
const n = (v, d = 2) => v == null || Number.isNaN(v) ? '&mdash;' : Number(v).toFixed(d);
const words = s => (s || '').replace(/_/g, ' ');
function price(v){
  if (v == null) return '—';
  const a = Math.abs(v);
  return v.toFixed(a >= 1000 ? 1 : a >= 1 ? 3 : a >= 0.01 ? 5 : 7);
}
function ago(iso){
  if (!iso) return '—';
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 90) return Math.round(s) + 's';
  if (s < 5400) return Math.round(s / 60) + 'm';
  return (s / 3600).toFixed(1) + 'h';
}
function until(iso){
  if (!iso) return '—';
  const s = (new Date(iso).getTime() - Date.now()) / 1000;
  return s <= 0 ? 'due' : s < 90 ? Math.round(s) + 's' : Math.round(s / 60) + 'm';
}

/* ---------- derived read-only views of the snapshot ---------- */
/* Defaults only. The snapshot records the thresholds that produced its labels,
   so a non-default config draws its own gates rather than these. */
let RS_GATE = 3.5, P_GATE = 0.40;
const rows = () => (snap && snap.rows) || [];
const scored = () => rows().filter(r =>
  r.rs.score != null && !(r.rs.quality_flags || []).includes('stale_or_misaligned')
);
/* Signed distance from the CPR band in ATR units. P is the exact midpoint of the
   band (TC = 2P - BC), so leaving the band on one side already satisfies the
   pivot leg of the rule: this single axis is the whole structure gate. */
function bandATR(r){
  const d = r.market.level_distances_atr || {};
  if (r.market.atr == null || d.TC == null || d.BC == null) return null;
  return d.TC > 0 ? d.TC : d.BC < 0 ? d.BC : 0;
}
/* Same measure for the session open, so the gap between the two markers is the
   session's travel: d.TC is (price - top)/ATR, so shifting by (price - open)/ATR
   restates it from the open's point of view. */
function openBandATR(r){
  const d = r.market.level_distances_atr || {};
  if (r.market.atr == null || r.market.session_open == null || d.TC == null || d.BC == null) return null;
  const shift = (r.market.price - r.market.session_open) / r.market.atr;
  const top = d.TC - shift, bot = d.BC - shift;
  return top > 0 ? top : bot < 0 ? bot : 0;
}
const passRS = r => r.rs.score != null && Math.abs(r.rs.score) >= RS_GATE;
const passPersist = r => r.rs.persistence != null && Math.abs(r.rs.persistence) >= P_GATE;
const passStructure = r => r.market.price_cpr_position !== 'inside_cpr';
const weakBeta = r => (r.rs.quality_flags || []).includes('low_beta_fit');
function tone(r){
  const l = r.setup.label;
  if (r.setup.confirmation === 'PROVISIONAL') return 'blocked';
  return l === 'LONG_CANDIDATE' ? 'long' : l === 'SHORT_CANDIDATE' ? 'short' : l === 'WATCH' ? 'blocked' : 'dimmed';
}
const colourOf = r => 'var(--' + ({ long: 'long', short: 'short', blocked: 'blocked', dimmed: 'faint' })[tone(r)] + ')';

/* ---------- the cross-section map ---------- */
function renderMap(){
  const W = 640, H = 392, L = 48, R = 18, T = 14, B = 40;
  const iw = W - L - R, ih = H - T - B, XM = 10, YM = 2.5;
  const x = v => L + (Math.max(-XM, Math.min(XM, v)) + XM) / (2 * XM) * iw;
  const y = v => T + (YM - Math.max(-YM, Math.min(YM, v))) / (2 * YM) * ih;
  const p = [];

  p.push(`<rect x="${x(RS_GATE)}" y="${T}" width="${x(XM) - x(RS_GATE)}" height="${y(0) - T}" fill="var(--long)" opacity=".07"/>`);
  p.push(`<rect x="${L}" y="${y(0)}" width="${x(-RS_GATE) - L}" height="${y(-YM) - y(0)}" fill="var(--short)" opacity=".07"/>`);
  for (const v of [-10, -5, 0, 5, 10]) p.push(`<line x1="${x(v)}" y1="${T}" x2="${x(v)}" y2="${T + ih}" stroke="var(--rail)"/>`);
  for (const v of [-2, -1, 1, 2]) p.push(`<line x1="${L}" y1="${y(v)}" x2="${L + iw}" y2="${y(v)}" stroke="var(--rail)"/>`);
  for (const v of [-RS_GATE, RS_GATE]) p.push(`<line x1="${x(v)}" y1="${T}" x2="${x(v)}" y2="${T + ih}" stroke="var(--structure)" stroke-width="1" stroke-dasharray="3 3" opacity=".55"/>`);
  p.push(`<line x1="${L}" y1="${y(0)}" x2="${L + iw}" y2="${y(0)}" stroke="var(--structure)" stroke-width="1" stroke-dasharray="3 3" opacity=".55"/>`);

  for (const v of [-10, -5, 0, 5, 10]) p.push(`<text x="${x(v)}" y="${T + ih + 15}" fill="var(--faint)" font-size="9.5" text-anchor="middle">${v}</text>`);
  p.push(`<text x="${L + iw / 2}" y="${H - 6}" fill="var(--faint)" font-size="9.5" text-anchor="middle" letter-spacing="1.2">RELATIVE STRENGTH</text>`);
  for (const v of [-2, 0, 2]) p.push(`<text x="${L - 8}" y="${y(v) + 3}" fill="var(--faint)" font-size="9.5" text-anchor="end">${v > 0 ? '+' + v : v}</text>`);
  p.push(`<text transform="translate(13,${T + ih / 2}) rotate(-90)" fill="var(--faint)" font-size="9.5" text-anchor="middle" letter-spacing="1.2">ATR FROM CPR BAND</text>`);
  p.push(`<text x="${x(RS_GATE) + 6}" y="${T + 11}" fill="var(--long)" font-size="9" opacity=".8">LONG</text>`);
  p.push(`<text x="${x(-RS_GATE) - 6}" y="${T + ih - 4}" fill="var(--short)" font-size="9" opacity=".8" text-anchor="end">SHORT</text>`);

  for (const r of scored()){
    const b = bandATR(r); if (b == null) continue;
    const c = colourOf(r), filled = passPersist(r), o = weakBeta(r) ? 0.3 : 0.92;
    /* A surface-coloured halo keeps the dense cluster near the origin readable
       where marks overlap, without nudging any dot off its true coordinates. */
    p.push(`<circle cx="${x(r.rs.score)}" cy="${y(b)}" r="6.8" fill="none" stroke="var(--panel)" stroke-width="2" opacity="${o}"/>`);
    p.push(`<circle class="pt" data-symbol="${esc(r.symbol)}" cx="${x(r.rs.score)}" cy="${y(b)}" r="5.5"
      fill="${filled ? c : 'var(--panel)'}" stroke="${c}" stroke-width="1.6" opacity="${o}"/>`);
  }
  $('map').innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Relative strength against distance from the CPR band, in ATR units">${p.join('')}</svg>`;
  $('map').querySelectorAll('.pt').forEach(el => {
    el.style.cursor = 'pointer';
    el.addEventListener('mouseenter', e => showTip(e, el.dataset.symbol));
    el.addEventListener('mouseleave', hideTip);
    el.addEventListener('click', () => open(el.dataset.symbol));
  });
}
function showTip(e, symbol){
  const r = rows().find(x => x.symbol === symbol); if (!r) return;
  const t = $('tip');
  t.innerHTML = `<b>${esc(r.symbol)}</b><div>RS ${n(r.rs.score)} &middot; persistence ${n(r.rs.persistence)}</div>
    <div>${words(r.market.price_cpr_position)} &middot; ${n(bandATR(r))} ATR</div>
    <div class="${tone(r)}">${words(r.setup.confirmation || '')} ${words(r.setup.label)}</div>`;
  t.dataset.show = 'true';
  const b = e.target.getBoundingClientRect();
  t.style.left = Math.min(window.innerWidth - t.offsetWidth - 10, b.left + 14) + 'px';
  t.style.top = Math.max(8, b.top - t.offsetHeight - 8) + 'px';
}
const hideTip = () => { $('tip').dataset.show = 'false'; };

/* ---------- the ladder: price is fixed at centre, structure floats around it ----------
   Upright, because here it has the height to earn the metaphor. The axis is in
   ATR units — the only scaling under which "near R1" means the same thing on a
   $64,000 market and a $0.12 one. */
function ladder(r, w, h){
  const d = r.market.level_distances_atr || {};
  if (r.market.atr == null) return `<svg viewBox="0 0 ${w} ${h}" width="${w}" height="${h}"><text x="4" y="${h / 2}" fill="var(--blocked)" font-size="10">No ATR — cannot scale this ladder</text></svg>`;
  const names = ['R3', 'R2', 'R1', 'TC', 'P', 'BC', 'S1', 'S2', 'S3'];
  const present = names.filter(k => d[k] != null);
  const L = 24, RIGHT = w - 128, ATRX = w - 66;
  /* Fit the levels that exist rather than centring on price: pivots sit
     asymmetrically around it, and centring would spend a third of the canvas on
     empty space above R3. The scale stays linear, so the geometry is unchanged. */
  const vals = present.map(key => d[key]).concat([0]);
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const pad = ((hi - lo) || 1) * 0.07;
  const at = v => 12 + (v - (lo - pad)) / ((hi + pad) - (lo - pad)) * (h - 26);
  /* One precision for the whole ladder, taken from the live price, so the level
     column stays a column. level = price - distance x ATR. */
  const dp = Math.abs(r.market.price) >= 1000 ? 1 : Math.abs(r.market.price) >= 1 ? 3 : 5;
  const levelPrice = v => (r.market.price - v * r.market.atr).toFixed(dp);

  /* Labels are pushed apart only after their rails are placed, so the geometry
     stays true and only the text moves. */
  const items = present.map(name => ({ name, v: d[name], y: at(d[name]) })).sort((a, b) => a.y - b.y);
  const items2 = items.concat([{ name: 'PRICE', v: 0, y: at(0), price: true }]).sort((a, b) => a.y - b.y);
  let last = -99;
  for (const it of items2){ it.ly = Math.max(it.y, last + 10.5); last = it.ly; }

  const p = [];
  if (d.TC != null && d.BC != null){
    const top = at(d.TC), bot = at(d.BC);
    p.push(`<rect x="${L}" y="${Math.min(top, bot)}" width="${RIGHT - L}" height="${Math.max(1.5, Math.abs(bot - top))}" fill="var(--structure)" opacity=".26"/>`);
  }
  for (const it of items2){
    const isBand = it.name === 'TC' || it.name === 'BC';
    const c = it.price
      ? (r.market.price_cpr_position === 'above_tc' ? 'var(--long)' : r.market.price_cpr_position === 'below_bc' ? 'var(--short)' : 'var(--dim)')
      : isBand ? 'var(--structure)' : 'var(--faint)';
    if (it.price){
      p.push(`<line x1="${L - 6}" y1="${it.y}" x2="${RIGHT + 6}" y2="${it.y}" stroke="${c}" stroke-width="1.6"/>`);
      p.push(`<circle cx="${(L + RIGHT) / 2}" cy="${it.y}" r="3.4" fill="${c}"/>`);
    } else {
      p.push(`<line x1="${L}" y1="${it.y}" x2="${RIGHT}" y2="${it.y}" stroke="${isBand ? 'var(--structure)' : 'var(--rail2)'}" stroke-width="1" opacity=".85"/>`);
    }
    /* When a label had to move to avoid its neighbour, tie it back to its rail
       so the reader is never guessing which line a number belongs to. */
    if (Math.abs(it.ly - it.y) > 1.5){
      p.push(`<path d="M${RIGHT} ${it.y} L${RIGHT + 5} ${it.ly}" stroke="${c}" stroke-width=".8" fill="none" opacity=".5"/>`);
    }
    p.push(`<text x="0" y="${it.ly + 3}" fill="${c}" font-size="9" letter-spacing="${it.price ? '.5' : '0'}">${it.price ? 'NOW' : it.name}</text>`);
    p.push(`<text x="${ATRX}" y="${it.ly + 3}" fill="${it.price ? c : 'var(--dim)'}" font-size="9" text-anchor="end">${n(it.v, 2)}</text>`);
    p.push(`<text x="${w}" y="${it.ly + 3}" fill="${it.price ? c : 'var(--faint)'}" font-size="9" text-anchor="end">${it.price ? r.market.price.toFixed(dp) : levelPrice(it.v)}</text>`);
  }
  /* The open is just another level on this axis: (price - open) / ATR. */
  const o = r.market.session_open == null ? null : (r.market.price - r.market.session_open) / r.market.atr;
  if (o != null && o >= lo - pad && o <= hi + pad){
    const oy = at(o);
    p.push(`<circle cx="${(L + RIGHT) / 2}" cy="${oy}" r="3" fill="none" stroke="var(--dim)" stroke-width="1.2"/>`);
    p.push(`<text x="${(L + RIGHT) / 2 + 8}" y="${oy + 3}" fill="var(--dim)" font-size="8.5">open</text>`);
  }
  return `<svg viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" role="img" aria-label="${esc(r.symbol)} pivot ladder, price ${words(r.market.price_cpr_position)}">${p.join('')}</svg>`;
}

/* ---------- structure as a horizontal gate track ----------
   A table row is landscape, so the upright ladder belongs in the drawer where it
   has the height to earn its geometry. Here the same information is laid on its
   side: 112px of resolution instead of 34, and the same left-to-right grammar as
   the relative-strength and persistence tracks beside it. */
function structureTrack(r, w = 132, h = 18, span = 1.5){
  if (r.market.atr == null) return `<svg viewBox="0 0 ${w} ${h}" width="${w}" height="${h}"><text x="0" y="${h / 2 + 3}" fill="var(--blocked)" font-size="9">no ATR</text></svg>`;
  const d = r.market.level_distances_atr || {}, cy = h / 2;
  const at = v => (Math.max(-span, Math.min(span, v)) + span) / (2 * span) * w;
  const p = [`<line x1="0" y1="${cy}" x2="${w}" y2="${cy}" stroke="var(--rail)" stroke-width="3"/>`];
  /* The band drawn at its true ATR width: a tight regime is a sliver, a wide one a slab. */
  const bandW = (d.BC != null && d.TC != null) ? Math.abs(d.BC - d.TC) : 0;
  const halfPx = Math.max(1.6, bandW / (2 * span) * w / 2);
  p.push(`<rect x="${at(0) - halfPx}" y="${cy - 5}" width="${halfPx * 2}" height="10" fill="var(--structure)" opacity=".3"/>`);
  p.push(`<line x1="${at(0)}" y1="${cy - 6}" x2="${at(0)}" y2="${cy + 6}" stroke="var(--structure)" stroke-width="1" opacity=".75"/>`);
  const v = bandATR(r), o = openBandATR(r);
  if (o != null) p.push(`<circle cx="${at(o)}" cy="${cy}" r="2.3" fill="none" stroke="var(--dim)" stroke-width="1"/>`);
  if (v != null){
    const c = v > 0 ? 'var(--long)' : v < 0 ? 'var(--short)' : 'var(--dim)';
    p.push(`<line x1="${at(0)}" y1="${cy}" x2="${at(v)}" y2="${cy}" stroke="${c}" stroke-width="3"/>`);
    p.push(`<circle cx="${at(v)}" cy="${cy}" r="3.4" fill="${c}" stroke="var(--panel)" stroke-width="1"/>`);
  }
  return `<svg viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" role="img" aria-label="${esc(r.symbol)} ${words(r.market.price_cpr_position)}, ${n(v)} ATR from the band">${p.join('')}</svg>`;
}

/* ---------- the gate track primitive ---------- */
function track(value, gate, max, w = 132, h = 16){
  if (value == null) return `<svg viewBox="0 0 ${w} ${h}" width="${w}" height="${h}"><text x="0" y="${h / 2 + 3}" fill="var(--faint)" font-size="9">no value</text></svg>`;
  const cy = h / 2, at = v => (Math.max(-max, Math.min(max, v)) + max) / (2 * max) * w;
  const passed = Math.abs(value) >= gate;
  const c = !passed ? 'var(--dim)' : value > 0 ? 'var(--long)' : 'var(--short)';
  const p = [`<line x1="0" y1="${cy}" x2="${w}" y2="${cy}" stroke="var(--rail)" stroke-width="3"/>`];
  p.push(`<line x1="${at(0)}" y1="${cy}" x2="${at(value)}" y2="${cy}" stroke="${c}" stroke-width="3"/>`);
  for (const g of [-gate, gate]) p.push(`<line x1="${at(g)}" y1="${cy - 5}" x2="${at(g)}" y2="${cy + 5}" stroke="var(--structure)" stroke-width="1" opacity=".7"/>`);
  p.push(`<line x1="${at(0)}" y1="${cy - 4}" x2="${at(0)}" y2="${cy + 4}" stroke="var(--faint)" stroke-width="1"/>`);
  p.push(`<circle cx="${at(value)}" cy="${cy}" r="3.2" fill="${c}"/>`);
  return `<svg viewBox="0 0 ${w} ${h}" width="${w}" height="${h}">${p.join('')}</svg>`;
}

/* ---------- gate funnel ---------- */
function renderFunnel(){
  const all = scored(), total = all.length || 1;
  const gates = [
    ['Rel. strength', all.filter(passRS).length, '|RS| ≥ ' + RS_GATE],
    ['Persistence', all.filter(passPersist).length, '|persistence| ≥ ' + P_GATE.toFixed(2)],
    ['Structure', all.filter(passStructure).length, 'price outside the CPR band'],
  ];
  const min = Math.min(...gates.map(g => g[1]));
  $('funnel').innerHTML = gates.map(([label, count, detail]) => `
    <div class="gate" title="${detail}">
      <u>${label}</u>
      <div class="bar"><i class="${count === min ? 'bind' : ''}" style="width:${(count / total * 100).toFixed(1)}%"></i></div>
      <b>${count}</b>
    </div>`).join('');
  const binder = gates.find(g => g[1] === min);
  $('binding').innerHTML = all.length
    ? `Tightest gate: <em>${esc(binder[0].toLowerCase())}</em>, cleared by ${min} of ${all.length} scored assets.`
    : '';
}

/* ---------- queue and feed ---------- */
function renderQueue(){
  const confirmed = rows().filter(r => r.setup.label.endsWith('CANDIDATE') && r.setup.confirmation === 'CONFIRMED');
  const provisional = rows().filter(r => r.setup.label.endsWith('CANDIDATE') && r.setup.confirmation === 'PROVISIONAL');
  const cards = q => q.map(r => `
    <article class="card ${esc(r.setup.direction)}">
      <div class="card-h"><b>${esc(r.symbol)}</b><span class="tag ${tone(r)}">${words(r.setup.confirmation)} ${words(r.setup.label)}</span></div>
      <div class="dimmed">RS ${n(r.rs.score)} &middot; persistence ${n(r.rs.persistence)} &middot; strength ${n(r.setup.strength, 0)}</div>
      <ul class="why prose">${(r.setup.reasons || []).map(x => `<li>${esc(x)}</li>`).join('')}</ul>
    </article>`).join('');
  $('queue').innerHTML = confirmed.length || provisional.length
    ? `${confirmed.length ? '<p class="hint">Completed-bar confirmations</p>' + cards(confirmed) : ''}
       ${provisional.length ? '<p class="hint">Mid-price previews — no alert/history event</p>' + cards(provisional) : ''}`
    : `<p class="empty prose">No asset clears every gate. Watch labels and near misses are visible in the cross-section.</p>`;
}
function renderFeed(){
  $('feed').innerHTML = events.length ? events.map(e => {
    const s = e.current || e.previous || {};
    return `<div class="ev"><div><b>${esc(e.symbol)}</b> <span class="${s.direction === 'LONG' ? 'long' : s.direction === 'SHORT' ? 'short' : 'dimmed'}">${esc(e.event)}</span>
      <div class="dimmed">${words(s.label || '')} &middot; RS ${n(s.score)}</div></div>
      <time>${ago(e.timestamp)} ago</time></div>`;
  }).join('') : `<p class="empty prose">No candidate has activated, changed, or cleared yet.</p>`;
}

/* ---------- universe table ---------- */
function visible(){
  const q = $('q').value.trim().toLowerCase();
  return rows().filter(r => {
    if (filter === 'FLAGGED' ? !(r.rs.quality_flags || []).length
      : filter === 'CONFIRMED' ? r.setup.confirmation !== 'CONFIRMED'
      : filter === 'PROVISIONAL' ? r.setup.confirmation !== 'PROVISIONAL'
      : filter !== 'ALL' && r.setup.label !== filter) return false;
    return !q || r.symbol.toLowerCase().includes(q);
  }).sort((a, b) => {
    const get = r => sort === 'symbol' ? r.symbol
      : sort === 'persistence' ? (r.rs.persistence == null ? -9 : r.rs.persistence)
      : sort === 'width' ? (r.market.cpr_width_percentile == null ? -1 : r.market.cpr_width_percentile)
      : sort === 'structure' ? (bandATR(r) == null ? -9 : bandATR(r))
      : (r.rs.score == null ? -99 : r.rs.score);
    const x = get(a), y = get(b);
    return (typeof x === 'string' ? x.localeCompare(y) : x - y) * dir;
  });
}
function renderTable(){
  const list = visible();
  $('rows').innerHTML = list.length ? list.map(r => `
    <tr tabindex="0" data-symbol="${esc(r.symbol)}" aria-selected="${r.symbol === selected}">
      <td><div class="sym">${esc(r.symbol)}</div><div class="rank">${r.strong_rank == null ? '&mdash;' : '#' + r.strong_rank}</div></td>
      <td>${structureTrack(r, 132, 18)}<span class="rank">${n(bandATR(r))} ATR ${words(r.market.price_cpr_position)}</span></td>
      <td>${track(r.rs.score, RS_GATE, 10)}<span class="rank">${n(r.rs.score)}</span></td>
      <td>${track(r.rs.persistence, P_GATE, 1)}<span class="rank">${n(r.rs.persistence)}</span></td>
      <td><span class="${r.market.cpr_regime === 'wide' ? 'blocked' : ''}">${esc(r.market.cpr_regime)}</span>
          <span class="rank">${r.market.cpr_width_percentile == null ? '&mdash;' : Math.round(r.market.cpr_width_percentile * 100) + 'pct'}</span></td>
      <td><span class="tag ${tone(r)}">${r.setup.confirmation && r.setup.confirmation !== 'NONE' ? words(r.setup.confirmation) + ' ' : ''}${words(r.setup.label)}</span>
          ${(r.rs.quality_flags || []).length ? `<span class="rank blocked"> ${(r.rs.quality_flags || []).length} flag</span>` : ''}</td>
    </tr>`).join('')
    : `<tr><td colspan="6" class="empty prose">Nothing matches this filter.</td></tr>`;
  $('rows').querySelectorAll('tr[data-symbol]').forEach(tr => {
    tr.addEventListener('click', () => open(tr.dataset.symbol));
    tr.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(tr.dataset.symbol); } });
  });
}

/* ---------- detail drawer ---------- */
function horizons(rs){
  /* Plot inset from the frame so a clamped bar's value label stays inside it.
     The +/-3 robust-unit range mirrors the /3 normalisation the score applies.
     These are empirical MAD-scaled horizon sums, not Gaussian sigma claims. */
  const W = 258, H = 76, rowH = 22, max = 3, X0 = 46, X1 = 214;
  const at = v => X0 + (Math.max(-max, Math.min(max, v)) + max) / (2 * max) * (X1 - X0);
  const p = [`<line x1="${at(0)}" y1="6" x2="${at(0)}" y2="${H - 8}" stroke="var(--faint)" stroke-width="1"/>`];
  for (const g of [-max, max]) p.push(`<text x="${at(g)}" y="${H - 1}" fill="var(--faint)" font-size="8" text-anchor="middle">${g > 0 ? '+' : ''}${g}R</text>`);
  ['short', 'medium', 'long'].forEach((key, i) => {
    const v = (rs.horizon_z || {})[key], cy = 14 + i * rowH;
    p.push(`<text x="0" y="${cy + 3}" fill="var(--faint)" font-size="8.5" letter-spacing=".6">${key.toUpperCase()}</text>`);
    if (v == null){ p.push(`<text x="${at(0) + 6}" y="${cy + 3}" fill="var(--faint)" font-size="9">&mdash;</text>`); return; }
    const c = v > 0 ? 'var(--long)' : 'var(--short)', clamped = Math.abs(v) > max;
    p.push(`<rect x="${Math.min(at(0), at(v))}" y="${cy - 4}" width="${Math.max(1.5, Math.abs(at(v) - at(0)))}" height="8" rx="1.5" fill="${c}"/>`);
    if (clamped) p.push(`<path d="M${at(v) + (v > 0 ? 3 : -3)},${cy - 4} l${v > 0 ? 5 : -5},4 l${v > 0 ? -5 : 5},4z" fill="${c}"/>`);
    p.push(`<text x="${W}" y="${cy + 3}" fill="var(--dim)" font-size="9" text-anchor="end">${n(v, 1)}</text>`);
  });
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" preserveAspectRatio="xMidYMid meet" style="opacity:${(rs.quality_flags || []).includes('low_beta_fit') ? 0.34 : 1}">${p.join('')}</svg>`;
}

function horizonDetails(rs){
  return ['short', 'medium', 'long'].map(key => {
    const excess = (rs.horizon_excess_return || {})[key];
    const percentile = (rs.horizon_percentile || {})[key];
    return `<div class="kv"><span>${key} excess / percentile</span><b>${excess == null ? '&mdash;' : (excess * 100).toFixed(2) + '%'} &middot; ${percentile == null ? '&mdash;' : Math.round(percentile * 100) + 'pct'}</b></div>`;
  }).join('');
}

/* ---------- which gate is actually stopping this asset ----------
   assess_setup records a blocker for the structure leg only, so an asset held
   back purely by persistence arrives with an empty blockers list. These three
   lines restate the published numbers against the published thresholds; they do
   not re-derive the label, which is read from the snapshot as written. */
function gateVerdict(r){
  if (r.rs.score == null || r.rs.persistence == null) return '';
  const dir = r.rs.score >= 0 ? 1 : -1;
  const confirmationPrice = r.setup.confirmation === 'CONFIRMED' ? r.setup.confirmation_price : r.market.price;
  const cpr = r.market.active_cpr || {}, pivot = (r.market.pivots || {}).pivot;
  const pos = confirmationPrice == null ? 'unknown'
    : confirmationPrice > cpr.top && confirmationPrice > pivot ? 'above_tc'
    : confirmationPrice < cpr.bottom && confirmationPrice < pivot ? 'below_bc'
    : 'inside_cpr';
  const checks = [
    ['Relative strength', Math.abs(r.rs.score) >= RS_GATE, `${n(r.rs.score)} vs ${dir > 0 ? '' : '-'}${n(RS_GATE)}`],
    ['Persistence', Math.sign(r.rs.persistence) === dir && Math.abs(r.rs.persistence) >= P_GATE, `${n(r.rs.persistence)} vs ${dir > 0 ? '' : '-'}${n(P_GATE)}`],
    ['Structure', dir > 0 ? pos === 'above_tc' : pos === 'below_bc', words(pos)],
  ];
  return checks.map(([label, ok, detail]) =>
    `<div class="kv"><span>${ok ? '<b class="long">&check;</b>' : '<b class="blocked">&times;</b>'} ${label}</span><b class="${ok ? '' : 'blocked'}">${detail}</b></div>`
  ).join('');
}
function open(symbol){
  const r = rows().find(x => x.symbol === symbol); if (!r) return;
  selected = symbol;
  const m = r.market, rs = r.rs, b = rs.beta || {}, cx = r.context || {}, d = m.level_distances_atr || {};
  $('drawer').innerHTML = `
    <div class="drawer-h">
      <div><h3 id="dTitle">${esc(r.symbol)}</h3>
        <span class="tag ${tone(r)}">${words(r.setup.label)}</span>
        <span class="dimmed"> ${words(r.setup.confirmation || 'NONE')}</span>
        <span class="dimmed"> ${price(r.price)}</span></div>
      <button class="close" id="closeBtn" aria-label="Close">&times;</button>
    </div>
    <div class="dgrid">
      <div>
        <div class="sub">Pivot ladder &middot; ATR / level</div>
        ${ladder(r, 296, 348)}
        <div class="kv"><span>Session open</span><b>${price(m.session_open)}</b></div>
        <div class="kv"><span>CPR band</span><b>${d.TC == null || d.BC == null ? '&mdash;' : n(Math.abs(d.BC - d.TC), 2) + ' ATR wide'}</b></div>
      </div>
      <div>
        <div class="sub">Relative strength &middot; empirical robust units</div>
        ${horizons(rs)}
        ${horizonDetails(rs)}
        ${(rs.quality_flags || []).includes('low_beta_fit') ? `<p class="hint">Faded because the beta fit is weak — these residuals carry little information.</p>` : ''}
        <div class="sub">Gates &middot; ${r.rs.score >= 0 ? 'long' : 'short'} side</div>
        ${gateVerdict(r)}
        <div class="kv"><span>Confirmation close</span><b>${price(r.setup.confirmation_price)}</b></div>
        <div class="kv"><span>Acceleration</span><b>${n(rs.acceleration)}</b></div>
        <div class="sub">Beta fit vs ${esc(rs.benchmark)}</div>
        <div class="kv"><span>Beta</span><b>${n(b.beta)}</b></div>
        <div class="kv"><span>Beta uncertainty</span><b>${n(b.beta_standard_error, 3)}</b></div>
        <div class="kv"><span>Secondary factor</span><b>${b.secondary_benchmark ? esc(b.secondary_benchmark) + ' &beta; ' + n(b.secondary_beta) + ' &plusmn; ' + n(b.secondary_beta_standard_error, 3) : '&mdash;'}</b></div>
        <div class="kv"><span>Secondary vs BTC beta</span><b>${n(b.secondary_primary_beta)}</b></div>
        <div class="kv"><span>Model</span><b>${words(b.method || rs.model_version)}</b></div>
        <div class="kv"><span>R&sup2;</span><b>${n(b.r_squared, 3)}</b></div>
        <div class="kv"><span>Residual vol</span><b>${n(b.residual_volatility, 5)}</b></div>
        <div class="kv"><span>Observations</span><b>${b.observations == null ? '&mdash;' : b.observations}</b></div>
        <div class="kv"><span>Bar alignment</span><b>${n(rs.alignment_ratio, 3)}</b></div>
        <div class="sub">Session</div>
        <div class="kv"><span>CPR regime</span><b>${esc(m.cpr_regime)} &middot; ${m.cpr_width_percentile == null ? '&mdash;' : Math.round(m.cpr_width_percentile * 100) + 'pct'}</b></div>
        <div class="kv"><span>Price location</span><b>${words(m.price_cpr_position)}</b></div>
        <div class="kv"><span>Opened</span><b>${m.opening_cpr_position ? words(m.opening_cpr_position) : '&mdash;'}</b></div>
        <div class="kv"><span>Pivot band</span><b>${words(m.pivot_position)}</b></div>
        <div class="kv"><span>ATR &middot; realised vol</span><b>${price(m.atr)} &middot; ${n(m.realized_volatility)}</b></div>
        <div class="sub">Research context &middot; excluded from score</div>
        <div class="kv"><span>Relative notional proxy</span><b>${cx.relative_notional_volume == null ? '&mdash;' : n(cx.relative_notional_volume) + '&times;'}</b></div>
        <div class="kv"><span>Impact spread</span><b>${cx.impact_spread_bps == null ? '&mdash;' : n(cx.impact_spread_bps) + ' bps'}</b></div>
        <div class="kv"><span>Funding</span><b>${cx.funding_rate == null ? '&mdash;' : (cx.funding_rate * 100).toFixed(4) + '%'}</b></div>
        <div class="kv"><span>Open interest</span><b>${n(cx.open_interest, 0)}</b></div>
        <div class="kv"><span>Positive RS breadth</span><b>${snap && snap.breadth && snap.breadth.positive_rs_ratio != null ? Math.round(snap.breadth.positive_rs_ratio * 100) + '%' : '&mdash;'}</b></div>
      </div>
    </div>
    <div class="sub">Why this label</div>
    <ul class="why prose">
      ${(r.setup.reasons || []).map(x => `<li>${esc(x)}</li>`).join('')}
      ${(r.setup.blockers || []).map(x => `<li class="block">${esc(x)}</li>`).join('')}
      ${!(r.setup.reasons || []).length && !(r.setup.blockers || []).length ? '<li>No active condition.</li>' : ''}
    </ul>
    <div class="sub">Data quality</div>
    ${(rs.quality_flags || []).length ? (rs.quality_flags).map(f => `<span class="flag">${words(f)}</span>`).join('')
      : '<p class="hint prose">No flags. Beta sample and bar alignment are within tolerance.</p>'}
    ${rs.reason ? `<p class="hint prose">${esc(rs.reason)}</p>` : ''}`;
  $('drawer').dataset.open = 'true';
  $('scrim').dataset.open = 'true';
  $('closeBtn').addEventListener('click', close);
  $('closeBtn').focus();
  renderTable();
}
function close(){
  selected = null;
  $('drawer').dataset.open = 'false';
  $('scrim').dataset.open = 'false';
  renderTable();
}

/* ---------- health ---------- */
function renderHealth(){
  const state = !reachable ? 'unreachable' : status ? (status.loop || 'off') : 'off';
  $('loopDot').className = 'dot ' + state;
  $('loopText').textContent = state;
  /* Whatever is wrong gets said in words, at full width. A fault buried in a
     tooltip is a fault nobody reads. */
  const dropped = (status && status.excluded_symbols) || {};
  const names = Object.keys(dropped);
  /* A shrunk panel is never allowed to be silent: every rank on this screen is
     cross-sectional, so who is missing changes what the survivors mean. */
  /* Symbols usually drop out together and for one shared reason. Printing that
     reason once per symbol buries the names, which are the part worth reading. */
  const byReason = {};
  names.forEach(n => (byReason[dropped[n]] = byReason[dropped[n]] || []).push(n));
  const shrunk = names.length
    ? names.length + ' symbol' + (names.length > 1 ? 's' : '') + ' held out of the cross-section &mdash; '
      + Object.keys(byReason).map(r => esc(byReason[r].join(', ')) + ': ' + esc(r)).join(' &middot; ')
    : '';
  const fault = !reachable
    ? 'Cannot reach the scanner at ' + location.host + '. ' + esc(unreachableWhy) + ' &mdash; the server is probably not running.'
    : state === 'failing'
      ? 'The refresh loop has failed ' + status.consecutive_failures + ' times in a row. ' + esc(status.last_error || '')
      : shrunk;
  $('fault').innerHTML = fault;
  $('fault').hidden = !fault;
  $('age').textContent = snap ? ago(snap.as_of) : '—';
  $('count').textContent = snap ? rows().length : '—';
  const confirmed = rows().filter(r => r.setup.label.endsWith('CANDIDATE') && r.setup.confirmation === 'CONFIRMED').length;
  const provisional = rows().filter(r => r.setup.label.endsWith('CANDIDATE') && r.setup.confirmation === 'PROVISIONAL').length;
  $('cands').textContent = snap ? confirmed + ' / ' + provisional : '—';
  const f = rows().filter(r => (r.rs.quality_flags || []).length).length;
  $('flags').innerHTML = snap ? (f ? `<span class="warn">${f}</span>` : '0') : '—';
  $('nextRefresh').textContent = status && status.next_candle_refresh ? until(status.next_candle_refresh) : '—';
  /* An old snapshot presented as current is worse than no snapshot: desaturate
     the whole surface so staleness cannot be mistaken for a quiet market. */
  const ageSec = snap ? (Date.now() - new Date(snap.as_of).getTime()) / 1000 : Infinity;
  document.body.classList.toggle('degraded', state === 'failing' || state === 'unreachable' || ageSec > 5400);
}

function render(){
  renderHealth();
  if (!snap) return;
  renderMap(); renderFunnel(); renderQueue(); renderFeed(); renderTable();
}

async function refresh(){
  const get = async url => { const r = await fetch(url); return r.ok ? r.json() : null; };
  try {
    const [s, h, st] = await Promise.all([get('/api/snapshot'), get('/api/signals?limit=60'), get('/api/status')]);
    if (s){
      snap = s;
      if (s.gates){
        RS_GATE = s.gates.candidate_rs_score;
        P_GATE = s.gates.candidate_persistence;
      }
    }
    events = h || [];
    status = st;
    reachable = true;
  } catch (err) {
    /* A rejected fetch must not leave the page painting its start-up state.
       "Cannot reach the server" and "no loop is configured" are different
       situations, and only one of them is fine. */
    reachable = false;
    unreachableWhy = String(err && err.message || err);
  }
  render();
}

$('q').addEventListener('input', renderTable);
document.querySelectorAll('[data-filter]').forEach(b => b.addEventListener('click', () => {
  filter = b.dataset.filter;
  document.querySelectorAll('[data-filter]').forEach(x => x.setAttribute('aria-pressed', String(x === b)));
  renderTable();
}));
document.querySelectorAll('th[data-sort]').forEach(th => th.addEventListener('click', () => {
  const key = th.dataset.sort;
  dir = sort === key ? -dir : (key === 'symbol' ? 1 : -1);
  sort = key;
  document.querySelectorAll('th[data-sort]').forEach(x => x.removeAttribute('aria-sort'));
  th.setAttribute('aria-sort', dir === 1 ? 'ascending' : 'descending');
  renderTable();
}));
$('scrim').addEventListener('click', close);
document.addEventListener('keydown', e => { if (e.key === 'Escape') close(); });
refresh();
setInterval(refresh, 10000);
setInterval(renderHealth, 1000);
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    output_dir: Path
    live_scanner: Any = None

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
        if request.path == "/api/status":
            self._json(self._status())
            return
        if request.path == "/health":
            self._json({"ok": _read_snapshot(self.output_dir / "scanner_latest.json") is not None})
            return
        self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _status(self) -> dict[str, Any]:
        """Distinguish 'the loop is off' from 'the loop is failing'.

        A timestamp alone conflates the two, and they call for opposite
        reactions from whoever is reading the screen.
        """
        if self.live_scanner is None:
            return {"loop": "off", "detail": "snapshot is whatever a scan last wrote"}
        status = asdict(self.live_scanner.status())
        status["loop"] = "failing" if status["consecutive_failures"] else "live"
        return status

    def do_POST(self) -> None:  # noqa: N802 - required handler method
        self._json({"error": "This dashboard is read-only."}, HTTPStatus.METHOD_NOT_ALLOWED)

    do_PUT = do_POST
    do_DELETE = do_POST

    def log_message(self, *_: Any) -> None:
        """Avoid request noise; this is a local visualisation, not an audit log."""


def serve(output_dir: Path, port: int = 8765, live_scanner: Any = None) -> None:
    """Serve only on loopback. Do not add a non-local bind option here.

    ``live_scanner`` is read for status only. No route starts, stops, or steps
    it: the refresh timer lives in the process, never in a request handler, so
    this surface stays structurally side-effect-free.
    """
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    handler = type(
        "ScannerDashboardHandler", (_Handler,),
        {"output_dir": output_dir, "live_scanner": live_scanner},
    )
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    print(f"Strength Tracker dashboard: http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        server.server_close()
