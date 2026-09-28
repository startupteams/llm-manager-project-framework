"""TDR-0008 slices 2–3 ORM parity tests.

Same embedded-PG pattern as test_orm_parity.py (slice 1):
- deployment_revisions + deployments (slice 2)
- benchmark_runs + qualification_envelopes (slice 3)
Reflective models must map schema.sql exactly (no DDL drift) and the ORM
read endpoints must return the seeded rows.
"""
from __future__ import annotations

import os

import pytest

pytest.importorskip("pgserver", reason="pgserver required for parity tests")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(scope="module")
def econ_pg(tmp_path_factory):
    """Embedded PG from schema.sql seeded with slice-2/3 rows; SM env pointed at it."""
    import pgserver

    data_dir = tmp_path_factory.mktemp("llm-slices23-pgdata")
    with pgserver.get_server(str(data_dir)) as srv:
        uri = srv.get_uri()
        import psycopg2
        from urllib.parse import parse_qs, urlparse

        u = urlparse(uri)
        q = parse_qs(u.query)
        sock_host = q["host"][0]
        base = f"host={sock_host} user={u.username or 'postgres'}"
        conn = psycopg2.connect(base + " dbname=postgres")
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("CREATE DATABASE llmmanager")
        cur.execute("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='llmmanager') THEN CREATE ROLE llmmanager; END IF; END $$")
        cur.close()
        conn.close()
        raw = open(os.path.join(REPO, "db", "schema", "schema.sql")).read()
        schema = "\n".join(l for l in raw.splitlines()
                           if not l.lstrip().startswith("\\"))
        conn = psycopg2.connect(base + " dbname=llmmanager")
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(schema)
        cur.execute("SET search_path = public")
        cur.execute("""
            INSERT INTO public.hosts (host_id, name, guest_ip, gpu_count, gpu_model, node, vmid,
                               desired_power_state, management_mode, desired_service_state)
            VALUES (1, 'MIAM-00143', '10.0.20.163', 6, 'RTX 3080', 'miam00143', 109,
                    'RUNNING', 'MANAGED', 'SERVING')
        """)
        cur.execute("""
            INSERT INTO public.deployment_revisions (revision_id, revision_label, model,
                artifact, quantization, runtime, host, vm, guest_ip, gpu_model, gpu_count,
                context, parallelism, max_num_seqs, kv_cache, launch_command,
                systemd_unit, aliases, status, visible_by_default)
            VALUES (1, 'gemma4-baseline', 'gemma4-26b-a4b', 'cyankiwi/QAT-AWQ-INT4', 'AWQ-INT4',
                    'vllm', 'MIAM-00143', '109', '10.0.20.163', 'RTX 3080', 6,
                    70000, 'TP2/DP3+EP', 32, 'auto', 'vllm serve ...', 'vllm.service',
                    'fast2', 'ACTIVE', true),
                   (2, 'flashnext-rollback', 'qwen3.8-flash-next', 'todiadiyatmo/W4A16', 'FP8-PLE',
                    'vllm-fork', 'MIAM-00111', '102', '10.0.20.168', 'RTX 3080', 6,
                    70000, 'TP2xPP3+EP', 6, 'fp8_e4m3', 'python3 -c ...', 'flashnext.service',
                    'code', 'CANDIDATE', true)
        """)
        cur.execute("""
            INSERT INTO public.deployments (deployment_id, host_id, served_model,
                max_model_len, enabled, config_version, desired_service_state)
            VALUES (1, 1, 'gemma4-26b-a4b', 70000, true, 'v5-gemma', 'SERVING')
        """)
        cur.execute("""
            INSERT INTO public.benchmark_runs (run_id, preset, concurrency, requests, ok, failed,
                duration_s, output_tok_s, ttft_avg_s, gpu_watts_avg, target,
                deployment_revision_id, prompt_tokens, output_tokens,
                aggregate_output_tok_s, tested_context, hermes_tool_pass, status)
            VALUES (1, 'C8', 8, 40, 40, 0,
                    62.5, 76.2, 0.41, 410.5, 'http://10.0.20.163:8000/v1',
                    1, 8000, 256, 609.6, 70000, true, 'COMPLETED'),
                   (2, 'C4', 4, 20, 18, 2,
                    44.1, 62.7, 0.55, 395.0, 'http://10.0.20.163:8000/v1',
                    1, 8000, 256, 250.8, 70000, true, 'FAILED')
        """)
        cur.execute("""
            INSERT INTO public.qualification_envelopes (envelope_id, deployment_ip, model,
                recommended_concurrency, hard_concurrency_limit, qualified_context,
                benchmark_run_id, kv_cache_dtype, notes)
            VALUES (1, '10.0.20.163', 'gemma4-26b-a4b', 6, 8, 70000, 1, 'auto',
                    'V5 handoff envelope')
        """)
        conn.commit()
        cur.close()
        conn.close()

        os.environ["SERVER_MANAGER_LLM_PG_HOST"] = sock_host
        os.environ["SERVER_MANAGER_LLM_PG_DB"] = "llmmanager"
        os.environ["SERVER_MANAGER_LLM_PG_USER"] = u.username or "postgres"
        os.environ["SERVER_MANAGER_LLM_PG_PASSWORD"] = ""
        import server_manager.common.config.settings as cfg
        import server_manager.common.db.session as sess

        sess._engines.clear()
        sess._sessions.clear()
        cfg.get_settings.cache_clear()
        yield {"host": sock_host, "user": u.username or "postgres", "dbname": "llmmanager"}
        for k in ("SERVER_MANAGER_LLM_PG_HOST", "SERVER_MANAGER_LLM_PG_DB",
                  "SERVER_MANAGER_LLM_PG_USER", "SERVER_MANAGER_LLM_PG_PASSWORD"):
            os.environ.pop(k, None)
        sess._engines.clear()
        sess._sessions.clear()
        cfg.get_settings.cache_clear()
        tokens_path = os.environ.get("_SM_SESSION_TOKENS_FILE")
        if tokens_path:
            os.environ["SERVER_MANAGER_SERVICE_TOKENS_FILE"] = tokens_path
            import time as _t

            _t.sleep(0.01)
            os.utime(tokens_path)


def _engine(econ_pg):
    from sqlalchemy import create_engine
    from urllib.parse import quote_plus

    dsn = (f"postgresql+psycopg2://{quote_plus(econ_pg['user'])}:@/{econ_pg['dbname']}"
           f"?host={econ_pg['host']}")
    return create_engine(dsn)


def test_revision_model_maps_schema(econ_pg):
    from sqlalchemy import text

    from server_manager.llm_manager.models.orm import DeploymentRevisionRecord

    eng = _engine(econ_pg)
    with eng.connect() as conn:
        sql_rows = conn.execute(text(
            "SELECT revision_id, revision_label, model, status FROM deployment_revisions "
            "ORDER BY revision_id")).all()
        orm_rows = conn.execute(
            DeploymentRevisionRecord.__table__.select()
            .order_by(DeploymentRevisionRecord.revision_id)).all()
        assert [(r.revision_id, r.revision_label, r.model, r.status) for r in orm_rows] \
            == [tuple(r) for r in sql_rows]
        assert orm_rows[0].launch_command == "vllm serve ..."


def test_deployment_model_maps_schema(econ_pg):
    from server_manager.llm_manager.models.orm import DeploymentRecord

    eng = _engine(econ_pg)
    with eng.connect() as conn:
        rows = conn.execute(DeploymentRecord.__table__.select()
                            .order_by(DeploymentRecord.deployment_id)).all()
        assert len(rows) == 1
        assert rows[0].served_model == "gemma4-26b-a4b"
        assert rows[0].desired_service_state == "SERVING"


def test_benchmark_run_maps_schema_numeric_decimal(econ_pg):
    """numeric columns come back as Decimal in SQL and the ORM; both must agree."""
    from decimal import Decimal

    from sqlalchemy import text

    from server_manager.llm_manager.models.orm import BenchmarkRunRecord

    eng = _engine(econ_pg)
    with eng.connect() as conn:
        sql_rows = conn.execute(text(
            "SELECT run_id, output_tok_s, status, deployment_revision_id "
            "FROM benchmark_runs ORDER BY run_id")).all()
        orm_rows = conn.execute(
            BenchmarkRunRecord.__table__.select().order_by(BenchmarkRunRecord.run_id)).all()
        assert [(r.run_id, r.output_tok_s, r.status, r.deployment_revision_id)
                for r in orm_rows] == [tuple(r) for r in sql_rows]
        assert isinstance(orm_rows[0].output_tok_s, Decimal)
        assert orm_rows[1].status == "FAILED"  # failed runs recorded, not hidden


def test_qualification_envelope_maps_schema(econ_pg):
    from server_manager.llm_manager.models.orm import QualificationEnvelopeRecord

    eng = _engine(econ_pg)
    with eng.connect() as conn:
        rows = conn.execute(QualificationEnvelopeRecord.__table__.select()).all()
        assert len(rows) == 1
        assert rows[0].hard_concurrency_limit == 8
        assert rows[0].benchmark_run_id == 1


def test_slice23_endpoints(econ_pg):
    import time as _t

    tf = "/tmp/llm-slices23-tokens"
    prev = os.environ.get("SERVER_MANAGER_SERVICE_TOKENS_FILE")
    if prev:
        os.environ["_SM_SESSION_TOKENS_FILE"] = prev
    with open(tf, "w") as f:
        f.write("svc-acms=tok-slices23:ALL\n")
    os.environ["SERVER_MANAGER_SERVICE_TOKENS_FILE"] = tf
    _t.sleep(0.01)
    os.utime(tf)

    from fastapi.testclient import TestClient

    import server_manager.api_app as api_app
    import server_manager.common.config.settings as cfg
    import server_manager.common.db.session as sess

    sess._engines.clear()
    sess._sessions.clear()
    cfg.get_settings.cache_clear()
    c = TestClient(api_app.app)
    H = {"Authorization": "Bearer tok-slices23"}
    try:
        r = c.get("/api/v1/deployment-revisions", headers=H)
        assert r.status_code == 200, r.text
        revs = {x["revision_label"]: x for x in r.json()["revisions"]}
        assert revs["gemma4-baseline"]["systemd_unit"] == "vllm.service"
        assert revs["flashnext-rollback"]["status"] == "CANDIDATE"
        # model filter
        r = c.get("/api/v1/deployment-revisions", headers=H,
                  params={"model": "gemma4-26b-a4b"})
        labels = {x["revision_label"] for x in r.json()["revisions"]}
        assert labels == {"gemma4-baseline"}

        r = c.get("/api/v1/deployments", headers=H)
        assert r.status_code == 200
        deps = r.json()["deployments"]
        assert deps[0]["served_model"] == "gemma4-26b-a4b"

        r = c.get("/api/v1/benchmarks", headers=H)
        assert r.status_code == 200
        runs = r.json()["runs"]
        assert len(runs) == 2
        assert any(x["status"] == "FAILED" for x in runs)
        # filter by revision
        r2 = c.get("/api/v1/benchmarks", headers=H, params={"revision_id": 1})
        assert len(r2.json()["runs"]) == 2
        # filter by model name (join through revisions)
        r3 = c.get("/api/v1/benchmarks", headers=H,
                   params={"model": "gemma4-26b-a4b"})
        assert len(r3.json()["runs"]) == 2

        r = c.get("/api/v1/qualification-envelopes", headers=H)
        assert r.status_code == 200
        env = r.json()["envelopes"]
        assert env[0]["recommended_concurrency"] == 6
        assert env[0]["notes"] == "V5 handoff envelope"

        # auth: no token → 401
        assert c.get("/api/v1/benchmarks").status_code == 401
        # bad limit → 422
        assert c.get("/api/v1/benchmarks", headers=H,
                     params={"limit": 0}).status_code == 422
    finally:
        if prev:
            os.environ["SERVER_MANAGER_SERVICE_TOKENS_FILE"] = prev
