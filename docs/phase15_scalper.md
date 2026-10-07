# Phase 15 isolated intraday paper scalper

Phase 15 adds a second, independent market-data stream for short-horizon NIFTY
credit-spread research. It is disabled by default and has no broker order client or
order endpoint. The existing three-minute collector, Phase 14.2 pipeline,
forward-paper journal, and thresholds are unchanged.

## Data and deterministic decision path

`worker/scalper_worker.py` uses the existing read-only Kotak adapter. Each run gets
NIFTY spot, nearest-expiry option-chain data (which also supplies the future), India
VIX, and one batched depth request for the selected ATM window. It writes only to
`scalper_market_snapshots` and `scalper_option_quotes`. Broker source timestamps and
request/response ingestion timestamps remain separate. Missing instrument identity
fails the capture; missing, stale, crossed, wide, or undersized books cannot fill.

The deterministic score has momentum, short-window structure, futures,
participation, execution-quality, and contradiction terms. It ranges from 0 to 100
and defaults to two consecutive observations at or above 80. A strictly earlier
Phase 14 regime may be recorded as context. `NO_TRADE` context is a warning and is
never an automatic scalper veto.

The source does not provide NIFTY underlying volume, so an underlying VWAP is not
fabricated. `vwap` stays null with `UNDERLYING_VWAP_UNAVAILABLE`; the structure term
uses the recorded rolling reference and local breakouts. This limitation is stored
with every feature record.

## Paper execution and accounting

Bullish signals rank bull put spreads and bearish signals rank bear call spreads at
100/200/300/400-point widths. Entry is possible only on a later observation, at
short bid and long ask. Marks and exits use short ask and long bid. One scalper
position and one lot are hard configuration bounds. Expiry-day entries are always
rejected.

`scalper_trades`, `scalper_events`, and `scalper_cursor` form a separate state
machine and hash-chained journal. Projection and chain verification is available as
`verify_scalper_journal`. Runtime latency is logged rather than persisted in the
journal so identical ordered inputs reproduce identical evidence.

Brokerage and charge inputs default to explicit zero assumptions, but the schedule
is unconfigured and deliberately incomplete. Results therefore expose gross P&L,
set net P&L to null, label accounting `GROSS_ONLY`, and make no profitability claim.

## Exact safe local smoke test

The following offline smoke test uses generated books, makes no network call, and
verifies later-observation entry plus observed-book exit accounting:

```bash
cd /workspaces/AI_Trade
KOTAK_CONSUMER_KEY=offline-scalper \
PYTHONPATH=/workspaces/AI_Trade:/workspaces/AI_Trade/backend \
python -m pytest -q \
  backend/tests/test_scalper.py::test_strict_next_observation_entry_and_no_same_observation_fill \
  backend/tests/test_scalper.py::test_profit_capture_exit_uses_short_ask_and_long_bid_and_gross_only
```

To smoke-test the dedicated worker against read-only Kotak market data during the
configured weekday session, use a disposable local SQLite database. This neither
changes `.env.production` nor contacts an order API:

```bash
cd /workspaces/AI_Trade
export DATABASE_URL='sqlite+pysqlite:////tmp/ai_trade_phase15_smoke.db'
python -m alembic -c alembic.ini upgrade head
SCALPER_ENABLED=true TELEGRAM_ENABLED=false timeout 45s python worker/scalper_worker.py
SCALPER_ENABLED=true TELEGRAM_ENABLED=false \
  uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
curl -s http://127.0.0.1:8000/api/scalper/status
```

The worker requires `KOTAK_CONSUMER_KEY` from the ignored local `.env` and captures
only between `SCALPER_START_TIME` and `SCALPER_FORCED_EXIT_TIME` in Asia/Kolkata.
Stop the foreground API with Ctrl-C. Delete the disposable database when finished.

