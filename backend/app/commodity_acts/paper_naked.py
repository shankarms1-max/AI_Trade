"""Commodity-only single-short construction. Stress scenarios are NOT loss bounds.

Direction is separate from native single-option entry qualification. No spread
geometry or hedge-quality compatibility inputs are created.
"""

from datetime import datetime, time, timedelta
from decimal import Decimal
from math import isfinite
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.acts_paper import acts_v2
from app.commodity_acts.paper_execution import book, exact, candidates as spread_candidates, pair
from app.commodity_acts.paper_policy import ContractEvidence, MarginEvidence, PaperPolicy, margin_for
from app.research.manifest import digest
from app.scalper.models import ScalperLeg
from app.strategy.construction import ordinal_score
from app.commodity_acts import entry

VERSION = "COMMODITY_ACTS_NAKED_PAPER_V1"
LOTS = {"CRUDEOILM": 10, "NATGASMINI": 250}


class NakedContractEvidence(ContractEvidence):
    depth_unit: Literal["UNITS"]

    @model_validator(mode="after")
    def quantity(self):
        if self.lot_size != LOTS[self.instrument_id] or self.rupees_per_point_per_lot != self.lot_size:
            raise ValueError("COMMODITY_VERIFIED_UNDERLYING_LOT_REQUIRED")
        return self


class SingleMarginEvidence(MarginEvidence):
    # A broker order-margin estimate is account-contextual, not proof that a
    # standalone multi-leg portfolio was independently verified.
    # Intentional Pydantic specialization: preserve strict single-contract evidence.
    portfolio_verified: Literal[False] = False  # type: ignore[assignment]
    hedge_token: None = None
    measurement_kind: Literal["SINGLE_INSTRUMENT_ESTIMATE"] = "SINGLE_INSTRUMENT_ESTIMATE"  # type: ignore


class NakedPaperPolicy(PaperPolicy):
    strategy: Literal["NAKED_SHORT_OPTION"]
    evidence: NakedContractEvidence
    margin_evidence: tuple[SingleMarginEvidence, ...] = ()
    # Inherited name is retained for configuration compatibility, but this is an
    # exit trigger, never a maximum possible loss. New risk ceilings are explicit.
    max_loss_per_trade: float = Field(gt=0, description="Cash loss exit trigger; NOT a guaranteed maximum loss")
    max_stress_loss_per_trade: float = Field(gt=0)
    stress_underlying_move_pct: float = Field(gt=0, lt=100)
    stress_premium_multiple: float = Field(gt=1)
    pre_expiry_exit_days: int = Field(default=1, ge=1)
    pre_expiry_exit_time: time

    @model_validator(mode="after")
    def wall_time(self):
        if self.pre_expiry_exit_time.tzinfo is not None:
            raise ValueError("PRE_EXPIRY_CUTOFF_MUST_BE_IST_WALL_TIME")
        return self

    def expiry_cutoff(self):
        from app.alpha.clock import IST

        return datetime.combine(
            self.evidence.option_expiry - timedelta(days=self.pre_expiry_exit_days), self.pre_expiry_exit_time, IST
        )

    def metadata_reason(self, snapshot, known_at):
        if snapshot.future_lot_size != LOTS[self.instrument_id]:
            return "FUTURE_CONTRACT_QUANTITY_CHANGED"
        return super().metadata_reason(snapshot, known_at)

    def net(self, gross, day, entry_day=None):
        # Share dated-evidence validation, but deduct from gross exactly once:
        # one SELL and one BUY, versus four executions for a legacy spread.
        return self._net_after_execution_costs(gross, day, entry_day, executions=2)


def load_policy(document):
    return (NakedPaperPolicy if document.get("strategy") == "NAKED_SHORT_OPTION" else PaperPolicy).model_validate(
        document
    )


class NakedCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    candidate_id: str
    strategy_type: Literal["SHORT_PUT", "SHORT_CALL"]
    direction: Literal["BULL", "BEAR"]
    short_leg: ScalperLeg
    contract_quantity: int
    short_distance_points: float
    executable_credit: float
    stress_loss_estimate: float
    risk_reservation: float
    maximum_loss: None = None
    loss_is_bounded: Literal[False] = False
    ranking_score: float = 0
    ranking_components: dict = Field(default_factory=dict)
    quote_evidence: dict
    qualification: dict
    construction_method: Literal["OBSERVED_MCX_OTM_SINGLE_SHORT"] = "OBSERVED_MCX_OTM_SINGLE_SHORT"
    construction_evidence: dict


def stress(snapshot, quote, premium, policy):
    """Explicit operator scenario, not a quote, Greeks model or guaranteed bound."""
    move = policy.stress_underlying_move_pct / 100
    stressed_future = snapshot.future_price * (1 - move if quote.option_type.value == "PE" else 1 + move)
    intrinsic = max(
        0, quote.strike - stressed_future if quote.option_type.value == "PE" else stressed_future - quote.strike
    )
    close_estimate = max(quote.ask * policy.stress_premium_multiple, intrinsic + quote.ask)
    loss = float((Decimal(str(close_estimate)) - Decimal(str(premium))) * policy.evidence.lot_size)
    if not all(isfinite(value) for value in (stressed_future, close_estimate, loss)):
        return None
    return {
        "kind": "SCENARIO_ESTIMATE_NOT_MAXIMUM_LOSS",
        "stressed_future": stressed_future,
        "stress_close_premium_estimate": close_estimate,
        "stress_loss_estimate": max(0, loss),
        "underlying_move_pct": policy.stress_underlying_move_pct,
        "premium_multiple": policy.stress_premium_multiple,
    }


def execute(snapshot, candidate, policy, known_at, *, entry, after=None):
    if not isinstance(policy, NakedPaperPolicy):
        if policy.evidence.depth_unit != "UNITS" and any(
            q is not None and q.depth_unit == "UNITS"
            for q in (exact(snapshot, candidate["short_leg"]), exact(snapshot, candidate["long_leg"]))
        ):
            return None, "LEGACY_DEPTH_INTERPRETATION_CONFLICT"
        return pair(snapshot, candidate, policy, known_at, entry=entry, after=after)
    quote = exact(snapshot, candidate["short_leg"])
    if (
        entry
        and quote is not None
        and not (
            quote.strike < snapshot.future_price
            if quote.option_type.value == "PE"
            else quote.strike > snapshot.future_price
        )
    ):
        return None, "SHORT_NO_LONGER_OTM"
    # Both sides and quantities must be usable for a new short; exit requires
    # only actual buy-side execution depth, never a fabricated partial fill.
    result, failure = book(quote, snapshot, policy, known_at, entry=entry, side="bid" if entry else "ask", after=after)
    if failure:
        return None, failure
    if entry:
        _, failure = book(quote, snapshot, policy, known_at, entry=True, side="ask", after=after)
        if failure:
            return None, failure
    if entry and candidate.get("qualification"):
        # Recheck configured native floors against the subsequent observed book.
        from app.commodity_acts.entry import EntryPolicy, native_evidence

        thresholds = EntryPolicy.model_validate(candidate["qualification"]["policy"])
        strikes = sorted({q.strike for q in snapshot.options})
        step = min((b - a for a, b in zip(strikes, strikes[1:])), default=None)
        scenario = stress(snapshot, quote, quote.bid, policy)
        if scenario is None:
            return None, "STRESS_ESTIMATE_INVALID"
        qualification = native_evidence(snapshot, quote, policy, known_at, scenario, step, thresholds)
        if qualification["reasons"]:
            return None, qualification["reasons"][0]
    return {
        "value": result["price"],
        "short": result,
        "atomic": True,
        "action": "SELL" if entry else "BUY",
        "contract_quantity": policy.evidence.lot_size,
    }, None


def candidates(snapshot, direction, ctx, policy, known_at, *, deferred_margin=False, entry_policy=None):
    if not isinstance(policy, NakedPaperPolicy):
        return spread_candidates(snapshot, direction, ctx, policy, known_at)
    if direction not in ("BULL", "BEAR"):
        return None, {"DIRECTION_UNAVAILABLE": 1}
    failure = policy.metadata_reason(snapshot, known_at)
    if failure:
        return None, {failure: 1}
    if max(snapshot.timestamp_ist, known_at) + timedelta(seconds=policy.entry_ttl_seconds) >= policy.expiry_cutoff():
        return None, {"PRE_EXPIRY_ENTRY_CUTOFF": 1}
    entry_policy = entry_policy or entry.EntryPolicy()

    class Rejections(dict):
        def __init__(self):
            super().__init__()
            self.evaluations = []

    rejected, choices = Rejections(), []
    strikes = sorted({q.strike for q in snapshot.options})
    step = min((b - a for a, b in zip(strikes, strikes[1:])), default=None)

    def reject(reason):
        rejected[reason] = rejected.get(reason, 0) + 1
        rejected.evaluations.append(
            {"option_token": quote.instrument_token, "strike": quote.strike, "status": "REJECTED", "reason": reason}
        )

    side = "PE" if direction == "BULL" else "CE"
    for quote in snapshot.options:
        if quote.option_type.value != side:
            continue
        distance = snapshot.future_price - quote.strike if side == "PE" else quote.strike - snapshot.future_price
        if distance <= 0:
            reject("SHORT_NOT_OTM")
            continue
        bid, failure = book(quote, snapshot, policy, known_at, entry=True, side="bid")
        if not failure:
            _, failure = book(quote, snapshot, policy, known_at, entry=True, side="ask")
        if failure:
            reject(failure)
            continue
        scenario = stress(snapshot, quote, quote.bid, policy)
        if scenario is None:
            reject("STRESS_ESTIMATE_INVALID")
            continue
        immediate_loss = (quote.ask - quote.bid) * quote.lot_size
        if quote.bid <= policy.min_credit or scenario["stress_loss_estimate"] > policy.max_stress_loss_per_trade:
            reject("STRESS_LOSS_OR_PREMIUM_LIMIT")
            continue
        if immediate_loss >= policy.max_loss_per_trade or quote.ask >= quote.bid * policy.stop_credit_multiple:
            reject("ENTRY_ALREADY_AT_STOP")
            continue
        margin = margin_for(policy, quote.instrument_token, None, snapshot.expiry, known_at)
        if policy.max_broker_margin is not None and (
            (margin is None and not deferred_margin)
            or margin is not None
            and margin["required_margin"] > policy.max_broker_margin
        ):
            reject("MARGIN_UNVERIFIED_OR_LIMIT_EXCEEDED")
            continue
        qualification = entry.native_evidence(snapshot, quote, policy, known_at, scenario, step, entry_policy)
        if qualification["reasons"]:
            for reason in qualification["reasons"]:
                reject(reason)
            continue
        qualification.update(
            status="ELIGIBLE", book_validation=bid["validation"], session_gate="ENFORCED_BY_PAPER_ENGINE"
        )
        leg = ScalperLeg(
            expiry=quote.expiry,
            strike=quote.strike,
            option_type=side,
            exchange=quote.exchange,
            trading_symbol=quote.trading_symbol,
            instrument_token=quote.instrument_token,
        )
        choices.append(
            NakedCandidate(
                candidate_id=digest(
                    {"version": entry.VERSION, "instrument": snapshot.instrument_id, "short": quote.identity}
                ),
                strategy_type="SHORT_PUT" if side == "PE" else "SHORT_CALL",
                direction=direction,
                short_leg=leg,
                contract_quantity=quote.lot_size,
                short_distance_points=distance,
                executable_credit=quote.bid,
                stress_loss_estimate=scenario["stress_loss_estimate"],
                risk_reservation=max(scenario["stress_loss_estimate"], policy.max_loss_per_trade),
                quote_evidence={"short": bid},
                qualification=qualification,
                construction_evidence={
                    "stress": scenario,
                    "margin": {
                        "status": "VERIFIED_SINGLE_INSTRUMENT_ESTIMATE" if margin else "MARGIN_UNVERIFIED",
                        "required_margin": margin["required_margin"] if margin else None,
                        "premium_to_verified_margin_estimate": (
                            quote.bid * quote.lot_size / margin["required_margin"] if margin else None
                        ),
                    },
                },
            )
        )
    if not choices:
        return None, rejected
    # Deterministic native ordering: observed book tightness, stress exposure,
    # then OTM distance and premium. No blended spread score or liquidity floor.
    choices.sort(
        key=lambda row: (
            row.quote_evidence["short"]["validation"]["spread_pct"],
            row.stress_loss_estimate,
            -row.short_distance_points,
            -row.executable_credit,
            row.candidate_id,
        )
    )
    for index, row in enumerate(choices):
        row.ranking_score = ordinal_score(index, len(choices))
        row.ranking_components = {
            "spread_pct": row.quote_evidence["short"]["validation"]["spread_pct"],
            "stress_loss_estimate": row.stress_loss_estimate,
            "otm_distance_points": row.short_distance_points,
        }
        row.construction_evidence["ranking"] = "SPREAD_ASC_STRESS_ASC_DISTANCE_DESC_PREMIUM_DESC_ID_ASC"
        rejected.evaluations.append(
            {
                "option_token": row.short_leg.instrument_token,
                "strike": row.short_leg.strike,
                "status": "SELECTED" if index == 0 else "ELIGIBLE_NOT_SELECTED",
                "qualification": row.qualification,
                "ranking": row.ranking_components,
            }
        )
    return choices[0], rejected


def decide(ctx, watch, candidate, rejected, blockers, scorer):
    if candidate is not None and not isinstance(candidate, NakedCandidate):
        return acts_v2.decide(ctx, watch, candidate, rejected, blockers, scorer)
    return entry.decide(ctx, watch, candidate, rejected, blockers, scorer)


def reservation(document):
    return document["risk_reservation"] if "risk_reservation" in document else document["defined_risk"]


def candidate_risk(candidate):
    return candidate.risk_reservation if isinstance(candidate, NakedCandidate) else candidate.defined_max_loss_per_lot
