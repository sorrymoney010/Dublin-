"""Acurast CEO — live KPI dashboard.

A tiny ``http.server`` app (mirrors the existing dublin_bot dashboard style)
that renders the Sentinel's KPI snapshot, the fleet table, open alerts, the
inventory benchmark table, and the current Capital Allocation recommendation.

Endpoints:
  GET /                      -> HTML dashboard (auto-refresh 60s)
  GET /api/kpi               -> latest KPI snapshot (JSON)
  GET /api/fleet             -> per-processor latest status (JSON)
  GET /api/alerts            -> open alerts (JSON)
  GET /api/inventory         -> Phase Zero benchmark table (JSON)
  GET /api/allocate?cap=100  -> capital allocation decision (JSON)

Read-only: never places orders, never deploys, never buys.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import AcurastSettings
from .store import Store

HOST = "0.0.0.0"
PORT = 8891


def _json_handler(handler, payload, code=200):
    body = json.dumps(payload).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class _Handler(BaseHTTPRequestHandler):
    store: Store
    settings: AcurastSettings

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/kpi":
                self._api_kpi()
            elif path == "/api/fleet":
                self._api_fleet()
            elif path == "/api/alerts":
                self._api_alerts()
            elif path == "/api/inventory":
                self._api_inventory()
            elif path == "/api/allocate":
                self._api_allocate(parse_qs(parsed.query))
            elif path == "/":
                self._page()
            else:
                _json_handler(self, {"error": "not found"}, HTTPStatus.NOT_FOUND.value)
        except Exception as e:  # surface errors instead of 500 silently
            _json_handler(self, {"error": str(e)}, 500)

    # ── APIs ──
    def _api_kpi(self):
        snap = self.store.latest_kpi()
        _json_handler(self, snap.__dict__ if snap else {})

    def _api_fleet(self):
        fleet = [s.__dict__ for s in self.store.all_latest()]
        _json_handler(self, fleet)

    def _api_alerts(self):
        _json_handler(self, self.store.open_alerts())

    def _api_inventory(self):
        _json_handler(self, [i.model_dump() for i in self.store.get_inventory()])

    def _api_allocate(self, qs):
        from .allocator import CapitalAllocator, InsufficientData

        cap = float(qs.get("cap", ["100"])[0])
        alloc = CapitalAllocator(self.settings, self.store)
        try:
            dec = alloc.decide(cap)
            _json_handler(self, dec.as_dict())
        except InsufficientData as e:
            _json_handler(self, {"error": str(e)}, 409)

    # ── Page ──
    def _page(self):
        html = render_dashboard(self.store, self.settings)
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Refresh", "60")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # quiet
        return


def render_dashboard(store: Store, settings: AcurastSettings) -> str:
    snap = store.latest_kpi()
    fleet = store.all_latest()
    alerts = store.open_alerts()
    inv = store.get_inventory()

    def f(x, n=2):
        try:
            return f"{x:.{n}f}"
        except (TypeError, ValueError):
            return str(x)

    def ago(ts):
        if not ts:
            return "never"
        secs = datetime.now(timezone.utc).timestamp() - ts
        return f"{secs/60:.0f}m ago" if secs < 7200 else f"{secs/3600:.1f}h ago"

    rows = "".join(
        f"<tr class='{'off' if not s.online else ''}'>"
        f"<td><code>{s.address[:10]}…</code></td>"
        f"<td>{'🟢' if s.online else '🔴'}</td>"
        f"<td>{ago(s.last_heartbeat_ts)}</td>"
        f"<td>{'✓' if s.attested else '—'}</td>"
        f"<td>{f(s.battery_pct,0)}%</td>"
        f"<td>{f(s.temperature,1)}°C</td>"
        f"<td>{f(s.reputation,2)}</td>"
        f"<td>{'Core' if s.is_core else 'Lite'}</td>"
        f"<td>{s.deployment_status or 'idle'}</td></tr>"
        for s in fleet
    ) or "<tr><td colspan=9>No processors polled yet. Run: acurast-ceo seed-demo</td></tr>"

    inv_rows = "".join(
        f"<tr><td>{i.device_id}</td><td>${f(i.purchase_cost,0)}</td><td>{i.cpu or '—'}</td>"
        f"<td>{f(i.ram_gb,1) or '—'}</td><td>{i.android_version or '—'}</td>"
        f"<td>{'✓' if i.core_eligible else ('—' if i.core_eligible is None else '✗')}</td>"
        f"<td>{f(i.benchmark_score,1)}</td><td>{f(i.uptime_pct,1)}%</td>"
        f"<td>{f(i.acu_earned,2)}</td><td>${f(i.usd_per_day,3)}</td></tr>"
        for i in inv
    ) or "<tr><td colspan=10>Phase Zero: no inventory yet.</td></tr>"

    alert_rows = "".join(
        f"<tr class='alert'><td>{a['kind']}</td><td>{a['message']}</td><td>{ago(a['ts'])}</td></tr>"
        for a in alerts
    ) or "<tr><td colspan=3>✅ no open alerts</td></tr>"

    kpi = {
        "Phones online": f"{snap.phones_online}/{snap.total_phones}" if snap else "—",
        "Uptime %": f"{f(snap.uptime_pct,1)}%" if snap else "—",
        "Avg benchmark": f"{f(snap.avg_benchmark,1)}" if snap else "—",
        "ACU earned (farm)": f"{f(snap.acu_earned_farm,3)}" if snap else "—",
        "USD equiv": f"${f(snap.usd_equivalent,2)}" if snap else "—",
        "Busy epochs": f"{snap.busy_epochs}" if snap else "—",
        "Avg reputation": f"{f(snap.avg_reputation,2)}" if snap else "—",
        "Power": f"{f(snap.power_consumption_w,0)}W" if snap else "—",
        "Revenue/phone": f"${f(snap.revenue_per_phone_usd,3)}/day" if snap else "—",
        "Payback": f"{f(snap.payback_months,1)} mo" if snap and snap.payback_months != float('inf') else "—",
    }
    kpi_cards = "".join(
        f"<div class='card'><div class='k'>{k}</div><div class='v'>{v}</div></div>"
        for k, v in kpi.items()
    )

    return f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Acurast CEO — Farm Sentinel</title>
<style>
:root{{--bg:#0b0e14;--card:#151a23;--fg:#e6e6e6;--muted:#8b97a7;--accent:#39d98a;--alert:#ff5c5c;border:#222b38}}
body{{background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,system-ui,sans-serif;margin:0;padding:18px}}
h1{{font-size:18px;margin:0 0 2px}}h2{{font-size:13px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em;margin:22px 0 8px}}
.sub{{color:var(--muted);font-size:12px;margin-bottom:14px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px}}
.card .k{{color:var(--muted);font-size:11px}} .card .v{{font-size:20px;font-weight:600;margin-top:4px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{text-align:left;padding:7px 8px;border-bottom:1px solid var(--border)}}
th{{color:var(--muted);font-weight:600;font-size:11px;text-transform:uppercase}}
tr.off{{opacity:.55}} tr.alert td{{color:var(--alert)}}
code{{color:var(--accent)}} .refresh{{color:var(--muted);font-size:11px}}
</style></head><body>
<h1>Acurast CEO — Farm Sentinel</h1>
<div class=sub>Six-engine compute business · Sentinel + Capital Allocation · auto-refresh 60s · read-only</div>
<div class=cards>{kpi_cards}</div>
<h2>Fleet</h2><table><thead><tr>
<th>Address</th><th>Status</th><th>Last seen</th><th>Att</th><th>Batt</th><th>Temp</th><th>Rep</th><th>Mode</th><th>Deploy</th>
</tr></thead><tbody>{rows}</tbody></table>
<h2>Open Alerts ({len(alerts)})</h2><table><thead><tr><th>Kind</th><th>Message</th><th>When</th></tr></thead><tbody>{alert_rows}</tbody></table>
<h2>Engine 1 — Phase Zero Inventory (benchmark what you own)</h2>
<table><thead><tr><th>Device</th><th>Cost</th><th>CPU</th><th>RAM</th><th>Android</th><th>Core?</th><th>Bench</th><th>Uptime</th><th>ACU</th><th>$/day</th></tr></thead><tbody>{inv_rows}</tbody></table>
<div class=refresh>Data: local SQLite ({Path(settings.db_file).name}) · ACU↔USD {settings.acu_usd_price} (accounting only, never ROI)</div>
</body></html>"""


def run_dashboard(store: Store, settings: AcurastSettings, host: str = HOST, port: int = PORT):
    _Handler.store = store
    _Handler.settings = settings
    httpd = ThreadingHTTPServer((host, port), _Handler)
    print(f"Acurast CEO dashboard: http://{host}:{port}/  (Ctrl-C to stop)")
    httpd.serve_forever()
