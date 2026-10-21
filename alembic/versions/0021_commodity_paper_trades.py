"""Add isolated commodity paper lifecycle only; preserve all existing research."""
from alembic import op
import sqlalchemy as sa

revision = "0021_commodity_paper_trades"
down_revision = "0020_commodity_acts_research"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("commodity_paper_cursors", sa.Column("instrument_id", sa.String(24), primary_key=True),
        sa.Column("sequence", sa.Integer(), nullable=False), sa.Column("head_hash", sa.String(64), nullable=False),
        sa.Column("observed_at", sa.String(64)), sa.Column("snapshot_id", sa.Integer()),
        sa.Column("state_json", sa.Text(), nullable=False), sa.Column("latest_json", sa.Text(), nullable=False),
        sa.CheckConstraint("instrument_id IN ('CRUDEOILM','NATGASMINI')"))
    op.create_table("commodity_paper_controls", sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("lock_version", sa.Integer(), nullable=False), sa.Column("policy_json", sa.Text(), nullable=False),
        sa.CheckConstraint("id=1"))
    op.bulk_insert(sa.table("commodity_paper_controls", sa.column("id", sa.Integer()), sa.column("lock_version", sa.Integer()),
        sa.column("policy_json", sa.Text())), [{"id": 1, "lock_version": 0, "policy_json": "{}"}])
    op.bulk_insert(sa.table("commodity_paper_cursors", sa.column("instrument_id", sa.String()), sa.column("sequence", sa.Integer()),
        sa.column("head_hash", sa.String()), sa.column("state_json", sa.Text()), sa.column("latest_json", sa.Text())),
        [{"instrument_id": item, "sequence": 0, "head_hash": "GENESIS", "state_json": "{}", "latest_json": "{}"}
         for item in ("CRUDEOILM", "NATGASMINI")])
    op.create_table("commodity_paper_positions", sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("instrument_id", sa.String(24), nullable=False), sa.Column("state", sa.String(32), nullable=False),
        sa.Column("document", sa.Text(), nullable=False), sa.Column("observed_at", sa.String(64), nullable=False), sa.UniqueConstraint("instrument_id", "id"),
        sa.CheckConstraint("instrument_id IN ('CRUDEOILM','NATGASMINI')"))
    for field in ("instrument_id", "state"):
        op.create_index(f"ix_commodity_paper_positions_{field}", "commodity_paper_positions", [field])
    op.create_index("ix_commodity_paper_positions_observed_at", "commodity_paper_positions", ["observed_at"])
    op.create_table("commodity_paper_orders", sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("instrument_id", sa.String(24), nullable=False), sa.Column("position_id", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False), sa.Column("document", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.String(64), nullable=False),
        sa.UniqueConstraint("instrument_id", "id"), sa.ForeignKeyConstraint(["instrument_id", "position_id"],
            ["commodity_paper_positions.instrument_id", "commodity_paper_positions.id"]))
    op.create_index("ix_commodity_paper_orders_instrument_id", "commodity_paper_orders", ["instrument_id"])
    op.create_index("ix_commodity_paper_orders_observed_at", "commodity_paper_orders", ["observed_at"])
    op.create_table("commodity_paper_fills", sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("instrument_id", sa.String(24), nullable=False), sa.Column("order_id", sa.String(64), nullable=False, unique=True),
        sa.Column("document", sa.Text(), nullable=False), sa.Column("observed_at", sa.String(64), nullable=False), sa.ForeignKeyConstraint(["instrument_id", "order_id"],
            ["commodity_paper_orders.instrument_id", "commodity_paper_orders.id"]))
    op.create_index("ix_commodity_paper_fills_instrument_id", "commodity_paper_fills", ["instrument_id"])
    op.create_index("ix_commodity_paper_fills_observed_at", "commodity_paper_fills", ["observed_at"])
    op.create_table("commodity_paper_events", sa.Column("instrument_id", sa.String(24), primary_key=True),
        sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=False), sa.Column("event_key", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(40), nullable=False), sa.Column("previous_hash", sa.String(64), nullable=False),
        sa.Column("event_hash", sa.String(64), nullable=False), sa.Column("payload", sa.Text(), nullable=False),
        sa.UniqueConstraint("instrument_id", "event_key"), sa.CheckConstraint("instrument_id IN ('CRUDEOILM','NATGASMINI')"))
    dialect = op.get_context().dialect.name
    if dialect == "postgresql":
        op.execute("CREATE FUNCTION reject_commodity_paper_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ "
                   "BEGIN RAISE EXCEPTION 'COMMODITY_PAPER_EVIDENCE_IS_IMMUTABLE'; END; $$")
        for table in ("commodity_paper_fills", "commodity_paper_events"):
            op.execute(f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON {table} "
                       "FOR EACH STATEMENT EXECUTE FUNCTION reject_commodity_paper_mutation()")
    elif dialect == "sqlite":
        for table in ("commodity_paper_fills", "commodity_paper_events"):
            for action in ("UPDATE", "DELETE"):
                op.execute(f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} "
                           "BEGIN SELECT RAISE(ABORT, 'COMMODITY_PAPER_EVIDENCE_IS_IMMUTABLE'); END")
    else:
        raise RuntimeError("Unsupported commodity paper database")


def downgrade():
    for table in ("commodity_paper_events", "commodity_paper_fills", "commodity_paper_orders",
                  "commodity_paper_positions", "commodity_paper_controls", "commodity_paper_cursors"):
        op.drop_table(table)
    if op.get_context().dialect.name == "postgresql":
        op.execute("DROP FUNCTION reject_commodity_paper_mutation()")
