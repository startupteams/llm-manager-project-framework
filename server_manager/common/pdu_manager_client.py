"""pdu_manager_client — reusable Python client for the PDU Manager /api/v1 (REV4 §10C Phase 8).

Rules (plan §10C): TLS verification REQUIRED (verify=False is an explicit
opt-out only, never a default); no automatic new idempotency key after a
timeout (the caller decides; idempotency replays the original job);
structured exceptions; correlation metadata passthrough; credentials from
external configuration only (never hard-coded).

Usage:
    c = PduManagerClient("https://10.0.20.156/api/v1", user="svc-acms",
                         password=..., cafile="/path/ca.pem")
    c.health()
    plan = c.plan_action(asset_id="MIAM-00119", action="reboot")
    job = c.submit_action(asset_id="MIAM-00119", action="reboot",
                          reason="...", request_id="uuid",
                          correlation_id=..., source_service="svc-acms")
    c.wait_for_job(job["job_id"], timeout=120)
"""
from __future__ import annotations

import base64
import json
import ssl
import time
import urllib.error
import urllib.request


class PduError(Exception):
    """Transport/protocol error (includes HTTP status when present)."""

    def __init__(self, message: str, status_code: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class PduIdentityMismatch(PduError):
    """409 TARGET_IDENTITY_MISMATCH — the wrong-target guard fired (never dispatch)."""


class PduProtectedPolicy(PduError):
    """Protected-outlet policy refusal (PROTECTED_OFF_FORBIDDEN etc.)."""


class PduAuth(PduError):
    """401/403 from the API."""


class PduManagerClient:
    def __init__(self, base_url: str, user: str, password: str,
                 cafile: str | None = None, verify: bool | str = True, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self._auth = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.timeout = timeout
        if verify is False:
            # explicit opt-out ONLY (plan rule: no verify=False default)
            ctx = ssl._create_unverified_context()
        elif isinstance(verify, str):
            ctx = ssl.create_default_context(cafile=verify)
        elif cafile:
            ctx = ssl.create_default_context(cafile=cafile)
        else:
            ctx = ssl.create_default_context()
        self._ctx = ctx

    # ------------------------------------------------------------- transport
    def _call(self, method: str, path: str, body: dict | None = None,
              timeout: int | None = None, expected: tuple[int, ...] = (200, 201, 202)) -> dict:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Basic {self._auth}")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout,
                                        context=self._ctx) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:400]
            code = None
            try:
                code = json.loads(detail).get("error", {}).get("code")
            except ValueError:
                pass
            if code == "TARGET_IDENTITY_MISMATCH":
                raise PduIdentityMismatch(detail, status_code=e.code, body=detail) from None
            if code and ("PROTECTED" in code or "SELF_HOST" in code):
                raise PduProtectedPolicy(detail, status_code=e.code, body=detail) from None
            if e.code in (401, 403):
                raise PduAuth(detail, status_code=e.code, body=detail) from None
            raise PduError(f"HTTP {e.code}: {detail}", status_code=e.code, body=detail) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise PduError(str(e)) from None

    # ------------------------------------------------------------------- api
    def health(self) -> dict:
        return self._call("GET", "/health")

    def capabilities(self) -> dict:
        return self._call("GET", "/capabilities")

    def version(self) -> dict:
        return self._call("GET", "/version")

    def me(self) -> dict:
        return self._call("GET", "/me")

    def list_assets(self) -> dict:
        return self._call("GET", "/assets")

    def get_asset(self, asset_id: str) -> dict:
        return self._call("GET", f"/assets/{asset_id}")

    def get_power_state(self, asset_id: str) -> str:
        return self.get_asset(asset_id).get("state", "UNKNOWN")

    def plan_action(self, asset_id: str, action: str) -> dict:
        """Dry-run: NEVER actuates. Verify identity + acks before a real submit."""
        return self._call("POST", "/action-plans", {"asset_id": asset_id, "action": action})

    def submit_action(self, asset_id: str, action: str, reason: str, request_id: str,
                      correlation_id: str | None = None, source_service: str | None = None,
                      upstream_operation_id: str | None = None,
                      admin_override: bool = False,
                      acknowledge_protected_device: bool = False,
                      acknowledge_controller_may_go_offline: bool = False) -> dict:
        body: dict = {"action": action, "reason": reason, "request_id": request_id}
        if correlation_id:
            body["correlation_id"] = correlation_id
        if source_service:
            body["source_service"] = source_service
        if upstream_operation_id:
            body["upstream_operation_id"] = upstream_operation_id
        if admin_override:
            body["admin_override"] = True
        if acknowledge_protected_device:
            body["acknowledge_protected_device"] = True
        if acknowledge_controller_may_go_offline:
            body["acknowledge_controller_may_go_offline"] = True
        return self._call("POST", f"/assets/{asset_id}/actions", body)

    def get_job(self, job_id: str) -> dict:
        return self._call("GET", f"/jobs/{job_id}")

    def wait_for_job(self, job_id: str, timeout: int = 180, poll_s: float = 2.0) -> dict:
        deadline = time.time() + timeout
        last: dict = {}
        while time.time() < deadline:
            last = self.get_job(job_id)
            if last.get("state") in ("DONE", "FAILED", "CANCELLED"):
                return last
            time.sleep(poll_s)
        return {**last, "wait_timed_out": True}

    def audit(self, lines: int = 40) -> list:
        """Recent audit records (admin path; availability depends on deployment)."""
        out = self._call("GET", f"/audit?lines={lines}", expected=(200, 404))
        return out.get("records", []) if isinstance(out, dict) else []