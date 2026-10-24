"""Causal Commodity metadata renewal, separate from frozen execution/risk rules."""

from datetime import datetime, timedelta
from decimal import Decimal
import json

from app.commodity_acts.paper_naked import NakedContractEvidence, NakedPaperPolicy


class RenewalError(ValueError):
    pass


def tick(row):
    try:
        precision = Decimal(str(row["lPrecision"]))
        raw = Decimal(str(row["dTickSize"]))
        if (
            not precision.is_finite()
            or precision != int(precision)
            or not 0 <= precision <= 8
            or not raw.is_finite()
            or raw <= 0
        ):
            raise ValueError()
        return float(raw / Decimal(10) ** int(precision))
    except (KeyError, ArithmeticError, ValueError, TypeError):
        raise RenewalError("CONTRACT_TICK_METADATA_UNAVAILABLE") from None


def choose(records, spec, policy, at, required_options=()):
    """Nearest entry-eligible expiry; actual pAssetCode identifies its future.

    Nonexpired held contracts pin capture even after their entry cutoff. An
    expired unresolved position is never closed/forgotten: risk remains reserved
    by the unchanged engine, while current market capture can continue.
    """
    futures = [r for r in records if r["pInstType"].upper().startswith("FUT") and r["pOptionType"].upper() == "XX"]
    options = [
        r for r in records if r["pInstType"].upper().startswith("OPT") and r["pOptionType"].upper() in ("CE", "PE")
    ]
    held = {str(r["expiry"]) for r in required_options if str(r.get("expiry", "")) >= at.date().isoformat()}
    if len(held) > 1:
        raise RenewalError("HELD_MULTIPLE_EXPIRIES_UNSUPPORTED")
    expiries = sorted({r["resolved_expiry"] for r in options})
    if held:
        expiries = [expiry for expiry in expiries if expiry.isoformat() in held]
        if not expiries:
            raise RenewalError("HELD_CONTRACT_METADATA_UNAVAILABLE")
    else:
        expiries = [
            expiry
            for expiry in expiries
            if (expiry - at.date()).days >= policy.min_entry_dte
            and at + timedelta(seconds=policy.entry_ttl_seconds)
            < datetime.combine(
                expiry - timedelta(days=policy.pre_expiry_exit_days), policy.pre_expiry_exit_time, at.tzinfo
            )
        ]
    if not expiries:
        raise RenewalError("NO_ELIGIBLE_COMMODITY_OPTION_EXPIRY")
    selected = [r for r in options if r["resolved_expiry"] == expiries[0]]
    links = {str(r.get("pAssetCode", "")).strip() for r in selected}
    if len(links) != 1 or not next(iter(links)):
        raise RenewalError("OPTION_FUTURE_ASSET_LINK_UNAVAILABLE_OR_AMBIGUOUS")
    linked = [r for r in futures if r["resolved_token"] in links]
    if len(linked) != 1 or linked[0]["resolved_expiry"] <= expiries[0]:
        raise RenewalError("OPTION_LINKED_FUTURE_UNAVAILABLE_OR_AMBIGUOUS")
    future = linked[0]
    if future["resolved_lot"] != policy.evidence.lot_size or any(
        r["resolved_lot"] != policy.evidence.lot_size for r in selected
    ):
        raise RenewalError("CONTRACT_LOT_METADATA_CHANGED")
    if any(tick(r) != policy.evidence.tick_size for r in selected):
        raise RenewalError("CONTRACT_OPTION_TICK_METADATA_CHANGED")
    expected_future_tick = {"CRUDEOILM": 1.0, "NATGASMINI": 0.1}[spec.instrument_id]
    if tick(future) != expected_future_tick:
        raise RenewalError("CONTRACT_FUTURE_TICK_METADATA_CHANGED")
    for leg in required_options:
        if str(leg.get("expiry")) not in held:
            continue
        matches = [r for r in selected if r["resolved_token"] == leg.get("instrument_token")]
        if len(matches) != 1:
            raise RenewalError("HELD_CONTRACT_METADATA_UNAVAILABLE")
        r = matches[0]
        if (
            r.get("pTrdSymbol") != leg.get("trading_symbol")
            or r["pOptionType"] != leg.get("option_type")
            or Decimal(str(r.get("dStrikePrice;"))) / 100 != Decimal(str(leg.get("strike")))
        ):
            raise RenewalError("HELD_CONTRACT_METADATA_MISMATCH")
    return future, selected


def evidence(future, options, spec, verified_at):
    return NakedContractEvidence(
        instrument_id=spec.instrument_id,
        future_token=future["resolved_token"],
        future_expiry=future["resolved_expiry"],
        option_expiry=options[0]["resolved_expiry"],
        lot_size=future["resolved_lot"],
        rupees_per_point_per_lot=future["resolved_lot"],
        tick_size=tick(options[0]),
        depth_unit="UNITS",
        option_future_link_verified=True,
        source="KOTAK_MCX_MASTER_PASSETCODE_EXACT_LINK_V1",
        verified_at=verified_at,
        valid_until=datetime.combine(verified_at.date(), spec.market_session.end, verified_at.tzinfo),
    )


def renew(policy, snapshot, spec, known_at):
    if not isinstance(policy, NakedPaperPolicy):
        raise RenewalError("AUTOMATIC_RENEWAL_REQUIRES_NAKED_PAPER_POLICY")
    try:
        item = NakedContractEvidence.model_validate(json.loads(snapshot.provenance["contract_evidence"]))
    except (KeyError, ValueError, TypeError):
        raise RenewalError("CONTRACT_RENEWAL_EVIDENCE_UNAVAILABLE") from None
    if (
        item.source != "KOTAK_MCX_MASTER_PASSETCODE_EXACT_LINK_V1"
        or item.instrument_id != policy.instrument_id
        or item.lot_size != policy.evidence.lot_size
        or item.tick_size != policy.evidence.tick_size
        or item.depth_unit != policy.evidence.depth_unit
        or item.verified_at.date() != snapshot.timestamp_ist.date()
        or item.valid_until
        != datetime.combine(item.verified_at.date(), spec.market_session.end, item.verified_at.tzinfo)
    ):
        raise RenewalError("CONTRACT_RENEWAL_EVIDENCE_INCOMPATIBLE")
    updated = NakedPaperPolicy.model_validate({**policy.model_dump(), "evidence": item})
    failure = updated.metadata_reason(snapshot, known_at)
    if failure:
        raise RenewalError(failure)
    return updated
