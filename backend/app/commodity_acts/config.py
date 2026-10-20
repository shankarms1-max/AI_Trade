"""Commodity aliases/configuration are distinct from resolved broker identities."""
from datetime import date, datetime, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.alpha.clock import IST, SessionClock

InstrumentId = Literal["CRUDEOILM", "NATGASMINI"]
INSTRUMENTS = ("CRUDEOILM", "NATGASMINI")
VERSION = "COMMODITY_ACTS_PAPER_V1"


class MarketSession(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    start: time
    end: time
    forced_exit: time
    verified_source: str = Field(min_length=1)
    holidays: frozenset[date] = frozenset()

    @model_validator(mode="after")
    def ordered(self):
        if not self.start < self.forced_exit <= self.end or not self.verified_source.strip():
            raise ValueError("COMMODITY_SESSION_SETTINGS_INVALID")
        if any(value.tzinfo is not None for value in (self.start, self.end, self.forced_exit)):
            raise ValueError("COMMODITY_SESSION_CLOCK_MUST_BE_IST_WALL_TIME")
        return self

    def clock(self, instrument: str) -> SessionClock:
        return SessionClock(f"MCX_{instrument}", self.start, self.end)

    def contains(self, at: datetime) -> bool:
        if at.tzinfo is None:
            raise ValueError("COMMODITY_AWARE_CLOCK_REQUIRED")
        local = at.astimezone(IST)
        return local.weekday() < 5 and local.date() not in self.holidays and self.start <= local.time().replace(tzinfo=None) <= self.end


class InstrumentAlias(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    instrument_id: InstrumentId
    underlying: str = Field(min_length=1)

    @model_validator(mode="after")
    def exact_supported_family(self):
        if self.underlying.strip().upper() != self.instrument_id:
            raise ValueError("COMMODITY_INSTRUMENT_ALIAS_MISMATCH")
        return self


class InstrumentSpec(InstrumentAlias):
    display_name: str
    exchange: Literal["MCX"] = "MCX"
    instrument_family: Literal["COMMODITY_MINI"] = "COMMODITY_MINI"
    market_session: MarketSession
    future_required: Literal[True] = True
    options_required: Literal[True] = True


class CommoditySettings(BaseSettings):
    """Separate opt-in settings; no change to existing NIFTY Settings defaults."""
    model_config = SettingsConfigDict(env_prefix="COMMODITY_ACTS_", env_file=".env", extra="ignore", hide_input_in_errors=True)
    paper_enabled: bool = False
    crudeoilm_enabled: bool = False
    natgasmini_enabled: bool = False
    crudeoilm_underlying: str = "CRUDEOILM"
    natgasmini_underlying: str = "NATGASMINI"
    crudeoilm_session_start: time | None = None
    crudeoilm_session_end: time | None = None
    crudeoilm_forced_exit: time | None = None
    crudeoilm_session_source: str = ""
    natgasmini_session_start: time | None = None
    natgasmini_session_end: time | None = None
    natgasmini_forced_exit: time | None = None
    natgasmini_session_source: str = ""
    holidays: str = ""
    strikes_each_side: int = Field(default=10, ge=5, le=10)
    max_quote_age_seconds: int = Field(default=30, ge=1, le=600)
    minimum_option_pairs: int = Field(default=3, ge=1, le=21)
    history_limit: int = Field(default=5000, ge=100, le=10000)

    def enabled(self, instrument: InstrumentId) -> bool:
        return self.paper_enabled and getattr(self, f"{instrument.lower()}_enabled")

    def specification(self, instrument: InstrumentId) -> InstrumentSpec:
        if instrument not in INSTRUMENTS:
            raise ValueError("COMMODITY_INSTRUMENT_UNSUPPORTED")
        prefix = instrument.lower()
        values = [getattr(self, f"{prefix}_{name}") for name in ("session_start", "session_end", "forced_exit")]
        if any(value is None for value in values) or not getattr(self, f"{prefix}_session_source").strip():
            raise ValueError("COMMODITY_VERIFIED_SESSION_SETTINGS_REQUIRED")
        session = MarketSession(start=values[0], end=values[1], forced_exit=values[2],
            verified_source=getattr(self, f"{prefix}_session_source"),
            holidays=frozenset(date.fromisoformat(item.strip()) for item in self.holidays.split(",") if item.strip()))
        return InstrumentSpec(instrument_id=instrument, display_name=instrument,
                              underlying=getattr(self, f"{prefix}_underlying"), market_session=session)
