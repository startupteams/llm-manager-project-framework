"""ARM (Agent Runtime Manager) test suite — shared fixtures.

Runs against an embedded PostgreSQL (pgserver, no Docker). One pgserver
instance per pytest session; one isolated ARM schema per test via
transaction rollback: the session-level DB setup runs ``alembic upgrade
head`` once, and every test executes inside a transaction that is rolled
back afterwards, so tests are isolated without re-running migrations.
"""
from __future__ import annotations

import os
import sys
import time
import uuid

import pytest
import sqlalchemy as sa

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

pgserver = pytest.importorskip("pgserver", reason="pgserver required for ARM DB-backed tests")

from server_manager.agent_runtime_manager.models import orm as orm_mod  # noqa: E402
from server_manager.agent_runtime_manager.models.orm import Base  # noqa: E402


# --------------------------------------------------------------------------
# session-scoped embedded PostgreSQL + migrations
# --------------------------------------------------------------------------
@pytest.fixture(scope="session")
def arm_pg(tmp_path_factory):
    """Embedded PostgreSQL server + fully-migrated ARM schema.

    Sets SERVER_MANAGER_ARM_PG_DSN for the whole session; returns the DSN.
    """
    data_dir = tmp_path_factory.mktemp("arm-pgdata")
    with pgserver.get_server(str(data_dir)) as srv:
        uri = srv.get_uri()
        if uri.startswith("postgres://"):
            uri = uri.replace("postgres://", "postgresql+psycopg2://", 1)
        elif uri.startswith("postgresql://"):
            uri = uri.replace("postgresql://", "postgresql+psycopg2://", 1)
        os.environ["SERVER_MANAGER_ARM_PG_DSN"] = uri

        from alembic import command
        from alembic.config import Config

        cfg = Config(os.path.join(REPO, "alembic.ini"))
        # Do NOT set sqlalchemy.url — env.py resolves SERVER_MANAGER_ARM_PG_DSN.
        command.upgrade(cfg, "head")

        yield uri

        os.environ.pop("SERVER_MANAGER_ARM_PG_DSN", None)


@pytest.fixture()
def arm_engine(arm_pg):
    """Engine bound to the migrated ARM schema (fresh per test)."""
    return sa.create_engine(arm_pg, future=True)


@pytest.fixture()
def arm_schema(arm_engine):
    """Wrap every test in a transaction that is rolled back afterwards.

    The test-visible connection runs inside an outer transaction; the
    Session joins it via savepoint-mapped ``join_transaction_mode="create_savepoint"``.
    The outer transaction is rolled back at teardown, undoing all writes.
    """
    conn = arm_engine.connect()
    outer = conn.begin()
    yield {"connection": conn, "engine": arm_engine}
    outer.rollback()
    conn.close()


@pytest.fixture()
def arm_session(arm_schema):
    """ORM Session bound to the per-test savepoint transaction."""
    from sqlalchemy.orm import Session

    return Session(bind=arm_schema["connection"], join_transaction_mode="create_savepoint",
                   expire_on_commit=False, future=True)


# --------------------------------------------------------------------------
# embedded FastAPI app (same DB, same rolled-back transaction)
# --------------------------------------------------------------------------
@pytest.fixture()
def arm_app(arm_schema):
    """FastAPI app with api.v1's get_session_factory bound to the test transaction.

    api.v1 does ``from server_manager.common.db.session import get_session_factory``
    so the name must be patched in the api.v1 namespace.
    """
    from unittest import mock

    from server_manager.agent_runtime_manager.app import app as fastapi_app
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=arm_schema["connection"], join_transaction_mode="create_savepoint",
                           expire_on_commit=False, future=True)
    with mock.patch("server_manager.agent_runtime_manager.api.v1.get_session_factory",
                    return_value=factory):
        yield fastapi_app


@pytest.fixture()
def arm_client(arm_app):
    from fastapi.testclient import TestClient

    return TestClient(arm_app)


# --------------------------------------------------------------------------
# service identity (bearer tokens) — temp file, mtime-bumped for cache
# --------------------------------------------------------------------------
@pytest.fixture(scope="session")
def tokens_file(tmp_path_factory):
    """Session-scoped tokens file: svc-acms has ALL scopes; svc-readonly is read-only.

    service_tokens caches by mtime — new tokens are added by APPENDING and
    bumping the mtime (os.utime), never by rewriting the file.
    """
    path = tmp_path_factory.mktemp("arm-auth") / "service_tokens"
    path.write_text("# test service tokens\nsvc-acms=tok-acms-full-0001:ALL\n")
    os.environ["SERVER_MANAGER_SERVICE_TOKENS_FILE"] = str(path)
    time.sleep(0.01)
    os.utime(path)  # cache key includes mtime; ensure > previous stat
    yield path
    os.environ.pop("SERVER_MANAGER_SERVICE_TOKENS_FILE", None)


@pytest.fixture()
def add_token(tokens_file):
    """Append an extra identity to the tokens file (mtime-bumped)."""
    def _add(service: str, token: str, scopes: str) -> None:
        with open(tokens_file, "a") as f:
            f.write(f"{service}={token}:{scopes}\n")
        time.sleep(0.01)
        os.utime(tokens_file)
    return _add


@pytest.fixture()
def auth_headers():
    def _h(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}
    return _h


# --------------------------------------------------------------------------
# provider fakes
# --------------------------------------------------------------------------
class FakeProxmoxProvider:
    """In-memory ProxmoxVMProvider stand-in. No network.

    Satisfies everything ProvisioningService / verify_ownership / the API
    touch: next_vmid, clone_template, wait_clone_lock_release,
    configure_cloud_init, start, wait_for_ip, shutdown, destroy,
    vm_exists, vm_config, vm_status.
    """

    def __init__(self, ip: str | None = "10.0.20.200", vm_config: dict | None = None,
                 exists: bool = True):
        self.ip = ip
        self.exists = exists
        self.vm_config_map: dict[tuple[str, int], dict] = {}
        self.calls: list[tuple] = []
        if vm_config:
            self.vm_config_map.setdefault(("n1", 1), vm_config)

    # -- lifecycle ---------------------------------------------------------
    def next_vmid(self) -> int:
        self.calls.append(("next_vmid",))
        return 3100 + len(self.calls)

    def clone_template(self, spec, new_vmid: int) -> None:
        self.calls.append(("clone", spec.template_vmid, new_vmid))
        self.vm_config_map[(spec.template_node, new_vmid)] = {}

    def wait_clone_lock_release(self, node: str, vmid: int, max_s: int = 240) -> None:
        self.calls.append(("lock_wait", node, vmid))

    def configure_cloud_init(self, spec, vmid: int, extra: dict | None = None) -> None:
        self.calls.append(("cloud_init", vmid))
        self.vm_config_map[(spec.template_node, vmid)]["description"] = \
            __import__("json").dumps(spec.description_meta)

    def start(self, node: str, vmid: int) -> None:
        self.calls.append(("start", node, vmid))

    def shutdown(self, node: str, vmid: int, timeout_s: int = 180) -> None:
        self.calls.append(("shutdown", node, vmid))

    def stop_force(self, node: str, vmid: int) -> None:
        self.calls.append(("stop_force", node, vmid))

    def destroy(self, node: str, vmid, purge: bool = True, destroy_unreferenced: bool = True) -> None:
        self.calls.append(("destroy", node, vmid))
        self.vm_config_map.pop((node, int(vmid)), None)
        self.exists = False

    # -- reads -------------------------------------------------------------
    def vm_config(self, node: str, vmid: int) -> dict:
        self.calls.append(("vm_config", node, vmid))
        return self.vm_config_map.get((node, int(vmid)), {})

    def vm_status(self, node: str, vmid: int) -> dict:
        return {}

    def vm_exists(self, node: str, vmid: int) -> bool:
        return self.exists and (node, int(vmid)) in self.vm_config_map

    def wait_for_ip(self, node: str, vmid: int, max_s: int = 300) -> str | None:
        self.calls.append(("wait_for_ip", node, vmid))
        return self.ip


@pytest.fixture()
def fake_provider():
    return FakeProxmoxProvider()


@pytest.fixture()
def fake_no_ip_provider():
    p = FakeProxmoxProvider()
    p.ip = None  # wait_for_ip returns None → provisioning must fail
    return p


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def make_runtime_request(request_id: str | None = None, **overrides):
    from server_manager.agent_runtime_manager.services.provisioning import ProvisionRequest

    defaults = dict(
        acms_agent_id=str(uuid.uuid4()),
        request_id=request_id or f"req-{uuid.uuid4().hex[:12]}",
        name=f"t-vm-{uuid.uuid4().hex[:6]}",
    )
    defaults.update(overrides)
    return ProvisionRequest(**defaults)


@pytest.fixture()
def make_provisioned_runtime(arm_session, fake_provider):
    """Factory: submit + run a happy-path job with the fake provider.

    Returns (runtime, job); both committed inside the test transaction.
    """
    def _make(**overrides) -> tuple[orm_mod.AgentRuntime, object]:
        from server_manager.agent_runtime_manager.services.provisioning import ProvisioningService

        svc = ProvisioningService(provider=fake_provider)
        req = make_runtime_request(**overrides)
        job, _created = svc.submit(arm_session, req, requested_by="svc-acms", authority="test")
        arm_session.commit()
        svc.run_job(arm_session, job.job_id)
        arm_session.commit()
        return arm_session.get(orm_mod.AgentRuntime, job.runtime_id), job
    return _make
