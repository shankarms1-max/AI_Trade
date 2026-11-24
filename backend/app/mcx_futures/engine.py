"""ACTS_V3_MCX_FUTURES paper engine: one instance per instrument, no shared mutable state.

``on_observation`` is the only input.  It returns JSON-safe journal events that
form a hash chain (``prev_hash`` -> ``hash``) for integrity.  Decisions are made
only on COMPLETED feature bars; entries and signal exits fill only on a LATER
observation; protective stops, forced exits and holding limits are checked on
every fresh observation.  ``snapshot``/``restore`` make the engine restart-safe:
an observation at or before the last processed one is rejected (no duplicate
execution), and restore + replay of the same observations reproduces the same
events byte for byte.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
from typing import Any

from app.mcx_futures import cycle, features, paper, risk, score
from app.mcx_futures.bars import MinuteBarBuilder, Observation, Resampler, validate_observation
from app.mcx_futures.config import STRATEGY_ID, InstrumentConfig
from app.mcx_futures.costs import CostSchedule
from app.mcx_futures.session import OPEN, SessionCalendar, SessionDay
from app.mcx_futures.spec import ContractSpec

SCHEMA = "ACTS_V3_MCX_FUTURES_ENGINE_STATE_V1"
EVENT_TYPES = (
    "OBSERVATION", "OBSERVATION_REJECTED", "BAR", "DECISION", "ENTRY_PENDING", "ENTRY_FILLED", "ENTRY_REJECTED", "MARK",
    "MARK_UNAVAILABLE", "EXIT", "CYCLE", "SESSION", "RISK_BLOCK",
)
GENESIS = "0" * 64


class McxFuturesEngine:
    def __init__(self, config: InstrumentConfig, spec: ContractSpec, costs: CostSchedule,
                 state: dict[str, Any] | None = None, calendar: SessionCalendar | None = None) -> None:
        if config.instrument != spec.instrument:
            raise ValueError("CONFIG_SPEC_INSTRUMENT_MISMATCH")
        self.config = config
        self.spec = spec
        self.costs = costs
        self.calendar = calendar or SessionCalendar()
        if state is None:
            state = self._fresh_state()
        else:
            state = deepcopy(state)
            self._check_identity(state)
        self.s = state
        self._builder = MinuteBarBuilder(self.s["builder"])
        self._resampler = Resampler(config.feature_timeframe_minutes, self.s["resampler"])

    # -- persistence -------------------------------------------------------------------------------------------
    def _identity(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "strategy_id": STRATEGY_ID,
            "instrument": self.config.instrument,
            "config_hash": self.config.config_hash,
            "spec": self.spec.to_dict(),
            "cost_schedule": self.costs.version,
            "calendar_source": self.calendar.source,
        }

    def _fresh_state(self) -> dict[str, Any]:
        return {
            **self._identity(),
            "seq": 0,
            "last_hash": GENESIS,
            "last_at": None,
            "last_fresh_at": None,
            "last_ltp": None,
            "last_atr": None,
            "session": None,
            "builder": MinuteBarBuilder().to_dict(),
            "resampler": Resampler(self.config.feature_timeframe_minutes).to_dict(),
            "bars": [],
            "cycle": None,
            "risk": None,
            "position": None,
            "pending_entry": None,
            "counters": {"observations": 0, "rejected": 0, "decisions": 0, "entries_filled": 0,
                         "entries_rejected": 0, "exits": 0},
        }

    def _check_identity(self, state: dict[str, Any]) -> None:
        expected = self._identity()
        for key, value in expected.items():
            if state.get(key) != value:
                raise ValueError(f"SNAPSHOT_IDENTITY_MISMATCH:{key}")

    def snapshot(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.s))

    @classmethod
    def restore(cls, config: InstrumentConfig, spec: ContractSpec, costs: CostSchedule, state: dict[str, Any],
                calendar: SessionCalendar | None = None) -> McxFuturesEngine:
        return cls(config, spec, costs, state=state, calendar=calendar)

    @property
    def position(self) -> dict[str, Any] | None:
        return self.s["position"]

    # -- journal -------------------------------------------------------------------------------------------------
    def _emit(self, out: list[dict[str, Any]], kind: str, at: datetime, payload: dict[str, Any]) -> None:
        if kind not in EVENT_TYPES:
            raise ValueError(f"UNKNOWN_EVENT_TYPE:{kind}")
        self.s["seq"] += 1
        body = {
            "seq": self.s["seq"],
            "type": kind,
            "at": at.isoformat(),
            "strategy_id": STRATEGY_ID,
            "instrument": self.config.instrument,
            "trading_symbol": self.spec.trading_symbol,
            "config_id": self.config.config_id,
            "payload": payload,
            "prev_hash": self.s["last_hash"],
        }
        blob = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False)
        event = json.loads(blob)
        event["hash"] = hashlib.sha256(blob.encode()).hexdigest()
        self.s["last_hash"] = event["hash"]
        out.append(event)

    # -- main entry point ----------------------------------------------------------------------------------------
    def on_observation(self, obs: Observation, *, allow_entries: bool = True) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        problem = validate_observation(obs)
        if problem:
            self.s["counters"]["rejected"] += 1
            self._emit(out, "OBSERVATION_REJECTED", obs.at, {"reason": problem})
            return out
        last = self.s["last_at"]
        if last is not None and obs.at <= datetime.fromisoformat(last):
            self.s["counters"]["rejected"] += 1
            reason = "DUPLICATE_OBSERVATION" if obs.at == datetime.fromisoformat(last) else "OUT_OF_ORDER_OBSERVATION"
            self._emit(out, "OBSERVATION_REJECTED", obs.at, {"reason": reason, "last_processed_at": last})
            return out
        self.s["last_at"] = obs.at.isoformat()
        state = self.calendar.state(obs.at)
        if state.status != OPEN or state.session is None:
            self._close_open_bars(out, obs.at)
            self.s["counters"]["rejected"] += 1
            pending = self.s["pending_entry"]
            if pending is not None:
                self._release(out, obs, pending, "SESSION_CLOSED")
            if self.s["position"]:
                self._emit(out, "MARK_UNAVAILABLE", obs.at, {"reason": "SESSION_CLOSED"})
            self._emit(out, "OBSERVATION_REJECTED", obs.at, {
                "reason": "OUTSIDE_SESSION", "session_status": state.status,
                "next_open": state.next_open.isoformat() if state.next_open else None})
            return out
        new_session = self.s["session"] is None or self.s["session"]["session_id"] != state.session.session_id
        if new_session:
            self._roll_session(out, state.session, obs.at)
        self.s["counters"]["observations"] += 1
        self._emit(out, "OBSERVATION", obs.at, {
            "quote": obs.to_dict(), "contract_token": self.spec.token,
            "contract_expiry": self.spec.expiry.isoformat(),
            "lot_size": self.spec.lot_size, "tick_size": self.spec.tick_size,
        })
        built = self._builder.add(obs)
        if not built.accepted:  # pragma: no cover - guarded above
            self._emit(out, "OBSERVATION_REJECTED", obs.at, {"reason": built.reason})
            return out
        fresh_problem = paper.quote_problem(obs, self.config, self.spec)
        held = self.s["position"]
        if fresh_problem is None and held:
            fresh_problem = paper.depth_problem(obs, held["direction"], held["lots"], self.spec,
                                                exit_side=True)
        fresh = fresh_problem is None
        if new_session or built.gaps:
            self._gap_guard(out, obs, built.gaps)
        just_filled = self._resolve_pending_entry(out, obs, allow_entries)
        if just_filled and self.s["position"]:
            fresh_problem = paper.depth_problem(obs, self.s["position"]["direction"],
                                                self.s["position"]["lots"], self.spec, exit_side=True)
            fresh = fresh_problem is None
            if fresh_problem:
                self._emit(out, "MARK_UNAVAILABLE", obs.at, {"reason": fresh_problem,
                                                              "stop": self.s["position"]["stop"]})
        if self.s["position"] and not just_filled:
            self._manage_position(out, obs, fresh_problem)
        completed: list[dict[str, Any]] = []
        for bar in built.completed:
            self._emit(out, "BAR", obs.at, {"timeframe_minutes": 1, "bar": bar})
            completed.extend(self._resampler.add(bar))
        for bar in completed:
            self._append_bar(out, obs.at, bar)
        if completed:
            self._decide(out, obs, completed[-1], allow_entries)
        if fresh:
            self.s["last_fresh_at"] = obs.at.isoformat()
        self.s["last_ltp"] = obs.ltp
        if self.s["position"] and fresh:
            self._mark(out, obs)
        return out

    # -- session ---------------------------------------------------------------------------------------------------
    def _roll_session(self, out: list[dict[str, Any]], day: SessionDay, at: datetime) -> None:
        self._close_open_bars(out, at)
        last_entry, forced = day.cutoffs(self.config.last_entry_minutes_before_close,
                                         self.config.forced_exit_minutes_before_close)
        previous = self.s["session"]
        self.s["session"] = {**day.to_dict(), "last_entry_at": last_entry.isoformat(),
                             "forced_exit_at": forced.isoformat()}
        self.s["cycle"] = cycle.ensure_session(self.s["cycle"], day.session_id)
        self.s["risk"] = risk.ensure_session(self.s["risk"], day.session_id)
        carried = self.s["position"]
        if carried:
            carried["exit_pending"] = "SESSION_FORCED_EXIT_LATE"
        self._emit(out, "SESSION", at, {
            "event": "SESSION_START", "session": self.s["session"],
            "previous_session_id": previous["session_id"] if previous else None,
            "days_to_expiry": self.spec.days_to_expiry(day.open_at.date()),
            "position_carried": bool(carried),
        })

    def _close_open_bars(self, out: list[dict[str, Any]], at: datetime) -> None:
        if self.s["session"] is None:
            return
        bars = self._builder.flush()
        completed: list[dict[str, Any]] = []
        for bar in bars:
            self._emit(out, "BAR", at, {"timeframe_minutes": 1, "bar": bar, "closed_by": "SESSION_END"})
            completed.extend(self._resampler.add(bar))
        completed.extend(self._resampler.flush())
        for bar in completed:
            self._append_bar(out, at, bar)

    def _append_bar(self, out: list[dict[str, Any]], at: datetime, bar: dict[str, Any]) -> None:
        self.s["bars"].append(bar)
        del self.s["bars"][:-self.config.feature_window_bars]
        self._emit(out, "BAR", at, {"timeframe_minutes": bar["minutes"], "bar": bar})

    def _gap_guard(self, out: list[dict[str, Any]], obs: Observation, gaps: list[dict[str, Any]]) -> None:
        evidence = risk.check_gap(self.s["risk"], obs.at, obs.ltp, self.s["last_ltp"], self.s["last_atr"],
                                  self.config)
        if evidence:
            self._emit(out, "RISK_BLOCK", obs.at, {"rule": "GAP_GUARD", "evidence": evidence, "gaps": gaps})
        elif gaps:
            self._emit(out, "RISK_BLOCK", obs.at, {"rule": "DATA_GAP_OBSERVED", "evidence": None, "gaps": gaps})

    # -- execution -----------------------------------------------------------------------------------------------
    def _resolve_pending_entry(self, out: list[dict[str, Any]], obs: Observation, allow_entries: bool) -> bool:
        pending = self.s["pending_entry"]
        if pending is None:
            return False
        self.s["pending_entry"] = None
        session = self.s["session"]
        extra: list[str] = []
        if not allow_entries:
            extra.append("PAPER_EXECUTION_DISABLED")
        if session["session_id"] != pending["session_id"]:
            extra.append("SESSION_CHANGED")
        if obs.at >= datetime.fromisoformat(session["last_entry_at"]):
            extra.append("AFTER_LAST_ENTRY_TIME")
        if self.config.blackout(obs.at):
            extra.append("EVENT_BLACKOUT")
        price, reason = paper.entry_fill(pending, obs, self.config, self.spec, extra)
        if price is None:
            self._release(out, obs, pending, str(reason))
            return False
        position = paper.open_position(pending, obs, price, self.config, self.spec)
        risk_rupees = abs(position["entry_price"] - position["stop"]) * self.spec.lot_size * position["lots"]
        if risk_rupees > self.config.max_risk_per_trade:
            self._release(out, obs, pending, "STOP_RISK_EXCEEDS_MAX_AT_FILL")
            return False
        self.s["position"] = position
        self.s["counters"]["entries_filled"] += 1
        risk.record_entry(self.s["risk"])
        audit = cycle.filled(self.s["cycle"], obs.at, position["direction"])
        self._emit(out, "ENTRY_FILLED", obs.at, {
            "direction": position["direction"], "price": position["entry_price"], "lots": position["lots"],
            "lot_size": self.spec.lot_size, "stop": position["stop"], "stop_points": position["stop_points"],
            "risk_rupees": round(risk_rupees, 4), "decided_at": pending["decided_at"], "bid": obs.bid,
            "ask": obs.ask, "entry_reason": position["entry_reason"], "cycle_id": position["cycle_id"]})
        self._emit(out, "CYCLE", obs.at, {"event": "CONSUMED_ON_FILL", "cycle": audit})
        return True

    def _release(self, out: list[dict[str, Any]], obs: Observation, pending: dict[str, Any], reason: str) -> None:
        """Unfilled entry: journal the exact reason and release the reservation WITHOUT consuming."""
        self.s["pending_entry"] = None
        self.s["counters"]["entries_rejected"] += 1
        audit = cycle.released(self.s["cycle"], obs.at)
        self._emit(out, "ENTRY_REJECTED", obs.at, {
            "reason": reason, "direction": pending["direction"], "decided_at": pending["decided_at"],
            "bid": obs.bid, "ask": obs.ask, "quote_age_seconds": obs.quote_age_seconds(), "consumed": False})
        self._emit(out, "CYCLE", obs.at, {"event": "RESERVATION_RELEASED", "cycle": audit})

    def _manage_position(self, out: list[dict[str, Any]], obs: Observation, fresh_problem: str | None) -> None:
        position = self.s["position"]
        if fresh_problem:
            position["stale_since"] = position["stale_since"] or obs.at.isoformat()
            self._emit(out, "MARK_UNAVAILABLE", obs.at, {
                "reason": fresh_problem, "stop": position["stop"],
                "stale_since": position["stale_since"]})
            return
        position["stale_since"] = None
        session = self.s["session"]
        fill = paper.stop_fill(position, obs, self.config, self.spec)
        if fill is not None:
            moved = position["stop"] != position["initial_stop"]
            self._exit(out, obs, fill, "TRAILING_STOP" if position["trail_active"] and moved else "STOP")
            return
        price = paper.executable(position, obs)
        assert price is not None
        last_fresh = self.s["last_fresh_at"]
        if position["exit_pending"]:
            self._exit(out, obs, price, position["exit_pending"])
        elif obs.at >= datetime.fromisoformat(session["forced_exit_at"]):
            self._exit(out, obs, price, "SESSION_FORCED_EXIT")
        elif last_fresh and (obs.at - datetime.fromisoformat(last_fresh)).total_seconds() >= \
                self.config.stale_exit_seconds:
            self._exit(out, obs, price, "STALE_FEED_EXIT")
        elif obs.at - datetime.fromisoformat(position["entry_at"]) >= timedelta(
                minutes=self.config.max_holding_minutes):
            self._exit(out, obs, price, "MAX_HOLDING_TIME")

    def _exit(self, out: list[dict[str, Any]], obs: Observation, price: float, reason: str) -> None:
        position = self.s["position"]
        trade = paper.close(position, obs.at, price, reason, self.costs, self.spec)
        trade["observed_bid"], trade["observed_ask"] = obs.bid, obs.ask
        trade["stop_at_exit"] = position["stop"]
        self.s["position"] = None
        self.s["counters"]["exits"] += 1
        risk.record_exit(self.s["risk"], obs.at, trade["net_pnl"])
        audit = cycle.closed(self.s["cycle"], obs.at, trade["direction"], reason)
        self._emit(out, "EXIT", obs.at, {"trade": trade, "risk": deepcopy(self.s["risk"])})
        self._emit(out, "CYCLE", obs.at, {"event": "CLOSED", "cycle": audit})

    def _mark(self, out: list[dict[str, Any]], obs: Observation) -> None:
        position = self.s["position"]
        price = paper.executable(position, obs)
        assert price is not None
        paper.update_excursions(position, price)
        gross = (price - position["entry_price"]) * position["sign"] * self.spec.lot_size * position["lots"]
        self._emit(out, "MARK", obs.at, {
            "price": price, "unrealised_gross": round(gross, 6), "stop": position["stop"],
            "mae_points": position["mae_points"], "mfe_points": position["mfe_points"]})

    # -- decisions -----------------------------------------------------------------------------------------------
    def _decide(self, out: list[dict[str, Any]], obs: Observation, bar: dict[str, Any],
                allow_entries: bool) -> None:
        cfg = self.config
        session = self.s["session"]
        bar_end = datetime.fromisoformat(bar["end"])
        lateness = (obs.at - bar_end).total_seconds()
        if bar["session_id"] != session["session_id"] or lateness > cfg.bar_seconds:
            self._emit(out, "DECISION", obs.at, {"status": "SKIPPED", "reason": "BAR_COMPLETED_LATE_OR_PRIOR_SESSION",
                                                 "bar_start": bar["start"], "lateness_seconds": lateness})
            return
        self.s["counters"]["decisions"] += 1
        quote = {"bid": obs.bid, "ask": obs.ask,
                 "source_ts": obs.source_ts.isoformat() if obs.source_ts else None}
        feats = features.compute(self.s["bars"], cfg, quote, obs.at, self.spec.tick_size)
        atr = feats["atr"]["value"]
        if atr is not None:
            self.s["last_atr"] = atr
        blackout = cfg.blackout(obs.at)
        facts = {
            "blackout": blackout,
            "in_entry_window": obs.at < datetime.fromisoformat(session["last_entry_at"]),
            "days_to_expiry": self.spec.days_to_expiry(bar_end.date()),
            "bar_missing_minutes": bar["missing_minutes"],
        }
        evaluation = score.evaluate(feats, facts, cfg)
        position = self.s["position"]
        risk_blockers: list[str] = []
        if position is None and self.s["pending_entry"] is None:
            risk_blockers = risk.entry_blockers(
                self.s["risk"], obs.at, atr=atr, cfg=cfg, spec=self.spec, session=session, position_open=False,
                quote_age=obs.quote_age_seconds())
        blockers = list(dict.fromkeys(evaluation["vetoes"] + risk_blockers))
        if not allow_entries:
            blockers.append("PAPER_EXECUTION_DISABLED")
        before = cycle.audit(self.s["cycle"])
        result = cycle.decide(self.s["cycle"], obs.at, evaluation, blockers, cfg)
        exit_signal = None
        if position is not None:
            streak = self.s["cycle"].get("signal_streak") or {}
            if (evaluation["direction"] == score.opposite(position["direction"])
                    and evaluation["score"] >= cfg.opposite_exit_score_floor
                    and streak.get("direction") == evaluation["direction"]
                    and streak.get("count", 0) >= cfg.confirmation_bars
                    and not position["exit_pending"]):
                position["exit_pending"] = exit_signal = cycle.OPPOSITE_SIGNAL
            if paper.update_trailing(position, atr, cfg, self.spec):
                self._emit(out, "DECISION", obs.at, {"status": "TRAIL_STOP_MOVED", "stop": position["stop"],
                                                     "best_price": position["best_price"]})
        self._emit(out, "DECISION", obs.at, {
            "status": "ENTER" if result["enter"] else ("EXIT_SIGNAL" if exit_signal else "NO_ENTRY"),
            "bar_start": bar["start"],
            "timeframe_minutes": bar["minutes"],
            "direction": evaluation["direction"],
            "market_state": evaluation["market_state"],
            "score": evaluation["score"],
            "entry_score_floor": cfg.entry_score_floor,
            "components": evaluation["components"],
            "evidence": evaluation["evidence"],
            "vetoes": evaluation["vetoes"],
            "risk_blockers": risk_blockers,
            "blockers": result["blockers"],
            "entry_signal": result["entry_signal"],
            "blackout": blackout,
            "warm": evaluation["warm"],
            "cycle": result["cycle"],
        })
        if self._cycle_changed(before, result["cycle"]):
            self._emit(out, "CYCLE", obs.at, {"event": "DECISION_UPDATE", "cycle": result["cycle"]})
        if result["entry_signal"] and risk_blockers:
            self._emit(out, "RISK_BLOCK", obs.at, {"rule": "ENTRY_RISK", "blockers": risk_blockers,
                                                   "direction": evaluation["direction"]})
        if result["enter"]:
            stop_points = risk.stop_distance(float(atr), cfg, self.spec)
            pending = {
                "direction": evaluation["direction"],
                "decided_at": obs.at.isoformat(),
                "session_id": session["session_id"],
                "atr": atr,
                "stop_points": stop_points,
                "risk_rupees": risk.risk_rupees(stop_points, cfg, self.spec),
                "score": evaluation["score"],
                "entry_reason": result["entry_reason"],
                "cycle_id": result["cycle"]["cycle_id"],
                "entry_evidence": {
                    "efficiency_ratio": evaluation["evidence"].get("efficiency_ratio"),
                    "atr_percentile": evaluation["evidence"].get("atr_percentile"),
                    "atr": atr,
                    "market_state": evaluation["market_state"],
                },
            }
            self.s["pending_entry"] = pending
            self._emit(out, "ENTRY_PENDING", obs.at, pending)

    @staticmethod
    def _cycle_changed(before: dict[str, Any], after: dict[str, Any]) -> bool:
        ignore = {"signal_streak", "reversal_confirmation_seconds"}
        return any(before.get(key) != after.get(key) for key in after if key not in ignore)
