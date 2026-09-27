"""Proxmox VM provider — runtime-provider abstraction (§8) with DESTRUCTIVE
permissions gated behind ownership verification.

Hard rules (§8):
- May create/destroy VMs ONLY when the VM was created/claimed by ARM and
  carries durable ownership metadata proving it.
- NEVER destroy: STEA-004/VM906, pre-existing VMs, model-serving VMs,
  infrastructure VMs, or any VM whose ownership marker cannot be verified.
- Before destructive deletion: verify ownership → verify not protected →
  preserve state per policy → snapshot if required → audit → clean stop →
  destroy VM + disposable storage.
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
import urllib.error
import ssl
from dataclasses import dataclass, field
from typing import Any

from server_manager.agent_runtime_manager.models.orm import OWNERSHIP_REQUIRED_KEYS, AgentRuntime
from server_manager.common.config.settings import get_settings


class OwnershipError(Exception):
    """Raised when a destructive action targets a VM without verified ARM ownership."""


@dataclass
class VMSpec:
    name: str
    node: str
    template_vmid: int
    template_node: str
    bridge: str = "vmbr0"
    ciuser: str = "stagent"
    ip_mode: str = "dhcp"
    description_meta: dict[str, Any] = field(default_factory=dict)


class ProxmoxVMProvider:
    """Thin, allowlisted Proxmox API client for runtime lifecycle only."""

    def __init__(self, api_url: str | None = None, token_id: str | None = None, token_secret: str | None = None,
                 verify_tls: bool = False):
        s = get_settings()
        self.api_url = (api_url or s.pve_api_url or os_env("SERVER_MANAGER_PVE_URL", "")).rstrip("/")
        self.token_id = token_id or s.pve_token_id or os_env("SERVER_MANAGER_PVE_TOKEN_ID", "")
        self.token_secret = token_secret or os_env("SERVER_MANAGER_PVE_TOKEN_SECRET", "")
        self.verify_tls = verify_tls
        self._ctx = ssl.create_default_context()
        if not verify_tls:
            self._ctx.check_hostname = False
            self._ctx.verify_mode = ssl.CERT_NONE

    # ------------------------------------------------------------- transport
    def _call(self, method: str, path: str, body: dict | None = None, timeout: int = 30) -> Any:
        url = f"{self.api_url}{path}"
        data = None
        headers = {"Authorization": f"PVEAPIToken={self.token_id}={self.token_secret}"}
        if body is not None:
            data = urllib.parse.urlencode(body).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self._ctx) as resp:
                payload = json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:300]
            raise RuntimeError(f"PVE {method} {path} -> HTTP {e.code}: {detail}") from e
        return payload.get("data", payload)

    # ------------------------------------------------------------ lifecycle
    def next_vmid(self) -> int:
        return int(self._call("GET", "/cluster/nextid"))

    def clone_template(self, spec: VMSpec, new_vmid: int) -> None:
        self._call("POST", f"/nodes/{spec.template_node}/qemu/{spec.template_vmid}/clone",
                   {"newid": new_vmid, "name": spec.name[:63], "full": 1, "target": spec.template_node}, timeout=600)

    def wait_clone_lock_release(self, node: str, vmid: int, max_s: int = 240) -> None:
        deadline = time.time() + max_s
        while time.time() < deadline:
            cfg = self.vm_config(node, vmid) or {}
            if not cfg.get("lock"):
                return
            time.sleep(2)
        raise RuntimeError(f"clone lock still held on {node}/{vmid}")

    def configure_cloud_init(self, spec: VMSpec, vmid: int, extra: dict | None = None) -> None:
        body = {
            "net0": f"virtio,bridge={spec.bridge}",
            "ipconfig0": f"ip={spec.ip_mode}",
            "ciuser": spec.ciuser,
            "description": json.dumps(spec.description_meta)[:8190],  # §8 durable ownership marker
        }
        if extra:
            body.update(extra)
        self._call("POST", f"/nodes/{spec.template_node}/qemu/{vmid}/config", body)

    def start(self, node: str, vmid: int) -> None:
        self._call("POST", f"/nodes/{node}/qemu/{vmid}/status/start", {}, timeout=120)

    def shutdown(self, node: str, vmid: int, timeout_s: int = 180) -> None:
        try:
            self._call("POST", f"/nodes/{node}/qemu/{vmid}/status/shutdown",
                       {"timeout": timeout_s}, timeout=timeout_s + 30)
        except Exception:
            self.stop_force(node, vmid)  # §8: clean stop where possible, escalate otherwise

    def stop_force(self, node: str, vmid: int) -> None:
        self._call("POST", f"/nodes/{node}/qemu/{vmid}/status/stop", {}, timeout=120)

    def destroy(self, node: str, vmid: int, purge: bool = True, destroy_unreferenced: bool = True) -> None:
        body: dict[str, Any] = {}
        if purge:
            body["purge"] = 1
        if destroy_unreferenced:
            body["destroy-unreferenced-disks"] = 1
        self._call("DELETE", f"/nodes/{node}/qemu/{vmid}", body or None, timeout=300)

    # ------------------------------------------------------------- reads
    def vm_config(self, node: str, vmid: int) -> dict:
        try:
            return self._call("GET", f"/nodes/{node}/qemu/{vmid}/config") or {}
        except Exception:
            return {}

    def vm_status(self, node: str, vmid: int) -> dict:
        try:
            return self._call("GET", f"/nodes/{node}/qemu/{vmid}/status/current") or {}
        except Exception:
            return {}

    def vm_exists(self, node: str, vmid: int) -> bool:
        try:
            return bool(self._call("GET", f"/nodes/{node}/qemu/{vmid}/status/current"))
        except Exception:
            return False

    def wait_for_ip(self, node: str, vmid: int, max_s: int = 300) -> str | None:
        deadline = time.time() + max_s
        while time.time() < deadline:
            try:
                ifs = self._call("GET", f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces") or {}
                for ifc in (ifs.get("result") or []):
                    for a in (ifc.get("ip-addresses") or []):
                        ip = a.get("ip-address", "")
                        if a.get("ip-address-type") == "ipv4" and not ip.startswith("127."):
                            return ip
            except Exception:
                pass
            time.sleep(3)
        return None


def os_env(key: str, default: str) -> str:
    import os

    return os.environ.get(key, default)


# ---------------------------------------------------------------------------
# OWNERSHIP VERIFICATION (§8) — the safety core
# ---------------------------------------------------------------------------

def verify_ownership(provider: ProxmoxVMProvider, node: str, vmid: int, runtime: AgentRuntime,
                     protected_vmids: set[int], require_protection_check: bool = True) -> None:
    """Verify a VM is Runtime-Manager-owned before ANY destructive action.

    Ownership = the VM's PVE description carries the durable marker created at
    provision time AND matches this runtime's identity AND the VMID is not
    protected/pinned.
    """
    # 1) protected/pinned check (VM906 etc., §8)
    if require_protection_check and vmid in protected_vmids:
        raise OwnershipError(f"VMID {vmid} is protected — destruction refused (§8)")

    # 2) VM must still exist
    if not provider.vm_exists(node, vmid):
        raise OwnershipError(f"VM {node}/{vmid} does not exist — nothing to act on")

    # 3) runtime row must claim ownership
    if not runtime.created_by_agent_runtime_manager:
        raise OwnershipError(f"runtime {runtime.runtime_id} is not marked created_by_agent_runtime_manager")

    # 4) durable on-VM ownership marker must match
    desc = provider.vm_config(node, vmid).get("description") or ""
    try:
        marker = json.loads(desc)
    except (ValueError, TypeError):
        raise OwnershipError(f"VM {node}/{vmid} has no parseable ownership marker")

    if not marker.get("created_by_agent_runtime_manager"):
        raise OwnershipError(f"VM {node}/{vmid} marker lacks created_by_agent_runtime_manager")
    if str(marker.get("server_runtime_id", "")) != str(runtime.runtime_id):
        raise OwnershipError(f"VM {node}/{vmid} marker runtime-id mismatch (foreign VM)")

    # 5) marker must be complete (§8 minimum set)
    missing = [k for k in OWNERSHIP_REQUIRED_KEYS if k not in marker and k != "created_by_agent_runtime_manager"]
    if missing:
        raise OwnershipError(f"VM {node}/{vmid} ownership marker incomplete: missing {missing}")


def ownership_marker(runtime: AgentRuntime, node: str, vmid: int, template: str) -> dict:
    """Build the durable ownership description written at provision time (§8)."""
    return {
        "server_runtime_id": str(runtime.runtime_id),
        "acms_agent_id": runtime.acms_agent_id,
        "provisioning_request_id": runtime.provisioning_request_id,
        "provider": "proxmox_vm",
        "node": node,
        "vmid": vmid,
        "created_by_agent_runtime_manager": True,
        "created_at": runtime.created_at.isoformat() if runtime.created_at else None,
        "template_source": template,
        "managed_by": "server-manager/agent-runtime-manager",
    }
