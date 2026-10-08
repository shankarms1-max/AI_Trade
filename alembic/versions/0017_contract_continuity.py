"""Add nullable supplementary exact-contract capture evidence to each snapshot."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0017_contract_continuity"
down_revision = "0016_scalper"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("market_snapshots", sa.Column(
        "required_contracts_json", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
        nullable=True))


def downgrade():
    # Explicit downgrade discards supplementary quotes, never original chain/journal rows.
    op.drop_column("market_snapshots", "required_contracts_json")
