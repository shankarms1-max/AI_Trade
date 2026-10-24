"""Add isolated MCX research evidence; no alteration of existing NIFTY tables."""

from alembic import op
import sqlalchemy as sa

revision = "0020_commodity_acts_research"
down_revision = "0019_parallel_paper"
branch_labels = None
depends_on = None

IMMUTABLE = (
    "commodity_market_snapshots",
    "commodity_option_quotes",
    "commodity_acts_events",
    "commodity_acts_decisions",
)


def upgrade():
    op.create_table(
        "commodity_market_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("instrument_id", sa.String(24), nullable=False),
        sa.Column("capture_key", sa.String(64), unique=True, nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("snapshot_json", sa.Text(), nullable=False),
        sa.UniqueConstraint("instrument_id", "id", name="uq_commodity_snapshot_owner"),
        sa.CheckConstraint("instrument_id IN ('CRUDEOILM','NATGASMINI')", name="ck_commodity_snapshot_instrument"),
    )
    for name in ("instrument_id", "captured_at"):
        op.create_index(f"ix_commodity_market_snapshots_{name}", "commodity_market_snapshots", [name])
    op.create_table(
        "commodity_option_quotes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("instrument_id", sa.String(24), nullable=False),
        sa.Column("snapshot_id", sa.Integer(), nullable=False),
        sa.Column("identity_key", sa.String(64), nullable=False),
        sa.Column("quote_json", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["instrument_id", "snapshot_id"],
            ["commodity_market_snapshots.instrument_id", "commodity_market_snapshots.id"],
        ),
        sa.UniqueConstraint("instrument_id", "snapshot_id", "identity_key", name="uq_commodity_quote_identity"),
    )
    op.create_index("ix_commodity_option_quotes_snapshot_id", "commodity_option_quotes", ["snapshot_id"])
    op.create_table(
        "commodity_acts_cursors",
        sa.Column("instrument_id", sa.String(24), primary_key=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("head_hash", sa.String(64), nullable=False),
        sa.Column("lock_version", sa.Integer(), nullable=False),
        sa.Column("observed_at", sa.String(64)),
        sa.Column("state_json", sa.Text(), nullable=False),
        sa.Column("latest_json", sa.Text(), nullable=False),
        sa.CheckConstraint("instrument_id IN ('CRUDEOILM','NATGASMINI')", name="ck_commodity_cursor_instrument"),
    )
    cursors = sa.table(
        "commodity_acts_cursors",
        sa.column("instrument_id", sa.String()),
        sa.column("sequence", sa.Integer()),
        sa.column("head_hash", sa.String()),
        sa.column("lock_version", sa.Integer()),
        sa.column("state_json", sa.Text()),
        sa.column("latest_json", sa.Text()),
    )
    op.bulk_insert(
        cursors,
        [
            {
                "instrument_id": instrument,
                "sequence": 0,
                "head_hash": "GENESIS",
                "lock_version": 0,
                "state_json": "{}",
                "latest_json": "{}",
            }
            for instrument in ("CRUDEOILM", "NATGASMINI")
        ],
    )
    op.create_table(
        "commodity_acts_events",
        sa.Column("instrument_id", sa.String(24), primary_key=True),
        sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("event_key", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("previous_hash", sa.String(64), nullable=False),
        sa.Column("event_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.UniqueConstraint("instrument_id", "event_key", name="uq_commodity_event_key"),
        sa.CheckConstraint("instrument_id IN ('CRUDEOILM','NATGASMINI')", name="ck_commodity_event_instrument"),
    )
    op.create_table(
        "commodity_acts_decisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("instrument_id", sa.String(24), nullable=False),
        sa.Column("snapshot_id", sa.Integer(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("document", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["instrument_id", "snapshot_id"],
            ["commodity_market_snapshots.instrument_id", "commodity_market_snapshots.id"],
        ),
        sa.UniqueConstraint("instrument_id", "snapshot_id", name="uq_commodity_decision_snapshot"),
    )
    dialect = op.get_context().dialect.name
    if dialect == "postgresql":
        op.execute(
            "CREATE FUNCTION reject_commodity_research_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'COMMODITY_RESEARCH_EVIDENCE_IS_IMMUTABLE'; END; $$"
        )
        for table in IMMUTABLE:
            op.execute(
                f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON {table} "
                "FOR EACH STATEMENT EXECUTE FUNCTION reject_commodity_research_mutation()"
            )
    elif dialect == "sqlite":
        for table in IMMUTABLE:
            for action in ("UPDATE", "DELETE"):
                op.execute(
                    f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} "
                    "BEGIN SELECT RAISE(ABORT, 'COMMODITY_RESEARCH_EVIDENCE_IS_IMMUTABLE'); END"
                )
    else:
        raise RuntimeError("Commodity research supports PostgreSQL and SQLite only")


def downgrade():
    for table in reversed(IMMUTABLE):
        op.drop_table(table)
    op.drop_table("commodity_acts_cursors")
    if op.get_context().dialect.name == "postgresql":
        op.execute("DROP FUNCTION reject_commodity_research_mutation()")
