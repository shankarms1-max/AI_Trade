from app.risk.models import EvaluationContext
from app.shadow.entry import create_shadow_entry
from app.shadow.exits import ShadowConfig
from app.shadow.lifecycle import update_trade
from app.shadow.models import ShadowEntryResult
from app.shadow.repository import ShadowRepository


def config_from_settings(settings) -> ShadowConfig:
    return ShadowConfig(
        profit_target_credit_capture_pct=settings.shadow_profit_target_credit_capture_pct,
        stop_loss_credit_multiple=settings.shadow_stop_loss_credit_multiple,
        force_exit_time=settings.shadow_force_exit_time,
        hard_exit_cutoff=settings.shadow_hard_exit_cutoff,
        exit_on_opposite_regime=settings.shadow_exit_on_opposite_regime,
        exit_on_structural_breach=settings.shadow_exit_on_structural_breach,
        max_new_trades_per_day=settings.shadow_max_new_trades_per_day,
        allow_multiple_open_trades=settings.shadow_allow_multiple_open_trades,
    )


def build_shadow_entry(
    repository: ShadowRepository, snapshot_id: int, config: ShadowConfig
) -> ShadowEntryResult:
    context = repository.load_entry_context(snapshot_id)
    if context is None:
        return ShadowEntryResult(
            snapshot_id=snapshot_id, created=False, trade=None,
            reason_codes=["NO_APPROVED_RISK_DECISION"],
        )
    if len(context) == 5:  # Backward-compatible repository doubles and pre-alpha contexts.
        snapshot, feature, regime, candidate_set, approved = context
        alpha = None
    else:
        snapshot, feature, regime, candidate_set, approved, alpha = context
    result = create_shadow_entry(
        snapshot_id, snapshot, feature, regime, candidate_set, approved, alpha,
        trades_today=repository.count_entries_on(snapshot.timestamp_ist.date()),
        open_trade_exists=repository.has_open_trade(),
        max_new_trades_per_day=config.max_new_trades_per_day,
        allow_multiple_open_trades=config.allow_multiple_open_trades,
    )
    if not result.created or result.trade is None:
        return result
    if repository.has_fingerprint(result.trade.candidate_fingerprint):
        return ShadowEntryResult(
            snapshot_id=snapshot_id, created=False, trade=None,
            reason_codes=["DAILY_SHADOW_LIMIT_REACHED"],
        )
    stored = repository.create_trade(result.trade)
    return result.model_copy(update={"trade": stored})


def update_open_trades(
    repository: ShadowRepository, snapshot_id: int, config: ShadowConfig
) -> int:
    context = repository.load_snapshot_context(snapshot_id)
    if context is None:
        raise LookupError(f"market snapshot {snapshot_id} not found")
    snapshot, regime = context
    updated = 0
    for trade in repository.open_trades():
        result = update_trade(trade, snapshot_id, snapshot, regime, config)
        if result.changed:
            repository.save_update(result.trade, result.mark)
            updated += 1
    return updated


def replay_all(repository: ShadowRepository, config: ShadowConfig) -> dict[str, int]:
    snapshots = entries = updates = 0
    for snapshot_id in repository.snapshot_ids_chronological():
        snapshots += 1
        updates += update_open_trades(repository, snapshot_id, config)
        result = build_shadow_entry(repository, snapshot_id, config)
        entries += int(result.created)
    return {"snapshots": snapshots, "entries": entries, "updates": updates}
