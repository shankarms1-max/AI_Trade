"""Shared live/replay entry diagnostics, including non-entry observations."""
from app.scalper.risk import entry_blockers, evaluate_entry
from app.scalper.strategy import CandidateBuildResult, build_candidates


def assess_entry(raw, signal, state, config, *, event_blocked=False):
    blockers = entry_blockers(signal, state, config, raw.captured_at,
                              event_blocked=event_blocked)
    blockers += [item for item in signal.primary_blockers if item not in
                 {"ENTRY_NOT_EVALUATED", "SIGNAL_NOT_CONFIRMED"}]
    built = CandidateBuildResult([], {})
    risk = None
    if not state.open_positions + state.unresolved_positions and signal.confirmed:
        built = build_candidates(raw, signal, config)
        if built.candidates:
            candidate = built.candidates[0]
            risk = evaluate_entry(candidate, signal, state, config, raw.captured_at,
                                  event_blocked=event_blocked)
            blockers.extend(risk.reasons)
            if raw.captured_at.date() >= candidate.short_leg.expiry:
                blockers.append("EXPIRY_DAY_ENTRY_FORBIDDEN")
        else:
            blockers.extend(["NO_EXECUTABLE_CANDIDATE", *sorted(built.rejection_counts)])
    signal = signal.model_copy(update={
        "entry_qualified": bool(built.candidates and risk and risk.approved and not blockers),
        "primary_blockers": list(dict.fromkeys(blockers)),
        "candidate_count": len(built.candidates), "candidate_rejections": built.rejection_counts})
    return signal, built, risk
