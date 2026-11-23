"""Read-only AST_FUTURES_V1_DIRECT monitoring API over the isolated AST Direct database.

GET only. No broker credentials, no order, acknowledgement, kill-switch or other mutation capability.
Status reuses the authoritative ``report()``; nothing here alters strategy state or the journal.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import hmac
import json
import os
from pathlib import Path
import stat
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, func, select

from app.adaptive_supertrend.direct import FILL_BASIS, FILL_BASIS_NOTE, VERSION
from app.adaptive_supertrend.direct.engine import ACTIVE
from app.adaptive_supertrend.direct.models import TRADE_STATES, DirectCursor, DirectEvent, DirectTrade
from app.adaptive_supertrend.direct.policy import DirectPolicy
from app.adaptive_supertrend.direct.report import report
from app.adaptive_supertrend.direct.service import JOURNAL
from app.db.session import build_session_factory
from app.scalper.repository import IST

SERVICE = "ast-direct-read-api"
TRADE_FIELDS = (
    "trade_id", "trading_date", "state", "side", "direction", "future_instrument_id", "future_expiry", "lots",
    "lot_size", "quantity", "signal_timestamp", "signal_price", "decision_timestamp", "entry_timestamp",
    "entry_price", "exit_timestamp", "exit_price", "exit_reason", "exit_category", "pending_exit_reason",
    "gross_points", "gross_inr", "friction_points", "net_research_points", "mfe_points", "mae_points",
    "rejection_reason", "unresolved_reason", "fill_basis", "atr_period", "multiplier", "opening_range", "ast",
)
NO_STORE = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _token(path: str | None) -> str:
    """Server-held bearer token for the internal bridge. Fails closed when absent or unsafe."""
    if not path:
        raise HTTPException(503, "AUTH_NOT_CONFIGURED")
    try:
        info = Path(path).lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4096 or info.st_mode & 0o077:
            raise HTTPException(503, "AUTH_SECRET_UNSAFE")
        value = Path(path).read_text().strip()
    except OSError:
        raise HTTPException(503, "AUTH_NOT_CONFIGURED") from None
    if len(value) < 32:
        raise HTTPException(503, "AUTH_SECRET_UNSAFE")
    return value


def _project(doc: dict) -> dict:
    return {key: doc.get(key) for key in TRADE_FIELDS}


def _drawdown(values: list[float]) -> tuple[float, float, float]:
    """Closed-trade equity from zero: (max drawdown, current peak, current drawdown)."""
    equity = peak = worst = Decimal(0)
    for value in values:
        equity += Decimal(str(value))
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return float(worst), float(peak), float(peak - equity)


def performance(docs: list[dict]) -> dict:
    closed = sorted((d for d in docs if d.get("state") == "CLOSED"), key=lambda d: d["exit_timestamp"])
    points = [d["gross_points"] for d in closed]
    inr = [d.get("gross_inr") for d in closed]
    net = [d.get("net_research_points") for d in closed]
    wins, losses = [p for p in points if p > 0], [p for p in points if p < 0]
    daily: dict[str, Decimal] = {}
    counts: dict[str, int] = {}
    for doc in closed:
        day = doc.get("closed_date") or doc["trading_date"]
        daily[day] = daily.get(day, Decimal(0)) + Decimal(str(doc["gross_points"]))
        counts[day] = counts.get(day, 0) + 1
    cumulative, rows = Decimal(0), []
    for day in sorted(daily):
        cumulative += daily[day]
        rows.append({"date": day, "trades": counts[day], "points": float(daily[day]),
                     "cumulative_points": float(cumulative)})
    worst, peak, current = _drawdown(points)
    best = max(closed, key=lambda d: d["gross_points"]) if closed else None
    poorest = min(closed, key=lambda d: d["gross_points"]) if closed else None
    total = float(sum(Decimal(str(p)) for p in points))
    return {
        "label": "FORWARD_PAPER_PERFORMANCE",
        "basis": "CLOSED_PAPER_TRADES_FUTURES_LTP_PAPER_V1",
        "drawdown_basis": "CLOSED_TRADE_EQUITY_FROM_ZERO_NOT_MARK_TO_MARKET",
        "closed_trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "flat": len(points) - len(wins) - len(losses),
        "win_rate": len(wins) / len(points) if points else None,
        "gross_points": total,
        "gross_inr": float(sum(Decimal(str(v)) for v in inr)) if closed and all(v is not None for v in inr) else None,
        "net_research_points": (
            float(sum(Decimal(str(v)) for v in net)) if closed and all(v is not None for v in net) else None
        ),
        "average_points": total / len(points) if points else None,
        "best_trade": {k: best.get(k) for k in ("trade_id", "trading_date", "side", "gross_points")} if best else None,
        "worst_trade": (
            {k: poorest.get(k) for k in ("trade_id", "trading_date", "side", "gross_points")} if poorest else None
        ),
        "daily": rows,
        "current_peak_points": peak,
        "current_drawdown_points": current,
        "max_realized_drawdown_points": worst,
        "excluded_unresolved_exposures": sum(d.get("state") in ("UNRESOLVED_EXPOSURE", "UNRESOLVED_ACKNOWLEDGED")
                                             for d in docs),
    }


def _summary(event: DirectEvent, payload: dict) -> dict:
    if event.event_type == "OBSERVATION":
        decision = payload.get("decision") or {}
        return {"status": decision.get("status"), "blockers": decision.get("blockers"),
                "fill_blocker": decision.get("fill_blocker")}
    doc = payload.get("trade_document") or {}
    keys = ("state", "side", "entry_price", "exit_price", "exit_reason", "gross_points", "rejection_reason",
            "unresolved_reason", "pending_exit_reason")
    return {key: doc.get(key) for key in keys if doc.get(key) is not None}


def create_app(sessions=None, *, token_file: str | None = None) -> FastAPI:
    if sessions is None:
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise RuntimeError("DATABASE_URL is required")
        # Every transaction is read-only at the database, not merely by convention.
        options = {"options": "-c default_transaction_read_only=on"} if url.startswith("postgresql") else {}
        sessions = build_session_factory(create_engine(url, pool_pre_ping=True, connect_args=options))
    token_path = token_file if token_file is not None else os.getenv("AST_DIRECT_API_TOKEN_FILE")
    app = FastAPI(title=SERVICE, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def read_only(request: Request, call_next):
        if request.method not in ("GET", "HEAD"):
            return JSONResponse({"detail": "READ_ONLY"}, status_code=405, headers=NO_STORE)
        response = await call_next(request)
        response.headers.update(NO_STORE)
        return response

    def authorized(authorization: str | None = Header(default=None)) -> None:
        expected = _token(token_path)
        supplied = authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
        if not hmac.compare_digest(supplied.encode(), expected.encode()):
            raise HTTPException(401, "AUTHENTICATION_REQUIRED")

    def policy() -> DirectPolicy | None:
        try:
            return DirectPolicy.model_validate_json(os.environ.get("AST_DIRECT_CONFIG_JSON", ""))
        except ValueError:
            return None

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "service": SERVICE, "mode": "READ_ONLY", "strategy_id": VERSION}

    @app.get("/api/ast-direct/status", dependencies=[Depends(authorized)])
    def status() -> dict:
        configured = policy()
        kill_file = os.getenv("AST_DIRECT_KILL_SWITCH_FILE")
        heartbeat = os.getenv("AST_DIRECT_HEARTBEAT_FILE")
        value = report(
            sessions, datetime.now(IST), configured,
            heartbeat=Path(heartbeat) if heartbeat else None,
            kill_switch=_flag("AST_DIRECT_KILL_SWITCH") or bool(kill_file and Path(kill_file).exists()),
            enabled=_flag("AST_DIRECT_ENABLED") if "AST_DIRECT_ENABLED" in os.environ else None,
        )
        try:
            JOURNAL.verify(sessions)
            value["journal_verification"] = "VALID"
        except RuntimeError as exc:
            value["journal_verification"] = f"INVALID:{exc}"
        value["policy_configuration"] = (
            {
                "history_sessions": configured.history_sessions,
                "lot_size": configured.lot_size,
                "lots": configured.lots,
                "opening_range_ist": configured.opening_range_ist,
                "session_exit_time_ist": configured.session_exit_time_ist,
                "friction_points_per_round_trip": configured.friction_points_per_round_trip,
            }
            if configured else None
        )
        with sessions() as session:
            active = session.scalars(select(DirectTrade.document).where(DirectTrade.state.in_(ACTIVE))).first()
        value["position_detail"] = _project(json.loads(active)) if active else None
        value["fill_basis"] = FILL_BASIS
        value["fill_basis_note"] = FILL_BASIS_NOTE
        position, mark = value.get("position"), value.get("mark")
        value["position_mark_gross_inr"] = (
            float(Decimal(str(mark["points"])) * position["lot_size"])
            if position and mark and mark.get("trade_id") == position.get("trade_id")
            and mark.get("points") is not None and position.get("lot_size") is not None else None
        )
        return value

    @app.get("/api/ast-direct/trades", dependencies=[Depends(authorized)])
    def trades(
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0, le=100000),
        date_filter: date | None = Query(None, alias="date"),
        state: str | None = Query(None, pattern="^(" + "|".join(TRADE_STATES) + ")$"),
        side: Literal["LONG", "SHORT"] | None = None,
    ) -> dict:
        query = select(DirectTrade)
        if date_filter is not None:
            query = query.where(DirectTrade.trading_date == date_filter)
        if state is not None:
            query = query.where(DirectTrade.state == state)
        with sessions() as session:
            order = (DirectTrade.trading_date.desc(), DirectTrade.decision_snapshot_id.desc())
            rows = list(session.scalars(query.order_by(*order)))
        docs = [json.loads(row.document) for row in rows]
        if side is not None:
            docs = [doc for doc in docs if doc.get("side") == side]
        page = docs[offset:offset + limit]
        return {"items": [_project(doc) for doc in page], "total": len(docs), "limit": limit, "offset": offset,
                "has_more": offset + limit < len(docs), "fill_basis": FILL_BASIS}

    @app.get("/api/ast-direct/events", dependencies=[Depends(authorized)])
    def events(
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0, le=100000),
        scope: Literal["all", "lifecycle"] = "all",
    ) -> dict:
        query = select(DirectEvent)
        count = select(func.count()).select_from(DirectEvent)
        if scope == "lifecycle":
            query = query.where(DirectEvent.event_type != "OBSERVATION")
            count = count.where(DirectEvent.event_type != "OBSERVATION")
        with sessions() as session:
            total = session.scalar(count) or 0
            rows = list(session.scalars(query.order_by(DirectEvent.sequence.desc()).offset(offset).limit(limit)))
            cursor = session.get(DirectCursor, 1)
        items: list[dict[str, Any]] = []
        for event in rows:
            payload = json.loads(event.payload)
            items.append({
                "sequence": event.sequence, "event_type": event.event_type, "timestamp": payload.get("timestamp"),
                "snapshot_id": event.snapshot_id, "trade_id": event.trade_id, "event_hash": event.event_hash,
                "previous_hash": event.previous_hash, "summary": _summary(event, payload),
            })
        return {"items": items, "total": total, "limit": limit, "offset": offset, "has_more": offset + limit < total,
                "scope": scope, "journal_sequence": cursor.sequence if cursor else None,
                "journal_head": cursor.head_hash if cursor else None}

    @app.get("/api/ast-direct/performance", dependencies=[Depends(authorized)])
    def forward_performance() -> dict:
        with sessions() as session:
            docs = [json.loads(doc) for doc in session.scalars(select(DirectTrade.document))]
        return performance(docs)

    return app


def main() -> None:  # pragma: no cover - container entrypoint
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.getenv("PORT", "8010")), workers=1, access_log=False)


if __name__ == "__main__":  # pragma: no cover
    main()
