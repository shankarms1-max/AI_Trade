"""Register/run an isolated replay from an offline JSON export; no external clients."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from pydantic import TypeAdapter
from app.alpha.experiments import ExperimentParameters
from app.alpha.models import AlphaFeatureSnapshot
from app.alpha.replay import ReplayRow, replay_parameters
from app.data.models import MarketSnapshot
from app.features.models import MarketFeatureSnapshot
from app.regime.engine import RegimeConfig
from app.research.config import ReplayIntegrityConfig, replay_configuration, validate_replay_parameters
from app.research.costs import CostSchedule
from app.research.ledger import ResearchLedger
from app.research.manifest import FrozenManifest, create_manifest, validate_axis_bindings, validate_experiment_axes
from app.risk.event_checks import ConfiguredMarketEventProvider
from app.risk.limits import RiskConfig
from app.shadow.exits import ShadowConfig
from app.strategy.candidate_engine import StrategyConfig


def load_dataset(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = []
    for item in payload:
        rows.append(ReplayRow(item["snapshot_id"], MarketSnapshot.model_validate(item["snapshot"]),
            None if item.get("feature") is None else MarketFeatureSnapshot.model_validate(item["feature"]),
            item.get("feature_id"),
            None if item.get("alpha") is None else AlphaFeatureSnapshot.model_validate(item["alpha"])))
    return rows


def configurations(payload):
    values = tuple(TypeAdapter(kind).validate_python(payload[key]) for key, kind in (
        ("parameters", ExperimentParameters), ("strategy", StrategyConfig), ("risk", RiskConfig),
        ("regime", RegimeConfig), ("shadow", ShadowConfig), ("integrity", ReplayIntegrityConfig)))
    parameters, strategy, risk, regime, shadow, integrity = values
    if not integrity.enabled or not strategy.credit_spread_policy.enabled:
        raise ValueError("configuration must explicitly declare isolated integrity replay")
    if strategy.credit_spread_policy != risk.credit_spread_policy or strategy.credit_spread_policy != regime.credit_spread_policy:
        raise ValueError("strategy/risk/regime research policies must match")
    validate_replay_parameters(parameters, strategy, shadow)
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-json", required=True, help="Offline raw/feature/alpha export including missing feature rows")
    parser.add_argument("--namespace", required=True, help="Separate local output directory; never a production database")
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--register", action="store_true")
    actions.add_argument("--run-id")
    parser.add_argument("--configuration-json", help="Required for registration; frozen config is used when running")
    parser.add_argument("--split", choices=("TRAIN", "VALIDATION"), default="TRAIN",
                        help="FINAL_TEST is sealed in this initial CLI")
    args = parser.parse_args()
    rows = load_dataset(args.dataset_json)
    root = Path(args.namespace).resolve()
    if args.register:
        if not args.configuration_json:
            parser.error("--register requires --configuration-json")
        registration = json.loads(Path(args.configuration_json).read_text(encoding="utf-8"))
        values = configurations(registration)
        parameters, strategy, risk, regime, shadow, integrity = values
        requested = registration.get("experiment_axes", {})
        validate_experiment_axes(requested, use_alpha=regime.use_statistical_alpha,
                                 theta_carry_enabled=strategy.credit_spread_policy.theta_carry_enabled)
        if any(len(axis) > 1 for axis in requested.values()):
            raise ValueError("register one explicit policy per manifest; comparisons require separate registered policies")
        validate_axis_bindings(requested, parameters, strategy, risk, shadow)
        schedule = TypeAdapter(CostSchedule).validate_python(registration.get("cost_schedule", {}))
        events = registration.get("event_config", [])
        ConfiguredMarketEventProvider.from_json(json.dumps(events))
        project = Path(__file__).resolve().parents[1]
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=project, text=True).strip()
        manifest = create_manifest(rows, code_sha=sha, config=replay_configuration(*values, quantity=registration.get("requested_lots", 1)), cost_schedule=schedule,
            expected_move_source=registration["expected_move_source"], sessions=registration["sessions"],
            event_config=events, active_axes=requested.keys(),
            inactive_axes=("confirmations", "volatility_distance_multiplier") + (() if regime.use_statistical_alpha else ("alpha_threshold",)),
            calculation_mode=registration.get("calculation_mode", "HISTORICAL_REPLAY"))
        ResearchLedger.register(root, manifest)
        print(json.dumps({"run_id": manifest.run_id, "manifest_hash": manifest.manifest_hash,
                          "policy_hash": manifest.payload()["policy_hash"], "state": "REGISTERED_FINAL_TEST_SEALED"}))
    else:
        # Validate UUID before constructing a path from a CLI argument.
        from uuid import UUID
        UUID(args.run_id)
        manifest = FrozenManifest.from_payload(json.loads((root / args.run_id / "manifest.json").read_text(encoding="utf-8")))
        data = manifest.payload()
        values = configurations(data["config"])
        parameters, strategy, risk, regime, shadow, integrity = values
        schedule = TypeAdapter(CostSchedule).validate_python(data["cost_schedule"])
        ledger = ResearchLedger(root, manifest, create=True, split=args.split)
        try:
            result = replay_parameters(rows, parameters, strategy, risk, regime, shadow, integrity_config=integrity,
                                       manifest=manifest, ledger=ledger, cost_schedule=schedule, split=args.split,
                                       quantity=data["config"].get("requested_lots", 1))
        except Exception as exc:
            ledger.append({"event_type": "RUN_FAILED", "error_type": type(exc).__name__, "reason": str(exc)})
            raise
        print(json.dumps({key: value for key, value in result.items() if key != "attempts"}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
