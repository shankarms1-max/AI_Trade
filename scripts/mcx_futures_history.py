"""MCX futures history collection + pre-registered research run for ACTS_V3_MCX_FUTURES (READ-ONLY market data).

No historical MCX data exists in the repository, so no performance claim can be made yet. This tool closes that gap
on a host that holds the Kotak consumer key (market-data read access only; no order method is reachable: the
client is wrapped in a three-method facade - search_scrip, quotes, historical_data - and checked by
assert_market_only).

    collect  python scripts/mcx_futures_history.py collect --instrument CRUDEOILM --days 120 --out data/mcx_history
             1-minute candles for every currently listed future of the instrument, in <=30-day windows (Kotak's
             documented limit for 1/3/5-minute candles), one request at a time with a pause; raw responses are
             hashed; candles are written per contract as CSV with a coverage report. Nothing is filled or adjusted.
    research python scripts/mcx_futures_history.py research --instrument CRUDEOILM --data data/mcx_history
             builds a TRADABLE (unadjusted) near-month series that rolls to the next contract before the expiry
             window (the same rule the live engine uses), then runs app.mcx_futures.research.run_research with the
             pre-declared grid. It prints INSUFFICIENT_DATA rather than numbers when coverage is too short.

Candle row format (Kotak Neo, as handled on the AST history adapter): data.candles = [ts, o, h, l, c, volume, oi].
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import sys
import time
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

IST = ZoneInfo("Asia/Kolkata")
SEGMENT = "mcx_fo"


class HistoryOnlySDK:
    """search_scrip + quotes + historical_data only; trading capabilities are not reachable through this object."""

    __slots__ = ("__s", "__q", "__h")

    def __init__(self, search, quote, history):
        self.__s, self.__q, self.__h = search, quote, history

    def search_scrip(self, **kw):
        return self.__s(**kw)

    def quotes(self, **kw):
        return self.__q(**kw)

    def historical_data(self, **kw):
        return self.__h(**kw)


def parse_candles(response) -> list[dict]:
    rows = ((response.get("data") or {}).get("candles") if isinstance(response, dict) else None) or []
    out = []
    for r in rows:
        if not isinstance(r, (list, tuple)) or len(r) < 6:
            continue
        ts = datetime.fromisoformat(str(r[0])) if not isinstance(r[0], (int, float)) else \
            datetime.fromtimestamp(float(r[0]), IST)
        ts = ts if ts.tzinfo else ts.replace(tzinfo=IST)
        out.append({"ts": ts.astimezone(IST).isoformat(), "open": float(r[1]), "high": float(r[2]),
                    "low": float(r[3]), "close": float(r[4]), "volume": float(r[5]),
                    "oi": float(r[6]) if len(r) > 6 and r[6] is not None else None})
    return out


def collect(client, instrument: str, days: int, out: Path, pause: float = 1.0) -> dict:
    from app.commodity_acts.broker import assert_market_only
    from app.mcx_futures.spec import spec_from_row, _is_future  # noqa: PLC2701 - same-package resolution rules

    assert_market_only(client)
    today = datetime.now(IST).date()
    rows = client.search_scrip(exchange_segment=SEGMENT, symbol=instrument, ignore_50multiple=True)
    rows = rows.get("data") if isinstance(rows, dict) else rows
    futures = [spec_from_row(r, instrument, today) for r in rows if _is_future(r, instrument)]
    report = {"instrument": instrument, "collected_on": today.isoformat(), "contracts": []}
    out.mkdir(parents=True, exist_ok=True)
    for spec in sorted(futures, key=lambda s: s.expiry):
        start, candles, hashes = today - timedelta(days=days), [], []
        while start <= today:
            end = min(start + timedelta(days=29), today)
            resp = client.historical_data(neosymbol=f"{SEGMENT}|{spec.token}", interval="1min",
                                          from_date=start.isoformat(), to_date=end.isoformat())
            hashes.append(hashlib.sha256(json.dumps(resp, sort_keys=True, default=str).encode()).hexdigest())
            candles += parse_candles(resp)
            start = end + timedelta(days=1)
            time.sleep(pause)
        uniq = {c["ts"]: c for c in candles}
        path = out / f"{instrument}_{spec.trading_symbol}.csv"
        with path.open("w") as fh:
            fh.write("ts,open,high,low,close,volume,oi\n")
            for c in sorted(uniq.values(), key=lambda c: c["ts"]):
                fh.write(f"{c['ts']},{c['open']},{c['high']},{c['low']},{c['close']},{c['volume']},{c['oi']}\n")
        days_seen = sorted({c["ts"][:10] for c in uniq.values()})
        report["contracts"].append({"trading_symbol": spec.trading_symbol, "token": spec.token,
                                    "expiry": spec.expiry.isoformat(), "lot_size": spec.lot_size,
                                    "tick_size": spec.tick_size, "candles": len(uniq), "sessions": len(days_seen),
                                    "first": days_seen[0] if days_seen else None,
                                    "last": days_seen[-1] if days_seen else None, "response_sha256": hashes,
                                    "file": path.name})
    (out / f"{instrument}_coverage.json").write_text(json.dumps(report, indent=1))
    return report


def tradable_series(data: Path, instrument: str, window_days: int):
    """Near-month UNADJUSTED series: on each session use the nearest contract outside the expiry window."""
    import pandas as pd

    cov = json.loads((data / f"{instrument}_coverage.json").read_text())
    frames = {c["expiry"]: pd.read_csv(data / c["file"], parse_dates=["ts"]) for c in cov["contracts"]}
    expiries = sorted(date.fromisoformat(e) for e in frames)
    parts = []
    for exp in expiries:
        f = frames[exp.isoformat()]
        if f.empty:
            continue
        f["day"] = f["ts"].dt.tz_convert(IST).dt.date
        eligible = [e for e in expiries]

        def chosen(day):
            return next((e for e in eligible if (e - day).days >= window_days), None)

        parts.append(f[f["day"].map(chosen) == exp].assign(contract_expiry=exp.isoformat()))
    if not parts:
        return None, cov
    s = pd.concat(parts).sort_values("ts").drop_duplicates("ts").reset_index(drop=True)
    return s, cov


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--instrument", choices=["CRUDEOILM", "NATGASMINI"], required=True)
    c.add_argument("--days", type=int, default=120)
    c.add_argument("--out", type=Path, required=True)
    r = sub.add_parser("research")
    r.add_argument("--instrument", choices=["CRUDEOILM", "NATGASMINI"], required=True)
    r.add_argument("--data", type=Path, required=True)
    a = ap.parse_args(argv)
    if a.cmd == "collect":
        from app.broker.kotak.auth import create_market_data_client
        from app.core.config import get_settings

        sdk = create_market_data_client(get_settings())
        client = HistoryOnlySDK(sdk.search_scrip, sdk.quotes, sdk.historical_data)
        print(json.dumps(collect(client, a.instrument, a.days, a.out), indent=1))
        return 0
    from app.mcx_futures.config import CONFIGS
    from app.mcx_futures.costs import DEFAULT_SCHEDULE
    from app.mcx_futures.research import run_research
    from app.mcx_futures.spec import ContractSpec

    cfg = CONFIGS[a.instrument]
    series, cov = tradable_series(a.data, a.instrument, cfg.expiry_window_days)
    if series is None:
        print(json.dumps({"status": "INSUFFICIENT_DATA", "reason": "NO_CANDLES", "coverage": cov}, indent=1))
        return 0
    c0 = cov["contracts"][0]
    # Replay treats the rolled near-month series as one tradable stream; roll days are reported, never adjusted.
    spec = ContractSpec(instrument=a.instrument, trading_symbol=f"{a.instrument}_NEAR_MONTH_ROLLED", token="REPLAY",
                        segment=SEGMENT, expiry=date(2099, 12, 31), lot_size=c0["lot_size"], tick_size=c0["tick_size"],
                        precision=2, quotation_unit="BBL" if a.instrument == "CRUDEOILM" else "MMBTU",
                        rupees_per_point_per_lot=float(c0["lot_size"]), source="KOTAK_HISTORY_ROLLED")
    result = run_research(series.rename(columns={"ts": "ts"}), cfg, spec, DEFAULT_SCHEDULE)
    print(json.dumps(result, indent=1, default=str)[:200000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
