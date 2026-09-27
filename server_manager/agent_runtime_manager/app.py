"""Agent Runtime Manager FastAPI app (peer deployable service, §7)."""
from __future__ import annotations

import os

from fastapi import FastAPI

from server_manager.agent_runtime_manager.api.v1 import API_CONTRACT_VERSION, router

APP_VERSION = os.environ.get("SERVER_MANAGER_ARM_VERSION", "0.1.0-jint001")

app = FastAPI(
    title="Server Manager — Agent Runtime Manager",
    version=APP_VERSION,
    description="Runtime lifecycle service (provisioning, desired-state, ownership-safe destroy). "
                f"API contract v{API_CONTRACT_VERSION}.",
)


@app.get("/healthz")
def healthz():
    return {
        "ok": True,
        "app": "agent-runtime-manager",
        "version": APP_VERSION,
        "api_contract_version": API_CONTRACT_VERSION,
    }


app.include_router(router)