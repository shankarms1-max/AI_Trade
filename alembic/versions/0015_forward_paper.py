"""Add isolated forward-paper state and immutable evidence. No historical edits."""
from alembic import op
import sqlalchemy as sa

revision = "0015_forward_paper"
down_revision = "0014_phase14_2_1_replay_integrity"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("paper_cursor",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("snapshot_id", sa.Integer()), sa.Column("observed_at", sa.String(64)),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("head_hash", sa.String(64), nullable=False),
        sa.Column("lock_version", sa.Integer(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_paper_single_cursor"))
    op.execute("INSERT INTO paper_cursor (id, sequence, head_hash, lock_version) VALUES (1, 0, 'GENESIS', 0)")
    op.create_table("paper_trades",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("decision_snapshot_id", sa.Integer(), sa.ForeignKey("market_snapshots.id"), unique=True, nullable=False),
        sa.Column("state", sa.String(32), nullable=False), sa.Column("document", sa.Text(), nullable=False),
        sa.CheckConstraint("state IN ('PENDING_PAPER_ENTRY','OPEN','CLOSED','ENTRY_REJECTED','UNRESOLVED_EXPOSURE')",
                           name="ck_paper_trade_state"))
    op.create_index("ix_paper_trades_state", "paper_trades", ["state"])
    op.create_table("paper_events",
        sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("event_key", sa.String(180), unique=True, nullable=False),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("snapshot_id", sa.Integer(), sa.ForeignKey("market_snapshots.id"), nullable=False),
        sa.Column("trade_id", sa.String(64), sa.ForeignKey("paper_trades.id")),
        sa.Column("previous_hash", sa.String(64), nullable=False),
        sa.Column("event_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False))
    dialect = op.get_context().dialect.name
    if dialect == "postgresql":
        op.execute("CREATE FUNCTION reject_paper_evidence_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'PAPER_EVIDENCE_IS_IMMUTABLE'; END; $$")
        op.execute("CREATE TRIGGER paper_evidence_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON paper_events FOR EACH STATEMENT EXECUTE FUNCTION reject_paper_evidence_mutation()")
    elif dialect == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(f"CREATE TRIGGER paper_evidence_no_{action.lower()} BEFORE {action} ON paper_events BEGIN SELECT RAISE(ABORT, 'PAPER_EVIDENCE_IS_IMMUTABLE'); END")
    else:
        raise RuntimeError("forward paper supports PostgreSQL and SQLite only")


def downgrade():
    # Explicit schema rollback destroys paper evidence; export it before downgrade.
    op.drop_table("paper_events")
    if op.get_context().dialect.name == "postgresql":
        op.execute("DROP FUNCTION reject_paper_evidence_mutation()")
    op.drop_index("ix_paper_trades_state", "paper_trades")
    op.drop_table("paper_trades")
    op.drop_table("paper_cursor")
