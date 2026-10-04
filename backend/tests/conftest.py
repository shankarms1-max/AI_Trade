from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.data.models import MarketSnapshot, OptionContractSnapshot
from app.db.base import Base
from app.db.repositories import SnapshotRepository

IST = ZoneInfo("Asia/Kolkata")
EXPIRY = date(2099, 10, 8)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    value = create_engine(f"sqlite+pysqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(value)
    try:
        yield value
    finally:
        value.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def repository(session_factory: sessionmaker[Session]) -> SnapshotRepository:
    return SnapshotRepository(session_factory)


@pytest.fixture
def market_snapshot() -> MarketSnapshot:
    options: list[OptionContractSnapshot] = []
    for index in range(21):
        strike = 24_500 + index * 50
        for option_type in ("CE", "PE"):
            options.append(
                OptionContractSnapshot(
                    strike=strike,
                    option_type=option_type,
                    expiry=EXPIRY,
                    trading_symbol=f"NIFTY99OCT{strike}{option_type}",
                    instrument_token=f"nse_fo|{strike}{option_type}",
                    ltp=100.125 + index,
                    open_interest=1000 + index,
                    previous_open_interest=900 + index,
                    change_in_open_interest=100,
                    volume=500 + index,
                )
            )
    return MarketSnapshot(
        timestamp_ist=datetime(2099, 10, 1, 10, 16, 25, tzinfo=IST),
        nifty_spot=25_012.55,
        nifty_future=25_040.25,
        india_vix=None,
        atm_strike=25_000,
        expiry=EXPIRY,
        options=options,
    )
