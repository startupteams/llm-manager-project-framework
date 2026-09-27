"""SQLAlchemy 2.x engine/session factories for the Server Manager databases.

Two logical databases (§1): llmmanager (existing) + agent_runtime_manager (new).
Both may share the same PG host; credentials are separate; no cross-database FKs.
"""
from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from server_manager.common.config.settings import get_settings

_engines: dict[str, Engine] = {}
_sessions: dict[str, sessionmaker] = {}


def _make_url(dsn_host: str, port: int, db: str, user: str, password: str) -> str:
    from urllib.parse import quote_plus

    return f"postgresql+psycopg2://{quote_plus(user)}:{quote_plus(password)}@{dsn_host}:{port}/{db}"


def get_engine(db: str = "arm") -> Engine:
    """db='arm' → agent_runtime_manager DB; db='llm' → llmmanager DB."""
    if db in _engines:
        return _engines[db]
    s = get_settings()
    if db == "arm":
        url = _make_url(s.arm_pg_host, s.arm_pg_port, s.arm_pg_db, s.arm_pg_user, s.arm_pg_password)
    elif db == "llm":
        url = _make_url(s.llm_pg_host, s.llm_pg_port, s.llm_pg_db, s.llm_pg_user, s.llm_pg_password)
    else:
        raise ValueError(f"unknown logical db {db!r} (use 'arm' or 'llm')")
    eng = create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5, future=True)
    _engines[db] = eng
    return eng


def get_session_factory(db: str = "arm") -> sessionmaker:
    if db in _sessions:
        return _sessions[db]
    _sessions[db] = sessionmaker(bind=get_engine(db), expire_on_commit=False, future=True)
    return _sessions[db]


def session_scope(db: str = "arm") -> Session:
    """Context-manager session: commit on success, rollback on error, always close."""
    factory = get_session_factory(db)
    return factory()