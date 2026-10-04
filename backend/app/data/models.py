from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class OptionType(str, Enum):
    CE = "CE"
    PE = "PE"


class OptionContractSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strike: float = Field(gt=0)
    option_type: OptionType
    expiry: date
    trading_symbol: str
    instrument_token: str | None = None
    exchange: str = "nse_fo"
    source_market_timestamp: datetime | None = None
    bid_quantity: int | None = None
    ask_quantity: int | None = None
    ltp: float | None = None
    open_interest: int | None = None
    previous_open_interest: int | None = None
    change_in_open_interest: int | None = None
    volume: int | None = None
    implied_volatility: float | None = None
    bid: float | None = None
    ask: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None

class MarketSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp_ist: datetime
    nifty_spot: float = Field(gt=0)
    nifty_future: float | None = Field(default=None, gt=0)
    future_instrument_id: str | None = None
    future_expiry: date | None = None
    source_market_timestamp: datetime | None = None
    request_started_at: datetime | None = None
    response_received_at: datetime | None = None
    snapshot_persisted_at: datetime | None = None
    india_vix: float | None = Field(default=None, ge=0)
    lot_size: int | None = Field(default=None, gt=0)
    atm_strike: float = Field(gt=0)
    expiry: date
    source: str = "KOTAK_NEO"
    options: list[OptionContractSnapshot]

    @model_validator(mode="after")
    def option_chain_must_be_usable(self) -> "MarketSnapshot":
        if not self.options:
            raise ValueError("option chain is empty")
        if all(contract.ltp is None for contract in self.options):
            raise ValueError("all option LTP values are missing")
        if any(contract.expiry != self.expiry for contract in self.options):
            raise ValueError("option expiry does not match snapshot expiry")
        return self

