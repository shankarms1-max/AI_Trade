"""Instrument-aware observations; no NIFTY fields, spot/VIX proxies, or fills."""
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.commodity_acts.config import InstrumentId
from app.data.models import OptionContractSnapshot


class CommodityOption(OptionContractSnapshot):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    exchange: Literal["mcx_fo"] = "mcx_fo"
    instrument_token: str = Field(min_length=1)
    lot_size: int = Field(gt=0)
    depth_unit: Literal["UNKNOWN"] = "UNKNOWN"
    raw_master_strike: float
    master_strike_divisor: Literal[100] = 100
    raw_master_tick_size: float | None = None
    raw_source_timestamp: str | int | None = None

    @property
    def identity(self):
        return (self.exchange, self.instrument_token, self.expiry, self.strike, self.option_type.value, self.trading_symbol)


class CommoditySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    instrument_id: InstrumentId
    session_id: str
    timestamp_ist: datetime
    request_started_at: datetime
    response_received_at: datetime
    snapshot_persisted_at: datetime | None = None
    reference_price: float = Field(gt=0)
    reference_basis: Literal["SELECTED_MCX_FUTURE"] = "SELECTED_MCX_FUTURE"
    future_price: float = Field(gt=0)
    future_exchange: Literal["mcx_fo"] = "mcx_fo"
    future_instrument_id: str = Field(min_length=1)
    future_token: str = Field(min_length=1)
    future_expiry: date
    future_lot_size: int = Field(gt=0)
    source_market_timestamp: datetime | None = None
    raw_future_source_timestamp: str | int | None = None
    expiry: date
    lot_size: int = Field(gt=0)
    atm_strike: float = Field(gt=0)
    source: Literal["KOTAK_NEO_MCX"] = "KOTAK_NEO_MCX"
    options: list[CommodityOption]
    provenance: dict[str, str]

    @field_validator("timestamp_ist", "request_started_at", "response_received_at", "snapshot_persisted_at", "source_market_timestamp")
    @classmethod
    def aware(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError("COMMODITY_AWARE_TIMESTAMP_REQUIRED")
        if value is None:
            return None
        from app.alpha.clock import IST
        return value.astimezone(IST)

    @model_validator(mode="after")
    def usable(self):
        if not self.request_started_at <= self.response_received_at <= self.timestamp_ist:
            raise ValueError("COMMODITY_CAPTURE_TIME_INVALID")
        if self.reference_price != self.future_price:
            raise ValueError("COMMODITY_REFERENCE_MUST_BE_SELECTED_FUTURE")
        if self.session_id != f"MCX_{self.instrument_id}_{self.timestamp_ist.date().isoformat()}":
            raise ValueError("COMMODITY_SESSION_IDENTITY_MISMATCH")
        if not self.options:
            raise ValueError("OPTION_CHAIN_UNAVAILABLE")
        identities = [quote.identity for quote in self.options]
        contracts = [(quote.expiry, quote.strike, quote.option_type) for quote in self.options]
        tokens = [(quote.exchange, quote.instrument_token) for quote in self.options]
        if any(len(values) != len(set(values)) for values in (identities, contracts, tokens)):
            raise ValueError("COMMODITY_DUPLICATE_QUOTE_IDENTITY")
        if self.future_expiry < self.timestamp_ist.date() or self.expiry < self.timestamp_ist.date():
            raise ValueError("COMMODITY_EXPIRED_INSTRUMENT")
        for quote in self.options:
            if quote.expiry != self.expiry or quote.lot_size != self.lot_size:
                raise ValueError("COMMODITY_OPTION_IDENTITY_MISMATCH")
            source = quote.source_market_timestamp
            if source is not None and (source.tzinfo is None or source > self.response_received_at):
                raise ValueError("COMMODITY_FUTURE_QUOTE_TIMESTAMP")
        if self.source_market_timestamp is not None and self.source_market_timestamp > self.response_received_at:
            raise ValueError("COMMODITY_FUTURE_QUOTE_TIMESTAMP")
        return self
