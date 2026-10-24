"""Reuse V1 capture/Alpha intact; recover the separate V2 paper stream after gaps."""

from datetime import datetime
from contextvars import ContextVar

from sqlalchemy import inspect, select

from app.commodity_acts.broker import assert_market_only
from app.commodity_acts.models import CommodityActsDecision, CommodityMarketSnapshotRecord
from app.commodity_acts.paper_engine import CommodityPaperEngine
from app.commodity_acts.paper_models import CommodityPaperCursor, CommodityPaperPosition
from app.commodity_acts.paper_policy import ACTIVE
from app.commodity_acts.service import CommodityResearch
from app.research.manifest import digest
import json


class CommodityPaperCoordinator(CommodityResearch):
    def __init__(self, *args, margin_reader=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._observation_history = ContextVar("commodity_observation_history", default=None)
        with self.sessions() as session:
            present = inspect(session.get_bind()).has_table("commodity_paper_cursors")
        if not present and self.config.trading_enabled:
            raise RuntimeError("COMMODITY_PAPER_MIGRATION_REQUIRED")
        self.paper = CommodityPaperEngine(self, margin_reader=margin_reader) if present else None

    def _history(self, current):
        shared = self._observation_history.get()
        if shared is None:
            return super()._history(current)
        key = digest(current)
        if key not in shared:
            shared[key] = super()._history(current)
        return shared[key]

    def process_snapshot(self, snapshot_id):
        # One causal history view per observation, reset on success or failure.
        token = self._observation_history.set({})
        try:
            result = super().process_snapshot(snapshot_id)
            if self.paper:
                self.paper.process_snapshot(snapshot_id)
            return result
        finally:
            self._observation_history.reset(token)

    def capture_once(self, market, at):
        assert_market_only(market)
        if self.paper:
            with self.sessions() as session:
                cursor = session.get(CommodityPaperCursor, self.spec.instrument_id)
                pending = (
                    list(
                        session.scalars(
                            select(CommodityActsDecision.snapshot_id)
                            .join(
                                CommodityMarketSnapshotRecord,
                                CommodityActsDecision.snapshot_id == CommodityMarketSnapshotRecord.id,
                            )
                            .where(
                                CommodityActsDecision.instrument_id == self.spec.instrument_id,
                                CommodityMarketSnapshotRecord.captured_at > datetime.fromisoformat(cursor.observed_at),
                            )
                            .order_by(CommodityMarketSnapshotRecord.captured_at)
                            .limit(20)
                        )
                    )
                    if cursor and cursor.observed_at
                    else list(
                        session.scalars(
                            select(CommodityActsDecision.snapshot_id)
                            .join(
                                CommodityMarketSnapshotRecord,
                                CommodityActsDecision.snapshot_id == CommodityMarketSnapshotRecord.id,
                            )
                            .where(CommodityActsDecision.instrument_id == self.spec.instrument_id)
                            .order_by(CommodityMarketSnapshotRecord.captured_at.desc())
                            .limit(1)
                        )
                    )
                )
            for snapshot_id in pending:
                self.paper.process_snapshot(snapshot_id)
            if len(pending) == 20:
                return None
            with self.sessions() as session:
                held = [
                    json.loads(row.document)["candidate"]
                    for row in session.scalars(
                        select(CommodityPaperPosition).where(
                            CommodityPaperPosition.instrument_id == self.spec.instrument_id,
                            CommodityPaperPosition.state.in_(ACTIVE),
                        )
                    )
                ]
            required = [item[leg] for item in held for leg in ("short_leg", "long_leg") if item.get(leg)]
            if required:

                class WithHeldQuotes:
                    def capture(_, spec):
                        return market.capture(spec, required_options=required)

                return super().capture_once(WithHeldQuotes(), at)
        return super().capture_once(market, at)
