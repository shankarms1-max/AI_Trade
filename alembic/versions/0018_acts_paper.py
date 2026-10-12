"""Add isolated ACTS_V1 paper projections and append-only event evidence."""
from alembic import op
import sqlalchemy as sa

revision = "0018_acts_paper"
down_revision = "0017_contract_continuity"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "acts_paper_cursor",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("snapshot_id", sa.Integer()),
        sa.Column("observed_at", sa.String(64)),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("head_hash", sa.String(64), nullable=False),
        sa.Column("lock_version", sa.Integer(), nullable=False),
        sa.Column("watch_json", sa.Text(), nullable=False),
        sa.Column("latest_json", sa.Text(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_acts_paper_single_cursor"))
    op.execute("INSERT INTO acts_paper_cursor "
               "(id, sequence, head_hash, lock_version, watch_json, latest_json) "
               "VALUES (1, 0, 'GENESIS', 0, '{}', '{}')")
    op.create_table(
        "acts_paper_trades",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("decision_snapshot_id", sa.Integer(),
                  sa.ForeignKey("scalper_market_snapshots.id"), nullable=False,
                  unique=True),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("document", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "state IN ('PENDING_PAPER_ENTRY','OPEN','CLOSED','ENTRY_REJECTED',"
            "'UNRESOLVED_EXPOSURE')", name="ck_acts_paper_trade_state"))
    op.create_index("ix_acts_paper_trades_state", "acts_paper_trades", ["state"])
    op.create_table(
        "acts_paper_events",
        sa.Column("sequence", sa.Integer(), primary_key=True,
                  autoincrement=False),
        sa.Column("event_key", sa.String(220), nullable=False, unique=True),
        sa.Column("event_type", sa.String(48), nullable=False),
        sa.Column("snapshot_id", sa.Integer(),
                  sa.ForeignKey("scalper_market_snapshots.id"), nullable=False),
        sa.Column("trade_id", sa.String(64),
                  sa.ForeignKey("acts_paper_trades.id")),
        sa.Column("previous_hash", sa.String(64), nullable=False),
        sa.Column("event_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False))
    dialect = op.get_context().dialect.name
    if dialect == "postgresql":
        op.execute("CREATE FUNCTION reject_acts_paper_event_mutation() RETURNS trigger "
                   "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION "
                   "'ACTS_PAPER_EVIDENCE_IS_IMMUTABLE'; END; $$")
        op.execute("CREATE TRIGGER acts_paper_events_immutable BEFORE UPDATE OR DELETE OR "
                   "TRUNCATE ON acts_paper_events FOR EACH STATEMENT EXECUTE FUNCTION "
                   "reject_acts_paper_event_mutation()")
    elif dialect == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER acts_paper_events_no_{action.lower()} BEFORE {action} "
                "ON acts_paper_events BEGIN SELECT "
                "RAISE(ABORT, 'ACTS_PAPER_EVIDENCE_IS_IMMUTABLE'); END")
    else:
        raise RuntimeError("ACTS paper supports PostgreSQL and SQLite only")


def downgrade():
    op.drop_table("acts_paper_events")
    if op.get_context().dialect.name == "postgresql":
        op.execute("DROP FUNCTION reject_acts_paper_event_mutation()")
    op.drop_index("ix_acts_paper_trades_state", "acts_paper_trades")
    op.drop_table("acts_paper_trades")
    op.drop_table("acts_paper_cursor")
