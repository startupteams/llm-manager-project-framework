"""Scoped service authentication (plan §11).

MVP: scoped bearer credentials from a root-owned 0600 file, private network,
audited, rotatable outside Git. Token file format:

    # service   = <token>
    svc-acms    = <token>
    svc-server-manager = <token>

Each caller identity carries an action scope. Documented TDR: temporary
service token → future mTLS (plan §11/§19).
"""
from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass
from functools import lru_cache

from fastapi import HTTPException, Request

# Scope names (least-privilege defaults per §10B/§13)
SCOPES_READ = {"runtime:read", "job:read", "route:read", "usage:read", "health:read"}
SCOPES_WRITE = {"runtime:write", "runtime:destroy", "job:submit"}
ALL_SCOPES = SCOPES_READ | SCOPES_WRITE


@dataclass(frozen=True)
class ServiceIdentity:
    service: str
    scopes: frozenset[str]

    def can(self, scope: str) -> bool:
        return scope in self.scopes


def _load_identities() -> dict[str, tuple[str, frozenset[str]]]:
    """Returns {bearer_token: (service, scopes)} from the tokens file.

    File format (0600, outside Git):
        # service=token:scope1,scope2
        svc-acms=token1:runtime:read,runtime:write,job:read,job:submit
        svc-server-manager=token2:ALL
    """
    path = os.environ.get("SERVER_MANAGER_SERVICE_TOKENS_FILE", "/etc/llm-manager/secrets/service_tokens")
    out: dict[str, tuple[str, frozenset[str]]] = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                service, rest = line.split("=", 1)
                token, _, scope_s = rest.partition(":")
                token = token.strip()
                if not token:
                    continue
                if scope_s.strip() in ("", "ALL"):
                    scopes = frozenset(ALL_SCOPES)
                else:
                    scopes = frozenset(x.strip() for x in scope_s.split(",") if x.strip())
                out[token] = (service.strip(), scopes)
    except OSError:
        pass
    return out


@lru_cache(maxsize=1)
def _identities_cached(mtime: float) -> dict[str, tuple[str, frozenset[str]]]:
    return _load_identities()


def _identities() -> dict[str, tuple[str, frozenset[str]]]:
    path = os.environ.get("SERVER_MANAGER_SERVICE_TOKENS_FILE", "/etc/llm-manager/secrets/service_tokens")
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        mtime = 0
    return _identities_cached(mtime)


def constant_time_lookup(token: str) -> ServiceIdentity | None:
    identities = _identities()
    for tok, (service, scopes) in identities.items():
        if hmac.compare_digest(tok, token):
            return ServiceIdentity(service=service, scopes=scopes)
    return None


def require_identity(request: Request, scope: str) -> ServiceIdentity:
    """FastAPI dependency: verify bearer token + scope; 401 first, then 403."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer credential")
    token = auth[len("Bearer "):].strip()
    ident = constant_time_lookup(token)
    if ident is None:
        raise HTTPException(status_code=401, detail="invalid credential")
    if not ident.can(scope):
        raise HTTPException(status_code=403, detail=f"identity '{ident.service}' lacks scope '{scope}'")
    return ident
