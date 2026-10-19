"""Versioned executable liquidation books for the six Phase 15 PAPER strategies."""
from typing import Any

VERSION = "PHASE15_EXIT_BOOK_V1"
WIDE_SPREAD = "BID_ASK_SPREAD_TOO_WIDE"


def policy() -> dict[str, str]:
    return {"version": VERSION, "spread_limit": "ENTRY_ONLY",
            "wide_exit_spread": "EXECUTABLE_WITH_WARNING",
            "exit_prices": "EXACT_SHORT_ASK_MINUS_HEDGE_BID",
            "other_book_checks": "UNCHANGED"}


def warning(evidence: dict[str, Any]) -> str | None:
    return WIDE_SPREAD if any(evidence[leg]["validation"].get("wide_spread") is True
                              for leg in ("short", "long")) else None
