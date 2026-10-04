from typing import Any, Protocol


class KotakSDKClient(Protocol):
    """Only the SDK calls allowed in this research-only adapter."""

    def quotes(
        self,
        instrument_tokens: list[dict[str, str]] | None = None,
        quote_type: str | None = None,
    ) -> Any: ...

    def expiries(
        self,
        exchange: str,
        underlying: str,
        instrument_type: str | None = None,
    ) -> Any: ...

    def option_chain(
        self,
        exchange: str,
        underlying: str,
        expiry: str | None = None,
        instrument_type: str | None = None,
        count: int | None = None,
    ) -> Any: ...

    def search_scrip(
        self,
        exchange_segment: str = "",
        symbol: str = "",
        expiry: str | None = None,
        option_type: str | None = None,
        strike_price: str | None = None,
        ignore_50multiple: bool = True,
    ) -> Any: ...

