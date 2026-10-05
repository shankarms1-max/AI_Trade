"""Add Phase 14.2 context without relabeling or deleting historical records."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0013_phase14_2_strategy_logic"
down_revision = "0012_phase14_1"
branch_labels = None
depends_on = None


def upgrade():
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    for table in (
        "market_regime_snapshots",
        "strategy_candidate_sets",
        "shadow_trades",
    ):
        op.add_column(
            table, sa.Column("strategy_logic_version", sa.String(32), nullable=True)
        )
        op.add_column(
            table, sa.Column("strategy_context_json", json_type, nullable=True)
        )
        op.create_index(
            f"ix_{table}_strategy_logic_version", table, ["strategy_logic_version"]
        )
    for name, size in (
        ("market_bias", 16),
        ("directional_strength", 16),
        ("strategy_family_eligibility", 24),
    ):
        op.add_column(
            "market_regime_snapshots", sa.Column(name, sa.String(size), nullable=True)
        )
    for table in ("strategy_candidate_sets", "shadow_trades"):
        op.add_column(table, sa.Column("strategy_family", sa.String(32), nullable=True))


def downgrade():
    for table in ("strategy_candidate_sets", "shadow_trades"):
        op.drop_column(table, "strategy_family")
    for name in ("market_bias", "directional_strength", "strategy_family_eligibility"):
        op.drop_column("market_regime_snapshots", name)
    for table in (
        "market_regime_snapshots",
        "strategy_candidate_sets",
        "shadow_trades",
    ):
        op.drop_index(f"ix_{table}_strategy_logic_version", table_name=table)
        op.drop_column(table, "strategy_context_json")
        op.drop_column(table, "strategy_logic_version")
