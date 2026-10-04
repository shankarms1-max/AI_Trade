from abc import ABC, abstractmethod
from datetime import date

from app.data.models import OptionContractSnapshot


class MarketDataError(RuntimeError):
    """Raised when required broker market data is unavailable or invalid."""


class BrokerSessionError(MarketDataError):
    """Raised when broker credentials or session are invalid."""


class MarketDataBroker(ABC):
    """Read-only broker contract. Order operations deliberately do not exist."""

    @abstractmethod
    def get_nifty_spot(self) -> float:
        raise NotImplementedError

    @abstractmethod
    def get_nifty_future(self) -> float | None:
        raise NotImplementedError

    @abstractmethod
    def get_india_vix(self) -> float | None:
        raise NotImplementedError

    @abstractmethod
    def get_nifty_expiries(self) -> list[date]:
        raise NotImplementedError

    @abstractmethod
    def get_nifty_option_chain(self, expiry: date) -> list[OptionContractSnapshot]:
        raise NotImplementedError

    def get_nifty_lot_size(self) -> int | None:
        """Return broker-confirmed option lot size when available."""
        return None
