"""Fast timing plus causal intraday context; book quality is not directional alpha."""
from __future__ import annotations

from typing import Literal

from app.scalper.models import (ScalperDirection, ScalperFeatures,
                                ScalperMarketSnapshot, ScalperSignal,
                                ScalperStrength)


def _clip(value: float, low: float = -1, high: float = 1) -> float:
    return min(high, max(low, value))


def _direction(value: float, minimum: float) -> ScalperDirection:
    return (ScalperDirection.BULL if value >= minimum else
            ScalperDirection.BEAR if value <= -minimum else ScalperDirection.NEUTRAL)


def _slow_evidence(features: ScalperFeatures) -> tuple[dict[str, float], float, bool]:
    context = features.intraday
    if context is None:
        return {}, 0.0, False
    parts = {"slow_session": _clip(context.session_move_bps / 40) * 20}
    available = 20.0
    for label, weight, scale in (("1m", 5, 6), ("3m", 10, 12),
                                 ("5m", 15, 20), ("15m", 15, 40)):
        value = context.returns_bps[label]
        if value is not None:
            parts[f"slow_{label}"] = _clip(value / scale) * weight
            available += weight
    if context.range_position is not None and context.session_range_bps >= 10:
        parts["slow_location"] = (context.range_position * 2 - 1) * 10
        available += 10
    if context.opening_range_complete:
        high, low = context.opening_high_distance_bps, context.opening_low_distance_bps
        assert high is not None and low is not None
        parts["slow_opening_range"] = _clip((max(0, high) + min(0, low)) / 10) * 10
        available += 10
    future_return = context.futures_returns_bps.get("5m")
    if future_return is not None:
        parts["slow_futures"] = _clip(future_return / 20) * 12
        available += 12
    if context.futures_basis_change_5m is not None:
        parts["slow_basis_change"] = _clip(context.futures_basis_change_5m / 10) * 3
        available += 3
    net = sum(parts.values()) / available * 100
    sign = 1 if net > 0 else -1
    three, five = context.returns_bps.get("3m"), context.returns_bps.get("5m")
    strong = (abs(net) >= 60 and sign * context.session_move_bps >= 10
              and three is not None and sign * three >= 2
              and five is not None and sign * five >= 4
              and sign * context.horizon_agreement >= .75)
    return parts, net, strong


def build_signal(snapshot: ScalperMarketSnapshot, features: ScalperFeatures,
                 prior_signals: list[ScalperSignal], *, min_score: float = 80,
                 trend_aligned_min_score: float = 68,
                 mixed_min_score: float | None = None,
                 countertrend_min_score: float = 85,
                 min_confirmations: int = 2,
                 max_confirmation_gap_seconds: float | None = None,
                 phase14_context: dict | None = None) -> ScalperSignal:
    """Thresholds select regimes; confirmation never borrows a lower prior threshold."""
    momentum = _clip(features.momentum / 6) * 40
    structure = 0.0
    if features.rolling_reference is not None:
        structure = _clip((snapshot.nifty_spot - features.rolling_reference) / 8) * 20
    if features.local_high is not None and snapshot.nifty_spot > features.local_high:
        structure += 10
    elif features.local_low is not None and snapshot.nifty_spot < features.local_low:
        structure -= 10
    # Direction comes from spot timing. Basis and participation cannot invent a direction.
    direction = _direction(momentum + structure, 10)
    sign = (1 if direction == ScalperDirection.BULL else
            -1 if direction == ScalperDirection.BEAR else 0)
    futures = (_clip(features.futures_return_bps / 6) * 17
               if features.futures_return_bps is not None else 0.0)
    if features.futures_basis_change is not None:
        futures += _clip(features.futures_basis_change / 5) * 3
    participation_terms = []
    for call, put in ((features.call_participation_change, features.put_participation_change),
                      (features.call_volume_change, features.put_volume_change)):
        if call is not None and put is not None:
            participation_terms.append(_clip((put - call) / max(abs(call), abs(put), 1)))
    participation = (sum(participation_terms) / len(participation_terms) * 10
                     if participation_terms else 0.0)
    fast_parts = dict(momentum=momentum, structure=structure,
                      futures=futures, participation=participation)
    # Missing futures/participation contributes no conviction; no positive execution points.
    fast_score = _clip(sign * sum(fast_parts.values()), 0, 100) if sign else 0.0
    contradiction = sum(max(0, -sign * value) for value in fast_parts.values()) if sign else 0.0
    slow_parts, slow_net, strong = _slow_evidence(features)
    slow_direction = _direction(slow_net, 20)
    alignment: Literal["MIXED", "ALIGNED", "OPPOSED"] = (
                 "MIXED" if direction == ScalperDirection.NEUTRAL
                 or slow_direction == ScalperDirection.NEUTRAL else
                 "ALIGNED" if direction == slow_direction else "OPPOSED")
    regime: Literal["TREND_ALIGNED", "COUNTERTREND", "MIXED"] = (
              "TREND_ALIGNED" if strong and alignment == "ALIGNED" else
              "COUNTERTREND" if alignment == "OPPOSED" else "MIXED")
    mixed = min_score if mixed_min_score is None else mixed_min_score
    threshold = {"TREND_ALIGNED": trend_aligned_min_score, "MIXED": mixed,
                 "COUNTERTREND": countertrend_min_score}[regime]
    # Partial session context must not suppress a fully evidenced fast setup during warm-up.
    slow_ready = (features.intraday is not None
                  and features.intraday.returns_bps.get("5m") is not None)
    score = fast_score
    if slow_ready and alignment == "ALIGNED":
        score = .70 * fast_score + .30 * abs(slow_net)
    elif alignment == "OPPOSED":
        score -= .10 * abs(slow_net)
        contradiction += .10 * abs(slow_net)
    score = round(_clip(score, 0, 100), 6) if sign else 0.0
    count = 0
    if sign and score >= threshold:
        count = 1
        for previous in reversed(prior_signals):
            gap = (snapshot.captured_at - previous.timestamp).total_seconds()
            previous_threshold = (previous.applicable_entry_threshold
                                  if previous.applicable_entry_threshold is not None else min_score)
            if (previous.direction != direction or previous.score < previous_threshold
                    or previous.applicable_entry_threshold is None
                    or previous.threshold_regime != regime or gap <= 0
                    or (max_confirmation_gap_seconds is not None
                        and gap > max_confirmation_gap_seconds)):
                break
            count = previous.confirmation_count + 1
            break
    confirmed = count >= min_confirmations
    reasons = [f"{name.upper()}_{'BULL' if value > 0 else 'BEAR'}"
               for name, value in {**fast_parts, **slow_parts}.items() if abs(value) >= 3]
    reasons.append(f"THRESHOLD_{regime}")
    if strong:
        reasons.append("STRONG_INTRADAY_TREND")
    if contradiction:
        reasons.append("CONTRADICTORY_EVIDENCE_PRESENT")
    blockers = (["NEUTRAL_FAST_DIRECTION"] if not sign else
                ["SCORE_BELOW_REGIME_THRESHOLD"] if score < threshold else
                ["CONFIRMATION_PENDING"] if not confirmed else ["ENTRY_NOT_EVALUATED"])
    warnings = list(features.warnings)
    if not slow_ready:
        warnings.append("SLOW_CONTEXT_WARMUP")
    if phase14_context and phase14_context.get("regime") == "NO_TRADE":
        warnings.append("PHASE14_NO_TRADE_CONTEXT_ONLY")
    spread_quality = (0.0 if features.median_spread_pct is None else
                      _clip(1 - features.median_spread_pct / 30, 0, 1))
    return ScalperSignal(
        timestamp=snapshot.captured_at, direction=direction,
        strength=(ScalperStrength.STRONG if score >= threshold else
                  ScalperStrength.MODERATE if score >= 65 else
                  ScalperStrength.WEAK if score > 0 else ScalperStrength.NONE),
        score=score, combined_score=score,
        components={key: round(value, 6) for key, value in {
            **fast_parts, **slow_parts,
            "execution": features.executable_book_coverage * 60 + spread_quality * 40}.items()},
        contradiction_penalty=round(contradiction, 6), reasons=reasons, warnings=warnings,
        confirmation_count=count, confirmed=confirmed, phase14_context=phase14_context,
        fast_direction=direction, slow_direction=slow_direction, trend_alignment=alignment,
        fast_score=round(fast_score, 6), slow_context_score=round(abs(slow_net), 6),
        applicable_entry_threshold=threshold, threshold_regime=regime,
        strong_slow_trend=strong, signal_qualified=confirmed, primary_blockers=blockers)
