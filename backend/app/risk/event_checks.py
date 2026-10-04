import json
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, model_validator


class MarketEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    start_time: datetime
    end_time: datetime
    severity: str
    block_entries: bool

    @model_validator(mode="after")
    def valid_window(self) -> "MarketEvent":
        if self.start_time.tzinfo is None or self.end_time.tzinfo is None:
            raise ValueError("market-event timestamps must be timezone-aware")
        if self.end_time < self.start_time:
            raise ValueError("market-event end_time precedes start_time")
        return self


class MarketEventProvider(Protocol):
    def events_at(self, timestamp: datetime) -> list[MarketEvent]: ...


class ConfiguredMarketEventProvider:
    def __init__(self, events: list[MarketEvent] | None = None) -> None:
        self._events = events or []

    @classmethod
    def from_json(cls, value: str) -> "ConfiguredMarketEventProvider":
        if not value.strip():
            return cls()
        raw = json.loads(value)
        if not isinstance(raw, list):
            raise ValueError("RISK_MARKET_EVENTS_JSON must be a JSON list")
        return cls([MarketEvent.model_validate(item) for item in raw])

    def events_at(self, timestamp: datetime) -> list[MarketEvent]:
        return [item for item in self._events if item.start_time <= timestamp <= item.end_time]
