"""Read exact identities from active forward-paper evidence, without changing it."""
import json

from sqlalchemy import select

from app.data.models import OptionContractSnapshot
from app.paper.models import PaperTrade
from app.research.quotes import ContractIdentity
from app.strategy.models import CreditSpreadCandidate


def required_paper_contracts(sessions) -> list[OptionContractSnapshot]:
    required = {}
    with sessions() as session:
        rows = session.scalars(select(PaperTrade).where(
            PaperTrade.state.in_(("PENDING_PAPER_ENTRY", "OPEN"))
        ).order_by(PaperTrade.id))
        for row in rows:
            candidate = CreditSpreadCandidate.model_validate(json.loads(row.document)["candidate"])
            for leg in (candidate.short_leg, candidate.long_leg):
                identity = ContractIdentity.of(leg)
                if not identity.valid():
                    raise ValueError("PAPER_CONTINUITY_INVALID_IDENTITY")
                # Only identity travels from old evidence. All market fields start unknown.
                required[identity] = OptionContractSnapshot(
                    exchange=leg.exchange, instrument_token=leg.instrument_token,
                    expiry=leg.expiry, strike=leg.strike, option_type=leg.option_type,
                    trading_symbol=leg.trading_symbol)
    return sorted(required.values(), key=lambda item: (
        item.expiry, item.strike, item.option_type.value, item.exchange, item.instrument_token))
