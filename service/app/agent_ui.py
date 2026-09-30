"""Agent Runtime Manager UI (window-5 plan §4) — reachable from the LLM Manager
operator shell.

Architecture boundary (plan §4): agent instantiation belongs in Agent Runtime
Manager; this module gives the OPERATOR SHELL (LLM Manager web, LDAP login)
server-side access to the SM/ARM API (:8300) via the dedicated
``svc-server-manager`` identity from the SAME service_tokens file. The LLM
Manager app runs on the same VM as SM — the token file is root-owned 0600.

Pages:
  /admin/agents            — runtime list (identity/runtime-id/VM/state/health)
  /admin/agents/{id}       — runtime detail (bridge/state-sync/desired-vs-actual)
  /admin/agents/new        — + New Agent wizard (6 steps, §4.2) calling the
                             EXISTING ARM provisioning path — no new semantics.
Wizard honesty rules (§4.3-§4.5):
  - unsupported policy/model fields render as unavailable, never faked;
  - internal workers = approved internal Hermes profile baseline; external
    workers = agentifyme-external-agent-base (NOT YET AUTHORIZED → the wizard
    offers internal only and shows external as unavailable);
  - clone-hygiene fail-closed validation already lives in ARM provisioning;
  - errors render the durable provisioning error + next action, never a raw 500.
"""
from __future__ import annotations

import json
import os
import urllib.request
import urllib.error

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse

_TOKEN_FILE = os.environ.get("SERVER_MANAGER_SERVICE_TOKENS_FILE",
                             "/etc/llm-manager/secrets/service_tokens")
_SM_BASE = os.environ.get("SERVER_MANAGER_API_BASE", "http://127.0.0.1:8300")
_TIMEOUT = int(os.environ.get("SERVER_MANAGER_UI_TIMEOUT", "30"))
_CREATE_TIMEOUT = int(os.environ.get("SERVER_MANAGER_UI_CREATE_TIMEOUT", "900"))


def _sm_token() -> str:
    """svc-server-manager bearer token (never logged, never returned)."""
    for line in open(_TOKEN_FILE):
        line = line.strip()
        if line.startswith("svc-server-manager="):
            return line.split("=", 1)[1].split(":", 1)[0]
    return ""


def sm_api(method: str, path: str, body: dict | None = None,
           timeout: int | None = None) -> tuple[int, dict | list | None, str]:
    """Call the Server Manager API as the server-manager service identity."""
    tok = _sm_token()
    if not tok:
        return 503, None, "server-manager service token not available"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{_SM_BASE}{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {tok}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=timeout or _TIMEOUT) as r:
            return r.status, json.load(r), ""
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e), ""
        except Exception:
            return e.code, None, e.read().decode(errors="replace")[:300]
    except Exception as e:
        return 599, None, f"{type(e).__name__}: {str(e)[:200]}"


def _derive_runtime_state(r: dict) -> tuple[str, str]:
    """Honest operator state from desired vs actual (never one green dot)."""
    actual = (r.get("actual_state") or "").upper()
    desired = (r.get("desired_state") or "").upper()
    if actual == "LIVE":
        return "LIVE", "ok"
    if actual in ("DESIRED_STOPPED", "STOPPED", "STOPPING"):
        return "STOPPED", "muted"
    if actual in ("ERROR", "FAILED", "SUPERSEDED"):
        return actual, "bad"
    if actual in ("PROVISIONING", "PENDING", "STARTING"):
        return actual, "warn"
    return actual or "UNKNOWN", "warn"


AGENTS_HTML = r"""<!doctype html><html><head><title>Agent Runtimes — LLM Manager</title><style>
body{font-family:system-ui;background:#0d1117;color:#c9d1d9;margin:0;padding:1.2rem}
a{color:#58a6ff}button{background:#238636;color:#fff;border:0;border-radius:6px;padding:.45rem .9rem;cursor:pointer}
button.gray{background:#30363d}input,select{background:#0d1117;color:#c9d1d9;border:1px solid #30363d;border-radius:6px;padding:.4rem}
table.data{border-collapse:collapse;width:100%;margin-top:.8rem}
table.data th,table.data td{border:1px solid #30363d;padding:.45rem .6rem;font-size:.82rem;text-align:left}
th{background:#161b22}
.bad{color:#f85149}.ok{color:#3fb950}.warn{color:#d29922}.muted{color:#8b949e}
.notice{border:1px solid #30363d;border-radius:8px;padding:.8rem;margin:.8rem 0;background:#161b22}
.st-LIVE{color:#3fb950;font-weight:600}.st-STOPPED{color:#8b949e}.st-ERROR,.st-FAILED,.st-SUPERSEDED{color:#f85149;font-weight:600}
</style></head><body>
<h1>Agent Runtimes <span style="font-size:.8rem;color:#8b949e">__VER__</span></h1>
<div>__USER__ <span style="font-size:.8rem">__ROLE__</span> · <a href="/admin">← dashboard</a> · <a href="/admin/agents/new">+ New Agent</a></div>
<div id="msg" class="notice" style="display:none"></div>
<div id="list"></div>
<script>
async function loadAgents(){
  const r=await fetch('/api/agent-runtimes');if(r.status===401){location='/login';return}
  const d=await r.json();
  if(d.error){document.getElementById('msg').style.display='block';
    document.getElementById('msg').innerHTML='<span class="bad">'+d.error+'</span> — Agent Runtime Manager integration unavailable (SM API down or token missing). Nothing is fabricated.';return}
  const rows=(d.runtimes||[]).map(x=>{
    const st=x.state||'?';
    return `<tr>
      <td><a href="/admin/agents/${x.runtime_id}">${x.name}</a></td>
      <td><code>${x.runtime_id.slice(0,8)}</code></td>
      <td><code>${(x.acms_agent_id||'').slice(0,8)}</code></td>
      <td>${x.node||'—'}/${x.vmid||'—'}</td>
      <td>${x.harness}</td>
      <td><span class="st-${st}">${st}</span> <span class="muted">desired=${x.desired_state}</span></td>
      <td>${x.state_sync_health||'PENDING'}</td>
      <td>${x.model_route||'—'}</td>
      <td class="muted">${x.last_error||''}</td>
    </tr>`;}).join('');
  document.getElementById('list').innerHTML =
   `<table class="data"><thead><tr><th>Name</th><th>Runtime</th><th>ACMS agent</th><th>Node/VM</th>
     <th>Harness</th><th>State</th><th>State-sync</th><th>Model route</th><th>Last error</th></tr></thead>
    <tbody>${rows||'<tr><td colspan=9 class="muted">no runtimes yet — use + New Agent</td></tr>'}</tbody></table>`;
}
loadAgents();
</script></body></html>"""

AGENT_DETAIL_HTML = r"""<!doctype html><html><head><title>Agent Runtime — LLM Manager</title><style>
body{font-family:system-ui;background:#0d1117;color:#c9d1d9;margin:0;padding:1.2rem}
a{color:#58a6ff}button{background:#238636;color:#fff;border:0;border-radius:6px;padding:.45rem .9rem;cursor:pointer}
button.gray{background:#30363d}
table.data{border-collapse:collapse;width:100%;margin-top:.8rem}
table.data th,table.data td{border:1px solid #30363d;padding:.45rem .6rem;font-size:.82rem;text-align:left}
th{background:#161b22}.bad{color:#f85149}.ok{color:#3fb950}.warn{color:#d29922}.muted{color:#8b949e}
.notice{border:1px solid #30363d;border-radius:8px;padding:.8rem;margin:.8rem 0;background:#161b22}
</style></head><body>
<h1>__NAME__ <span style="font-size:.8rem;color:#8b949e">__STATE__</span></h1>
<div><a href="/admin/agents">← all runtimes</a> · <a href="/admin">dashboard</a></div>
<div id="detail"></div>
<div class="notice" id="ctl"></div>
<script>
const RID='__RID__';
async function load(){
  const r=await fetch('/api/agent-runtimes/'+RID);if(r.status===401){location='/login';return}
  const x=await r.json();
  if(x.error){document.getElementById('detail').innerHTML='<div class="notice bad">'+x.error+'</div>';return}
  document.getElementById('detail').innerHTML=`
   <table class="data">
    <tr><th>runtime_id</th><td><code>${x.runtime_id}</code></td></tr>
    <tr><th>ACMS agent</th><td><code>${x.acms_agent_id||'—'}</code></td></tr>
    <tr><th>provisioning request</th><td><code>${x.provisioning_request_id||'—'}</code></td></tr>
    <tr><th>placement</th><td>${x.node||'—'} / VM ${x.vmid||'—'} (${x.site})</td></tr>
    <tr><th>harness / class</th><td>${x.harness} / ${x.runtime_class}</td></tr>
    <tr><th>state</th><td>desired=<b>${x.desired_state}</b> actual=<b>${x.actual_state}</b></td></tr>
    <tr><th>state-sync (ARM-owned)</th><td>${x.state_sync_health||'PENDING'} · branch=${x.hermes_state_branch||'—'} · sha=${(x.last_state_commit_sha||'—').slice(0,8)}</td></tr>
    <tr><th>model route</th><td>${x.model_route||'—'}</td></tr>
    <tr><th>recovery</th><td>count=${x.recovery_count??0} · last=${x.last_reconcile_at||'—'} ${x.last_error?`<span class="bad">${x.last_error}</span>`:''}</td></tr>
   </table>`;
  document.getElementById('ctl').innerHTML=`
   <h3>Lifecycle (authorized controls only)</h3>
   <button onclick="ctl('DESIRED_RUNNING')">Start</button>
   <button class="gray" onclick="ctl('DESIRED_STOPPED')">Stop</button>
   <button class="gray" onclick="rec()">Reconcile now</button>
   <p class="muted">Destroy is intentionally NOT exposed (ARM API-only, §4). PDU hard-power is never here.</p>`;
}
async function ctl(desired){
  const r=await fetch('/api/agent-runtimes/'+RID+'/desired-state',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({desired_state:desired,reason:'operator shell'})});
  const d=await r.json();alert(d.error?('refused: '+d.error):('desired='+desired+' — reconciler converges; check back'));
  load();
}
async function rec(){
  const r=await fetch('/api/agent-runtimes/'+RID+'/reconcile',{method:'POST'});
  const d=await r.json();alert(d.error?('refused: '+d.error):('reconcile: '+(d.action||'ok')+' ('+(d.before)+'→'+(d.after)+')'));
  load();
}
load();
</script></body></html>"""

NEW_AGENT_HTML = r"""<!doctype html><html><head><title>New Agent — LLM Manager</title><style>
body{font-family:system-ui;background:#0d1117;color:#c9d1d9;margin:0;padding:1.2rem;max-width:760px}
a{color:#58a6ff}button{background:#238636;color:#fff;border:0;border-radius:6px;padding:.5rem 1rem;cursor:pointer}
input,select,textarea{background:#0d1117;color:#c9d1d9;border:1px solid #30363d;border-radius:6px;padding:.45rem;width:100%;box-sizing:border-box}
label{display:block;margin:.7rem 0 .2rem;font-size:.85rem}
.bad{color:#f85149}.ok{color:#3fb950}.muted{color:#8b949e}
.notice{border:1px solid #30363d;border-radius:8px;padding:.8rem;margin:.8rem 0;background:#161b22}
.step{margin:1rem 0;border-top:1px solid #21262d;padding-top:.6rem}
code{background:#161b22;padding:.1rem .3rem;border-radius:4px}
</style></head><body>
<h1>+ New Agent <span class="muted" style="font-size:.8rem">(wizard → existing ARM provisioning; nothing new invented)</span></h1>
<div><a href="/admin/agents">← all runtimes</a></div>
<div class="notice"><b>Step 3 — State baseline (honest):</b> internal workers use the approved internal Hermes profile mechanism.
External/customer workers need <code>agentifyme-external-agent-base</code> which is NOT yet authorized —
external is shown but refused at create time.</div>
<form id="f">
 <div class="step"><h3>1 · Identity</h3>
  <label>Display name <input name="name" required placeholder="acms-worker-002"></label>
  <label>ACMS agent id — persistent identity (created/reserved in ACMS separately; paste or leave blank to
   auto-generate a placeholder that ARM records as correlation) <input name="acms_agent_id" placeholder="uuid"></label>
 </div>
 <div class="step"><h3>2 · Runtime</h3>
  <label>Harness <select name="harness"><option value="hermes">Hermes (only supported today)</option></select></label>
  <label>Runtime type <select name="runtime_class"><option value="software_development_worker">software_development_worker</option></select></label>
  <label>Placement — node (blank = automatic/preferred) <input name="node" placeholder="miam-00135"></label>
  <p class="muted">VM-based provisioning via the proven clone path (hygiene gate fail-closed).</p>
 </div>
 <div class="step"><h3>4 · Model route (logical profile preferred)</h3>
  <label>Route <select name="model_route">
    <option value="">— (server default)</option>
    <option value="worker">worker</option><option value="fast">fast</option>
    <option value="code">code</option><option value="review">review</option>
    <option value="frontier">frontier</option>
  </select></label>
 </div>
 <div class="step"><h3>5 · Budget / runtime policy</h3>
  <p class="muted">Budget fields are set in ACMS (REQ-059) after creation — not faked here.
  Runtime policy: ARM reconciler semantics apply (desired-state + reconcile).</p>
 </div>
 <div class="step"><h3>6 · Preview &amp; create</h3>
  <div id="preview" class="notice muted">Fill the fields to see the exact provisioning plan.</div>
  <button type="submit">Create (calls ARM provisioning — synchronous; may take minutes)</button>
 </div>
</form>
<div id="result"></div>
<script>
const f=document.getElementById('f');
f.addEventListener('input',()=>{
  const d=Object.fromEntries(new FormData(f).entries());
  document.getElementById('preview').innerHTML=
   `ACMS identity: <code>${d.acms_agent_id||'(auto-correlation placeholder)'}</code><br>`+
   `runtime: hermes / ${d.runtime_class} · node: ${d.node||'automatic'}<br>`+
   `model route: ${d.model_route||'(server default)'}<br>`+
   `provisioning: POST /api/v1/agent-runtimes (authority=sprint_execution_context)`;
});
f.addEventListener('submit',async e=>{
  e.preventDefault();
  const d=Object.fromEntries(new FormData(f).entries());
  const body={acms_agent_id:d.acms_agent_id||crypto.randomUUID(),request_id:'ui-'+crypto.randomUUID(),
    name:d.name,harness:d.harness,runtime_class:d.runtime_class,site:'marion-ia-usa',
    model_route:d.model_route||null,bridge_profile:null,desired_state:'DESIRED_RUNNING'};
  const r=await fetch('/api/agent-runtimes',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)});
  const x=await r.json();
  if(x.error){document.getElementById('result').innerHTML='<div class="notice"><span class="bad">'+x.error+'</span>'+
    '<p class="muted">Durable provisioning error — resolve the cause and retry; nothing partial was created.</p></div>';return}
  const job=x.job||x;
  document.getElementById('result').innerHTML='<div class="notice">job <code>'+job.job_id+'</code> state <b>'+job.state+'</b>'+
    (job.error?' — <span class="bad">'+job.error+'</span>':'')+
    (job.runtime_id?' — runtime <a href="/admin/agents/'+job.runtime_id+'">'+job.runtime_id.slice(0,8)+'</a>':'')+
    '<br><a href="/admin/agents">back to list</a></div>';
});
</script></body></html>"""


def render(page_html: str, request: Request) -> HTMLResponse | RedirectResponse:
    """Auth-gated render using the LLM Manager session (imported late to avoid cycles)."""
    from main import APP_VERSION, current_user

    user, role = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return HTMLResponse(page_html
                        .replace("__VER__", APP_VERSION)
                        .replace("__USER__", user)
                        .replace("__ROLE__", role or ""))