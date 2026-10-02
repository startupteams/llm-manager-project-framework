"""Sandbox network identity (plan §W4.1) — OPNsense/Kea reservation isolation.

The W4 live-found defect: a sandbox's DHCP lease collided with a production
worker's STATIC address (10.0.20.203) because worker statics sit inside the Kea
dynamic pool and sandboxes boot as unclassified DHCP clients.

Remediation design (plan §W4.1, approved fallback):
- The OPNsense Kea plugin (v1.0.6) exposes NO client-classes; raw manual Kea
  config is out of bounds ("do not invent unsupported syntax"). The sanctioned
  fallback is **Server-Manager-managed Kea reservations**: before a sandbox's
  first network boot, SM creates a Kea reservation pinning the sandbox MAC to a
  dedicated sandbox-pool address (SERVER_MANAGER_SANDBOX_DHCP_RANGE, default
  10.0.20.222-10.0.20.249 — occupancy-verified 2026-10-02). Kea carves
  reserved addresses out of the dynamic pool, so:
    * a sandbox ALWAYS receives its pre-reserved sandbox-pool address,
    * ordinary clients can NEVER receive a reserved (sandbox) address,
    * classification is deterministic before the first network boot.
- FAIL-CLOSED: no reservation → no boot; no free sandbox-pool address → the
  provisioning job FAILS (never borrows from the general pool).
- Release: when a sandbox is destroyed (API destroy path or TTL-desired flip
  observed), the reservation is deleted so the address can be reused safely.

This module never mutates pools/ranges/subnets — ONLY adds/deletes
reservations — and holds NO credentials in Git (session-auth cookie via the
root-password contract file, same pattern as other VM114 services).
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from ipaddress import IPv4Address
from typing import Any

from server_manager.common.config.settings import get_settings

DEFAULT_RANGE_START = "10.0.20.222"
DEFAULT_RANGE_END = "10.0.20.249"
SUBNET_DESCRIPTION_TAG = "ACMS_SANDBOX"


class SandboxNetworkError(RuntimeError):
    """Reservation lifecycle failure (fail-closed semantics)."""


@dataclass
class ReservationRecord:
    ip: str
    mac: str
    hostname: str
    uuid: str
    description: str = ""


def _mac_norm(mac: str) -> str:
    return (mac or "").strip().lower().replace("-", ":")


def _valid_mac(mac: str) -> bool:
    return bool(re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", _mac_norm(mac)))


def parse_pool_range(spec: str) -> tuple[IPv4Address, IPv4Address]:
    """"a.b.c.d - w.x.y.z" (or comma variants) → (start, end). Fail-closed."""
    m = re.search(r"(\d+\.\d+\.\d+\.\d+)\s*-\s*(\d+\.\d+\.\d+\.\d+)", spec or "")
    if not m:
        raise SandboxNetworkError(f"invalid sandbox DHCP range spec: {spec!r}")
    start, end = IPv4Address(m.group(1)), IPv4Address(m.group(2))
    if start > end:
        raise SandboxNetworkError(f"sandbox DHCP range start>end: {spec!r}")
    return start, end


class KeaReservationClient:
    """OPNsense session-auth client for Kea DHCPv4 reservation CRUD.

    Endpoints (live-verified 2026-10-02):
      POST /api/kea/dhcpv4/add_reservation/   {"reservation": {...}}
      POST /api/kea/dhcpv4/del_reservation/{uuid}/
      GET  /api/kea/dhcpv4/search_reservation/
      GET  /api/kea/leases4/search/
      POST /api/kea/service/reconfigure       (apply model → running Kea)
    """

    def __init__(self, base_url: str, password: str, *, subnet_description: str | None = None,
                 timeout: int = 20):
        if not base_url or not password:
            raise SandboxNetworkError(
                "OPNsense base URL / password not configured (sandbox network gate)")
        self.base = base_url.rstrip("/")
        self.password = password
        self.timeout = timeout
        self._cookie_jar = ""
        self._csrf = ""
        self._subnet_uuid_cache: str | None = None
        self._subnet_description = subnet_description

    # -------------------------------------------------------------- session
    def _login(self) -> None:
        """Session login through the SHARED cookie opener (the session cookie
        must land in the same jar the API calls use — live-found 2026-10-02:
        logging in on a bare urlopen without the jar = 403 on the first API
        call because the session cookie never persisted)."""
        page = self._get("/index.php")
        m = re.search(r'name="([A-Za-z0-9_]{15,40})"\s+value="([^"]{10,80})"', page)
        if not m:
            raise SandboxNetworkError("OPNsense login page CSRF token not found")
        data = urllib.parse.urlencode({
            "usernamefld": "root", "passwordfld": self.password,
            "login": "1", m.group(1): m.group(2)}).encode()
        req = urllib.request.Request(f"{self.base}/index.php", data=data)
        try:
            with self._opener().open(req, timeout=self.timeout) as resp:
                body = resp.read().decode(errors="replace")
        except urllib.error.HTTPError as e:
            raise SandboxNetworkError(f"OPNsense login failed: HTTP {e.code}") from e
        if "logout" not in body.lower():
            raise SandboxNetworkError("OPNsense login did not establish a session")
        self._csrf = ""
        # capture cookie jar from Set-Cookie headers
        # (urllib needs a CookieJar; simplest: use a shared opener with HTTPCookieProcessor)
        # NOTE: handled in _opener()

    def _opener(self) -> urllib.request.OpenerDirector:
        if not hasattr(self, "_openr"):
            import http.cookiejar
            cj = http.cookiejar.CookieJar()
            self._openr = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
            self._cj = cj
        return self._openr

    def _get(self, path: str) -> str:
        req = urllib.request.Request(f"{self.base}{path}", method="GET")
        with self._opener().open(req, timeout=self.timeout) as resp:
            return resp.read().decode(errors="replace")

    def _post_json(self, path: str, payload: dict | None = None, _retried: bool = False) -> dict:
        if not self._csrf:
            ui = self._get("/ui/kea/dhcpv4")
            m = re.search(r'setRequestHeader\("X-CSRFToken",\s*"([^"]+)"', ui)
            if not m:
                raise SandboxNetworkError("OPNsense CSRF token not found")
            self._csrf = m.group(1)
        data = json.dumps(payload or {}).encode()
        req = urllib.request.Request(f"{self.base}{path}", data=data, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "X-CSRFToken": self._csrf})
        try:
            with self._opener().open(req, timeout=self.timeout) as resp:
                raw = resp.read().decode(errors="replace")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403) and not _retried:
                self._login(); self._csrf = ""
                return self._post_json(path, payload, _retried=True)
            raise SandboxNetworkError(f"OPNsense API {path} -> HTTP {e.code}") from e
        # an unauthenticated session receives the LOGIN PAGE (HTML), not JSON —
        # login + retry once (live-found 2026-10-02: first API call after boot).
        if raw.lstrip()[:1] not in "[{" and ("<html" in raw[:400].lower() or not raw.strip()):
            if _retried:
                raise SandboxNetworkError(
                    f"OPNsense API {path} returned non-JSON even after login "
                    f"(len={len(raw)})")
            self._login(); self._csrf = ""
            return self._post_json(path, payload, _retried=True)
        try:
            return json.loads(raw or "{}")
        except json.JSONDecodeError as e:
            raise SandboxNetworkError(
                f"OPNsense API {path} returned invalid JSON ({e}); head={raw[:120]!r}") from e

    def ensure_session(self) -> None:
        # a cheap authenticated probe; re-login when the probe fails
        try:
            self._post_json("/api/kea/dhcpv4/search_reservation/")
        except SandboxNetworkError:
            self._login()
            self._csrf = ""
            self._post_json("/api/kea/dhcpv4/search_reservation/")

    # --------------------------------------------------------------- reads
    def subnet_uuid(self) -> str:
        if self._subnet_uuid_cache:
            return str(self._subnet_uuid_cache)
        out = self._post_json("/api/kea/dhcpv4/search_subnet/")
        rows = out.get("rows") or []
        if self._subnet_description:
            for r in rows:
                if r.get("description") == self._subnet_description:
                    self._subnet_uuid_cache = r["uuid"]
                    return self._subnet_uuid_cache
        if not rows:
            raise SandboxNetworkError("no Kea subnet4 found on OPNsense")
        if len(rows) > 1:
            # ambiguous — require the env-selected uuid
            s = get_settings()
            if s.sandbox_kea_subnet_uuid:
                self._subnet_uuid_cache = str(s.sandbox_kea_subnet_uuid)
                return self._subnet_uuid_cache
            raise SandboxNetworkError(
                f"ambiguous Kea subnets {[r.get('subnet') for r in rows]}; set "
                "SERVER_MANAGER_SANDBOX_KEA_SUBNET_UUID")
        self._subnet_uuid_cache = str(rows[0]["uuid"])
        return self._subnet_uuid_cache

    def reservations(self) -> list[dict]:
        out = self._post_json("/api/kea/dhcpv4/search_reservation/")
        return out.get("rows") or []

    def leases(self) -> list[dict]:
        out = self._post_json("/api/kea/leases4/search/")
        return out.get("rows") or []

    def active_lease_ips(self) -> set[str]:
        """State-0 (active) lease addresses."""
        return {r["address"] for r in self.leases()
                if r.get("state") == 0 and r.get("address")}

    # ---------------------------------------------------------------- CRUD
    def create_reservation(self, ip: str, mac: str, hostname: str, description: str) -> str:
        body = {"reservation": {
            "subnet": self.subnet_uuid(), "ip_address": ip, "hw_address": _mac_norm(mac),
            "hostname": hostname, "description": description}}
        out = self._post_json("/api/kea/dhcpv4/add_reservation/", body)
        if out.get("result") != "saved" or not out.get("uuid"):
            errs = out.get("validations") or {}
            raise SandboxNetworkError(f"reservation create failed: {out} {errs}")
        self.reconfigure()
        return out["uuid"]

    def delete_reservation(self, uuid: str) -> None:
        out = self._post_json(f"/api/kea/dhcpv4/del_reservation/{uuid}/")
        if out.get("result") != "deleted":
            raise SandboxNetworkError(f"reservation delete failed: {out}")
        self.reconfigure()

    def reconfigure(self) -> None:
        out = self._post_json("/api/kea/service/reconfigure")
        if out.get("status") != "ok":
            raise SandboxNetworkError(f"Kea reconfigure failed: {out}")


class SandboxNetworkService:
    """Allocate → reserve → verify → release lifecycle for sandbox addresses."""

    def __init__(self, client: KeaReservationClient | None = None):
        s = get_settings()
        self.client = client or KeaReservationClient(
            s.sandbox_opnsense_url, s.sandbox_opnsense_password)
        self.range_start, self.range_end = parse_pool_range(s.sandbox_dhcp_range)

    # ------------------------------------------------------------ allocation
    def _pool(self) -> list[IPv4Address]:
        return [IPv4Address(i) for i in range(int(self.range_start), int(self.range_end) + 1)]

    def _reserved_ips(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for r in self.client.reservations():
            ip = r.get("ip_address")
            if ip:
                out[str(ip)] = r
        return out

    def _leased_ips(self) -> set[str]:
        return self.client.active_lease_ips()

    def _alive_ips(self, pool_ips: list[str]) -> set[str]:
        """Live-L2 defense-in-depth: ARP/neigh probe via the local stack (VM114
        sits on the same LAN as the DHCP pool). Cheap: only for the candidate."""
        import subprocess
        alive: set[str] = set()
        for ip in pool_ips:
            try:
                subprocess.run(["ping", "-c1", "-W1", ip], capture_output=True, timeout=3)
                out = subprocess.run(["ip", "neigh", "show", ip],
                                     capture_output=True, text=True, timeout=3)
                if "lladdr" in out.stdout and "FAILED" not in out.stdout:
                    alive.add(ip)
            except Exception:  # pragma: no cover - best-effort probe
                continue
        return alive

    def allocate(self) -> tuple[str, str | None]:
        """Pick the lowest free sandbox-pool address. Returns (ip, existing_uuid).
        existing_uuid is set when a prior SM reservation (description-tagged) is
        present but the address is NOT leased/alive — i.e. reusable after a
        crashed create."""
        reserved = self._reserved_ips()
        leased = self._leased_ips()
        tag = SUBNET_DESCRIPTION_TAG
        sm_owned: dict[str, dict] = {}
        for ip, r in reserved.items():
            desc = r.get("description") or ""
            if desc.startswith(tag):
                sm_owned[ip] = r
        alive = None  # lazily computed only for candidates
        for i in self._pool():
            ip = str(IPv4Address(i))
            r = reserved.get(ip)
            if r is not None and ip not in sm_owned:
                # foreign reservation (static infra) — skip forever
                continue
            if r is not None and ip in sm_owned:
                if ip in leased:
                    continue
                if alive is None:
                    alive = self._alive_ips([x for x in (str(IPv4Address(k)) for k in self._pool())
                                             if x in sm_owned or x not in reserved])
                if ip in alive:
                    continue
                return ip, r["uuid"]
            # never hand out an actively-leased address
            if ip in leased:
                continue
            if alive is None:
                alive = self._alive_ips([ip])
            if ip in alive:
                continue
            return ip, None
        raise SandboxNetworkError(
            "sandbox DHCP pool exhausted (fail-closed; no general-pool borrow)")

    # ------------------------------------------------------------- lifecycle
    def reserve_for_mac(self, ip: str, mac: str, hostname: str, work_uid: str,
                        agent_name: str, existing_uuid: str | None = None) -> ReservationRecord:
        if not _valid_mac(mac):
            raise SandboxNetworkError(f"invalid MAC for reservation: {mac!r}")
        if existing_uuid:
            # reuse the crashed-create reservation after verifying it matches
            rows = {r.get("uuid"): r for r in self.client.reservations()}
            r = rows.get(existing_uuid)
            if r and r.get("ip_address") == ip and _mac_norm(r.get("hw_address") or "") == _mac_norm(mac):
                return ReservationRecord(ip=ip, mac=_mac_norm(mac), hostname=hostname,
                                         uuid=existing_uuid, description=r.get("description", ""))
            self.client.delete_reservation(existing_uuid)
        desc = f"{SUBNET_DESCRIPTION_TAG}: work={work_uid} agent={agent_name} host={hostname}"
        u = self.client.create_reservation(ip, mac, hostname, desc)
        return ReservationRecord(ip=ip, mac=_mac_norm(mac), hostname=hostname, uuid=u,
                                 description=desc)

    def verify_reserved(self, ip: str) -> None:
        """Post-create verification: reservation present + Kea applied (reconfigured)."""
        rows = self._reserved_ips()
        if ip not in rows:
            raise SandboxNetworkError(f"reservation verification failed for {ip}")

    def release(self, ip: str) -> None:
        """Delete the SM-owned reservation for ip (idempotent)."""
        r = self._reserved_ips().get(ip)
        if r is None:
            return
        desc = r.get("description") or ""
        if not desc.startswith(SUBNET_DESCRIPTION_TAG):
            # never delete foreign/static reservations
            raise SandboxNetworkError(f"refusing to release foreign reservation {ip}: {desc[:60]}")
        self.client.delete_reservation(r["uuid"])