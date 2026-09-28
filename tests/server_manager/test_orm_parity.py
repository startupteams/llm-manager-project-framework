"""TDR-0008 slice-1 ORM parity tests (reflective models over llmmanager).

Runs against an embedded PostgreSQL created from db/schema/schema.sql (the
clean-install reference), proving:
- ORM models map the schema without DDL drift;
- ORM reads return the same rows as raw SQL (row-count + content parity);
- the ORM /hosts + /model-routes endpoints serve the same data.

No production DB is touched (pgserver embedded instance).
"""
from __future__ import annotations

import os

import pytest

pytest.importorskip("pgserver", reason="pgserver required for parity tests")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(scope="module")
def llm_pg(tmp_path_factory):
    """Embedded PG loaded from schema.sql; sets SERVER_MANAGER_LLM_PG_* env."""
    import pgserver

    data_dir = tmp_path_factory.mktemp("llm-parity-pgdata")
    with pgserver.get_server(str(data_dir)) as srv:
        uri = srv.get_uri()  # postgresql://postgres:@/postgres?host=<sockdir>
        import psycopg2

        from urllib.parse import parse_qs, urlencode, urlparse

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
        # strip pg_dump ≥17 \restrict/\unrestrict meta lines (psql-only commands)
        schema = "\n".join(l for l in raw.splitlines()
                           if not l.lstrip().startswith("\\"))
        conn = psycopg2.connect(base + " dbname=llmmanager")
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(schema)
        # pg_dump leaves search_path='' on the session (set_config('search_path','',false));
        # restore so subsequent unqualified writes/reads use public.
        cur.execute("SET search_path = public")
        # active_hosts is created by the app at startup (active_deployments.DDL, §21.7) —
        # apply the same DDL here so the parity DB matches production shape.
        from service.app import active_deployments as _ad  # type: ignore

        cur.execute(_ad.DDL)
        # seed parity fixtures
        cur.execute("""
            INSERT INTO public.hosts (host_id, name, guest_ip, gpu_count, gpu_model, node, vmid,
                               desired_power_state, management_mode, desired_service_state)
            VALUES (1, 'MIAM-00111', '10.0.20.168', 6, 'RTX 3080', 'miam00111', 102, 'RUNNING', 'MANAGED', 'SERVING'),
                   (2, 'MIAM-00143', '10.0.20.163', 6, 'RTX 3080', 'miam00143', 109, 'RUNNING', 'MANAGED', 'SERVING')
        """)
        cur.execute("""
            INSERT INTO public.active_hosts (physical_host, active_host_ip, updated_by)
            VALUES ('MIAM-00111', '10.0.20.168', 'test-seed')
        """)
        cur.execute("""
            INSERT INTO public.model_registry (logical_model_name, host_id, engine, backend_url,
                                        context_limit, health, routable, is_alias, alias_target)
            VALUES ('fast', 1, 'vllm', 'http://10.0.20.168:8000/v1', 262144, 'healthy', true, false, NULL),
                   ('code', NULL, NULL, NULL, NULL, 'unknown', false, true, 'qwen3.8-27b'),
                   ('qwen3.8-27b', 1, 'vllm', 'http://10.0.20.168:8000/v1', 70000, 'healthy', true, false, NULL)
        """)
        conn.commit()
        cur.close()
        conn.close()

        # point the SM settings at the embedded DB for db='llm' (socket dir works as PGHOST)
        os.environ["SERVER_MANAGER_LLM_PG_HOST"] = sock_host
        os.environ["SERVER_MANAGER_LLM_PG_DB"] = "llmmanager"
        os.environ["SERVER_MANAGER_LLM_PG_USER"] = u.username or "postgres"
        os.environ["SERVER_MANAGER_LLM_PG_PASSWORD"] = ""
        # clear cached engines/session factories so the env applies
        import server_manager.common.db.session as sess
        import server_manager.common.config.settings as cfg

        sess._engines.clear()
        sess._sessions.clear()
        cfg.get_settings.cache_clear()
        yield {"host": sock_host, "user": u.username or "postgres", "dbname": "llmmanager"}
        os.environ.pop("SERVER_MANAGER_LLM_PG_HOST", None)
        os.environ.pop("SERVER_MANAGER_LLM_PG_DB", None)
        os.environ.pop("SERVER_MANAGER_LLM_PG_USER", None)
        os.environ.pop("SERVER_MANAGER_LLM_PG_PASSWORD", None)
        sess._engines.clear()
        sess._sessions.clear()
        cfg.get_settings.cache_clear()
        # restore the session-scoped tokens file binding so later tests are unaffected
        tokens_path = os.environ.get("_SM_SESSION_TOKENS_FILE")
        if tokens_path:
            os.environ["SERVER_MANAGER_SERVICE_TOKENS_FILE"] = tokens_path
        import time as _t
        _t.sleep(0.01)
        if tokens_path:
            os.utime(tokens_path)


def _engine(llm_pg):
    from sqlalchemy import create_engine
    from urllib.parse import quote_plus

    dsn = (f"postgresql+psycopg2://{quote_plus(llm_pg['user'])}:@/{llm_pg['dbname']}"
           f"?host={llm_pg['host']}")
    return create_engine(dsn)


def test_host_record_maps_schema(llm_pg):
    from sqlalchemy import text

    eng = _engine(llm_pg)
    with eng.connect() as conn:
        n = conn.execute(text("SELECT count(*) FROM hosts")).scalar()
        assert n == 2
    from server_manager.llm_manager.models.orm import HostRecord

    with eng.connect() as conn:
        rows = conn.execute(text("SELECT host_id, name, guest_ip FROM hosts ORDER BY host_id")).all()
        orm_rows = conn.execute(
            HostRecord.__table__.select().order_by(HostRecord.host_id)).all()
        assert [(r.host_id, r.name, r.guest_ip) for r in orm_rows] == [tuple(r) for r in rows]


def test_active_host_record_maps_schema(llm_pg):
    from sqlalchemy import text

    eng = _engine(llm_pg)
    from server_manager.llm_manager.models.orm import ActiveHostRecord

    with eng.connect() as conn:
        orm_rows = conn.execute(ActiveHostRecord.__table__.select()).all()
        assert len(orm_rows) == 1
        assert orm_rows[0].physical_host == "MIAM-00111"
        assert orm_rows[0].active_host_ip == "10.0.20.168"


def test_model_registry_record_maps_schema(llm_pg):
    from sqlalchemy import text

    eng = _engine(llm_pg)
    from server_manager.llm_manager.models.orm import ModelRegistryRecord

    with eng.connect() as conn:
        sql_rows = conn.execute(text(
            "SELECT logical_model_name, engine, backend_url, health, routable, is_alias "
            "FROM model_registry ORDER BY id")).all()
        orm_rows = conn.execute(
            ModelRegistryRecord.__table__.select().order_by(ModelRegistryRecord.id)).all()
        assert [(r.logical_model_name, r.engine, r.backend_url, r.health, r.routable, r.is_alias)
                for r in orm_rows] == [tuple(r) for r in sql_rows]
        # the ORM route read matches the Core text() read (the existing API path)
        core_rows = conn.execute(text(
            "SELECT logical_model_name, engine, backend_url, context_limit, health, routable "
            "FROM model_registry WHERE logical_model_name = 'fast'")).all()
        assert len(core_rows) == 1
        assert core_rows[0][0] == "fast"


def test_orm_api_hosts_endpoint(llm_pg, monkeypatch):
    from server_manager.common.auth import service_tokens
    import time as _t

    prev_tokens = os.environ.get("SERVER_MANAGER_SERVICE_TOKENS_FILE")
    if prev_tokens:
        os.environ["_SM_SESSION_TOKENS_FILE"] = prev_tokens
    tf = "/tmp/llm-parity-tokens"
    with open(tf, "w") as f:
        f.write("svc-acms=tok-parity-0001:ALL\n")
    os.environ["SERVER_MANAGER_SERVICE_TOKENS_FILE"] = tf
    _t.sleep(0.01)
    os.utime(tf)

    from fastapi.testclient import TestClient

    import server_manager.common.config.settings as cfg
    import server_manager.common.db.session as sess

    sess._engines.clear()
    sess._sessions.clear()
    cfg.get_settings.cache_clear()

    import server_manager.api_app as api_app

    c = TestClient(api_app.app)
    r = c.get("/api/v1/hosts", headers={"Authorization": "Bearer tok-parity-0001"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["api_contract_version"] == "1.0.0"
    hosts = {h["name"]: h for h in body["hosts"]}
    assert hosts["MIAM-00111"]["active_model_host"] == "10.0.20.168"
    assert hosts["MIAM-00143"]["active_model_host"] is None
    r2 = c.get("/api/v1/model-routes", headers={"Authorization": "Bearer tok-parity-0001"})
    assert r2.status_code == 200
    routes = r2.json()["routes"]
    assert routes["code"][0]["is_alias"] is True
    assert routes["code"][0]["alias_target"] == "qwen3.8-27b"
