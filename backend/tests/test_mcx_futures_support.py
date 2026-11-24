"""SYNTHETIC test-data helpers for ACTS_V3_MCX_FUTURES (no real market data anywhere)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
import json
from pathlib import Path
import random

import pandas as pd

from app.mcx_futures.bars import Observation
from app.mcx_futures.spec import IST, ContractSpec, resolve_futures

FIXTURES = Path(__file__).parent / "fixtures"


def master_rows(instrument: str) -> list[dict]:
    data = json.loads((FIXTURES / "mcx_futures_master_synthetic.json").read_text())
    assert data["label"].startswith("SYNTHETIC")
    return data[instrument]


def spec_for(instrument: str, today: date = date(2026, 11, 24)) -> ContractSpec:
    return resolve_futures(master_rows(instrument), instrument, today, 3, master_date=date(2026, 11, 24))


def ist(y: int, mo: int, d: int, h: int, mi: int, s: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=IST)


def obs(at: datetime, ltp: float, half: float = 1.0, cum: float | None = None,
        age: float = 0.0, bid: float | None = None, ask: float | None = None) -> Observation:
    return Observation(at=at, ltp=ltp, bid=ltp - half if bid is None else bid, ask=ltp + half if ask is None else ask,
                       bid_qty=1000, ask_qty=1000, cum_volume=cum, source_ts=at - timedelta(seconds=age))


def trading_days(start: date, count: int) -> list[date]:
    days: list[date] = []
    day = start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def random_walk_bars(days: list[date], *, start_price: float, step: float, tick: float, seed: int,
                     open_time: time = time(9, 0), minutes: int = 870, drift: float = 0.0,
                     volume: float | None = 100.0) -> pd.DataFrame:
    """SYNTHETIC 1-minute OHLCV bars from a seeded Gaussian random walk (no edge by construction)."""
    rng = random.Random(seed)
    price = start_price
    rows = []
    for day in days:
        start = datetime.combine(day, open_time, IST)
        for i in range(minutes):
            o = price
            points = [o]
            for _ in range(4):
                price = max(tick * 10, price + rng.gauss(drift, step))
                points.append(price)
            c = price

            def snap(x: float) -> float:
                return round(round(x / tick) * tick, 6)
            rows.append({"ts": start + timedelta(minutes=i), "open": snap(o), "high": snap(max(points)),
                         "low": snap(min(points)), "close": snap(c),
                         "volume": volume if volume is not None else float("nan")})
            price = c
    return pd.DataFrame(rows)


def trend_bars(day: date, *, start_price: float, slope: float, tick: float, minutes: int = 600,
               open_time: time = time(9, 0), wiggle: float = 0.0, seed: int = 1) -> pd.DataFrame:
    """SYNTHETIC steady trend with optional noise."""
    rng = random.Random(seed)
    start = datetime.combine(day, open_time, IST)
    rows = []
    price = start_price
    for i in range(minutes):
        o = price
        c = o + slope + rng.uniform(-wiggle, wiggle)
        h, low = max(o, c) + abs(slope) * 0.2, min(o, c) - abs(slope) * 0.2

        def snap(x: float) -> float:
            return round(round(x / tick) * tick, 6)
        rows.append({"ts": start + timedelta(minutes=i), "open": snap(o), "high": snap(h), "low": snap(low),
                     "close": snap(c), "volume": 100.0})
        price = c
    return pd.DataFrame(rows)


def observations_from_bars(bars: pd.DataFrame, tick: float, half_ticks: float = 1.0) -> list[Observation]:
    """SYNTHETIC 15-second observation stream (O, L, H, C at +0/15/30/45 s) with cumulative volume."""
    out: list[Observation] = []
    cum: dict[date, float] = {}
    half = half_ticks * tick
    for row in bars.to_dict("records"):
        ts = row["ts"].to_pydatetime() if hasattr(row["ts"], "to_pydatetime") else row["ts"]
        before = cum.get(ts.date(), 0.0)
        cum[ts.date()] = before + row["volume"]
        for second, price, volume in ((0, row["open"], before), (15, row["low"], before),
                                      (30, row["high"], before), (45, row["close"], before + row["volume"])):
            at = ts + timedelta(seconds=second)
            out.append(Observation(at=at, ltp=price, bid=price - half, ask=price + half, cum_volume=volume,
                                   bid_qty=1000, ask_qty=1000, source_ts=at))
    return out


def synth_spec(instrument: str, expiry: date = date(2099, 12, 31)) -> ContractSpec:
    """SYNTHETIC far-dated contract so multi-week synthetic replays never hit the expiry window."""
    lot, tick, unit = {"CRUDEOILM": (10, 1.0, "BBL"), "NATGASMINI": (250, 0.1, "MMBTU")}[instrument]
    return ContractSpec(instrument=instrument, trading_symbol=f"{instrument}SYNTHETICFUT", token="SYNTHETIC",
                        segment="mcx_fo", expiry=expiry, lot_size=lot, tick_size=tick, precision=2,
                        quotation_unit=unit, rupees_per_point_per_lot=float(lot), source="SYNTHETIC_TEST_SPEC")


def test_synthetic_generators_are_deterministic() -> None:
    days = trading_days(date(2026, 11, 2), 2)
    first = random_walk_bars(days, start_price=5600, step=1.0, tick=1.0, seed=7, minutes=30)
    second = random_walk_bars(days, start_price=5600, step=1.0, tick=1.0, seed=7, minutes=30)
    assert first.equals(second)
    assert (first["high"] >= first[["open", "close"]].max(axis=1)).all()
    assert (first["low"] <= first[["open", "close"]].min(axis=1)).all()
