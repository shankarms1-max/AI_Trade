from dataclasses import dataclass

from app.data.models import MarketSnapshot
from app.regime.models import RegimeResult
from app.shadow.exits import ShadowConfig, exit_reason
from app.shadow.models import ShadowStatus, ShadowTrade, ShadowTradeMark
from app.shadow.valuation import create_mark, value_trade


@dataclass(frozen=True)
class LifecycleUpdate:
    trade: ShadowTrade
    mark: ShadowTradeMark | None
    changed: bool


def update_trade(
    trade: ShadowTrade,
    snapshot_id: int,
    snapshot: MarketSnapshot,
    regime: RegimeResult | None,
    config: ShadowConfig,
) -> LifecycleUpdate:
    if trade.status != ShadowStatus.OPEN or snapshot.timestamp_ist <= trade.entry_timestamp:
        return LifecycleUpdate(trade, None, False)
    local_time = snapshot.timestamp_ist.time().replace(tzinfo=None)
    if snapshot.timestamp_ist.date() > trade.entry_timestamp.date():
        invalid = trade.model_copy(update={
            "status": ShadowStatus.INVALID,
            "exit_reason": "VALUATION_UNAVAILABLE",
            "holding_minutes": (snapshot.timestamp_ist - trade.entry_timestamp).total_seconds() / 60,
            "warnings": list(dict.fromkeys(trade.warnings + ["VALUATION_UNAVAILABLE"])),
            "reason_codes": list(dict.fromkeys(trade.reason_codes + ["EXPIRY_EXIT_UNRESOLVED"])),
            "updated_at": snapshot.timestamp_ist,
        })
        return LifecycleUpdate(invalid, None, True)
    if snapshot.timestamp_ist.date() >= trade.expiry:
        invalid = trade.model_copy(update={
            "status": ShadowStatus.INVALID,
            "exit_reason": "EXPIRY_EXIT_UNRESOLVED",
            "holding_minutes": (snapshot.timestamp_ist - trade.entry_timestamp).total_seconds() / 60,
            "reason_codes": list(dict.fromkeys(trade.reason_codes + ["EXPIRY_EXIT_UNRESOLVED"])),
            "updated_at": snapshot.timestamp_ist,
        })
        return LifecycleUpdate(invalid, None, True)
    valuation = value_trade(trade, snapshot)
    if valuation is None:
        if local_time > config.hard_exit_cutoff:
            invalid = trade.model_copy(update={
                "status": ShadowStatus.INVALID,
                "exit_reason": "VALUATION_UNAVAILABLE",
                "holding_minutes": (snapshot.timestamp_ist - trade.entry_timestamp).total_seconds() / 60,
                "warnings": list(dict.fromkeys(trade.warnings + ["MISSING_CONTRACT", "VALUATION_UNAVAILABLE"])),
                "updated_at": snapshot.timestamp_ist,
            })
            return LifecycleUpdate(invalid, None, True)
        pending = trade.model_copy(update={
            "warnings": list(dict.fromkeys(trade.warnings + ["MISSING_CONTRACT", "VALUATION_UNAVAILABLE"])),
            "updated_at": snapshot.timestamp_ist,
        })
        return LifecycleUpdate(pending, None, True)
    mark = create_mark(trade, snapshot_id, snapshot, valuation)
    mfe = max(trade.mfe_per_unit, mark.pnl_per_unit)
    mae = min(trade.mae_per_unit, mark.pnl_per_unit)
    warnings = list(dict.fromkeys(trade.warnings + mark.warnings))
    reasons = list(dict.fromkeys(trade.reason_codes + [
        "VALUATION_BID_ASK" if valuation.basis.value == "BID_ASK" else "VALUATION_LTP_ESTIMATE"
    ]))
    updated = trade.model_copy(update={
        "mfe_per_unit": mfe,
        "mae_per_unit": mae,
        "mfe_per_lot": None if trade.lot_size is None else mfe * trade.lot_size,
        "mae_per_lot": None if trade.lot_size is None else mae * trade.lot_size,
        "warnings": warnings,
        "reason_codes": reasons,
        "updated_at": snapshot.timestamp_ist,
    })
    reason = exit_reason(updated, mark, regime, config)
    if reason is not None:
        updated = updated.model_copy(update={
            "status": ShadowStatus.CLOSED,
            "exit_market_snapshot_id": snapshot_id,
            "exit_timestamp": snapshot.timestamp_ist,
            "exit_reason": reason,
            "exit_short_price": mark.short_price,
            "exit_long_price": mark.long_price,
            "exit_debit": mark.exit_debit,
            "realized_pnl_per_unit": mark.pnl_per_unit,
            "realized_pnl_per_lot": mark.pnl_per_lot,
            "holding_minutes": (snapshot.timestamp_ist - trade.entry_timestamp).total_seconds() / 60,
            "reason_codes": list(dict.fromkeys(reasons + [reason])),
        })
    return LifecycleUpdate(updated, mark, True)
