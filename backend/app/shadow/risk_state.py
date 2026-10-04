from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import ShadowTradeRecord
from app.risk.exposure import RiskState


class ShadowRiskStateProvider:
    """Authoritative only for the persisted shadow-trading universe."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    def get_trades_today(self, trading_date: date) -> int:
        with self._sessions() as session:
            return session.scalar(select(func.count(ShadowTradeRecord.id)).where(
                func.date(ShadowTradeRecord.entry_timestamp) == trading_date
            )) or 0

    def get_realized_pnl_today(self, trading_date: date) -> tuple[float, bool]:
        with self._sessions() as session:
            records = session.scalars(select(ShadowTradeRecord).where(
                ShadowTradeRecord.status == "CLOSED",
                func.date(ShadowTradeRecord.exit_timestamp) == trading_date,
            )).all()
            complete = all(item.realized_pnl_per_lot is not None for item in records)
            value = sum(float(item.realized_pnl_per_lot) for item in records
                        if item.realized_pnl_per_lot is not None)
            return value, complete

    def get_open_strategy_fingerprints(self) -> frozenset[str]:
        with self._sessions() as session:
            return frozenset(session.scalars(select(ShadowTradeRecord.candidate_fingerprint).where(
                ShadowTradeRecord.status == "OPEN"
            )))

    def is_authoritative(self, context: str = "SHADOW") -> bool:
        return str(getattr(context, "value", context)) == "SHADOW"

    def get_state(self, trading_date: date) -> RiskState:
        pnl, complete = self.get_realized_pnl_today(trading_date)
        return RiskState(
            trades_today=self.get_trades_today(trading_date),
            realized_pnl_today=pnl,
            open_strategy_keys=self.get_open_strategy_fingerprints(),
            authoritative_for_live=False,
            authoritative_for_shadow=True,
            provider_kind="SHADOW",
            monetary_pnl_complete=complete,
        )
