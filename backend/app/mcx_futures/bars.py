"""Causal 1-minute bars from ~15-second quote observations, and N-minute resampling.

Rules: observations must be strictly increasing in time (duplicates and
out-of-order ones are rejected with a reason); a bar is complete only when an
observation from a later minute arrives or the session is flushed; missing
minutes are reported as gaps and are never filled with fabricated bars.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from math import isfinite
from typing import Any

from app.mcx_futures.spec import IST


@dataclass(frozen=True)
class Observation:
    at: datetime
    ltp: float
    bid: float | None
    ask: float | None
    bid_qty: float | None = None
    ask_qty: float | None = None
    cum_volume: float | None = None
    oi: float | None = None
    source_ts: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["at"] = self.at.isoformat()
        payload["source_ts"] = self.source_ts.isoformat() if self.source_ts else None
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Observation:
        data = dict(payload)
        data["at"] = datetime.fromisoformat(data["at"])
        data["source_ts"] = datetime.fromisoformat(data["source_ts"]) if data.get("source_ts") else None
        return cls(**data)

    def quote_age_seconds(self) -> float | None:
        if self.source_ts is None:
            return None
        return (self.at - self.source_ts).total_seconds()


def _finite_positive(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) and value > 0


def validate_observation(obs: Observation) -> str | None:
    """Content validation; None when usable."""
    if obs.at.tzinfo is None or obs.at.utcoffset() is None:
        return "NAIVE_TIMESTAMP"
    if obs.source_ts is not None and (obs.source_ts.tzinfo is None or obs.source_ts.utcoffset() is None):
        return "NAIVE_SOURCE_TIMESTAMP"
    if not _finite_positive(obs.ltp):
        return "INVALID_LTP"
    for name in ("bid", "ask"):
        value = getattr(obs, name)
        if value is not None and not _finite_positive(value):
            return f"INVALID_{name.upper()}"
    if obs.bid is not None and obs.ask is not None and obs.bid > obs.ask:
        return "CROSSED_QUOTE"
    if obs.cum_volume is not None and (not isinstance(obs.cum_volume, (int, float)) or obs.cum_volume < 0):
        return "INVALID_CUM_VOLUME"
    return None


def minute_floor(at: datetime) -> datetime:
    local = at.astimezone(IST)
    return local.replace(second=0, microsecond=0)


def session_id_of(at: datetime) -> str:
    return at.astimezone(IST).date().isoformat()


def bar_dict(start: datetime, minutes: int, o: float, h: float, low: float, c: float, *, bid: float | None,
             ask: float | None, volume: float | None, observations: int, missing_minutes: int = 0) -> dict[str, Any]:
    return {
        "start": start.isoformat(),
        "end": (start + timedelta(minutes=minutes)).isoformat(),
        "minutes": minutes,
        "session_id": session_id_of(start),
        "open": o,
        "high": h,
        "low": low,
        "close": c,
        "bid": bid,
        "ask": ask,
        "volume": volume,
        "observations": observations,
        "missing_minutes": missing_minutes,
    }


@dataclass
class BuildResult:
    accepted: bool
    reason: str | None
    completed: list[dict[str, Any]]
    gaps: list[dict[str, Any]]


class MinuteBarBuilder:
    """Incremental, restart-safe 1-minute bar builder (state is a JSON-safe dict)."""

    def __init__(self, state: dict[str, Any] | None = None) -> None:
        self.state: dict[str, Any] = state if state is not None else {
            "last_at": None, "current": None, "last_cum_volume": None, "volume_session": None,
            "last_completed_start": None,
        }

    def to_dict(self) -> dict[str, Any]:
        return self.state

    def add(self, obs: Observation) -> BuildResult:
        reason = validate_observation(obs)
        if reason:
            return BuildResult(False, reason, [], [])
        last = self.state["last_at"]
        if last is not None:
            previous = datetime.fromisoformat(last)
            if obs.at == previous:
                return BuildResult(False, "DUPLICATE_OBSERVATION", [], [])
            if obs.at < previous:
                return BuildResult(False, "OUT_OF_ORDER_OBSERVATION", [], [])
        self.state["last_at"] = obs.at.isoformat()
        minute = minute_floor(obs.at)
        session = session_id_of(obs.at)
        completed: list[dict[str, Any]] = []
        gaps: list[dict[str, Any]] = []
        current = self.state["current"]
        if current is not None and current["start"] != minute.isoformat():
            completed.append(self._finish(current))
            current = None
        reference = self.state["last_completed_start"]
        if current is None and reference is not None:
            ref = datetime.fromisoformat(reference)
            missing = int((minute - ref).total_seconds() // 60) - 1
            if missing > 0 and session_id_of(ref) == session:
                gaps.append({"from": (ref + timedelta(minutes=1)).isoformat(), "to": minute.isoformat(),
                             "missing_minutes": missing})
        volume_delta = self._volume_delta(obs, session)
        if current is None:
            current = {
                "start": minute.isoformat(), "open": obs.ltp, "high": obs.ltp, "low": obs.ltp, "close": obs.ltp,
                "bid": obs.bid, "ask": obs.ask, "volume": volume_delta, "observations": 1,
                "missing_minutes": gaps[0]["missing_minutes"] if gaps else 0,
            }
        else:
            current.update(high=max(current["high"], obs.ltp), low=min(current["low"], obs.ltp), close=obs.ltp,
                           bid=obs.bid, ask=obs.ask, observations=current["observations"] + 1)
            current["volume"] = (None if current["volume"] is None or volume_delta is None
                                 else current["volume"] + volume_delta)
        self.state["current"] = current
        return BuildResult(True, None, completed, gaps)

    def _volume_delta(self, obs: Observation, session: str) -> float | None:
        previous = self.state["last_cum_volume"]
        same_session = self.state["volume_session"] == session
        self.state["last_cum_volume"] = obs.cum_volume
        self.state["volume_session"] = session
        if obs.cum_volume is None:
            return None
        if not same_session or previous is None:
            # First observation of the session is only a baseline: volume traded
            # before it is unknown and is not attributed to any bar.
            return 0.0
        if obs.cum_volume < previous:
            return None
        return float(obs.cum_volume - previous)

    def _finish(self, current: dict[str, Any]) -> dict[str, Any]:
        self.state["current"] = None
        self.state["last_completed_start"] = current["start"]
        start = datetime.fromisoformat(current["start"])
        return bar_dict(start, 1, current["open"], current["high"], current["low"], current["close"],
                        bid=current["bid"], ask=current["ask"], volume=current["volume"],
                        observations=current["observations"], missing_minutes=current["missing_minutes"])

    def flush(self) -> list[dict[str, Any]]:
        """Session end: the in-progress bar is complete."""
        current = self.state["current"]
        return [self._finish(current)] if current is not None else []


def bucket_start(start: datetime, minutes: int) -> datetime:
    local = start.astimezone(IST)
    minute_of_day = local.hour * 60 + local.minute
    floored = minute_of_day - minute_of_day % minutes
    return local.replace(hour=floored // 60, minute=floored % 60, second=0, microsecond=0)


def _merge(bucket: datetime, minutes: int, bars: list[dict[str, Any]]) -> dict[str, Any]:
    volumes = [bar["volume"] for bar in bars]
    present = {bar["start"] for bar in bars}
    expected = [(bucket + timedelta(minutes=i)).isoformat() for i in range(minutes)]
    missing = sum(1 for item in expected if item not in present)
    return bar_dict(
        bucket, minutes, bars[0]["open"], max(bar["high"] for bar in bars), min(bar["low"] for bar in bars),
        bars[-1]["close"], bid=bars[-1]["bid"], ask=bars[-1]["ask"],
        volume=None if any(v is None for v in volumes) else float(sum(volumes)),
        observations=sum(bar["observations"] for bar in bars), missing_minutes=missing,
    )


class Resampler:
    """Completed 1-minute bars -> completed N-minute bars (clock-aligned buckets)."""

    def __init__(self, minutes: int, state: dict[str, Any] | None = None) -> None:
        if minutes < 1:
            raise ValueError("RESAMPLE_MINUTES_INVALID")
        self.minutes = minutes
        self.state: dict[str, Any] = state if state is not None else {"bucket": None, "bars": []}

    def to_dict(self) -> dict[str, Any]:
        return self.state

    def add(self, bar: dict[str, Any]) -> list[dict[str, Any]]:
        if bar["minutes"] != 1:
            raise ValueError("RESAMPLER_REQUIRES_1_MINUTE_BARS")
        bucket = bucket_start(datetime.fromisoformat(bar["start"]), self.minutes)
        out: list[dict[str, Any]] = []
        if self.state["bucket"] is not None and self.state["bucket"] != bucket.isoformat():
            out.extend(self.flush())
        self.state["bucket"] = bucket.isoformat()
        self.state["bars"].append(bar)
        last_minute = bucket + timedelta(minutes=self.minutes - 1)
        if datetime.fromisoformat(bar["start"]) >= last_minute:
            out.extend(self.flush())
        return out

    def flush(self) -> list[dict[str, Any]]:
        if self.state["bucket"] is None or not self.state["bars"]:
            self.state.update(bucket=None, bars=[])
            return []
        merged = _merge(datetime.fromisoformat(self.state["bucket"]), self.minutes, self.state["bars"])
        self.state.update(bucket=None, bars=[])
        return [merged]


def resample(bars: list[dict[str, Any]], minutes: int) -> list[dict[str, Any]]:
    """Batch resample of COMPLETED 1-minute bars; the trailing bucket is emitted only if full."""
    sampler = Resampler(minutes)
    out: list[dict[str, Any]] = []
    for bar in bars:
        out.extend(sampler.add(bar))
    return out
