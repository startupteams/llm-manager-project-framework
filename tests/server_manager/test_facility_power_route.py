"""Server Manager /api/v1/facility/power route (STEA-004 plan §17).

Covers: service-token auth (401 no token / 403 wrong scope / 200 with
usage:read), collector-missing honest error, per-channel staleness +
TOTAL-withheld semantics (never zero-filled).
"""
from __future__ import annotations

import os
import time

from datetime import datetime as _DT

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def fp_client(monkeypatch, tmp_path):
    """TestClient over the real api_app with a temp tokens file + stubbed llm session."""
    from server_manager.common.auth import service_tokens
    from server_manager.llm_manager.api import facility_power as fp
    from server_manager.common.db import session as db_session

    tf = tmp_path / "service_tokens"
    tf.write_text("svc-acms=tok-acms-full-0001:ALL\nsvc-nopow=svc-nopow-tok:health:read\n")
    prev = os.environ.get("SERVER_MANAGER_SERVICE_TOKENS_FILE")
    os.environ["SERVER_MANAGER_SERVICE_TOKENS_FILE"] = str(tf)
    time.sleep(0.01)
    os.utime(tf)

    from server_manager.api_app import app

    # stub get_session_factory to return a fake session-maker
    class _FakeResult:
        def __init__(self, value):
            self._v = value

        def scalar(self):
            return self._v

        def one(self):
            return self._v

        def mappings(self):
            return self

        def all(self):
            return self._v or []

    class _FakeSession:
        def __init__(self, script):
            self.script = script
            self.i = 0

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, q, params=None):
            step = self.script[self.i]
            self.i += 1
            sql = str(q)
            if "to_regclass" in sql:
                return _FakeResult(step.get("regclass", True))
            if "electricity_rates" in sql:
                return _FakeResult(step.get("rates", []))
            if "count(*), max(ts)" in sql:
                return _FakeResult((step.get("n1h", 12), step.get("last_ts")))
            if "SELECT count(*) FROM facility_power_samples" in sql:
                return _FakeResult(step.get("ntotal", 100))
            if "EXTRACT(epoch" in sql:
                return _FakeResult(step.get("age", 30.0))
            if "AVG(s.watts)" in sql:
                return _FakeResult(step.get("avg_w", 1200.0))
            if "interval '24 hours'" in sql:
                return _FakeResult(step.get("kwh24", 12.5))
            if "interval '30 days'" in sql:
                return _FakeResult(step.get("kwh30", 300.0))
            if "max(ts) FROM facility_power_samples s WHERE" in sql:
                return _FakeResult(step.get("ch_last"))
            return _FakeResult(None)

    script = [{}]  # one generic step consumed per execute call
    # build a script long enough: 1 regclass + 1 rates + 1 (n1h) + 1 ntotal
    # + 1 age + per-channel 4×(avg+24h+30d+max+age) + …
    big = [{"regclass": True, "rates": [], "n1h": 130, "last_ts": _DT(2026, 10, 1, 23, 0, 0),
            "ntotal": 50000, "age": 78.0, "avg_w": 1200.0, "kwh24": 11.5,
            "kwh30": 208.0, "ch_last": _DT(2026, 10, 1, 23, 0, 0)}] * 40
    maker = lambda *a, **k: (lambda: _FakeSession(big))  # noqa: E731 — factory returns Session() callable
    monkeypatch.setattr(fp, "get_session_factory", maker)

    yield TestClient(app)

    if prev:
        os.environ["SERVER_MANAGER_SERVICE_TOKENS_FILE"] = prev
    else:
        os.environ.pop("SERVER_MANAGER_SERVICE_TOKENS_FILE", None)
    time.sleep(0.01)
    os.utime(tf)


def test_facility_power_requires_auth(fp_client):
    r = fp_client.get("/api/v1/facility/power")
    assert r.status_code == 401


def test_facility_power_scope_gate(fp_client):
    r = fp_client.get("/api/v1/facility/power",
                      headers={"Authorization": "Bearer svc-nopow-tok"})
    assert r.status_code == 403


def test_facility_power_full_payload(fp_client):
    r = fp_client.get("/api/v1/facility/power",
                      headers={"Authorization": "Bearer tok-acms-full-0001"})
    assert r.status_code == 200
    d = r.json()
    assert d["error"] is None
    assert len(d["channels"]) == 4
    labels = " | ".join(c["label"] for c in d["channels"])
    assert "PDU MIAM-00151" in labels and "PDU MIAM-00153" in labels
    assert "mini split" in labels
    assert d["totals"]["label"] == "TOTAL MARION_IA_USA"
    assert d["totals"]["kwh_24h"] is not None  # fresh channels → total present
    assert d["collector"]["stale_policy"].startswith("stale")


def test_facility_power_collector_missing(fp_client, monkeypatch):
    from server_manager.llm_manager.api import facility_power as fp

    class _FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, q, params=None):
            class R:
                def scalar(self):
                    return False

            return R()

    monkeypatch.setattr(fp, "get_session_factory", lambda *a, **k: (lambda: _FakeSession()))
    r = fp_client.get("/api/v1/facility/power",
                      headers={"Authorization": "Bearer tok-acms-full-0001"})
    assert r.status_code == 200
    assert "not deployed" in (r.json()["error"] or "")