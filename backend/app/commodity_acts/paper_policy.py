"""Explicit commodity-only execution evidence and risk policy; no broker calls."""
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.commodity_acts.config import InstrumentId

PAPER_VERSION = "COMMODITY_ACTS_PAPER_V2"
ACTIVE = ("PENDING_PAPER_ENTRY", "OPEN", "PENDING_EXIT", "UNRESOLVED_EXPOSURE")


class ContractEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    instrument_id: InstrumentId
    future_token: str
    future_expiry: date
    option_expiry: date
    lot_size: int = Field(gt=0)
    rupees_per_point_per_lot: float = Field(gt=0)
    depth_unit: Literal["UNITS", "LOTS"]
    tick_size: float = Field(gt=0)
    option_future_link_verified: Literal[True]
    source: str = Field(min_length=8)
    verified_at: datetime
    valid_until: datetime

    @model_validator(mode="after")
    def dates(self):
        if (self.verified_at.tzinfo is None or self.valid_until.tzinfo is None
                or self.valid_until <= self.verified_at):
            raise ValueError("AWARE_DATED_CONTRACT_EVIDENCE_REQUIRED")
        return self


class MarginEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    instrument_id: InstrumentId
    short_token: str
    hedge_token: str | None
    expiry: date
    lot_size: int = Field(gt=0)
    required_margin: float = Field(gt=0)
    observed_at: datetime
    source: str = Field(min_length=8)
    product: Literal["NRML"] = "NRML"
    portfolio_verified: Literal[True]
    measurement_kind: Literal["PORTFOLIO_ESTIMATE", "ACTUAL_BLOCKED"] = "PORTFOLIO_ESTIMATE"

    @model_validator(mode="after")
    def aware(self):
        if self.observed_at.tzinfo is None:
            raise ValueError("AWARE_MARGIN_EVIDENCE_REQUIRED")
        return self


class PaperPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False, validate_default=True)
    instrument_id: InstrumentId
    evidence: ContractEvidence
    max_loss_per_trade: float = Field(gt=0)
    daily_loss_limit: float = Field(gt=0)
    max_entry_spread_pct: float = Field(gt=0, le=100)
    min_credit: float = Field(default=0, ge=0)
    max_positions: Literal[1] = 1
    lots: Literal[1] = 1
    max_trades_per_session: int = Field(default=3, ge=1, le=100)
    max_quote_age_seconds: int = Field(default=30, ge=1, le=60)
    entry_ttl_seconds: int = Field(default=60, ge=15, le=300)
    max_holding_minutes: int = Field(default=30, ge=1)
    profit_capture_pct: float = Field(default=50, gt=0, lt=100)
    stop_credit_multiple: float = Field(default=2, gt=1)
    trailing_activation_pct: float = Field(default=25, gt=0)
    trailing_giveback_pct: float = Field(default=15, gt=0)
    min_entry_dte: int = Field(default=1, ge=1)
    kill_switch: bool = False
    max_broker_margin: float | None = Field(default=None, gt=0)
    margin_evidence: tuple[MarginEvidence, ...] = ()
    margin_max_age_seconds: int = Field(default=300, ge=1, le=3600)
    all_in_cost_per_leg_execution: float | None = Field(default=None, gt=0)
    cost_model: Literal["UNCONFIGURED", "COMPLETE_FLAT_PER_EXECUTED_LEG"] = "UNCONFIGURED"
    cost_source: str | None = None
    cost_effective_from: date | None = None
    cost_effective_to: date | None = None

    @model_validator(mode="after")
    def ownership(self):
        if self.instrument_id != self.evidence.instrument_id:
            raise ValueError("CONTRACT_EVIDENCE_OWNER_MISMATCH")
        if self.all_in_cost_per_leg_execution is not None and (
                self.cost_model != "COMPLETE_FLAT_PER_EXECUTED_LEG" or not self.cost_source or not self.cost_source.strip() or self.cost_effective_from is None
                or self.cost_effective_to is None or self.cost_effective_from > self.cost_effective_to):
            raise ValueError("COMPLETE_DATED_COST_EVIDENCE_REQUIRED")
        return self

    def metadata_reason(self, snapshot, known_at):
        evidence = self.evidence
        if (snapshot.instrument_id, snapshot.future_token, snapshot.future_expiry, snapshot.expiry, snapshot.lot_size) != (
                evidence.instrument_id, evidence.future_token, evidence.future_expiry, evidence.option_expiry, evidence.lot_size):
            return "CONTRACT_EVIDENCE_IDENTITY_MISMATCH"
        if not evidence.verified_at <= snapshot.timestamp_ist <= known_at <= evidence.valid_until:
            return "CONTRACT_EVIDENCE_NOT_CURRENT_OR_NOT_CAUSAL"
        if (snapshot.source_market_timestamp is None or not
                0 <= (known_at-snapshot.source_market_timestamp).total_seconds() <= self.max_quote_age_seconds):
            return "REFERENCE_QUOTE_TIMESTAMP_MISSING_OR_STALE"
        return None

    def net(self, gross: float, day: date, entry_day: date | None = None):
        return self._net_after_execution_costs(gross, day, entry_day, executions=4)

    def _net_after_execution_costs(self, gross: float, day: date, entry_day: date | None, *, executions: int):
        if (self.all_in_cost_per_leg_execution is None or self.cost_effective_from is None or self.cost_effective_to is None
                or not self.cost_effective_from <= day <= self.cost_effective_to
                or entry_day is not None and not self.cost_effective_from <= entry_day <= self.cost_effective_to):
            return None
        return float(Decimal(str(gross)) - executions * Decimal(str(self.all_in_cost_per_leg_execution)))


def margin_for(policy: PaperPolicy, short_token, hedge_token, expiry, at):
    matches = [row for row in policy.margin_evidence if (
        row.instrument_id == policy.instrument_id and row.short_token == short_token and row.hedge_token == hedge_token
        and row.expiry == expiry and row.lot_size == policy.evidence.lot_size
        and 0 <= (at - row.observed_at).total_seconds() <= policy.margin_max_age_seconds)]
    return matches[0].model_dump(mode="json") if len(matches) == 1 else None


def margin_comparison(standalone, spread, *, credit, hedge_ask, width, multiplier):
    """Same-short portfolio evidence only. Never substitute maximum loss for margin."""
    result = {"status": "MARGIN_UNVERIFIED", "standalone_margin": None, "spread_margin": None,
              "margin_saved_pct": None, "hedge_premium_cost": hedge_ask * multiplier,
              "net_spread_credit": credit * multiplier, "maximum_theoretical_loss": (width - credit) * multiplier,
              "credit_to_blocked_margin": None, "credit_to_verified_margin_estimate": None}
    if spread is not None:
        result.update(spread_margin=spread["required_margin"],
                      credit_to_verified_margin_estimate=credit * multiplier / spread["required_margin"],
                      credit_to_blocked_margin=credit * multiplier / spread["required_margin"]
                          if spread.get("measurement_kind") == "ACTUAL_BLOCKED" else None)
    if standalone is not None and spread is None:
        result["standalone_margin"] = standalone["required_margin"]
    if standalone is not None and spread is not None:
        keys = ("instrument_id", "short_token", "expiry", "lot_size", "product")
        if (all(standalone[key] == spread[key] for key in keys) and standalone["hedge_token"] is None
                and spread["hedge_token"] is not None and standalone["portfolio_verified"] and spread["portfolio_verified"]):
            naked, hedged = standalone["required_margin"], spread["required_margin"]
            result.update(status="VERIFIED_PORTFOLIO_ESTIMATE", standalone_margin=naked, spread_margin=hedged,
                          margin_saved_pct=100 * (naked - hedged) / naked,
                          credit_to_verified_margin_estimate=credit * multiplier / hedged,
                          credit_to_blocked_margin=credit * multiplier / hedged if spread.get("measurement_kind") == "ACTUAL_BLOCKED" else None)
    return result
