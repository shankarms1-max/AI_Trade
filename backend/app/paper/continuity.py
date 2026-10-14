"""Read exact identities from active forward-paper evidence, without changing it."""
import json

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.broker.kotak.depth import quote_identity
from app.data.models import OptionContractSnapshot, OptionType
from app.paper.models import PaperTrade
from app.research.quotes import ContractIdentity
from app.strategy.models import CreditSpreadCandidate


def required_paper_contracts(sessions: sessionmaker[Session]) -> list[OptionContractSnapshot]:
    required: dict[ContractIdentity, OptionContractSnapshot] = {}
    by_token: dict[tuple[str, str], ContractIdentity] = {}
    with sessions() as session:
        rows = session.scalars(select(PaperTrade).where(
            PaperTrade.state.in_(("PENDING_PAPER_ENTRY", "OPEN"))
        ).order_by(PaperTrade.id))
        for row in rows:
            candidate = CreditSpreadCandidate.model_validate(json.loads(row.document)["candidate"])
            for leg in (candidate.short_leg, candidate.long_leg):
                # Only identity travels from old evidence. All market fields start unknown.
                contract = OptionContractSnapshot(
                    exchange=leg.exchange, instrument_token=leg.instrument_token,
                    expiry=leg.expiry, strike=leg.strike, option_type=OptionType(leg.option_type),
                    trading_symbol=leg.trading_symbol)
                identity = ContractIdentity.of(contract)
                token = quote_identity(contract)
                if not identity.valid() or token is None:
                    raise ValueError("PAPER_CONTINUITY_INVALID_IDENTITY")
                previous = by_token.get(token)
                if previous is not None and (previous != identity
                                             or required[previous].trading_symbol != leg.trading_symbol):
                    raise ValueError("PAPER_CONTINUITY_AMBIGUOUS_IDENTITY")
                by_token[token] = identity
                required[identity] = contract
    return sorted(required.values(), key=lambda item: (
        item.expiry, item.strike, item.option_type.value, item.exchange, item.instrument_token))
