"""Alembic environment for the agent_runtime_manager database (§1/§6)."""
from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from server_manager.agent_runtime_manager.models.orm import Base
from server_manager.common.config.settings import get_settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    dsn = os.environ.get("SERVER_MANAGER_ARM_PG_DSN")
    if dsn:
        return dsn
    s = get_settings()
    from urllib.parse import quote_plus

    return (
        f"postgresql+psycopg2://{quote_plus(s.arm_pg_user)}:{quote_plus(s.arm_pg_password)}"
        f"@{s.arm_pg_host}:{s.arm_pg_port}/{s.arm_pg_db}"
    )


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    cfg = config.get_section(config.config_ini_section, {})
    cfg["sqlalchemy.url"] = _url()
    connectable = engine_from_config(cfg, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
