"""Native scalar captures for AST. Kotak supplies NIFTY futures LTP in its chain header."""

from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.adaptive_supertrend import CAPTURE_MODE, VERSION
from app.broker.kotak.auth import create_market_data_client
from app.broker.kotak.instruments import NIFTY_UNDERLYING, NSE_FO
from app.broker.kotak.market_data import KotakMarketDataAdapter
from app.data.snapshot import calculate_atm_strike
from app.research.manifest import canonical, digest
from app.scalper.models import ScalperMarketSnapshotRecord as Snapshot
from app.scalper.research_capture import IST, capture_allowed, capture_bucket


def _positive(value):
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() and number > 0 else None


def make_reader(settings):
    client = create_market_data_client(settings)
    market = KotakMarketDataAdapter(client)

    def read():
        start = datetime.now(IST)
        spot = market.get_nifty_spot()
        expiry = min(market.get_nifty_expiries())
        response = client.option_chain(
            exchange=NSE_FO, underlying=NIFTY_UNDERLYING, expiry=expiry.isoformat(), instrument_type="option", count=10
        )
        at = datetime.now(IST)
        # Use only the reference header. Individual call/put rows and quotes are unnecessary.
        if not isinstance(response, dict) or str(response.get("stat", "")).lower() in {"error", "not_ok", "not ok"}:
            raise ValueError("AST_BROKER_REFERENCE_UNAVAILABLE")
        if (
            response.get("error")
            or response.get("Error")
            or response.get("Error Message")
            or int(response.get("stCode") or 0) >= 400
        ):
            raise ValueError("AST_BROKER_REFERENCE_UNAVAILABLE")
        # Both wrapped and top-level headers occur in the existing Kotak adapter.
        payload = response["data"] if isinstance(response.get("data"), dict) else response
        future = payload["future"] if isinstance(payload.get("future"), dict) else {}
        common = payload["common_data"] if isinstance(payload.get("common_data"), dict) else {}
        lot = _positive(common.get("mktLot"))
        if lot is None or lot != lot.to_integral_value():
            raise ValueError("AST_SHARED_SNAPSHOT_METADATA_UNAVAILABLE")
        try:
            future_expiry = date.fromisoformat(str(future.get("expiry")))
        except ValueError:
            future_expiry = None
        symbol = future.get("symbol")
        return dict(
            captured_at=at,
            request_started_at=start,
            response_received_at=at,
            nifty_future=_positive(future.get("ltp")),
            future_expiry=future_expiry,
            future_instrument_id=symbol.strip() if isinstance(symbol, str) else None,
            nifty_spot=spot,
            atm_strike=calculate_atm_strike(spot, settings.kotak_nifty_strike_step),
            expiry=expiry,
            lot_size=int(lot),
            source="KOTAK_NEO",
        )

    return read


def save_capture(sessions, fields: dict) -> int:
    bucket = capture_bucket(fields["captured_at"]).isoformat()
    key = digest({"mode": CAPTURE_MODE, "bucket": bucket})
    try:
        with sessions.begin() as session:
            row = Snapshot(
                **fields,
                capture_key=key,
                feature_json="{}",
                signal_json="{}",
                context_json=canonical(
                    {"mode": CAPTURE_MODE, "producer": VERSION, "bucket": bucket, "status": "CAPTURED"}
                )
            )
            session.add(row)
            session.flush()
            return row.id
    except IntegrityError:
        with sessions() as session:
            existing = session.scalar(select(Snapshot.id).where(Snapshot.capture_key == key))
            if existing is None:
                raise
            return existing


def capture_once(sessions, reader, now, holidays):
    if capture_allowed(now, time(9, 15), time(15, 30), holidays):
        return save_capture(sessions, reader())
    return None
