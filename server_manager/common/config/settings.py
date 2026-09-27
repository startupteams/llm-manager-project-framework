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
    pve_api_url: str = os.environ.get("SERVER_MANAGER_PVE_URL", "")
    pve_token_id: str = os.environ.get("SERVER_MANAGER_PVE_TOKEN_ID", "")

    # --- ownership safety (§8): protected VMs are NEVER destroyable ---
    protected_vmids: set[int] = field(default_factory=set)

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
