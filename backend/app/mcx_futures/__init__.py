"""ACTS_V3_MCX_FUTURES: instrument-aware directional OUTRIGHT futures PAPER strategy.

Instruments: MCX CRUDEOILM (crude oil mini) and NATGASMINI (natural gas mini).
Paper only: nothing in this package talks to a broker or the network.
Status: PRE_REGISTERED_UNVALIDATED - no historical MCX data was available, so
no performance claim exists.  See docs/acts-v3-mcx-futures.md.
"""

from app.mcx_futures.bars import Observation
from app.mcx_futures.config import CONFIGS, CRUDEOILM, NATGASMINI, STRATEGY_ID, InstrumentConfig
from app.mcx_futures.costs import DEFAULT_SCHEDULE, CostSchedule
from app.mcx_futures.engine import EVENT_TYPES, McxFuturesEngine
from app.mcx_futures.session import SessionCalendar
from app.mcx_futures.spec import ContractSpec, resolve_futures, round_to_tick

__all__ = [
    "CONFIGS", "CRUDEOILM", "DEFAULT_SCHEDULE", "EVENT_TYPES", "NATGASMINI", "STRATEGY_ID", "ContractSpec",
    "CostSchedule", "InstrumentConfig", "McxFuturesEngine", "Observation", "SessionCalendar", "resolve_futures",
    "round_to_tick",
]
