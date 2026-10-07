"""Deterministic six-part intraday signal score."""
from __future__ import annotations

from app.scalper.models import (ScalperDirection, ScalperFeatures,
                                ScalperMarketSnapshot, ScalperSignal,
                                ScalperStrength)


def _clip(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def build_signal(snapshot: ScalperMarketSnapshot, features: ScalperFeatures,
                 prior_signals: list[ScalperSignal], *, min_score: float = 80,
                 min_confirmations: int = 2,
                 max_confirmation_gap_seconds: float | None = None,
                 phase14_context: dict | None = None) -> ScalperSignal:
    """Score the current observation without consulting any future row."""
    momentum = _clip(features.momentum / 8.0, -1, 1) * 25.0
    structure = 0.0
    if features.rolling_reference is not None:
        structure += _clip((snapshot.nifty_spot - features.rolling_reference) / 8.0,
                           -1, 1) * 10.0
    if features.local_high is not None and snapshot.nifty_spot > features.local_high:
        structure += 10.0
    elif features.local_low is not None and snapshot.nifty_spot < features.local_low:
        structure -= 10.0
    structure = _clip(structure, -20, 20)
    futures = 0.0
    if features.futures_basis is not None:
        futures += _clip(features.futures_basis / 25.0, -1, 1) * 8.0
    if features.futures_basis_change is not None:
        futures += _clip(features.futures_basis_change / 5.0, -1, 1) * 7.0
    futures = _clip(futures, -15, 15)
    participation = 0.0
    participation_terms: list[float] = []
    if (features.call_participation_change is not None
            and features.put_participation_change is not None):
        scale = max(abs(features.call_participation_change),
                    abs(features.put_participation_change), 1.0)
        participation_terms.append(_clip((features.put_participation_change
                                          - features.call_participation_change) / scale,
                                         -1, 1))
    if features.call_volume_change is not None and features.put_volume_change is not None:
        scale = max(abs(features.call_volume_change),
                    abs(features.put_volume_change), 1.0)
        participation_terms.append(_clip((features.put_volume_change
                                          - features.call_volume_change) / scale,
                                         -1, 1))
    if participation_terms:
        participation = sum(participation_terms) / len(participation_terms) * 15.0
    spread_quality = (0.0 if features.median_spread_pct is None else
                      _clip(1.0 - features.median_spread_pct / 30.0, 0, 1))
    execution = features.executable_book_coverage * 15.0 + spread_quality * 10.0
    signed = {"momentum": momentum, "structure": structure,
              "futures": futures, "participation": participation}
    net_directional = sum(signed.values())
    direction = (ScalperDirection.BULL if net_directional >= 8 else
                 ScalperDirection.BEAR if net_directional <= -8 else
                 ScalperDirection.NEUTRAL)
    sign = (1 if direction == ScalperDirection.BULL else
            -1 if direction == ScalperDirection.BEAR else 0)
    aligned = sum(max(0.0, sign * value) for value in signed.values()) if sign else 0.0
    contradiction = sum(max(0.0, -sign * value) for value in signed.values()) if sign else 0.0
    score = _clip(execution + aligned - contradiction, 0, 100) if sign else 0.0
    strength = (ScalperStrength.STRONG if score >= min_score else
                ScalperStrength.MODERATE if score >= 65 else
                ScalperStrength.WEAK if score > 0 else ScalperStrength.NONE)
    count = 0
    if direction != ScalperDirection.NEUTRAL and score >= min_score:
        count = 1
        next_timestamp = snapshot.captured_at
        for previous in reversed(prior_signals):
            gap = (next_timestamp - previous.timestamp).total_seconds()
            if (previous.direction != direction or previous.score < min_score
                    or (max_confirmation_gap_seconds is not None
                        and gap > max_confirmation_gap_seconds)):
                break
            count += 1
            next_timestamp = previous.timestamp
    confirmed = count >= min_confirmations
    reasons = [f"{name.upper()}_{'BULL' if value > 0 else 'BEAR'}"
               for name, value in signed.items() if abs(value) >= 3]
    if execution >= 20:
        reasons.append("EXECUTION_QUALITY_STRONG")
    if contradiction:
        reasons.append("CONTRADICTORY_EVIDENCE_PRESENT")
    warnings = list(features.warnings)
    if phase14_context and phase14_context.get("regime") == "NO_TRADE":
        warnings.append("PHASE14_NO_TRADE_CONTEXT_ONLY")
    return ScalperSignal(
        timestamp=snapshot.captured_at,
        direction=direction,
        strength=strength,
        score=round(score, 6),
        components={**{key: round(value, 6) for key, value in signed.items()},
                    "execution": round(execution, 6)},
        contradiction_penalty=round(contradiction, 6),
        reasons=reasons,
        warnings=warnings,
        confirmation_count=count,
        confirmed=confirmed,
        phase14_context=phase14_context,
    )
