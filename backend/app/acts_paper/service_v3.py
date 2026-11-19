"""Opt-in V3 lifecycle; shared V2 signal, candidate, execution and mirror exits."""

from __future__ import annotations

from datetime import datetime
import json
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.acts_paper import acts_v2, acts_v3
from app.acts_paper.models import ActsPaperTrade
from app.acts_paper.service import ACTIVE, ActsPaperService
from app.parallel_paper.report import statistics
from app.research.manifest import canonical, digest, json_value


class ActsV3PaperService(ActsPaperService):
    def __init__(self, sessions: sessionmaker[Session], settings: Any) -> None:
        with sessions() as session:
            self._refuse_prior_versions(
                session.scalars(select(ActsPaperTrade).where(ActsPaperTrade.state.in_(ACTIVE))).all()
            )
        self._v3_initializing = True
        super().__init__(sessions, settings.model_copy(update={"acts_paper_strategy_version": acts_v2.STRATEGY_ID}))
        self.settings = settings
        self.strategy_id = acts_v3.STRATEGY_ID
        assert self.policy is not None
        self.policy = json_value(
            {
                **self.policy,
                "strategy_version": self.strategy_id,
                "underlying_signal_version": acts_v2.STRATEGY_ID,
                "opportunity_cycle_policy": acts_v3.PARAMETERS,
            }
        )
        self.policy_hash = digest(self.policy)
        self._v3_initializing = False
        with sessions() as session:
            self._assert_entry_policy_upgrade_safe(
                session.scalars(select(ActsPaperTrade).where(ActsPaperTrade.state.in_(ACTIVE))).all()
            )

    @staticmethod
    def _refuse_prior_versions(trades: Iterable[ActsPaperTrade]) -> None:
        for trade in trades:
            if trade.state in ACTIVE and json.loads(trade.document).get("strategy_version") != acts_v3.STRATEGY_ID:
                raise RuntimeError("ACTS_V3_STARTUP_REQUIRES_ZERO_PRIOR_VERSION_EXPOSURE")

    def _assert_entry_policy_upgrade_safe(self, trades: Iterable[ActsPaperTrade], at: datetime | None = None) -> None:
        rows = list(trades)
        self._refuse_prior_versions(rows)
        if self._v3_initializing:
            return
        for trade in rows:
            if trade.state in ACTIVE and json.loads(trade.document).get("policy_hash") != self.policy_hash:
                raise RuntimeError("ACTS_V3_POLICY_UPGRADE_REQUIRES_ZERO_ACTIVE_TRADES")

    def _uses_v2_execution(self, strategy: str | None) -> bool:
        return strategy in {acts_v2.STRATEGY_ID, acts_v3.STRATEGY_ID}

    def _construction_strategy(self) -> str:
        # Explicitly use V2's slow positioning/short-first ordering, not a new
        # strategy-ID fallback in the shared lab candidate builder.
        return acts_v2.STRATEGY_ID

    def _entry_market_state(self, ctx: dict, watch: dict) -> tuple:
        assert self.v2_config is not None
        acts_v3.state(
            watch,
            datetime.fromisoformat(ctx["timestamp"]),
            (ctx.get("future_instrument_id"), str(ctx.get("future_expiry")), str(ctx.get("expiry_date"))),
        )
        return acts_v2.market_state(ctx, watch.setdefault("signal_watch", {}), self.v2_config)

    def _entry_decision(self, ctx: dict, watch: dict, candidate: Any, rejected: dict, reasons: list[str]) -> dict:
        assert self.v2_config is not None
        return acts_v3.decide(ctx, watch, candidate, rejected, reasons, self.v2_config)

    def _entry_audit(self, decision: dict) -> dict:
        return {
            **{key: decision.get(key) for key in acts_v3.AUDIT_FIELDS},
            "opportunity_cycle": decision.get("opportunity_cycle"),
        }

    def _cycle_transition(self, session, cursor, snapshot_id, raw, trade, before) -> None:
        watch = json.loads(cursor.watch_json)
        doc = json.loads(trade.document)
        if doc.get("cycle_transition_snapshot_id") == snapshot_id and doc.get("cycle_transition_state") == trade.state:
            return
        if trade.state == "OPEN" and before == "PENDING_PAPER_ENTRY":
            evidence = acts_v3.filled(watch, raw.captured_at, doc["direction"])
        elif trade.state == "CLOSED" and before in ("OPEN", "UNRESOLVED_EXPOSURE"):
            evidence = acts_v3.closed(watch, raw.captured_at, doc["direction"], doc.get("exit_reason", "UNKNOWN"))
        elif trade.state == "ENTRY_REJECTED" and before == "PENDING_PAPER_ENTRY":
            evidence = acts_v3.rejected(watch, raw.captured_at)
        else:
            return
        doc.setdefault("opportunity_cycle_entry", doc.get("opportunity_cycle"))
        doc.update(
            evidence,
            opportunity_cycle=evidence,
            cycle_transition_snapshot_id=snapshot_id,
            cycle_transition_state=trade.state,
        )
        self._save(trade, doc)
        cursor.watch_json = canonical(watch)
        self._emit(session, cursor, "ACTS_OPPORTUNITY_CYCLE", snapshot_id, raw, trade, opportunity_cycle=evidence)

    def _terminalize_expired(self, session, cursor, snapshot_id, raw, trade) -> bool:
        self._assert_entry_policy_upgrade_safe([trade], raw.captured_at)
        before = trade.state
        changed = super()._terminalize_expired(session, cursor, snapshot_id, raw, trade)
        if changed:
            self._cycle_transition(session, cursor, snapshot_id, raw, trade, before)
        return changed

    def _transition(self, session, cursor, snapshot_id, raw, ctx, trade, blocked) -> None:
        before = trade.state
        super()._transition(session, cursor, snapshot_id, raw, ctx, trade, blocked)
        self._cycle_transition(session, cursor, snapshot_id, raw, trade, before)

    def status(self, at: datetime) -> dict[str, Any]:
        result = super().status(at)
        result["opportunity_cycle"] = (result["watch_state"] or {}).get("opportunity_cycle")
        result["opportunity_cycle_policy_version"] = acts_v3.POLICY_VERSION
        result["today_acts_trades"] = [
            trade for trade in result["today_acts_trades"] if trade["strategy_version"] == acts_v3.STRATEGY_ID
        ]
        with self.sessions() as session:
            rows = [
                (trade.state, json.loads(trade.document))
                for trade in self._trades(session)
                if json.loads(trade.document).get("strategy_version") == acts_v3.STRATEGY_ID
            ]
        metrics = statistics(rows, at)
        realized = metrics["realized_pnl_today"]
        unrealized = metrics["unrealized_pnl"] if any(state == "OPEN" for state, _ in rows) else None
        result.update(
            realized_gross_pnl=realized,
            unrealized_gross_pnl=unrealized,
            total_gross_pnl=(
                realized + unrealized if unrealized is not None else None if metrics["open_position"] else realized
            ),
            open_position_count=metrics["open_position_count"],
            unresolved_exposure_count=metrics["unresolved_exposure_count"],
            expired_unpriced_count=metrics["expired_unpriced_count"],
            risk_budget_scope="ALL_ACTS_VERSIONS",
        )
        return result
