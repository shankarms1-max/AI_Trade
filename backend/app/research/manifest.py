"""Frozen dataset/split registration and explicit active-axis/holdout controls."""
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime, time, timezone
from enum import Enum
import hashlib
import json
from math import isfinite
from uuid import uuid4
from pathlib import Path

from app.research import INTEGRITY_VERSION
from app.strategy.policy import LOGIC_VERSION


def json_value(value):
    if hasattr(value, "model_dump"):
        return json_value(value.model_dump(mode="python"))
    if is_dataclass(value):
        return json_value(asdict(value))
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        sequence = sorted(value, key=str) if isinstance(value, (set, frozenset)) else value
        return [json_value(item) for item in sequence]
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, float) and not isfinite(value):
        return {"invalid_numeric_value": str(value)}
    return value


def canonical(value):
    return json.dumps(json_value(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def implementation_hash():
    project = Path(__file__).resolve().parents[3]
    files = [path for directory in (project / "backend" / "app", project / "scripts")
             for path in directory.rglob("*.py")]
    return digest([(str(path.relative_to(project)).replace("\\", "/"),
                    hashlib.sha256(path.read_bytes()).hexdigest()) for path in sorted(files)])


def active_experiment_axes(*, use_alpha=False):
    axes = {"spread_widths", "strategy_family_mode", "entry_window", "short_strike_buffer",
            "minimum_credit_to_width", "expected_move_min_distance_units", "minimum_carry_score",
            "directional_min_strength", "dte_bucket", "profit_target_credit_capture_pct",
            "stop_loss_credit_multiple", "force_exit_time", "regime_persistence"}
    if use_alpha:
        axes.add("alpha_threshold")
    return frozenset(axes)


def validate_experiment_axes(requested, *, use_alpha=False, theta_carry_enabled=False):
    active = active_experiment_axes(use_alpha=use_alpha)
    inactive = set(requested) - active
    if inactive:
        raise ValueError(f"INACTIVE_EXPERIMENT_AXIS: {','.join(sorted(inactive))}")
    if "strategy_family_mode" in requested:
        modes = requested["strategy_family_mode"]
        if not theta_carry_enabled and any(mode in {"BOTH", "THETA_CARRY_ONLY"} for mode in modes):
            raise ValueError("INACTIVE_FAMILY_AXIS: theta carry is disabled for this research policy")
    return active


def validate_axis_bindings(requested, parameters, strategy, risk, shadow):
    """A named singleton axis must actually match the configuration it registers."""
    policy = strategy.credit_spread_policy
    bindings = {name: getattr(parameters, name) for name in (
        "alpha_threshold", "spread_widths", "short_strike_buffer", "minimum_credit_to_width",
        "expected_move_min_distance_units", "minimum_carry_score", "directional_min_strength",
        "strategy_family_mode", "dte_bucket", "profit_target_credit_capture_pct",
        "stop_loss_credit_multiple", "force_exit_time")}
    bindings.update(entry_window=(parameters.entry_start, parameters.entry_end),
                    regime_persistence=risk.required_consecutive_directional_snapshots)
    bindings["strategy_family_mode"] = parameters.strategy_family_mode or policy.family_mode
    bindings["directional_min_strength"] = parameters.directional_min_strength or policy.directional_alpha_min_strength
    bindings["expected_move_min_distance_units"] = (policy.expected_move_min_distance_units if parameters.expected_move_min_distance_units is None
                                                    else parameters.expected_move_min_distance_units)
    for name in ("profit_target_credit_capture_pct", "stop_loss_credit_multiple", "force_exit_time"):
        if bindings[name] is None:
            bindings[name] = getattr(shadow, name)
    for name, values in requested.items():
        if len(values) != 1 or name not in bindings or json_value(values[0]) != json_value(bindings[name]):
            raise ValueError(f"EXPERIMENT_AXIS_CONFIGURATION_MISMATCH: {name}")


def row_provenance(row):
    return {"snapshot_id": row.snapshot_id, "timestamp": row.snapshot.timestamp_ist.isoformat(),
            "raw_hash": digest(row.snapshot),
            "feature_id": row.feature_id, "feature_hash": None if row.feature is None else digest(row.feature),
            "feature_version": None if row.feature is None else row.feature.feature_version,
            "alpha_hash": None if row.alpha is None else digest(row.alpha),
            "alpha_version": None if row.alpha is None else row.alpha.alpha_version,
            "alpha_calculation_mode": None if row.alpha is None else row.alpha.calculation_mode.value}


@dataclass(frozen=True)
class FrozenManifest:
    run_id: str
    manifest_hash: str
    document_json: str

    def payload(self):
        return json.loads(self.document_json)

    @classmethod
    def from_payload(cls, payload):
        payload = dict(payload)
        expected = payload.pop("manifest_hash")
        if digest(payload) != expected:
            raise ValueError("MANIFEST_HASH_MISMATCH")
        return cls(payload["run_id"], expected, canonical(payload))

    def envelope(self):
        return self.payload() | {"manifest_hash": self.manifest_hash}


def create_manifest(rows, *, code_sha, config, cost_schedule, expected_move_source,
                    sessions, event_config, active_axes=(), inactive_axes=(),
                    calculation_mode="HISTORICAL_REPLAY", selected_policy_hash=None,
                    comparison_policy_hashes=(), run_id=None, created_at=None, source_tree_hash=None):
    from app.research.moves import MOVE_SOURCES
    from app.research.quotes import aware
    if expected_move_source not in MOVE_SOURCES:
        raise ValueError("fixed named expected-move source is required")
    if not code_sha or len(code_sha) != 40:
        raise ValueError("full Git commit SHA is required")
    try:
        int(code_sha, 16)
    except ValueError as exc:
        raise ValueError("Git commit SHA must be hexadecimal") from exc
    if not rows or any(not aware(row.snapshot.timestamp_ist) for row in rows):
        raise ValueError("aware raw observation timestamps are required")
    if calculation_mode not in {"HISTORICAL_REPLAY", "RESEARCH_RECOMPUTE"}:
        raise ValueError("research calculation mode required")
    ordered = sorted(rows, key=lambda row: (row.snapshot.timestamp_ist, row.snapshot_id))
    dataset = [row_provenance(row) for row in ordered]
    if len({row.snapshot_id for row in rows}) != len(rows):
        raise ValueError("duplicate raw snapshot IDs")
    if set(sessions) != {"TRAIN", "VALIDATION", "FINAL_TEST"}:
        raise ValueError("all three split memberships must be explicitly registered")
    membership = {name: sorted(str(day) for day in days) for name, days in sessions.items()}
    all_days = [day for days in membership.values() for day in days]
    observed_days = {row.snapshot.timestamp_ist.date().isoformat() for row in rows}
    if len(set(all_days)) != len(all_days) or set(all_days) != observed_days:
        raise ValueError("split sessions must partition the registered dataset without overlap")
    if any(membership[a] and membership[b] and max(membership[a]) >= min(membership[b])
           for a, b in (("TRAIN", "VALIDATION"), ("VALIDATION", "FINAL_TEST"), ("TRAIN", "FINAL_TEST"))):
        raise ValueError("split memberships must be chronological")
    source_tree_hash = source_tree_hash or implementation_hash()
    policy_hash = digest({"config": config, "cost_schedule": cost_schedule,
                          "expected_move_source_policy": expected_move_source,
                          "event_config": event_config, "calculation_mode": calculation_mode,
                          "git_commit_sha": code_sha, "working_tree_source_hash": source_tree_hash})
    document = {
        "run_id": run_id or str(uuid4()), "integrity_version": INTEGRITY_VERSION,
        "git_commit_sha": code_sha, "strategy_logic_version": LOGIC_VERSION,
        "working_tree_source_hash": source_tree_hash,
        "execution_mode": "ISOLATED_OFFLINE_REPLAY", "policy_version": INTEGRITY_VERSION,
        "policy_hash": policy_hash, "config_hash": digest(config), "config": config,
        "dataset_manifest_hash": digest(dataset), "dataset": dataset,
        "feature_provenance_hash": digest([(r["snapshot_id"], r["feature_id"], r["feature_hash"]) for r in dataset]),
        "alpha_provenance_hash": digest([(r["snapshot_id"], r["alpha_hash"]) for r in dataset]),
        "calculation_mode": calculation_mode, "event_config_hash": digest(event_config),
        "event_config": event_config, "cost_schedule_version": cost_schedule.cost_schedule_version,
        "cost_schedule_hash": digest(cost_schedule), "cost_schedule": cost_schedule,
        "expected_move_source_policy": expected_move_source, "sessions": membership,
        "start_timestamp": ordered[0].snapshot.timestamp_ist,
        "end_timestamp": ordered[-1].snapshot.timestamp_ist,
        "active_experiment_axes": sorted(active_axes), "inactive_experiment_axes": sorted(inactive_axes),
        "selected_policy_hash": selected_policy_hash,
        "comparison_policy_hashes": sorted(comparison_policy_hashes),
        "created_at": created_at or datetime.now(timezone.utc),
    }
    return FrozenManifest(document["run_id"], digest(document), canonical(document))


def registered_rows(manifest, rows):
    by_id = {row.snapshot_id: row for row in rows}
    if len(by_id) != len(rows):
        raise ValueError("DUPLICATE_DATASET_SNAPSHOT_ID")
    expected = manifest.payload()["dataset"]
    selected = []
    for entry in expected:
        row = by_id.get(entry["snapshot_id"])
        if row is None or row_provenance(row) != entry:
            raise ValueError(f"DATASET_PROVENANCE_CHANGED: {entry['snapshot_id']}")
        selected.append(row)
    return selected


def authorize_split(manifest, split, *, grid_size=1, policy_hash=None):
    if isinstance(grid_size, bool) or not isinstance(grid_size, int) or grid_size < 1:
        raise ValueError("invalid experiment grid size")
    if split not in {"TRAIN", "VALIDATION", "FINAL_TEST"}:
        raise ValueError("invalid research split")
    data = manifest.payload()
    if split == "FINAL_TEST":
        selected, protocol = data["selected_policy_hash"], data["comparison_policy_hashes"]
        if not ((selected and grid_size == 1 and policy_hash == selected)
                or (protocol and grid_size <= len(protocol) and policy_hash in protocol)):
            raise ValueError("FINAL_TEST_SEALED: selected policy or preregistered comparison required; unrestricted grid forbidden")
    return frozenset(data["sessions"][split])
