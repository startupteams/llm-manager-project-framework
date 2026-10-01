"""Facility power & cost view — STEA-004 fresh-session plan §16.

Authoritative channel mapping (Jordan, 2026-10-01):
    Emporia server#1 / ch1 = PDU MIAM-00151   (compute)
    Emporia server#2 / ch2 = PDU MIAM-00152   (compute)
    Emporia server#3 / ch3 = PDU MIAM-00153   (compute)
    Emporia ch4            = 36k 3 Ton mini split (cooling)
    TOTAL MARION_IA_USA    = PDU 151 + PDU 152 + PDU 153 + mini split

Honesty rules:
- STALE data NEVER renders as 0: stale channels show status=STALE + last sample
  timestamp + age, energy values are null (not zero).
- Collector health = samples in the last hour + last-sample age; reported as its
  own fact, never folded into the energy numbers.
- Rate comes from main.effective_rate() (versioned electricity_rates components
  when present, else built-in plan constants) — same source /api/cost uses.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import psycopg2

# main is importable in this app layout (fleet_view precedent)
from main import PG_DB, PG_HOST, PG_USER, _load_pg_pw

CHANNEL_LABELS: dict[str, str] = {
    "1": "PDU MIAM-00151 (Server#1 rack circuit)",
    "2": "PDU MIAM-00152 (Server#2 rack circuit)",
    "3": "PDU MIAM-00153 (Server#3 rack circuit)",
    "4": "36k 3 Ton mini split (cooling)",
}
TOTAL_LABEL = "TOTAL MARION_IA_USA"
STALE_SAMPLE_SECONDS = 600  # no fresh sample for 10 min → collector STALE


def _connect():
    pw = _load_pg_pw()
    if not pw:
        return None
    try:
        return psycopg2.connect(host=PG_HOST, dbname=PG_DB, user=PG_USER,
                                password=pw, connect_timeout=4)
    except Exception:
        return None


def facility_power_view() -> dict[str, Any]:
    """Per-channel + total energy/cost for daily (24h) and monthly (30d) windows,
    current watts (1h avg), collector health, honest staleness."""
    out: dict[str, Any] = {
        "error": None,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "channels": [],
        "totals": {},
        "collector": {},
        "rate": {},
    }
    from main import effective_rate

    now = datetime.now(timezone.utc)
    rate, rate_src = effective_rate(now.month)
    out["rate"] = {
        "effective_per_kwh": round(rate, 5),
        "display_cents": round(rate * 100, 2),
        "season": "summer" if now.month in (6, 7, 8) else "winter",
        "source": rate_src,
    }

    conn = _connect()
    if conn is None:
        out["error"] = "postgres unavailable"
        return out
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('facility_power_samples') IS NOT NULL")
            if not cur.fetchone()[0]:
                out["error"] = "Emporia collector not deployed (facility_power_samples missing)"
                return out

            # ---- collector health ------------------------------------------
            cur.execute("""
                SELECT count(*), max(ts) FROM facility_power_samples
                WHERE ts >= now() - interval '1 hour'""")
            n_1h, last_ts = cur.fetchone()
            cur.execute("SELECT count(*) FROM facility_power_samples")
            n_total = cur.fetchone()[0]
            last_age_s = None
            if last_ts is not None:
                cur.execute("SELECT EXTRACT(epoch FROM (now() - %s))", (last_ts,))
                last_age_s = float(cur.fetchone()[0])
            stale = last_ts is None or last_age_s is None or last_age_s > STALE_SAMPLE_SECONDS
            out["collector"] = {
                "samples_1h": int(n_1h or 0),
                "samples_total": int(n_total or 0),
                "last_sample_ts": last_ts.isoformat() if last_ts else None,
                "last_sample_age_seconds": round(last_age_s) if last_age_s is not None else None,
                "healthy": bool(n_1h and n_1h > 0),
                "stale": bool(stale),
                "stale_policy": "stale data shows STALE + timestamp — NEVER rendered as 0",
            }

            # ---- per-channel windows ---------------------------------------
            # current watts = avg over last hour; daily = 24h kWh; monthly = 30d kWh.
            # Each channel's staleness is judged from ITS OWN last sample (the
            # collector may drop a channel while others stay fresh).
            for cnum in ("1", "2", "3", "4"):
                label = CHANNEL_LABELS[cnum]
                cur.execute("""
                    SELECT AVG(s.watts) FROM facility_power_samples s
                    WHERE s.channel_num = %s AND s.ts >= now() - interval '1 hour'""",
                    (cnum,))
                avg_w = cur.fetchone()[0]
                cur.execute("""
                    SELECT SUM(s.kwh_interval) FROM facility_power_samples s
                    WHERE s.channel_num = %s AND s.ts >= now() - interval '24 hours'""",
                    (cnum,))
                kwh_24h = cur.fetchone()[0]
                cur.execute("""
                    SELECT SUM(s.kwh_interval) FROM facility_power_samples s
                    WHERE s.channel_num = %s AND s.ts >= now() - interval '30 days'""",
                    (cnum,))
                kwh_30d = cur.fetchone()[0]
                cur.execute("""
                    SELECT max(ts) FROM facility_power_samples s WHERE s.channel_num = %s""",
                    (cnum,))
                ch_last = cur.fetchone()[0]
                ch_age = None
                if ch_last is not None:
                    cur.execute("SELECT EXTRACT(epoch FROM (now() - %s))", (ch_last,))
                    ch_age = float(cur.fetchone()[0])
                ch_stale = ch_last is None or (ch_age is not None and ch_age > STALE_SAMPLE_SECONDS)
                out["channels"].append({
                    "channel_num": cnum,
                    "label": label,
                    "avg_watts_1h": round(float(avg_w), 1) if avg_w is not None and not ch_stale else None,
                    "kwh_24h": round(float(kwh_24h), 3) if kwh_24h is not None and not ch_stale else None,
                    "cost_usd_24h": round(float(kwh_24h) * rate, 4) if kwh_24h is not None and not ch_stale else None,
                    "kwh_30d": round(float(kwh_30d), 3) if kwh_30d is not None and not ch_stale else None,
                    "cost_usd_30d": round(float(kwh_30d) * rate, 4) if kwh_30d is not None and not ch_stale else None,
                    "last_sample_ts": ch_last.isoformat() if ch_last else None,
                    "last_sample_age_seconds": round(ch_age) if ch_age is not None else None,
                    "stale": bool(ch_stale),
                })

            # ---- TOTAL MARION_IA_USA = ch1+2+3+4 ----------------------------
            def _sum_ch(field: str) -> float | None:
                vals = [c.get(field) for c in out["channels"]]
                if any(v is None for v in vals):
                    return None  # any stale channel poisons the total honestly
                return round(sum(float(v) for v in vals), 3)

            t24, t30 = _sum_ch("kwh_24h"), _sum_ch("kwh_30d")
            out["totals"] = {
                "label": TOTAL_LABEL,
                "definition": "PDU MIAM-00151 + PDU MIAM-00152 + PDU MIAM-00153 + mini split",
                "kwh_24h": t24,
                "cost_usd_24h": round(t24 * rate, 4) if t24 is not None else None,
                "kwh_30d": t30,
                "cost_usd_30d": round(t30 * rate, 4) if t30 is not None else None,
                "incomplete_reason": "one or more channels STALE — total withheld (never zero-filled)"
                                    if t24 is None else None,
            }
        out["note"] = ("Energy measured by Emporia CTs (facility_power_samples); "
                       "cost = energy x effective utility rate. Stale channels/collector "
                       "show STALE + last timestamp, never 0.")
        return out
    except Exception as e:  # noqa: BLE001 — page must render with the error visible
        out["error"] = f"facility query failed: {e.__class__.__name__}: {e}"
        return out
    finally:
        conn.close()


POWER_HTML = r"""<!doctype html><html><head><title>Power &amp; Cost — LLM Manager</title><style>
body{font-family:system-ui;background:#0d1117;color:#c9d1d9;margin:0;padding:1.2rem}
a{color:#58a6ff}
.bad{color:#f85149}.ok{color:#3fb950}.warn{color:#d29922}.muted{color:#8b949e}
.st-STALE{color:#f0883e;font-weight:600}.st-OK{color:#3fb950;font-weight:600}
table.data{border-collapse:collapse;width:100%;margin-top:.8rem}
table.data th,table.data td{border:1px solid #30363d;padding:.45rem .6rem;font-size:.82rem;text-align:left}
th{background:#161b22}
.big{font-size:1.4rem;font-weight:700}
.cards{display:flex;gap:1rem;flex-wrap:wrap;margin:.8rem 0}
.card{border:1px solid #30363d;border-radius:8px;padding:.8rem 1rem;min-width:220px;background:#161b22}
</style></head><body>
<h1>Power &amp; Cost — MARION_IA_USA facility <span style="font-size:.8rem;color:#8b949e">__VER__</span></h1>
<div>__USER__ <span style="font-size:.8rem">__ROLE__</span> · <a href="/admin">← dashboard</a> · <a href="/admin/fleet">fleet</a></div>
<p class="muted" id="health">Loading collector health…</p>
<div id="content"></div>
<p class="muted">Emporia CT measurement; cost = energy × effective utility rate.
<b>Stale data shows STALE + last timestamp — never rendered as 0.</b>
TOTAL = PDU MIAM-00151 + PDU MIAM-00152 + PDU MIAM-00153 + mini split (authoritative mapping, 2026-10-01).</p>
<script>
async function load(){
  const r=await fetch('/api/facility/power');if(r.status===401){location='/login';return}
  const d=await r.json();
  if(d.error){document.getElementById('health').innerHTML='<span class="bad">'+d.error+'</span>';return}
  const c=d.collector||{};
  document.getElementById('health').innerHTML='collector: '+
    '<span class="'+(c.stale?'st-STALE':'st-OK')+'">'+(c.stale?'STALE':'OK')+'</span>'+
    ' · samples 1h: '+(c.samples_1h??'—')+' · last sample: '+(c.last_sample_ts||'—')+
    (c.last_sample_age_seconds!=null?' ('+c.last_sample_age_seconds+'s ago)':'')+
    ' · rate: $'+d.rate.effective_per_kwh+'/kWh ('+d.rate.season+', '+d.rate.source+')';
  let rows=d.channels.map(ch=>{
    const st=ch.stale?'<span class="st-STALE">STALE</span>':'<span class="st-OK">OK</span>';
    const fmt=(v,suf)=>v==null?'<span class="warn">stale—no value</span>':v+suf;
    return '<tr><td>'+ch.label+'</td><td>'+st+'</td>'+
      '<td>'+fmt(ch.avg_watts_1h,' W')+'</td>'+
      '<td>'+fmt(ch.kwh_24h,' kWh')+'</td><td>'+(ch.cost_usd_24h==null?'<span class="warn">stale</span>':'$'+ch.cost_usd_24h)+'</td>'+
      '<td>'+fmt(ch.kwh_30d,' kWh')+'</td><td>'+(ch.cost_usd_30d==null?'<span class="warn">stale</span>':'$'+ch.cost_usd_30d)+'</td>'+
      '<td class="muted">'+(ch.last_sample_ts||'—')+'</td></tr>';
  }).join('');
  const t=d.totals||{};
  document.getElementById('content').innerHTML=
   '<div class="cards">'+
     '<div class="card"><div class="muted">'+(t.label||'')+' — 24h</div><div class="big">'+
       (t.kwh_24h==null?'<span class="warn">WITHHELD (stale channel)</span>':t.kwh_24h+' kWh')+'</div>'+
       '<div>'+(t.cost_usd_24h==null?'':'$'+t.cost_usd_24h)+'</div></div>'+
     '<div class="card"><div class="muted">'+(t.label||'')+' — 30d</div><div class="big">'+
       (t.kwh_30d==null?'<span class="warn">WITHHELD (stale channel)</span>':t.kwh_30d+' kWh')+'</div>'+
       '<div>'+(t.cost_usd_30d==null?'':'$'+t.cost_usd_30d)+'</div></div>'+
   '</div>'+
   '<table class="data"><tr><th>channel</th><th>state</th><th>avg (1h)</th><th>24h</th><th>24h $</th><th>30d</th><th>30d $</th><th>last sample</th></tr>'+rows+'</table>'+
   (t.incomplete_reason?'<p class="warn">'+t.incomplete_reason+'</p>':'');
}
load();setInterval(load,60000);
</script></body></html>"""
