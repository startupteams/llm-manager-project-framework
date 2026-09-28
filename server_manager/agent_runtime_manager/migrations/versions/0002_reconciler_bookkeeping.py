"""0002 arm reconciler bookkeeping — last_reconcile_at, last_error, recovery_count (§10B).

Revision ID: 0002_reconciler_bookkeeping
Revises: 0001_arm_initial_schema
Create Date: 2026-09-28
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_reconciler_bookkeeping"
down_revision = "a73a833b72d1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_runtimes", sa.Column("last_reconcile_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("agent_runtimes", sa.Column("last_error", sa.Text(), nullable=True))
    op.add_column("agent_runtimes", sa.Column("recovery_count", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("agent_runtimes", "recovery_count")
    op.drop_column("agent_runtimes", "last_error")
    op.drop_column("agent_runtimes", "last_reconcile_at")
