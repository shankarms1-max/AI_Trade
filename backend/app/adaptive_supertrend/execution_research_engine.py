"""In-memory research projections delegating fills/risk/exits to AstPaperService.

No session factory, database, repository, broker, worker or network is constructed.
The shadow strategies never switch policies or discard unresolved exposures.
"""

from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timedelta
import json
from math import ceil
from statistics import median
from types import SimpleNamespace

from app.adaptive_supertrend.config import AstPolicy
from app.adaptive_supertrend.execution_research_config import Construction, ExecutionSpec
from app.adaptive_supertrend.journal import ACTIVE
from app.adaptive_supertrend.models import AstTrade
from app.adaptive_supertrend.optimizer import metrics
from app.adaptive_supertrend.service import AstPaperService
from app.core.config import Settings
from app.paper import exit_book
from app.research.manifest import canonical, digest, json_value
from app.risk.event_checks import ConfiguredMarketEventProvider
from app.scalper.models import ScalperDirection, ScalperSignal, ScalperStrength
from app.scalper.strategy import build_candidates

CONTROL_REASONS = {
    "KILL_SWITCH_ACTIVE",
    "MARKET_EVENT_BLOCKED",
    "OUTSIDE_ENTRY_WINDOW",
    "MAX_TRADES_PER_DAY_REACHED",
    "HARD_DAILY_LOSS_REACHED",
    "MAX_CONSECUTIVE_LOSSES_REACHED",
    "ANY_EXIT_COOLDOWN_ACTIVE",
    "LOSS_COOLDOWN_ACTIVE",
    "ENTRY_TTL_EXCEEDED",
    "FORCED_CLOSE",
    "HARD_DAILY_LOSS_EXIT",
    "MARKET_EVENT_EXIT",
}


def distribution(values: list[float]) -> dict:
    ordered = sorted(values)
    return {
        "count": len(values),
        "min": ordered[0] if ordered else None,
        "median": median(ordered) if ordered else None,
        "p95": ordered[ceil(0.95 * len(ordered)) - 1] if ordered else None,
        "max": ordered[-1] if ordered else None,
    }


def option_metrics(documents: list[dict], days: list[str]) -> dict:
    # Reuse the existing statistics exactly, with actual gross RUPEES as inputs.
    # Do not relabel these as futures points or fabricate unsupported net results.
    values = [{"date": doc["exit_timestamp"][:10], "signal_points": doc["gross_pnl"]} for doc in documents]
    result = metrics(values, len(days), max(len(days), 1), days)
    fields = (
        "expectancy",
        "profit_factor",
        "profit_factor_status",
        "win_rate",
        "max_drawdown",
        "average_win",
        "average_loss",
        "payoff_ratio",
        "sharpe_equivalent",
        "consistency",
        "total_trades",
    )
    return {
        **{k: result[k] for k in fields},
        "unit": "INR_CLOSED_OBSERVED_OPTION_BOOKS",
        "gross_option_pnl": sum(doc["gross_pnl"] for doc in documents),
        "net_option_pnl": (
            sum(doc["net_pnl"] for doc in documents)
            if documents and all(doc.get("net_pnl") is not None for doc in documents)
            else None
        ),
    }


class ExecutionShadow(AstPaperService):
    def __init__(self, signal: AstPolicy, spec: ExecutionSpec, candidate: Construction):
        # Deliberately do not call the DB-backed initializer. Only unchanged pure
        # lifecycle methods _risk/_transition and the constructor/book validators
        # are used. _save writes research memory, never a runtime journal.
        self.policy = spec.combined(signal, candidate)
        self.settings = Settings(  # type: ignore[call-arg]
            _env_file=None, ast_config_json=self.policy.model_dump_json()
        )
        assert self.policy.execution is not None
        self.config = self.policy.execution.scalper(self.settings)
        self.costs = self.policy.cost_schedule()
        self.events = ConfiguredMarketEventProvider.from_json(self.policy.execution.market_events_json)
        self.name, self.candidate_id = candidate.name, spec.candidate_id(candidate)
        self.rows: list[AstTrade] = []
        self.cursor = SimpleNamespace(state_json="{}")
        self.days: dict[str, dict] = defaultdict(
            lambda: {
                "opportunities": set(),
                "first_available": set(),
                "ever_available": set(),
                "entry_rejects": Counter(),
                "construction_rejects": Counter(),
                "controls": Counter(),
                "fill_rejects": Counter(),
                "exits": Counter(),
            }
        )
        self.trace: list[dict] = []
        self.trace_head = "GENESIS"
        self.trace_count = 0
        self.entry_metadata: dict[str, dict] = {}
        self.lifecycle: dict[str, list[tuple[str, str]]] = defaultdict(list)

    def _save(self, session, cursor, raw, snapshot_id, row, doc, kind, **details):
        row.document = canonical(doc)
        self.trace_count += 1
        payload = {
            "sequence": self.trace_count,
            "previous": self.trace_head,
            "type": kind,
            "snapshot_line": snapshot_id,
            "timestamp": raw.captured_at.isoformat(),
            "trade_id": row.id,
            "state": row.state,
            "document_hash": digest(doc),
            **details,
        }
        self.trace_head = digest(payload)
        if kind != "MARK":
            self.trace.append({**payload, "hash": self.trace_head})
        day = self.days[str(raw.captured_at.date())]
        if kind in ("ENTRY", "ENTRY_REJECTED", "EXIT", "UNRESOLVED_EXPOSURE"):
            self.lifecycle[row.id].append((raw.captured_at.isoformat(), row.state))
        if kind == "ENTRY":
            self.entry_metadata[row.id] = {"response_at": raw.response_received_at.isoformat()}
        if kind == "ENTRY_REJECTED":
            reason = doc["rejection_reason"]
            day["fill_rejects"][reason] += 1
            if reason in CONTROL_REASONS:
                day["controls"][reason] += 1
        if kind == "EXIT":
            day["exits"][doc["exit_reason"]] += 1
            if doc["exit_reason"] in CONTROL_REASONS:
                day["controls"][doc["exit_reason"]] += 1

    def observe(self, raw, line: int, signal_decision: dict, parameters: dict | None, orb: dict) -> None:
        decision = deepcopy(signal_decision)
        at = raw.captured_at
        state = json.loads(self.cursor.state_json)
        if state.get("date") != str(at.date()):
            state = {"date": str(at.date()), "consumed": []}
        # Runtime opportunity identity includes the full combined frozen policy.
        if decision["opportunity_id"] is not None and parameters is not None:
            decision["opportunity_id"] = digest(
                {
                    "date": str(at.date()),
                    "direction": decision["direction"],
                    "range": orb,
                    "parameters": parameters,
                    "transition": decision["features"]["ast_transition"],
                    "future_identity": (raw.future_instrument_id, str(raw.future_expiry)),
                }
            )
            decision["features"]["opportunity_id"] = decision["opportunity_id"]
        if decision["opportunity_id"] in state["consumed"]:
            decision["blockers"].append("AST_OPPORTUNITY_CONSUMED")
            decision["status"] = "NO_SETUP"
        self.cursor.state_json = canonical(state)
        assert self.config is not None
        earliest = (
            at
            - timedelta(seconds=max(self.config.cooldown_after_exit_seconds, self.config.cooldown_after_loss_seconds))
        ).date()
        rows = [
            row
            for row in self.rows
            if row.state in ACTIVE
            or row.trading_date == at.date()
            or row.closed_date is not None
            and row.closed_date >= earliest
        ]
        blocked = any(e.block_entries for e in self.events.events_at(at))
        for row in rows:
            if row.state in ("OPEN", "PENDING_PAPER_ENTRY"):
                self._transition(None, self.cursor, raw, line, row, rows, decision, blocked)
        state = json.loads(self.cursor.state_json)
        if decision["opportunity_id"] in state.get("consumed", []):
            decision["blockers"].append("AST_OPPORTUNITY_CONSUMED")
        risk = self._risk(rows, at, blocked)
        decision["blockers"].extend(risk)
        day = self.days[str(at.date())]
        if signal_decision["status"] != "ENTER":
            return
        opportunity = signal_decision["opportunity_id"]
        first = opportunity not in day["opportunities"]
        day["opportunities"].add(opportunity)
        # Static availability ignores portfolio/control blockers. Stop checking a
        # sticky opportunity once availability is demonstrated, unless an actual
        # new pending entry needs construction again. No all-observation rate is claimed.
        actual_attempt = decision["status"] == "ENTER" and not decision["blockers"]
        built = None
        if opportunity not in day["ever_available"] or actual_attempt:
            signal = ScalperSignal(
                timestamp=at,
                direction=ScalperDirection(decision["direction"]),
                strength=ScalperStrength.NONE,
                score=0,
                components={},
                contradiction_penalty=0,
                reasons=["AST_ORB_REGIME_ALIGNED"],
                warnings=[],
                confirmation_count=0,
                confirmed=True,
            )
            built = build_candidates(raw, signal, self.config)
            day["construction_rejects"].update(built.rejection_counts)
            if built.candidates and raw.expiry >= at.date():
                day["ever_available"].add(opportunity)
                if first:
                    day["first_available"].add(opportunity)
        if not actual_attempt:
            day["entry_rejects"].update(set(decision["blockers"]))
            day["controls"].update(reason for reason in set(risk) if reason in CONTROL_REASONS)
            return
        assert built is not None
        reason = (
            "EXPIRED_CONTRACT_ENTRY_FORBIDDEN"
            if raw.expiry < at.date()
            else "AST_EXECUTABLE_SPREAD_UNAVAILABLE" if not built.candidates else None
        )
        if reason:
            day["entry_rejects"][reason] += 1
            return
        candidate = built.candidates[0]
        trade_id = digest(
            {
                "strategy_id": "AST_V1",
                "strategy_version": "AST_V1",
                "snapshot_id": line,
                "opportunity": decision["opportunity_id"],
            }
        )
        document = json_value(
            {
                "trade_id": trade_id,
                "strategy_id": "AST_V1",
                "strategy_version": "AST_V1",
                "mode": "PAPER",
                "decision_timestamp": at,
                "candidate": candidate,
                "strategy_features": decision["features"],
                "session_parameters": parameters,
                "opportunity_id": decision["opportunity_id"],
                "lot_size": raw.lot_size,
                "lots": 1,
                "future_instrument_id": raw.future_instrument_id,
                "future_expiry": raw.future_expiry,
                "policy": self.policy.model_dump(mode="json"),
                "policy_hash": self.policy.policy_hash,
                "exit_book_policy": exit_book.policy(),
            }
        )
        row = AstTrade(
            id=trade_id,
            decision_snapshot_id=line,
            trading_date=at.date(),
            closed_date=None,
            state="PENDING_PAPER_ENTRY",
            document=canonical(document),
        )
        self.rows.append(row)
        self.lifecycle[row.id].append((at.isoformat(), row.state))
        self._save(None, self.cursor, raw, line, row, document, "PENDING_PAPER_ENTRY")

    def summary(self, days: list[str], cutoff: datetime) -> dict:
        # Counters are completed-session aggregates, never mid-session forecasts.
        if any(day >= str(cutoff.date()) for day in days):
            raise ValueError("AST_EXECUTION_RESEARCH_COMPLETED_SESSION_SUMMARY_REQUIRED")
        selected_days = set(days)
        docs = [json.loads(row.document) for row in self.rows]
        # Only facts already known at cutoff can enter validation ranking.
        entries = [
            d
            for d in docs
            if d.get("entry_timestamp", "")[:10] in selected_days
            and datetime.fromisoformat(d["entry_timestamp"]) < cutoff
        ]
        cohort_closed = [
            d for d in entries if d.get("exit_timestamp") and datetime.fromisoformat(d["exit_timestamp"]) < cutoff
        ]
        closed = sorted(
            [
                d
                for d in docs
                if d.get("exit_timestamp", "")[:10] in selected_days
                and datetime.fromisoformat(d["exit_timestamp"]) < cutoff
            ],
            key=lambda d: d["exit_timestamp"],
        )
        states = {
            row.id: next(
                (state for at, state in reversed(self.lifecycle[row.id]) if datetime.fromisoformat(at) < cutoff), None
            )
            for row in self.rows
        }
        counts = Counter(states.values())
        counters: dict[str, Counter] = {
            key: Counter() for key in ("entry_rejects", "construction_rejects", "controls", "fill_rejects", "exits")
        }
        for day in days:
            for key in counters:
                counters[key].update(self.days[day][key])
        samples: dict[str, list[float]] = defaultdict(list)
        widths: Counter[str] = Counter()
        for doc in entries:
            candidate = doc["candidate"]
            widths[str(candidate["spread_width"])] += 1
            for key, value in (
                ("short_distance", candidate["short_distance_points"]),
                ("entry_credit", doc["entry_credit"]),
                ("credit_to_width", doc["entry_credit"] / candidate["spread_width"]),
                ("defined_max_risk", doc["defined_max_risk"]),
            ):
                samples[key].append(value)
            response = datetime.fromisoformat(self.entry_metadata[doc["trade_id"]]["response_at"])
            for leg in ("short", "long"):
                evidence = doc["entry_quotes"][leg]
                samples["bid_ask_spread_pct"].append(evidence["validation"]["spread_pct"])
                quote = evidence["quote"]
                samples["quote_age_seconds"].append(
                    (response - datetime.fromisoformat(quote["source_market_timestamp"])).total_seconds()
                )
                for key in ("open_interest", "volume"):
                    if quote[key] is not None:
                        samples[key].append(quote[key])
        return {
            "session_dates": days,
            "as_of_exclusive": cutoff.isoformat(),
            "ast_opportunities": sum(len(self.days[d]["opportunities"]) for d in days),
            "opportunities_with_any_executable_candidate": sum(len(self.days[d]["ever_available"]) for d in days),
            "opportunities_executable_at_first_observation": sum(len(self.days[d]["first_available"]) for d in days),
            "execution_filters_removed_opportunities": sum(
                len(self.days[d]["opportunities"]) - len(self.days[d]["ever_available"]) for d in days
            ),
            "decision_to_fill_successes": len(entries),
            "actual_trades_opened": len(entries),
            "actual_closed_trades": len(closed),
            "unresolved_exposure": counts["UNRESOLVED_EXPOSURE"],
            "entry_cohort_closed_trades": len(cohort_closed),
            "entry_cohort_unpriced_at_cutoff": len(entries) - len(cohort_closed),
            "open_at_cutoff": counts["OPEN"],
            "pending_at_cutoff": counts["PENDING_PAPER_ENTRY"],
            "active_exposure_at_cutoff": sum(counts[s] for s in ACTIVE),
            "width_usage": dict(sorted(widths.items())),
            "distributions": {
                key: distribution(samples[key])
                for key in (
                    "short_distance",
                    "entry_credit",
                    "credit_to_width",
                    "defined_max_risk",
                    "bid_ask_spread_pct",
                    "quote_age_seconds",
                    "open_interest",
                    "volume",
                )
            },
            "entry_reject_counts": dict(sorted(counters["entry_rejects"].items())),
            "construction_rejection_counts": dict(sorted(counters["construction_rejects"].items())),
            "research_control_block_counts": dict(sorted(counters["controls"].items())),
            "fill_rejection_counts": dict(sorted(counters["fill_rejects"].items())),
            "exit_reason_counts": dict(sorted(counters["exits"].items())),
            "closed_option_metrics": option_metrics(closed, days),
            "entry_cohort_closed_metrics": option_metrics(cohort_closed, days),
        }
