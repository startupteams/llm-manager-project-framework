"""Agent Runtime UI tests — window-5 plan §4/§15.

Wizard validation, list/detail honesty, SM-API proxy behavior (failures
surface as actionable errors, never raw 500s / never fabricated).
"""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(REPO, "service", "app")
for p in (APP,):
    if p not in sys.path:
        sys.path.insert(0, APP)

import pytest

import agent_ui
from agent_ui import _derive_runtime_state

pytest.importorskip("ldap3")
pytest.importorskip("fastapi")


class TestDeriveRuntimeState:
    def test_live_is_ok(self):
        st, cls = _derive_runtime_state({"actual_state": "LIVE", "desired_state": "DESIRED_RUNNING"})
        assert st == "LIVE" and cls == "ok"

    def test_stopped_honest(self):
        st, cls = _derive_runtime_state({"actual_state": "DESIRED_STOPPED",
                                         "desired_state": "DESIRED_STOPPED"})
        assert st == "STOPPED"

    def test_error_visible(self):
        st, cls = _derive_runtime_state({"actual_state": "ERROR", "desired_state": "DESIRED_RUNNING"})
        assert st == "ERROR" and cls == "bad"

    def test_unknown_not_faked(self):
        st, cls = _derive_runtime_state({})
        assert st == "UNKNOWN" and cls == "warn"


class TestSmApi:
    def test_missing_token_is_503_not_crash(self, tmp_path, monkeypatch):
        f = tmp_path / "tokens"
        f.write_text("svc-acms=tok:runtime:read\n")
        monkeypatch.setattr(agent_ui, "_TOKEN_FILE", str(f))
        code, data, err = agent_ui.sm_api("GET", "/api/v1/agent-runtimes")
        assert code == 503 and data is None
        assert "token not available" in err

    def test_error_status_parsed(self, tmp_path, monkeypatch):
        f = tmp_path / "tokens"
        f.write_text("svc-server-manager=tok:ALL\n")
        monkeypatch.setattr(agent_ui, "_TOKEN_FILE", str(f))

        class Fake404(Exception):
            pass

        import urllib.error

        def fake_urlopen(req, data=None, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 404, "not found",
                                         hdrs=None, fp=None)
        monkeypatch.setattr(agent_ui.urllib.request, "urlopen", fake_urlopen)
        code, data, err = agent_ui.sm_api("GET", "/api/v1/agent-runtimes/nope")
        assert code == 404

    def test_network_error_is_599_actionable(self, tmp_path, monkeypatch):
        f = tmp_path / "tokens"
        f.write_text("svc-server-manager=tok:ALL\n")
        monkeypatch.setattr(agent_ui, "_TOKEN_FILE", str(f))

        def fake_urlopen(req, data=None, timeout=None):
            raise ConnectionError("connection refused")
        monkeypatch.setattr(agent_ui.urllib.request, "urlopen", fake_urlopen)
        code, data, err = agent_ui.sm_api("GET", "/api/v1/agent-runtimes")
        assert code == 599 and "connection refused" in err.lower()


class TestWizardPages:
    def test_pages_carry_no_fabricated_fields(self):
        """The wizard never invents unsupported policy/model semantics (§4.5)."""
        assert "Kubernetes" not in agent_ui.NEW_AGENT_HTML
        assert "Budget fields are set in ACMS" in agent_ui.NEW_AGENT_HTML
        assert "agentifyme-external-agent-base" in agent_ui.NEW_AGENT_HTML
        assert "NOT yet authorized" in agent_ui.NEW_AGENT_HTML

    def test_list_page_state_honesty(self):
        """The list renders state classes from _derive_runtime_state vocabulary."""
        # the JS applies class st-<state>; derive vocabulary includes PROVISIONING
        # via _derive_runtime_state (tested separately) — page just must render it
        assert 'st-' in agent_ui.AGENTS_HTML

    def test_detail_destroy_not_exposed(self):
        assert "Destroy is intentionally NOT exposed" in agent_ui.AGENT_DETAIL_HTML
        assert 'ctl(\'DESIRED_DESTROYED\')' not in agent_ui.AGENT_DETAIL_HTML

    def test_create_goes_through_existing_arm_path(self):
        assert "/api/v1/agent-runtimes" in agent_ui.NEW_AGENT_HTML
        # authority is FIXED server-side (sprint_execution_context), not wizard-chosen
        assert "sprint_execution_context" in agent_ui.NEW_AGENT_HTML