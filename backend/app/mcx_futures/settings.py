"""Deployment flags for ACTS_V3_MCX_FUTURES. PAPER is the only mode that exists: any other MCX_FUTURES_MODE value is
refused at start-up, and nothing in this package imports a broker order API."""
from __future__ import annotations

from dataclasses import dataclass
import os

from app.mcx_futures.models import INSTRUMENTS

TRUE = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class McxFuturesSettings:
    paper_enabled: bool
    trading_enabled: bool  # default TRUE: collect-only is an explicit opt-out (MCX_FUTURES_TRADING_ENABLED=false)
    instruments_enabled: dict[str, bool]
    mode: str
    kill_switch_file: str
    stale_after_seconds: int
    holidays: tuple[str, ...]

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "McxFuturesSettings":
        e = os.environ if env is None else env
        mode = e.get("MCX_FUTURES_MODE", "PAPER").strip().upper()
        return cls(paper_enabled=e.get("MCX_FUTURES_PAPER_ENABLED", "false").strip().lower() in TRUE,
                   trading_enabled=e.get("MCX_FUTURES_TRADING_ENABLED", "true").strip().lower() in TRUE,
                   instruments_enabled={i: e.get(f"MCX_FUTURES_{i}_ENABLED", "false").strip().lower() in TRUE
                                        for i in INSTRUMENTS},
                   mode=mode,
                   kill_switch_file=e.get("MCX_FUTURES_KILL_SWITCH_FILE", "/app/control/MCX_FUTURES_KILL"),
                   stale_after_seconds=int(e.get("MCX_FUTURES_STALE_AFTER_SECONDS", "60")),
                   holidays=tuple(d.strip() for d in e.get("MCX_FUTURES_HOLIDAYS", "").split(",") if d.strip()))

    def validate(self) -> None:
        if self.mode != "PAPER":
            raise ValueError("MCX_FUTURES_ONLY_PAPER_MODE_EXISTS")

    def enabled(self, instrument: str) -> bool:
        return self.paper_enabled and self.instruments_enabled.get(instrument, False)

    def entries_enabled(self, instrument: str) -> bool:
        return self.enabled(instrument) and self.trading_enabled

    def killed(self) -> bool:
        return os.path.exists(self.kill_switch_file)
