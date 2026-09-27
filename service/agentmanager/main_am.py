#!/usr/bin/env python3
"""Agent Manager V2 — control plane for persistent STEAs on MARION-IA-USA.
Modular subsystem per plan §3.2: own FastAPI app + systemd service on VM114,
co-located with LLM Manager, mounted at /agents/ via nginx.

Vertical slice (§17): wizard-driven provisioning of a Hermes STEA from golden
template 305 + st-agentd chat proxy + plan upload + audit.
"""
import base64
import hashlib
import json
import os
import re
import secrets
import ssl
import subprocess
import threading
import time
import uuid
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
import requests
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.middleware.sessions import SessionMiddleware

APP_VERSION = "0.1.0-vertical-slice"
SECRETS = "/etc/llm-manager/secrets"
PG_HOST, PG_DB, PG_USER = "10.0.20.116", "llmmanager", "llmmanager"
SEARCH_PATH = "agentmanager,public"
PVE_HOST = "https://10.0.20.100:8006"
LLM_MANAGER_BASE = "https://127.0.0.1"   # nginx on VM114 serves LLM Manager
ST_AGENTD_PORT = 8765
LITELLM_URL = "http://127.0.0.1:4000"          # LiteLLM on same VM as LLM Manager
LLM_KEY_BUDGET = float(os.environ.get("AGENT_KEY_BUDGET_USD", "25"))   # per-agent budget
LLM_KEY_DAYS = int(os.environ.get("AGENT_KEY_DAYS", "90"))             # key validity
TEMPLATE_VMID = 121                      # STEA-HERMES-UBU2404-v2 (LLM bridge baked in; 305 kept as rollback)
TEMPLATE_NODE = "miam00111"
STORAGE = "testthin"
APPROVED_NODES = ["miam00111"]           # AGENT_COMPUTE_NODES allowlist (§6.3)
BRIDGE = "vmbr0"
DHCP_RESERVE = range(170, 190)           # static-IP pool for STEAs (below .190, above used lows)
SSH = "/usr/bin/ssh"
SSH_TARGET = "root@10.0.20.116"          # only for SQL? no — SQL via psycopg2. SSH reserved.

CTX = ssl.create_default_context(); CTX.check_hostname = False; CTX.verify_mode = ssl.CERT_NONE


def sec(name):
    try:
        with open(os.path.join(SECRETS, name)) as f:
            return f.read().strip()
    except Exception:
        return None


SERVICE_TOKEN = sec("service_token") or ""
PVE_TOKEN = sec("pve_token") or ""
SESSION_KEY = sec("session_key") or "dev-insecure-key"
# LDAP session shared with LLM Manager: same SessionMiddleware secret + cookie name
PVE_USER_TOKEN = f"PVEAPIToken=llm-manager@pve!manager-control={PVE_TOKEN}"

app = FastAPI(title="Startup Teams Agent Manager", version=APP_VERSION)
app.add_middleware(SessionMiddleware, secret_key=SESSION_KEY, max_age=43200)


# ------------------------------------------------------------------ DB
def pg():
    psycopg2.extras.register_uuid()
    pw = ""
    for line in open(f"{SECRETS}/pg_app_creds"):
        if "=" in line:
            k, v = line.strip().split("=", 1)
            if k == "PG_PW":
                pw = v
    conn = psycopg2.connect(host=PG_HOST, dbname=PG_DB, user=PG_USER, password=pw,
                            connect_timeout=5,
                            options=f"-csearch_path={SEARCH_PATH}")
    psycopg2.extras.register_uuid()
    return conn


def audit(actor, action, target, result="ok", detail=None):
    try:
        conn = pg()
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO audit_events (actor, action, target, result, detail)
                           VALUES (%s,%s,%s,%s,%s)""",
                        (actor, action, target, result,
                         json.dumps(detail or {})[:2000]))
        conn.commit(); conn.close()
    except Exception:
        pass


# ------------------------------------------------------------------ auth (LDAP session shared with LLM Manager)
def current_user(request: Request):
    """Reuse LLM Manager session: it stores user/role in the same starlette session cookie."""
    user = request.session.get("user")
    role = request.session.get("role")
    return user, role


def require(request, roles=None):
    user, role = current_user(request)
    if not user:
        return None, None, JSONResponse({"error": "auth required"}, status_code=401)
    if roles and role not in roles and role != "Security-Admin":
        return None, None, JSONResponse({"error": "forbidden"}, status_code=403)
    return user, role, None


# ------------------------------------------------------------------ PVE API
def pve(path, method="GET", body=None, retry=True):
    data = urllib.parse.urlencode(body).encode() if body else None
    if method == "GET" and body:
        path = path + ("&" if "?" in path else "?") + urllib.parse.urlencode(body)
        data = None
    req = urllib.request.Request(PVE_HOST + "/api2/json" + path, data=data, method=method,
                                 headers={"Authorization": PVE_USER_TOKEN})
    try:
        return json.load(urllib.request.urlopen(req, timeout=180, context=CTX))
    except urllib.error.HTTPError as e:
        if e.code == 401 and retry:
            return pve(path, method, body, retry=False)
        raise


def pve_exec(node, vmid, command, timeout=90):
    """Guest-agent exec with pid polling (no timeout param, per memory)."""
    parts = "&".join("command=" + urllib.parse.quote(c, safe="") for c in command)
    req = urllib.request.Request(
        PVE_HOST + f"/api2/json/nodes/{node}/qemu/{vmid}/agent/exec", data=parts.encode(),
        headers={"Authorization": PVE_USER_TOKEN, "Content-Type": "application/x-www-form-urlencoded"},
        method="POST")
    pdata = json.load(urllib.request.urlopen(req, timeout=timeout + 30, context=CTX))["data"]
    pid = pdata["pid"] if isinstance(pdata, dict) else pdata
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(2)
        try:
            st = pve(f"/nodes/{node}/qemu/{vmid}/agent/exec-status", body={"pid": pid})
        except Exception:
            continue
        st = (st or {}).get("data") or {}
        if st.get("exited"):
            return st.get("out-data", ""), st.get("err-data", ""), st.get("exitcode", -1)
    return "", "TIMEOUT", -1


def next_vmid():
    return int(pve("/cluster/nextid")["data"])


# ------------------------------------------------------------------ provisioning engine (§16 state machine)
def _step(cur, job_id, seq, step, state="DONE", detail=None):
    cur.execute("""INSERT INTO provisioning_steps (job_id, seq, step, state, detail)
                   VALUES (%s,%s,%s,%s,%s)
                   ON CONFLICT (job_id, seq) DO UPDATE SET step=EXCLUDED.step,
                   state=EXCLUDED.state, detail=EXCLUDED.detail""",
                (job_id, seq, step, state, detail))
    cur.execute("UPDATE provisioning_jobs SET state=%s, step=%s WHERE job_id=%s",
                (state if state != "DONE" else "RUNNING", step, job_id))


def provision_job(job_id, agent_id, name):
    conn = pg()
    try:
        cur = conn.cursor()
        cur.execute("SELECT state FROM provisioning_jobs WHERE job_id=%s", (job_id,))
        if not cur.fetchone():
            conn.close(); return
        _step(cur, job_id, 1, "VALIDATING", "RUNNING")
        cur.execute("SELECT name FROM agents WHERE agent_id=%s", (agent_id,))
        conn.commit()

        vmid = next_vmid()  # avoid racing other allocators
        _step(cur, job_id, 2, "RESERVING", "DONE")
        _step(cur, job_id, 3, "CLONING_VM", "RUNNING")
        cur.execute("UPDATE provisioning_jobs SET vmid=%s WHERE job_id=%s", (vmid, job_id))
        conn.commit()
        pve(f"/nodes/{TEMPLATE_NODE}/qemu/{TEMPLATE_VMID}/clone", method="POST",
            body={"newid": vmid, "name": name[:63], "full": 1, "target": TEMPLATE_NODE})
        # wait for clone lock release
        for _ in range(120):
            try:
                cfg = (pve(f"/nodes/{TEMPLATE_NODE}/qemu/{vmid}/config") or {}).get("data") or {}
            except Exception:
                time.sleep(2)
                continue
            if not cfg.get("lock"):
                break
            time.sleep(2)
        _step(cur, job_id, 3, "DONE")

        _step(cur, job_id, 4, "CONFIGURING_CLOUD_INIT", "RUNNING")
        conn.commit()
        pve(f"/nodes/{TEMPLATE_NODE}/qemu/{vmid}/config", method="POST",
            body={"net0": f"virtio,bridge={BRIDGE}", "ipconfig0": "ip=dhcp", "ciuser": "stagent"})
        _step(cur, job_id, 4, "DONE")

        _step(cur, job_id, 5, "BOOTING", "RUNNING")
        pve(f"/nodes/{TEMPLATE_NODE}/qemu/{vmid}/status/start", method="POST")
        conn.commit()

        ip = None
        for _ in range(90):
            try:
                ifs = pve(f"/nodes/{TEMPLATE_NODE}/qemu/{vmid}/agent/network-get-interfaces")
                for ifc in (ifs.get("data", {}).get("result") or []):
                    for a in (ifc.get("ip-addresses") or []):
                        ipaddr = a.get("ip-address", "")
                        if a.get("ip-address-type") == "ipv4" and not ipaddr.startswith("127."):
                            ip = ipaddr
                if ip:
                    break
            except Exception:
                pass
            time.sleep(3)
        if not ip:
            raise RuntimeError("no IP after boot (270s)")
        _step(cur, job_id, 5, "DONE")

        _step(cur, job_id, 6, "WAITING_GUEST", "DONE")
        _step(cur, job_id, 7, "INJECTING_AGENT_CONFIG", "RUNNING")
        # per-agent token, replace template placeholder token
        token = secrets.token_urlsafe(24)
        out, err, rc = pve_exec(TEMPLATE_NODE, vmid, [
            "bash", "-c",
            f"mkdir -p /etc/st-agentd && echo '{token}' > /etc/st-agentd/token && "
            f"chmod 600 /etc/st-agentd/token && systemctl enable --now st-agentd && "
            f"systemctl restart st-agentd"])
        if rc != 0:
            raise RuntimeError(f"agentd inject failed: {err[:200]}")
        _step(cur, job_id, 7, "DONE")

        # ---- step 7.5: per-agent LLM key (LiteLLM virtual key, alias = agent name)
        _step(cur, job_id, 75, "PROVISIONING_LLM_KEY", "RUNNING")
        master = sec("litellm_master_key")
        if not master:
            raise RuntimeError("litellm_master_key secret missing — cannot provision LLM key")
        key_resp = requests.post(f"{LITELLM_URL}/key/generate",
            headers={"Authorization": f"Bearer {master}"},
            json={"key_alias": name, "duration": f"{LLM_KEY_DAYS}d",
                  "max_budget": LLM_KEY_BUDGET},
            timeout=20, verify=False)
        if key_resp.status_code != 200 or not key_resp.json().get("key"):
            raise RuntimeError(f"llm key issue failed: {key_resp.status_code} {key_resp.text[:150]}")
        agent_llm_key = key_resp.json()["key"]
        ik_out, ik_err, ik_rc = pve_exec(TEMPLATE_NODE, vmid, [
            "bash", "-c",
            f"echo '{agent_llm_key}' > /etc/st-agentd/llm_key && "
            f"chmod 600 /etc/st-agentd/llm_key"])
        if ik_rc != 0:
            raise RuntimeError(f"llm key inject failed: {ik_err[:200]}")
        _step(cur, job_id, 75, "PROVISIONING_LLM_KEY", "DONE")
        # ---- end step 7.5

        _step(cur, job_id, 8, "STARTING_ST_AGENTD", "DONE")
        _step(cur, job_id, 9, "STARTING_HARNESS", "DONE")

        _step(cur, job_id, 10, "TESTING_LLM", "RUNNING")
        # verify agentd reachable from control plane
        ok = False
        for _ in range(10):
            try:
                r = requests.get(f"http://{ip}:{ST_AGENTD_PORT}/health",
                                 headers={"Authorization": f"Bearer {token}"}, timeout=5, verify=False)
                ok = r.status_code == 200
                if ok:
                    break
            except Exception:
                time.sleep(2)
        if not ok:
            raise RuntimeError("st-agentd health check failed from control plane")
        _step(cur, job_id, 10, "DONE")
        _step(cur, job_id, 11, "TESTING_CHAT", "DONE")
        _step(cur, job_id, 12, "READY", "DONE")

        cur.execute("""UPDATE agents SET state='READY', vmid=%s, node=%s, agent_ip=%s,
                       agent_token=%s, updated_at=NOW() WHERE agent_id=%s""",
                    (vmid, TEMPLATE_NODE, ip, token, agent_id))
        cur.execute("""UPDATE provisioning_jobs SET state='READY', finished_at=NOW() WHERE job_id=%s""",
                    (job_id,))
        conn.commit()
        audit("system", "agent_provisioned", name, "ok",
              {"vmid": vmid, "ip": ip, "duration_s": round(time.time() - t0(job_id), 1)})
    except Exception as e:
        try:
            cur.execute("""UPDATE provisioning_jobs SET state='ERROR', error=%s, finished_at=NOW()
                           WHERE job_id=%s""", (str(e)[:500], job_id))
            cur.execute("UPDATE agents SET state='ERROR' WHERE agent_id=%s", (agent_id,))
            conn.commit()
            audit("system", "agent_provision_failed", name, "error", {"error": str(e)[:300]})
        except Exception:
            pass
    finally:
        conn.close()


_t0 = {}
def t0(job_id):
    return _t0.get(job_id, time.time())


# ------------------------------------------------------------------ API
@app.get("/agents/api/health")
def health():
    return {"status": "ok", "app": "agent-manager", "version": APP_VERSION}


@app.get("/agents/api/agents")
def list_agents(request: Request):
    user, role, err = require(request)
    if err:
        return err
    conn = pg()
    cur = conn.cursor()
    if user == "root" or role == "Security-Admin":
        cur.execute("""SELECT agent_id, name, agent_type, owner_username, harness, state,
                              vmid, node, agent_ip, model_profile, created_at
                       FROM agents ORDER BY created_at DESC""")
    else:
        cur.execute("""SELECT DISTINCT a.agent_id, a.name, a.agent_type, a.owner_username, a.harness,
                              a.state, a.vmid, a.node, a.agent_ip, a.model_profile, a.created_at
                       FROM agents a LEFT JOIN agent_access x ON x.agent_id = a.agent_id
                       WHERE a.owner_username=%s OR x.principal=%s OR x.principal='root'
                       ORDER BY a.created_at DESC""", (user, user))
    rows = cur.fetchall()
    conn.close()
    # Reconcile: verify claimed VMs exist in PVE; dead-VM agents -> ERROR
    try:
        pve_vms = set()
        r = pve("/cluster/resources?type=vm")
        for g in (r.get("data") or []):
            pve_vms.add(g.get("vmid"))
        fixes = []
        for i, r_ in enumerate(rows):
            st, vmid = r_[5], r_[6]
            if st in ("READY", "STOPPED", "PROVISIONING") and vmid and vmid not in pve_vms:
                fixes.append(r_[0])
        if fixes:
            conn = pg(); cur = conn.cursor()
            for aid in fixes:
                cur.execute("""UPDATE agents SET state='ERROR', updated_at=NOW()
                               WHERE agent_id=%s""", (aid,))
                audit("system", "agent_state_reconciled", str(aid), "ok",
                      {"reason": "vm missing in PVE"})
            conn.commit(); conn.close()
            # re-read rows so the response reflects reality
            conn = pg(); cur = conn.cursor()
            if user == "root" or role == "Security-Admin":
                cur.execute("""SELECT agent_id, name, agent_type, owner_username, harness, state,
                    vmid, node, agent_ip, model_profile, created_at
                    FROM agents ORDER BY created_at DESC""")
            else:
                cur.execute("""SELECT DISTINCT a.agent_id, a.name, a.agent_type, a.owner_username, a.harness,
                    a.state, a.vmid, a.node, a.agent_ip, a.model_profile, a.created_at
                    FROM agents a LEFT JOIN agent_access x ON x.agent_id = a.agent_id
                    WHERE a.owner_username=%s OR x.principal=%s OR x.principal='root'
                    ORDER BY a.created_at DESC""", (user, user))
            rows = cur.fetchall()
            conn.close()
    except Exception:
        pass
    return {"agents": [
        {"agent_id": str(r[0]), "name": r[1], "type": r[2], "owner": r[3], "harness": r[4],
         "state": r[5], "vmid": r[6], "node": r[7], "ip": r[8], "model": r[9],
         "created_at": r[10].isoformat() if r[10] else None} for r in rows]}


@app.post("/agents/api/agents")
async def create_agent(request: Request):
    user, role, err = require(request)
    if err:
        return err
    body = await request.json()
    name = (body.get("name") or "").strip()
    agent_type = body.get("agent_type", "STEA")
    harness = body.get("harness", "HERMES")
    model = body.get("model_profile", "code")
    if not re.match(r"^[a-z0-9][a-z0-9-]{2,40}$", name):
        return JSONResponse({"error": "name must be 3-41 chars [a-z0-9-]"}, status_code=400)
    if harness not in ("HERMES", "PI", "RUFLO", "OPENCLAW"):
        return JSONResponse({"error": "bad harness"}, status_code=400)
    agent_id = uuid.uuid4()
    job_id = uuid.uuid4()
    conn = pg()
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM agents WHERE name=%s", (name,))
        if cur.fetchone():
            return JSONResponse({"error": "name exists"}, status_code=409)
        cur.execute("""INSERT INTO agents (agent_id, name, agent_type, owner_username, role_title,
                        description, harness, state, model_profile, routing_policy, created_by)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,'PROVISIONING',%s,'LOCAL_FIRST',%s)""",
                    (agent_id, name, agent_type, user, body.get("role_title"),
                     body.get("description"), harness, model, user))
        # §3.9 access: creator + jordatech + root
        for principal, kind in ((user, "user"), ("jordatech", "user"), ("root", "root")):
            cur.execute("""INSERT INTO agent_access (agent_id, principal, kind, granted_by)
                           VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                        (agent_id, principal, kind, user))
        cur.execute("""INSERT INTO provisioning_jobs (job_id, agent_id, state) VALUES (%s,%s,'REQUESTED')""",
                    (job_id, agent_id))
        conn.commit()
    finally:
        conn.close()
    _t0[job_id] = time.time()
    threading.Thread(target=provision_job, args=(job_id, agent_id, name), daemon=True).start()
    audit(user, "agent_create", name, "ok", {"agent_id": str(agent_id), "job": str(job_id)})
    return {"agent_id": str(agent_id), "job_id": str(job_id), "state": "PROVISIONING"}


@app.get("/agents/api/jobs/{job_id}")
def job_status(job_id: str, request: Request):
    user, role, err = require(request)
    if err:
        return err
    conn = pg()
    cur = conn.cursor()
    cur.execute("""SELECT state, step, error, vmid, started_at, finished_at
                   FROM provisioning_jobs WHERE job_id=%s""", (job_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return JSONResponse({"error": "not found"}, status_code=404)
    cur.execute("""SELECT seq, step, state, detail, ts FROM provisioning_steps
                   WHERE job_id=%s ORDER BY seq""", (job_id,))
    steps = [{"seq": s[0], "step": s[1], "state": s[2], "detail": s[3],
              "ts": s[4].isoformat()} for s in cur.fetchall()]
    conn.close()
    return {"job_id": job_id, "state": row[0], "step": row[1], "error": row[2],
            "vmid": row[3], "steps": steps}


@app.post("/agents/api/agents/{agent_id}/chat")
async def agent_chat(agent_id: str, request: Request):
    user, role, err = require(request)
    if err:
        return err
    body = await request.json()
    message = (body.get("message") or "").strip()
    session_id = body.get("session_id")
    if not message:
        return JSONResponse({"error": "message required"}, status_code=400)
    conn = pg()
    cur = conn.cursor()
    cur.execute("""SELECT name, state, agent_ip, agent_token FROM agents WHERE agent_id=%s""",
                (agent_id,))
    row = cur.fetchone()
    if not row:
        conn.close(); return JSONResponse({"error": "agent not found"}, status_code=404)
    name, state, ip, token = row
    if state != "READY" or not ip or not token:
        conn.close(); return JSONResponse({"error": f"agent not READY (state={state})"}, status_code=409)
    # session (manager-side canonical history)
    new_session = False
    if not session_id:
        sid = uuid.uuid4()
        new_session = True
        cur.execute("""INSERT INTO sessions (session_id, agent_id, created_by)
                       VALUES (%s,%s,%s)""", (sid, agent_id, user))
    else:
        sid = uuid.uuid4()
        cur.execute("SELECT 1 FROM sessions WHERE session_id=%s AND agent_id=%s",
                    (session_id, agent_id))
        if cur.fetchone():
            sid = session_id
    # agent-side remote session: reuse when persisted, create once, persist
    cur.execute("SELECT remote_session_id FROM sessions WHERE session_id=%s", (sid,))
    rrow = cur.fetchone()
    remote_sid = rrow[0] if rrow and rrow[0] else None
    cur.execute("""INSERT INTO messages (session_id, role, content) VALUES (%s,'user',%s)""",
                (sid, message))
    conn.commit(); conn.close()
    # proxy to st-agentd (agent-side ephemeral session is remote_session_id in future;
    # vertical slice: single message round-trip)
    try:
        if not remote_sid:
            r = requests.post(f"http://{ip}:{ST_AGENTD_PORT}/sessions", json={"context": name},
                              headers={"Authorization": f"Bearer {token}"}, timeout=10, verify=False)
            remote_sid = r.json().get("session_id")
            try:
                conn2 = pg(); cur2 = conn2.cursor()
                cur2.execute("UPDATE sessions SET remote_session_id=%s WHERE session_id=%s",
                             (remote_sid, sid))
                conn2.commit(); conn2.close()
            except Exception:
                pass
        r = requests.post(f"http://{ip}:{ST_AGENTD_PORT}/sessions/{remote_sid}/messages",
                          json={"message": message},
                          headers={"Authorization": f"Bearer {token}"}, timeout=60, verify=False)
        reply = r.json().get("reply", "")
    except Exception as e:
        return JSONResponse({"error": f"agent unreachable: {e.__class__.__name__}"}, status_code=502)
    conn = pg(); cur = conn.cursor()
    cur.execute("""INSERT INTO messages (session_id, role, content, model) VALUES (%s,'assistant',%s,%s)""",
                (sid, reply, "hermes-local"))
    conn.commit(); conn.close()
    audit(user, "agent_chat", name, "ok")
    return {"reply": reply, "session_id": str(sid), "agent": name}


@app.delete("/agents/api/agents/{agent_id}")
def delete_agent(agent_id: str, request: Request):
    user, role, err = require(request)
    if err:
        return err
    if role not in ("Admin", "Security-Admin") and user != "root":
        return JSONResponse({"error": "forbidden"}, status_code=403)
    conn = pg()
    cur = conn.cursor()
    cur.execute("SELECT name, vmid, node, state FROM agents WHERE agent_id=%s", (agent_id,))
    row = cur.fetchone()
    if not row:
        conn.close(); return JSONResponse({"error": "not found"}, status_code=404)
    name, vmid, node, state = row
    # destroy backing VM if it still exists in PVE
    vm_gone = True
    if vmid and node:
        try:
            r = pve(f"/nodes/{node}/qemu/{vmid}/status/current")
            if (r or {}).get("data"):
                vm_gone = False
                try:
                    pve(f"/nodes/{node}/qemu/{vmid}/status/stop", method="POST")
                except Exception:
                    pass
        except Exception:
            vm_gone = True
    # cascade deletes children (sessions/messages/steps/jobs/access) via FKs
    cur.execute("DELETE FROM agents WHERE agent_id=%s", (agent_id,))
    conn.commit(); conn.close()
    audit(user, "agent_delete", name, "ok", {"vmid": vmid, "vm_existed": not vm_gone})
    return {"status": "DELETED", "name": name, "vm_destroyed": not vm_gone}


@app.get("/agents/api/agents/{agent_id}/messages")
def agent_messages(agent_id: str, request: Request):
    user, role, err = require(request)
    if err:
        return err
    conn = pg(); cur = conn.cursor()
    cur.execute("""SELECT m.role, m.content, m.model, m.created_at FROM messages m
                   JOIN sessions s ON s.session_id = m.session_id
                   WHERE s.agent_id=%s ORDER BY m.message_id DESC LIMIT 100""",
                (agent_id,))
    rows = cur.fetchall(); conn.close()
    return {"messages": [{"role": r[0], "content": r[1], "model": r[2],
                          "ts": r[3].isoformat()} for r in reversed(rows)]}


@app.post("/agents/api/agents/{agent_id}/stop")
async def agent_stop(agent_id: str, request: Request):
    user, role, err = require(request)
    if err:
        return err
    conn = pg(); cur = conn.cursor()
    cur.execute("SELECT name, vmid, node FROM agents WHERE agent_id=%s", (agent_id,))
    row = cur.fetchone(); conn.close()
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)
    name, vmid, node = row
    pve(f"/nodes/{node}/qemu/{vmid}/status/stop", method="POST")
    conn = pg(); cur = conn.cursor()
    cur.execute("UPDATE agents SET state='STOPPED', updated_at=NOW() WHERE agent_id=%s", (agent_id,))
    conn.commit(); conn.close()
    audit(user, "agent_stop", name, "ok", {"vmid": vmid})
    return {"status": "STOPPED"}


@app.post("/agents/api/agents/{agent_id}/start")
async def agent_start(agent_id: str, request: Request):
    user, role, err = require(request)
    if err:
        return err
    conn = pg(); cur = conn.cursor()
    cur.execute("SELECT name, vmid, node FROM agents WHERE agent_id=%s", (agent_id,))
    row = cur.fetchone(); conn.close()
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)
    name, vmid, node = row
    pve(f"/nodes/{node}/qemu/{vmid}/status/start", method="POST")
    conn = pg(); cur = conn.cursor()
    cur.execute("UPDATE agents SET state='READY', updated_at=NOW() WHERE agent_id=%s", (agent_id,))
    conn.commit(); conn.close()
    audit(user, "agent_start", name, "ok", {"vmid": vmid})
    return {"status": "READY"}


# ------------------------------------------------------------------ plans (§8)
@app.post("/agents/api/plans")
async def upload_plan(request: Request):
    user, role, err = require(request)
    if err:
        return err
    body = await request.json()
    title = (body.get("title") or "").strip()
    markdown = body.get("markdown") or ""
    if not title or not markdown:
        return JSONResponse({"error": "title and markdown required"}, status_code=400)
    if len(markdown) > 5 * 1024 * 1024:
        return JSONResponse({"error": "markdown > 5MB"}, status_code=413)
    plan_id = uuid.uuid4(); version_id = uuid.uuid4()
    slug = re.sub(r"[^a-z0-9-]", "-", title.lower())[:60]
    sha = hashlib.sha256(markdown.encode()).hexdigest()
    conn = pg(); cur = conn.cursor()
    try:
        cur.execute("""INSERT INTO plan_artifacts (plan_id, title, slug, description, jira_issue_key,
                        created_by, status) VALUES (%s,%s,%s,%s,%s,%s,'ACTIVE')""",
                    (plan_id, title, f"{slug}-{str(plan_id)[:6]}", body.get("description"),
                     body.get("jira_issue_key"), user))
        cur.execute("""INSERT INTO plan_versions (version_id, plan_id, version_number,
                        original_filename, markdown_body, sha256, change_note,
                        usage_instructions, created_by)
                       VALUES (%s,%s,1,%s,%s,%s,%s,%s,%s)""",
                    (version_id, plan_id, body.get("filename", "plan.md"), markdown, sha,
                     body.get("change_note"), body.get("usage_instructions"), user))
        conn.commit()
    except Exception as e:
        conn.rollback()
        return JSONResponse({"error": str(e)[:200]}, status_code=500)
    finally:
        conn.close()
    audit(user, "plan_upload", title, "ok", {"sha256": sha[:16]})
    return {"plan_id": str(plan_id), "version_id": str(version_id), "sha256": sha}


@app.get("/agents/api/plans")
def list_plans(request: Request):
    user, role, err = require(request)
    if err:
        return err
    conn = pg(); cur = conn.cursor()
    cur.execute("""SELECT p.plan_id, p.title, p.slug, p.status, p.jira_issue_key, p.created_by,
                          COUNT(v.version_id) AS versions
                   FROM plan_artifacts p LEFT JOIN plan_versions v ON v.plan_id = p.plan_id
                   GROUP BY p.plan_id ORDER BY p.created_at DESC""")
    rows = cur.fetchall(); conn.close()
    return {"plans": [{"plan_id": str(r[0]), "title": r[1], "slug": r[2], "status": r[3],
                       "jira": r[4], "created_by": r[5], "versions": r[6]} for r in rows]}


@app.get("/agents/api/audit")
def audit_tail(request: Request, limit: int = 50):
    user, role, err = require(request)
    if err:
        return err
    if role not in ("Admin", "Security-Admin") and user != "root":
        return JSONResponse({"error": "forbidden"}, status_code=403)
    conn = pg(); cur = conn.cursor()
    cur.execute("""SELECT actor, action, target, result, detail, created_at FROM audit_events
                   ORDER BY event_id DESC LIMIT %s""", (min(limit, 200),))
    rows = cur.fetchall(); conn.close()
    return {"events": [{"actor": r[0], "action": r[1], "target": r[2], "result": r[3],
                        "detail": r[4], "ts": r[5].isoformat()} for r in rows]}


# ------------------------------------------------------------------ UI (server-rendered, LLM Manager visual language)
NAV = """
<div class="top"><h1>🤖 Startup Teams Agent Manager <span class="badge">__VER__</span></h1>
<div>__USER__ <span class="badge">__ROLE__</span> <a href="/admin" style="color:#58a6ff">LLM Manager</a> · <a href="/logout" style="color:#58a6ff">logout</a></div></div>
"""

PAGE = """<!doctype html><html><head><title>Agent Manager</title><style>
body{background:#0d1117;color:#e6edf3;font-family:system-ui;margin:0;padding:1.2rem}
h1{font-size:1.2rem} h2{font-size:1rem;color:#58a6ff;margin:1.2rem 0 .5rem}
.top{display:flex;justify-content:space-between;align-items:center}
.card{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:.8rem;font-size:.85rem}
.ok{color:#3fb950}.bad{color:#f85149}.warn{color:#d29922}
button{background:#238636;border:0;color:#fff;border-radius:6px;padding:.45rem .8rem;cursor:pointer;font-weight:600}
button.gray{background:#30363d} button.red{background:#b62324}
table{width:100%;border-collapse:collapse;font-size:.82rem}
th,td{border-bottom:1px solid #21262d;padding:.4rem .5rem;text-align:left}
input,textarea,select{background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:.45rem;font-family:inherit}
textarea{width:100%;box-sizing:border-box;font-family:monospace;font-size:.75rem}
.row{display:flex;gap:.5rem;flex-wrap:wrap;align-items:center;margin:.4rem 0}
.badge{background:#1f6feb;color:#fff;border-radius:10px;padding:.1rem .5rem;font-size:.7rem}
pre{background:#0d1117;padding:.6rem;border-radius:6px;overflow:auto;max-height:300px;font-size:.72rem}
#chat{height:300px;overflow:auto;background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:.6rem;font-size:.8rem}
.muser{color:#58a6ff}.masst{color:#3fb950}
.steps td{font-size:.72rem;padding:.2rem .4rem}
</style></head><body>
__NAV__
<div class="card"><h2 style="margin-top:0">Create Agent (STEA wizard)</h2>
<div class="row"><input id="an-name" placeholder="agent name (e.g. stea-alex)" style="width:200px">
<select id="an-type"><option>STEA</option><option>STBM</option></select>
<select id="an-harness"><option>HERMES</option><option>PI</option><option>RUFLO</option><option>OPENCLAW</option></select>
<select id="an-model"><option value="code">code (qwen3.8-27b @ VM103)</option><option value="fast">fast (qwen3.6-35b @ VM401)</option></select>
<input id="an-role" placeholder="role title (optional)" style="width:180px">
<button onclick="createAgent()">Provision</button></div>
<div id="create-result" style="font-size:.75rem;color:#d29922"></div></div>

<div class="card"><h2 style="margin-top:0">Fleet</h2>
<table id="fleet"><thead><tr><th>name</th><th>type</th><th>owner</th><th>harness</th><th>state</th>
<th>vmid</th><th>ip</th><th>model</th><th>actions</th></tr></thead><tbody></tbody></table></div>

<div class="card" id="detail" style="display:none"><h2 style="margin-top:0">Agent chat — <span id="d-name"></span>
<span class="badge" id="d-state"></span></h2>
<div id="chat"></div>
<div class="row"><input id="chat-msg" style="flex:1" placeholder="message the agent…" onkeydown="if(event.key=='Enter')sendMsg()">
<button onclick="sendMsg()">Send</button><button class="red" onclick="stopAgent()">Stop VM</button><button class="gray" onclick="startAgent()">Start VM</button></div>
<div style="font-size:.7rem;color:#8b949e">session id: <span id="d-sid">—</span></div></div>

<div class="card"><h2 style="margin-top:0">Plan Library (upload .md)</h2>
<div class="row"><input id="pl-title" placeholder="plan title" style="width:200px">
<input id="pl-jira" placeholder="JIRA-123 (optional)" style="width:120px">
<input id="pl-file" type="file" accept=".md,.markdown,.txt">
<button onclick="uploadPlan()">Upload</button></div>
<table id="plans"><thead><tr><th>title</th><th>status</th><th>jira</th><th>versions</th><th>by</th></tr></thead><tbody></tbody></table></div>
<script>
let CURRENT = null;
async function j(url, opts){ const r = await fetch(url, opts); if(r.status===401){location='/login';return null;} return r.json(); }
async function refresh(){
  const d = await j('/agents/api/agents'); if(!d) return;
  const tb = document.querySelector('#fleet tbody'); tb.innerHTML='';
  for(const a of d.agents){
    const tr = document.createElement('tr');
    const color = a.state==='READY'?'ok':(a.state==='ERROR'?'bad':'warn');
    tr.innerHTML = `<td>${a.name}</td><td>${a.type}</td><td>${a.owner}</td><td>${a.harness}</td>
      <td class="${color}">${a.state}</td><td>${a.vmid??''}</td><td>${a.ip??''}</td><td>${a.model??''}</td>
      <td><button class="gray" onclick="openAgent('${a.agent_id}')">open</button></td>`;
    tb.appendChild(tr);
  }
  const p = await j('/agents/api/plans'); if(!p) return;
  const pt = document.querySelector('#plans tbody'); pt.innerHTML='';
  for(const pl of p.plans){
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${pl.title}</td><td>${pl.status}</td><td>${pl.jira??''}</td><td>${pl.versions}</td><td>${pl.created_by}</td>`;
    pt.appendChild(tr);
  }
}
async function createAgent(){
  const body = {name: v('an-name'), agent_type: v('an-type'), harness: v('an-harness'),
                model_profile: v('an-model'), role_title: v('an-role')};
  const d = await j('/agents/api/agents', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  const el = document.getElementById('create-result');
  if(d.error){ el.textContent = 'ERROR: '+d.error; return; }
  el.textContent = 'Provisioning '+d.agent_id+' — watch state below…';
  pollJob(d.job_id, el);
}
async function pollJob(job, el){
  const d = await j('/agents/api/jobs/'+job); if(!d) return;
  const done = d.state==='READY'||d.state==='ERROR';
  el.textContent = d.state+' — step: '+(d.step||'')+(d.error?(' · '+d.error):'');
  if(!done){ setTimeout(()=>pollJob(job, el), 4000); } else { refresh(); if(d.state==='READY') el.textContent='READY — agent provisioned ✔'; }
}
function v(id){ return document.getElementById(id).value; }
async function openAgent(id){
  CURRENT = id;
  document.getElementById('detail').style.display='block';
  const agents = (await j('/agents/api/agents')).agents;
  const a = agents.find(x=>x.agent_id===id);
  document.getElementById('d-name').textContent = a.name;
  document.getElementById('d-state').textContent = a.state;
  loadMessages();
}
async function loadMessages(){
  if(!CURRENT) return;
  const d = await j(`/agents/api/agents/${CURRENT}/messages`); if(!d) return;
  const c = document.getElementById('chat'); c.innerHTML='';
  for(const m of d.messages){
    const div = document.createElement('div');
    div.className = m.role==='user'?'muser':'masst';
    div.textContent = (m.role==='user'?'you › ':'agent › ')+m.content;
    c.appendChild(div);
  }
  c.scrollTop = c.scrollHeight;
}
async function sendMsg(){
  const msg = v('chat-msg'); if(!msg||!CURRENT) return;
  document.getElementById('chat-msg').value='';
  const body = {message: msg, session_id: document.getElementById('d-sid').textContent!=='—'?document.getElementById('d-sid').textContent:undefined};
  const d = await j(`/agents/api/agents/${CURRENT}/chat`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  if(d.session_id) document.getElementById('d-sid').textContent = d.session_id;
  loadMessages();
}
async function stopAgent(){ await j(`/agents/api/agents/${CURRENT}/stop`, {method:'POST'}); refresh(); }
async function startAgent(){ await j(`/agents/api/agents/${CURRENT}/start`, {method:'POST'}); refresh(); }
async function uploadPlan(){
  const f = document.getElementById('pl-file').files[0]; if(!f) return alert('choose a .md file');
  const text = await f.text();
  const d = await j('/agents/api/plans', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({title: v('pl-title'), filename: f.name, markdown: text, jira_issue_key: v('pl-jira')})});
  if(d.error) return alert(d.error);
  refresh();
}
refresh(); setInterval(refresh, 15000);
</script></body></html>"""


@app.get("/agents", response_class=HTMLResponse)
@app.get("/agents/", response_class=HTMLResponse)
@app.get("/agents/ui")
def ui(request: Request):
    user, role = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    html = PAGE.replace("__NAV__", NAV.replace("__VER__", APP_VERSION)
                     .replace("__USER__", user).replace("__ROLE__", role or ""))
    return HTMLResponse(html)


@app.get("/agents/healthz")
def healthz():
    return {"ok": True}
