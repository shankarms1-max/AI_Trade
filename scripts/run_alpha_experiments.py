"""Chronological read-only market replay; registry writes only, no broker/orders."""
import argparse
import hashlib
import json
from dataclasses import asdict
from datetime import time
from pathlib import Path
import sys

from sqlalchemy import select
from sqlalchemy.orm import selectinload

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.alpha.experiments import adjacent_robustness, bounded_parameter_grid  # noqa: E402
from app.alpha.models import ALPHA_VERSION, AlphaFeatureSnapshot  # noqa: E402
from app.ai.models import AI_VERSION, PROMPT_VERSION  # noqa: E402
from app.alpha.replay import ReplayCosts, ReplayRow, replay_parameters  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.db.models import (AlphaFeatureSnapshotRecord, MarketFeatureSnapshotRecord,
                           MarketSnapshotRecord, ResearchExperimentRecord)  # noqa: E402
from app.db.session import build_engine, build_session_factory  # noqa: E402
from app.features.models import FEATURE_VERSION, MarketFeatureSnapshot  # noqa: E402
from app.features.repository import raw_record_to_model  # noqa: E402
from app.regime.engine import config_from_settings as regime_config  # noqa: E402
from app.risk.event_checks import ConfiguredMarketEventProvider  # noqa: E402
from app.risk.service import config_from_settings as risk_config  # noqa: E402
from app.shadow.service import config_from_settings as shadow_config  # noqa: E402
from app.strategy.service import config_from_settings as strategy_config  # noqa: E402

WINDOWS = ((time(9, 35), time(13, 30)), (time(9, 45), time(13, 45)),
           (time(10, 0), time(14, 0)), (time(10, 15), time(14, 15)))


def load_rows(session, *, preserve_missing=False) -> list[ReplayRow]:
    raw = session.scalars(select(MarketSnapshotRecord).options(selectinload(MarketSnapshotRecord.options))
                          .order_by(MarketSnapshotRecord.timestamp_ist, MarketSnapshotRecord.id)).all()
    features = {item.market_snapshot_id: item for item in session.scalars(
        select(MarketFeatureSnapshotRecord).where(MarketFeatureSnapshotRecord.feature_version == FEATURE_VERSION)).all()}
    alpha_records = session.scalars(select(AlphaFeatureSnapshotRecord).where(
        AlphaFeatureSnapshotRecord.alpha_version == ALPHA_VERSION,
        AlphaFeatureSnapshotRecord.calculation_mode != "RESEARCH_RECOMPUTE")).all()
    alpha_by_id = {}
    for item in sorted(alpha_records, key=lambda x: x.calculation_mode != "LIVE_ORIGINAL"):
        alpha_by_id.setdefault(item.market_snapshot_id, item)
    return [ReplayRow(item.id, raw_record_to_model(item, normalize_research_timestamps=preserve_missing),
                      None if item.id not in features else MarketFeatureSnapshot.model_validate(features[item.id].feature_json),
                      None if item.id not in features else features[item.id].id,
                      None if item.id not in alpha_by_id else
                      AlphaFeatureSnapshot.model_validate(alpha_by_id[item.id].result_json))
            for item in raw if preserve_missing or item.id in features]


def split_periods(rows: list[ReplayRow]):
    days = sorted({item.snapshot.timestamp_ist.date() for item in rows})
    if len(days) < 3:
        return {}
    train_end = max(1, int(len(days) * .6))
    validation_end = max(train_end + 1, int(len(days) * .8))
    groups = {"TRAIN": days[:train_end], "VALIDATION": days[train_end:validation_end],
              "FINAL_TEST": days[validation_end:]}
    return {name: (min(row.snapshot.timestamp_ist for row in rows
                       if row.snapshot.timestamp_ist.date() in group),
                   max(row.snapshot.timestamp_ist for row in rows
                       if row.snapshot.timestamp_ist.date() in group))
            for name, group in groups.items() if group}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--thresholds", default="0.70,0.75,0.80,0.85,0.90")
    parser.add_argument("--confirmations", default="1,2,3")
    parser.add_argument("--spread-width-sets", default="50,100,150,200",
                        help="Semicolon-separated width sets, e.g. 50,100;100,150")
    parser.add_argument("--short-strike-buffers", default="100")
    parser.add_argument("--minimum-credit-to-widths", default="0.03")
    parser.add_argument("--volatility-distance-multipliers", default="1.0")
    parser.add_argument("--profit-targets", default="", help="Credit capture percentages; empty uses configured rule")
    parser.add_argument("--stop-multiples", default="", help="Credit multiples; empty uses configured rule")
    parser.add_argument("--force-exit-times", default="", help="HH:MM values; empty uses configured rule")
    parser.add_argument("--expected-move-minimums", default="")
    parser.add_argument("--carry-thresholds", default="")
    parser.add_argument("--directional-strengths", default="")
    parser.add_argument("--strategy-family-modes", default="")
    parser.add_argument("--dte-buckets", default="")
    parser.add_argument("--split", choices=("TRAIN", "VALIDATION", "FINAL_TEST"), default="TRAIN")
    parser.add_argument("--inspect-final-test", action="store_true")
    parser.add_argument("--brokerage-per-order", type=float, default=0)
    parser.add_argument("--exchange-rate", type=float, default=0)
    parser.add_argument("--stt-rate", type=float, default=0)
    parser.add_argument("--gst-rate", type=float, default=0)
    parser.add_argument("--stamp-rate", type=float, default=0)
    parser.add_argument("--slippage-points-per-leg", type=float, default=0)
    args = parser.parse_args()
    if args.split == "FINAL_TEST" and not args.inspect_final_test:
        raise SystemExit("FINAL_TEST is sealed; pass --inspect-final-test explicitly and registry will audit reuse")
    costs = ReplayCosts(args.brokerage_per_order, args.exchange_rate, args.stt_rate,
                        args.gst_rate, args.stamp_rate, args.slippage_points_per_leg)
    if any(value < 0 for value in costs.__dict__.values()):
        raise SystemExit("cost inputs must be nonnegative")
    settings = get_settings()
    if settings.phase14_2_strategy_logic_enabled:
        raise SystemExit("Phase14.2 evidence requires a frozen isolated run: use scripts/run_integrity_replay.py; legacy experiment axes are not authoritative")
    if not settings.database_url:
        raise SystemExit("DATABASE_URL is required")
    sessions = build_session_factory(build_engine(settings.database_url.get_secret_value()))
    with sessions() as session:
        rows = load_rows(session)
    periods = split_periods(rows)
    if args.split not in periods:
        raise SystemExit("At least three observed sessions with Phase 3 features are required")
    period = periods[args.split]
    rows = [item for item in rows if item.snapshot.timestamp_ist <= period[1]]
    grid = bounded_parameter_grid(
        [float(x) for x in args.thresholds.split(",")],
        [int(x) for x in args.confirmations.split(",")], WINDOWS,
        settings.research_max_experiment_combinations,
        expected_move_minimums=[float(x) for x in args.expected_move_minimums.split(",")] if args.expected_move_minimums else [None],
        carry_thresholds=[float(x) for x in args.carry_thresholds.split(",")] if args.carry_thresholds else [None],
        directional_strengths=args.directional_strengths.split(",") if args.directional_strengths else [None],
        strategy_family_modes=args.strategy_family_modes.split(",") if args.strategy_family_modes else [None],
        dte_buckets=args.dte_buckets.split(",") if args.dte_buckets else [None],
        spread_width_sets=[tuple(float(value) for value in group.split(","))
                           for group in args.spread_width_sets.split(";")],
        short_strike_buffers=[float(x) for x in args.short_strike_buffers.split(",")],
        minimum_credit_to_widths=[float(x) for x in args.minimum_credit_to_widths.split(",")],
        volatility_distance_multipliers=[float(x) for x in args.volatility_distance_multipliers.split(",")],
        profit_targets=[float(x) for x in args.profit_targets.split(",")] if args.profit_targets else [None],
        stop_multiples=[float(x) for x in args.stop_multiples.split(",")] if args.stop_multiples else [None],
        force_exit_times=[time.fromisoformat(x) for x in args.force_exit_times.split(",")]
        if args.force_exit_times else [None],
    )
    runtime_config = {
        "alpha_version": ALPHA_VERSION, "ai_version": AI_VERSION, "prompt_version": PROMPT_VERSION,
        "strategy": asdict(strategy_config(settings)), "risk": asdict(risk_config(settings)),
        "regime": asdict(regime_config(settings)), "shadow": asdict(shadow_config(settings)),
        "data_snapshot_ids": [item.snapshot_id for item in rows],
    }
    with sessions() as session:
        reused = bool(session.scalar(select(ResearchExperimentRecord.id).where(
            ResearchExperimentRecord.split_name == "FINAL_TEST",
            ResearchExperimentRecord.test_period == {"start": str(period[0]), "end": str(period[1])}
        ).limit(1))) if args.split == "FINAL_TEST" else False
    results = []
    for parameters in grid:
        parameter_json = {"alpha_threshold": parameters.alpha_threshold,
                          "confirmations": parameters.confirmations,
                          "entry_start": parameters.entry_start.isoformat(),
                          "entry_end": parameters.entry_end.isoformat(),
                          "entry_window": f"{parameters.entry_start:%H:%M}-{parameters.entry_end:%H:%M}",
                          "spread_widths": parameters.spread_widths,
                          "short_strike_buffer": parameters.short_strike_buffer,
                          "minimum_credit_to_width": parameters.minimum_credit_to_width,
                          "volatility_distance_multiplier": parameters.volatility_distance_multiplier,
                          "profit_target_credit_capture_pct": parameters.profit_target_credit_capture_pct,
                          "stop_loss_credit_multiple": parameters.stop_loss_credit_multiple,
                          "force_exit_time": None if parameters.force_exit_time is None else
                          parameters.force_exit_time.isoformat(),
                          "expected_move_min_distance_units": parameters.expected_move_min_distance_units,
                          "minimum_carry_score": parameters.minimum_carry_score,
                          "directional_min_strength": parameters.directional_min_strength,
                          "strategy_family_mode": parameters.strategy_family_mode,
                          "dte_bucket": parameters.dte_bucket,
                          "costs": costs.__dict__}
        parameter_json["runtime_config"] = runtime_config
        config_hash = hashlib.sha256(json.dumps(parameter_json, sort_keys=True, default=str).encode()).hexdigest()
        with sessions.begin() as session:
            record = ResearchExperimentRecord(
                config_hash=config_hash, parameters_json=parameter_json, split_name=args.split,
                train_period=None if "TRAIN" not in periods else
                {"start": str(periods["TRAIN"][0]), "end": str(periods["TRAIN"][1])},
                validation_period=None if "VALIDATION" not in periods else
                {"start": str(periods["VALIDATION"][0]), "end": str(periods["VALIDATION"][1])},
                test_period=None if "FINAL_TEST" not in periods else
                {"start": str(periods["FINAL_TEST"][0]), "end": str(periods["FINAL_TEST"][1])},
                result_summary={"status": "ATTEMPTED"}, sample_tier="INSUFFICIENT")
            session.add(record)
            session.flush()
            experiment_id = record.id
        try:
            metrics = replay_parameters(rows, parameters, strategy_config(settings), risk_config(settings),
                                        regime_config(settings), shadow_config(settings), costs,
                                        entry_period=period,
                                        purge_unclosed_at_boundary=args.split != "FINAL_TEST",
                                        event_provider=ConfiguredMarketEventProvider.from_json(settings.risk_market_events_json))
        except Exception as exc:
            metrics = {"status": "NOT_EVALUABLE", "reason": type(exc).__name__,
                       "sample_tier": "INSUFFICIENT", "net_pnl": None}
        if reused:
            metrics["warning"] = "FINAL_TEST_REUSED"
        with sessions.begin() as session:
            record = session.get(ResearchExperimentRecord, experiment_id)
            record.result_summary = metrics
            record.sample_tier = metrics["sample_tier"]
        results.append({"experiment_id": experiment_id, "parameters": parameter_json, **metrics})
    print(json.dumps({"split": args.split, "period": [str(x) for x in period],
                      "experiments": results, "adjacent_robustness": adjacent_robustness(results),
                      "warning": "Research simulation only; no live fills or profitability claim"},
                     indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
