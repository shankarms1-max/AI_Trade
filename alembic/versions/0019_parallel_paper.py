"""Add independent parallel paper experiments without changing ACTS evidence."""
from alembic import op
import sqlalchemy as sa

revision = "0019_parallel_paper"
down_revision = "0018_acts_paper"
branch_labels = None
depends_on = None

STRATEGIES = ("ORB_15", "ORB_30", "ORB_60", "EMA_PULLBACK", "SUPERTREND_RSI_VOLUME")


def upgrade():
    op.create_table(
        "parallel_paper_cursors",
        sa.Column("strategy_id", sa.String(48), primary_key=True),
        sa.Column("snapshot_id", sa.Integer()),
        sa.Column("observed_at", sa.String(64)),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("head_hash", sa.String(64), nullable=False),
        sa.Column("lock_version", sa.Integer(), nullable=False),
        sa.Column("state_json", sa.Text(), nullable=False),
        sa.Column("latest_json", sa.Text(), nullable=False))
    cursors = sa.table("parallel_paper_cursors", sa.column("strategy_id", sa.String()),
                       sa.column("sequence", sa.Integer()), sa.column("head_hash", sa.String()),
                       sa.column("lock_version", sa.Integer()), sa.column("state_json", sa.Text()),
                       sa.column("latest_json", sa.Text()))
    op.bulk_insert(cursors, [
        {"strategy_id": name, "sequence": 0, "head_hash": "GENESIS",
         "lock_version": 0, "state_json": "{}", "latest_json": "{}"}
        for name in STRATEGIES])
    op.create_table(
        "parallel_paper_trades",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("strategy_id", sa.String(48),
                  sa.ForeignKey("parallel_paper_cursors.strategy_id"), nullable=False),
        sa.Column("strategy_version", sa.String(64), nullable=False),
        sa.Column("decision_snapshot_id", sa.Integer(),
                  sa.ForeignKey("scalper_market_snapshots.id"), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("document", sa.Text(), nullable=False),
        sa.UniqueConstraint("strategy_id", "decision_snapshot_id", name="uq_parallel_decision"),
        sa.UniqueConstraint("strategy_id", "id", name="uq_parallel_trade_owner"),
        sa.CheckConstraint("state IN ('PENDING_PAPER_ENTRY','OPEN','CLOSED','ENTRY_REJECTED',"
                           "'UNRESOLVED_EXPOSURE')", name="ck_parallel_trade_state"))
    op.create_index("ix_parallel_strategy_state", "parallel_paper_trades", ["strategy_id", "state"])
    op.create_table(
        "parallel_paper_events",
        sa.Column("strategy_id", sa.String(48),
                  sa.ForeignKey("parallel_paper_cursors.strategy_id"), primary_key=True),
        sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("event_key", sa.String(220), nullable=False),
        sa.Column("event_type", sa.String(48), nullable=False),
        sa.Column("snapshot_id", sa.Integer(),
                  sa.ForeignKey("scalper_market_snapshots.id"), nullable=False),
        sa.Column("trade_id", sa.String(64)),
        sa.Column("previous_hash", sa.String(64), nullable=False),
        sa.Column("event_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["strategy_id", "trade_id"],
                                ["parallel_paper_trades.strategy_id", "parallel_paper_trades.id"]),
        sa.UniqueConstraint("strategy_id", "event_key", name="uq_parallel_event"))
    dialect = op.get_context().dialect.name
    if dialect == "postgresql":
        op.execute("CREATE FUNCTION reject_parallel_paper_event_mutation() RETURNS trigger "
                   "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION "
                   "'PARALLEL_PAPER_EVIDENCE_IS_IMMUTABLE'; END; $$")
        op.execute("CREATE TRIGGER parallel_paper_events_immutable BEFORE UPDATE OR DELETE OR "
                   "TRUNCATE ON parallel_paper_events FOR EACH STATEMENT EXECUTE FUNCTION "
                   "reject_parallel_paper_event_mutation()")
    elif dialect == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(f"CREATE TRIGGER parallel_paper_events_no_{action.lower()} BEFORE {action} "
                       "ON parallel_paper_events BEGIN SELECT "
                       "RAISE(ABORT, 'PARALLEL_PAPER_EVIDENCE_IS_IMMUTABLE'); END")
    else:
        raise RuntimeError("Parallel paper supports PostgreSQL and SQLite only")


def downgrade():
    op.drop_table("parallel_paper_events")
    if op.get_context().dialect.name == "postgresql":
        op.execute("DROP FUNCTION reject_parallel_paper_event_mutation()")
    op.drop_index("ix_parallel_strategy_state", "parallel_paper_trades")
    op.drop_table("parallel_paper_trades")
    op.drop_table("parallel_paper_cursors")
