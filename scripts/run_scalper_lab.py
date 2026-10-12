#!/usr/bin/env python3
"""Compare four paper strategies on one local read-only Phase 15 capture set."""
from __future__ import annotations

import argparse
from datetime import date
import json
import os
from pathlib import Path
import sys
from typing import Any

from pydantic import SecretStr

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import Settings  # noqa: E402
from app.scalper.config import ScalperConfig  # noqa: E402
from app.scalper.paper import cost_schedule  # noqa: E402
from app.scalper_lab.engine import ScalperLab  # noqa: E402
from app.scalper_lab.alpha_bridge import (load_external_alpha,  # noqa: E402
                                          same_db_diagnostics)
from app.scalper_lab.io import (load_futures_csv, load_local_sqlite,  # noqa: E402
                                load_postgresql, write_result)


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    database = parser.add_mutually_exclusive_group()
    database.add_argument("--database", type=Path,
                          help="local Phase 15 SQLite file; opened with mode=ro")
    database.add_argument("--database-url",
                          help="PostgreSQL URL; opened in a read-only transaction")
    parser.add_argument("--alpha-database-url",
                        help="optional separate PostgreSQL Alpha source; read-only")
    parser.add_argument("--futures-csv", type=Path,
                        help="optional broker futures quote export with real volume and VWAP")
    parser.add_argument("--output", required=True, type=Path,
                        help="new directory for immutable comparison artifacts")
    parser.add_argument("--start", type=_date)
    parser.add_argument("--end", type=_date)
    parser.add_argument("--research-start", type=_date)
    parser.add_argument("--research-end", type=_date)
    parser.add_argument("--validation-start", type=_date)
    parser.add_argument("--validation-end", type=_date)
    args = parser.parse_args(argv)
    database_url = args.database_url or (os.getenv("DATABASE_URL") if args.database is None
                                         else None)
    alpha_database_url = (args.alpha_database_url if args.alpha_database_url is not None
                          else os.getenv("ALPHA_RESEARCH_DATABASE_URL"))
    if args.database is None and database_url is None:
        parser.error("provide --database, --database-url, or DATABASE_URL")
    split = [args.research_start, args.research_end,
             args.validation_start, args.validation_end]
    if all(item is not None for item in split):
        if args.start or args.end or not (split[0] <= split[1] < split[2] <= split[3]):
            parser.error("research and validation must be chronological and disjoint")
        periods = [("RESEARCH", split[0], split[1]),
                   ("OUT_OF_SAMPLE_VALIDATION", split[2], split[3])]
    elif any(item is not None for item in split):
        parser.error("provide all four research/validation dates")
    elif args.start and args.end and args.start <= args.end:
        periods = [("DIRECT", args.start, args.end)]
    else:
        parser.error("provide --start/--end or all research/validation dates")

    futures = load_futures_csv(args.futures_csv)
    # Do not load .env.production or enable a worker. Settings supply only the
    # existing validated paper risk and execution defaults.
    settings = Settings(_env_file=None,  # type: ignore[call-arg]
                        kotak_consumer_key=SecretStr("offline-strategy-lab"))
    lab = ScalperLab(ScalperConfig.from_settings(settings), cost_schedule(settings))
    results = []
    args.output.mkdir(parents=True, exist_ok=False)
    for period, start, end in periods:
        alpha_meta: dict[str, Any] = {}
        if args.database is not None:
            observations = load_local_sqlite(args.database, start, end, futures,
                                             alpha_meta=alpha_meta)
        else:
            assert database_url is not None
            observations = load_postgresql(database_url, start, end, futures,
                                           alpha_meta=alpha_meta)
        if alpha_database_url is not None:
            observations, alpha_report = load_external_alpha(
                observations, alpha_database_url)
        else:
            alpha_report = same_db_diagnostics(observations, alpha_meta)
        result = lab.run(observations, period=period,
                         alpha_bridge=alpha_report if alpha_database_url is not None else None)
        directory = args.output / period.lower()
        write_result(result, directory)
        results.append({"period": period, "observations": len(observations),
                        "output": str(directory), "comparison": result.comparison,
                        "alpha_bridge": alpha_report})
    print(json.dumps({"periods": results, "profitability_claim": False}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
