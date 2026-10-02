"""Server Manager common configuration.

Environment-first resolution: explicit env var > value from the app's
credential contract files > neutral local default. Prod endpoints are
documented in deploy/production-manifest.yaml, never hard-coded as defaults
(plan §8, proven by PR #20).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache


def _from_secrets_file(path: str, keys: list[str]) -> dict[str, str]:
    vals: dict[str, str] = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    if k in keys:
                        vals[k] = v
    except OSError:
        pass
    return vals


@dataclass
class DbSettings:
    """Connection settings for one logical database (§1: separate DBs per service)."""

    name: str
    host: str
    port: int
    db: str
    user: str
    password: str
    url: str  # optional full DSN override


@dataclass
class Settings:
    env: str = os.environ.get("SERVER_MANAGER_ENV", "production")

    # --- llmmanager database (existing LLM Manager data) ---
    llm_pg_host: str = ""
    llm_pg_port: int = 5432
    llm_pg_db: str = "llmmanager"
    llm_pg_user: str = "llmmanager"
    llm_pg_password: str = ""

    # --- agent_runtime_manager database (new, §1/§6) ---
    arm_pg_host: str = ""
    arm_pg_port: int = 5432
    arm_pg_db: str = "agent_runtime_manager"
    arm_pg_user: str = "arm"
    arm_pg_password: str = ""

    # --- service auth (§11): scoped bearer credentials, outside Git ---
    service_tokens: dict[str, str] = field(default_factory=dict)

    # --- LiteLLM / PVE references ---
    litellm_url: str = os.environ.get("SERVER_MANAGER_LITELLM_URL", "http://127.0.0.1:4000")
    pve_api_url: str = os.environ.get("SERVER_MANAGER_PVE_URL", "https://10.0.20.135:8006/api2/json")
    pve_token_id: str = os.environ.get("SERVER_MANAGER_PVE_TOKEN_ID", "llm-manager@pve!manager-control")

    # --- ownership safety (§8): protected VMs are NEVER destroyable ---
    protected_vmids: set[int] = field(default_factory=set)

    # --- sandbox TTL (plan §26): hours for runtime_class="sandbox" ---
    arm_sandbox_default_ttl_hours: int = 8
    arm_sandbox_max_ttl_hours: int = 72

    # --- sandbox network isolation (plan §W4.1): OPNsense/Kea reservations ---
    # Occupancy-verified 2026-10-02: dedicated block OUTSIDE the general pool
    # tails and clear of all statics (see STEA-004 OCCUPANCY-EVIDENCE-W41.md).
    sandbox_opnsense_url: str = "http://10.0.10.1"
    sandbox_opnsense_password: str = ""  # from secrets file, never Git
    sandbox_dhcp_range: str = "10.0.20.222 - 10.0.20.249"
    sandbox_kea_subnet_uuid: str = ""  # optional explicit subnet selection
    sandbox_network_enabled: int = 0   # W4.1: 1 = reservations live (unpause), 0 = sandboxes blocked

    def llm_dsn(self) -> str:
        return (
            f"host={self.llm_pg_host} port={self.llm_pg_port} dbname={self.llm_pg_db} "
            f"user={self.llm_pg_user} password={self.llm_pg_password}"
        )

    def arm_dsn(self) -> str:
        return (
            f"host={self.arm_pg_host} port={self.arm_pg_port} dbname={self.arm_pg_db} "
            f"user={self.arm_pg_user} password={self.arm_pg_password}"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()

    # sandbox TTL env overrides (plan §26) — read per-call so tests/deploys can
    # tune them without re-importing the module
    try:
        s.arm_sandbox_default_ttl_hours = int(os.environ.get(
            "SERVER_MANAGER_ARM_SANDBOX_DEFAULT_TTL_HOURS",
            str(s.arm_sandbox_default_ttl_hours)))
        s.arm_sandbox_max_ttl_hours = int(os.environ.get(
            "SERVER_MANAGER_ARM_SANDBOX_MAX_TTL_HOURS",
            str(s.arm_sandbox_max_ttl_hours)))
    except ValueError:
        pass

    # Sandbox network gate (plan §W4.1)
    s.sandbox_opnsense_url = os.environ.get("SERVER_MANAGER_SANDBOX_OPNSENSE_URL", s.sandbox_opnsense_url)
    s.sandbox_dhcp_range = os.environ.get("SERVER_MANAGER_SANDBOX_DHCP_RANGE", s.sandbox_dhcp_range)
    s.sandbox_kea_subnet_uuid = os.environ.get("SERVER_MANAGER_SANDBOX_KEA_SUBNET_UUID", s.sandbox_kea_subnet_uuid)
    try:
        s.sandbox_network_enabled = int(os.environ.get("SERVER_MANAGER_SANDBOX_NETWORK_ENABLED",
                                                       str(s.sandbox_network_enabled)))
    except ValueError:
        pass
    snw = _from_secrets_file(
        os.environ.get("SERVER_MANAGER_SANDBOX_SECRETS_FILE",
                       "/etc/llm-manager/secrets/sandbox_network"),
        ["OPNSENSE_PASSWORD", "SANDBOX_DHCP_RANGE", "OPNSENSE_URL"])
    s.sandbox_opnsense_password = os.environ.get("SERVER_MANAGER_SANDBOX_OPNSENSE_PASSWORD") or snw.get("OPNSENSE_PASSWORD", "")
    if snw.get("OPNSENSE_URL"):
        s.sandbox_opnsense_url = snw["OPNSENSE_URL"]
    if snw.get("SANDBOX_DHCP_RANGE"):
        s.sandbox_dhcp_range = snw["SANDBOX_DHCP_RANGE"]

    pg = _from_secrets_file(
        os.environ.get("SERVER_MANAGER_PG_CREDS_FILE", "/etc/llm-manager/secrets/pg_app_creds"),
        ["PG_HOST", "PG_PORT", "PG_DB", "PG_USER", "PG_PW"],
    )
    # The pg_app_creds file is the LLM Manager contract (PR #21/#22). The ARM DB
    # has its OWN credential file (separate credentials, §1).
    arm = _from_secrets_file(
        os.environ.get("SERVER_MANAGER_ARM_CREDS_FILE", "/etc/llm-manager/secrets/arm_db_creds"),
        ["PG_HOST", "PG_PORT", "PG_DB", "PG_USER", "PG_PW"],
    )

    s.llm_pg_host = os.environ.get("SERVER_MANAGER_LLM_PG_HOST") or pg.get("PG_HOST") or "127.0.0.1"
    s.llm_pg_port = int(os.environ.get("SERVER_MANAGER_LLM_PG_PORT") or pg.get("PG_PORT") or 5432)
    s.llm_pg_db = os.environ.get("SERVER_MANAGER_LLM_PG_DB") or pg.get("PG_DB") or "llmmanager"
    s.llm_pg_user = os.environ.get("SERVER_MANAGER_LLM_PG_USER") or pg.get("PG_USER") or "llmmanager"
    s.llm_pg_password = os.environ.get("SERVER_MANAGER_LLM_PG_PASSWORD") or pg.get("PG_PW") or ""

    s.arm_pg_host = os.environ.get("SERVER_MANAGER_ARM_PG_HOST") or arm.get("PG_HOST") or s.llm_pg_host
    s.arm_pg_port = int(os.environ.get("SERVER_MANAGER_ARM_PG_PORT") or arm.get("PG_PORT") or 5432)
    s.arm_pg_db = os.environ.get("SERVER_MANAGER_ARM_PG_DB") or arm.get("PG_DB") or "agent_runtime_manager"
    s.arm_pg_user = os.environ.get("SERVER_MANAGER_ARM_PG_USER") or arm.get("PG_USER") or "arm"
    s.arm_pg_password = os.environ.get("SERVER_MANAGER_ARM_PG_PASSWORD") or arm.get("PG_PW") or ""

    # Service tokens: comma/semicolon list of service:token pairs from a 0600 file
    tok_file = os.environ.get("SERVER_MANAGER_SERVICE_TOKENS_FILE", "/etc/llm-manager/secrets/service_tokens")
    try:
        with open(tok_file) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    service, token = line.split("=", 1)
                    s.service_tokens[service.strip()] = token.strip()
    except OSError:
        pass

    # Protected VMs (§8): STEA-004 VM906 is absolute; extendable via env CSV
    s.protected_vmids.add(906)
    extra = os.environ.get("SERVER_MANAGER_PROTECTED_VMIDS", "")
    for part in extra.split(","):
        part = part.strip()
        if part.isdigit():
            s.protected_vmids.add(int(part))

    return s
