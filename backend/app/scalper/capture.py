"""Broker market-data adaptation shared by live and research capture."""
from app.data.models import MarketSnapshot
from app.scalper.models import ScalperMarketSnapshot, ScalperOptionQuote


def to_scalper_snapshot(snapshot: MarketSnapshot) -> ScalperMarketSnapshot:
    """Strictly adapt read-only broker data; incomplete identities fail capture."""
    if snapshot.lot_size is None:
        raise ValueError("SCALPER_CONFIRMED_LOT_SIZE_REQUIRED")
    if any(not item.instrument_token for item in snapshot.options):
        raise ValueError("SCALPER_INCOMPLETE_QUOTE_IDENTITY")
    captured = snapshot.response_received_at or snapshot.timestamp_ist
    started = snapshot.request_started_at or snapshot.timestamp_ist
    quotes = [ScalperOptionQuote(
        expiry=item.expiry, strike=item.strike,
        option_type=item.option_type.value, exchange=item.exchange,
        trading_symbol=item.trading_symbol,
        instrument_token=item.instrument_token or "",
        source_market_timestamp=item.source_market_timestamp,
        bid=item.bid, ask=item.ask, bid_quantity=item.bid_quantity,
        ask_quantity=item.ask_quantity, depth_unit=item.depth_unit,
        tick_size=item.tick_size, ltp=item.ltp, volume=item.volume,
        open_interest=item.open_interest)
        for item in snapshot.options]
    source_times = [item.source_market_timestamp for item in quotes
                    if item.source_market_timestamp is not None]
    return ScalperMarketSnapshot(
        captured_at=captured, request_started_at=started,
        response_received_at=captured,
        source_market_timestamp=max(source_times) if source_times else None,
        nifty_spot=snapshot.nifty_spot, nifty_future=snapshot.nifty_future,
        future_instrument_id=snapshot.future_instrument_id,
        future_expiry=snapshot.future_expiry, india_vix=snapshot.india_vix,
        lot_size=snapshot.lot_size, atm_strike=snapshot.atm_strike,
        expiry=snapshot.expiry, source=snapshot.source, quotes=quotes)
