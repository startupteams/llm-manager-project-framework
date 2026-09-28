"""0003 arm superseded state — add SUPERSEDED to arm_runtime_actual_state.

REV2 §12 (2026-09-28): failed provisioning attempts are historical evidence —
they get an explicit SUPERSEDED state linked to the live retry that succeeded,
never deleted.

Revision ID: 0003_superseded_state
Revises: 0002_reconciler_bookkeeping
Create Date: 2026-09-28
"""
from alembic import op

revision = "0003_superseded_state"
down_revision = "0002_reconciler_bookkeeping"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Postgres enum: ALTER TYPE ... ADD VALUE cannot run inside a transaction
    # block on PG < 12 rules; alembic runs autocommit DDL here safely on PG16.
    op.execute("ALTER TYPE arm_runtime_actual_state ADD VALUE IF NOT EXISTS 'SUPERSEDED'")


def downgrade() -> None:
    # Enum value removal is not supported by PostgreSQL; leave the value in
    # place (harmless, unused) and document. This downgrade is a no-op by design.
    pass
