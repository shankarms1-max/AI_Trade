"""Preserve confirmed depth units and optional tick metadata; no relabeling.

Downgrade removes only these metadata columns. Export them before downgrading
if confirmed metadata must be retained. Migration 0013 is unchanged.
"""
from alembic import op
import sqlalchemy as sa

revision = "0014_phase14_2_1_replay_integrity"
down_revision = "0013_phase14_2_strategy_logic"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("option_contract_snapshots", sa.Column(
        "depth_unit", sa.String(12), nullable=False, server_default="UNKNOWN"))
    op.add_column("option_contract_snapshots", sa.Column("tick_size", sa.Numeric(20, 6), nullable=True))


def downgrade():
    op.drop_column("option_contract_snapshots", "tick_size")
    op.drop_column("option_contract_snapshots", "depth_unit")
