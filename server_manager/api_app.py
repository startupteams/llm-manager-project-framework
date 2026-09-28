"""Server Manager — combined FastAPI app.

Two peer services, one repo (§1): this process mounts the Agent Runtime
Manager API + the LLM Manager machine routes under /api/v1. Deployed as its
own systemd unit (server-manager-api) next to the existing llm-manager-web.
"""
from __future__ import annotations

import os

from fastapi import FastAPI

from server_manager.agent_runtime_manager.api.v1 import router as arm_router
from server_manager.llm_manager.api.v1 import router as llm_router

APP_VERSION = os.environ.get("SERVER_MANAGER_API_VERSION", "0.1.0-jint001")

app = FastAPI(
    title="Server Manager API",
    version=APP_VERSION,
    description="Machine API v1: agent-runtimes (ARM) + model-routes/usage (LLM Manager). "
    "Scoped service bearer credentials (§11).",
)

app.include_router(arm_router)
app.include_router(llm_router)

from server_manager.llm_manager.api.pdu_routes import router as pdu_router  # noqa: E402

app.include_router(pdu_router)


@app.get("/healthz")
def healthz():
    return {"ok": True, "app": "server-manager-api", "version": APP_VERSION}