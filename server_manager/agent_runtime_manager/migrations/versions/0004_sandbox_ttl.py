"""0004 arm sandbox ttl — sandbox_expires_at column on agent_runtimes.

STEA-004 plan §26 (2026-10-02): proxmox.sandbox.* — sandboxes are ARM runtimes
with runtime_class="sandbox" carrying a TTL. The ARM reconciler sweep flips
expired sandboxes to DESIRED_DESTROYED (API-only; nothing is auto-deleted from
PVE — the existing destroy policy applies).

Revision ID: 0004_sandbox_ttl
Revises: 0003_superseded_state
Create Date: 2026-10-02
"""
from alembic import op
import sqlalchemy as sa

revision = "0004_sandbox_ttl"
down_revision = "0003_superseded_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_runtimes",
        sa.Column("sandbox_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Partial index: the TTL sweep only scans live sandboxes.
    op.create_index(
        "ix_agent_runtimes_sandbox_expires",
        "agent_runtimes",
        ["sandbox_expires_at"],
        postgresql_where=sa.text("sandbox_expires_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_agent_runtimes_sandbox_expires", table_name="agent_runtimes")
    op.drop_column("agent_runtimes", "sandbox_expires_at")
