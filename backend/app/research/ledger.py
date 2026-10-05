"""Append-only file namespace, deliberately separate from production SQL state."""
from pathlib import Path
from uuid import UUID, uuid4
import json

from app.research.manifest import FrozenManifest, canonical, digest


class ResearchLedger:
    @classmethod
    def register(cls, root, manifest):
        UUID(manifest.run_id)
        root = Path(root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        directory = root / manifest.run_id
        directory.mkdir(exist_ok=False)
        cls._write_new(directory / "manifest.json", manifest.envelope())

    def __init__(self, root, manifest, *, create=False, split="TRAIN"):
        UUID(manifest.run_id)  # Run IDs cannot traverse an output namespace.
        if split not in {"TRAIN", "VALIDATION", "FINAL_TEST"}:
            raise ValueError("invalid research split")
        self.root = Path(root).resolve()
        self.manifest = manifest
        self.split = split
        registered = self.root / manifest.run_id
        if create and not registered.exists():
            self.register(root, manifest)
        recorded = FrozenManifest.from_payload(json.loads((registered / "manifest.json").read_text(encoding="utf-8")))
        if recorded != manifest:
            raise ValueError("RUN_IDENTITY_MISMATCH")
        self.directory = registered / split
        if create:
            self.directory.mkdir(exist_ok=False)
            (self.directory / "events").mkdir()
        history = self.events()
        self._count = len(history)
        self._previous_hash = history[-1]["event_hash"] if history else None

    @staticmethod
    def _write_new(path, data):
        with path.open("x", encoding="utf-8") as stream:
            stream.write(canonical(data) + "\n")

    def append(self, event):
        if (self.directory / "summary.json").exists():
            raise ValueError("SEALED_REPLAY_LEDGER")
        payload = {**event, "run_id": self.manifest.run_id,
                   "strategy_logic_version": self.manifest.payload()["strategy_logic_version"],
                   "integrity_version": self.manifest.payload()["integrity_version"],
                   "policy_hash": self.manifest.payload()["policy_hash"],
                   "execution_mode": "ISOLATED_OFFLINE_REPLAY", "research_split": self.split, "sequence": self._count,
                   "previous_event_hash": self._previous_hash}
        envelope = payload | {"event_hash": digest(payload)}
        self._write_new(self.directory / "events" / f"{self._count:08d}.json", envelope)
        self._count += 1
        self._previous_hash = envelope["event_hash"]
        return envelope

    def events(self, *, identity=None):
        data = self.manifest.payload()
        expected = (self.manifest.run_id, data["strategy_logic_version"], data["policy_hash"], data["execution_mode"])
        if identity is not None and tuple(identity) != expected:
            raise ValueError("RUN_IDENTITY_MISMATCH")
        result, prior = [], None
        for path in sorted((self.directory / "events").glob("*.json")):
            event = json.loads(path.read_text(encoding="utf-8"))
            checksum = event.pop("event_hash")
            if (digest(event) != checksum or event["previous_event_hash"] != prior
                    or event["sequence"] != len(result) or event["research_split"] != self.split
                    or tuple(event[key] for key in ("run_id", "strategy_logic_version", "policy_hash", "execution_mode")) != expected):
                raise ValueError("LEDGER_INTEGRITY_FAILURE")
            prior = checksum
            result.append(event | {"event_hash": checksum})
        summary_path = self.directory / "summary.json"
        if summary_path.exists():
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            checksum = summary.pop("summary_hash")
            seal = summary["ledger_seal"]
            if (digest(summary) != checksum or seal["event_count"] != len(result)
                    or seal["terminal_event_hash"] != prior):
                raise ValueError("LEDGER_SEAL_INTEGRITY_FAILURE")
        return result

    def record_final_exposure(self):
        membership = set(self.manifest.payload()["sessions"]["FINAL_TEST"])
        target = self.root / "final-test-exposures"
        target.mkdir(exist_ok=True)
        lock = self.root / ".final-test-exposure-lock"
        try:
            lock.mkdir(exist_ok=False)
        except FileExistsError as exc:
            raise RuntimeError("holdout exposure registry is busy") from exc
        try:
            prior = [json.loads(path.read_text(encoding="utf-8")) for path in target.glob("*.json")]
            overlap = [entry["run_id"] for entry in prior if membership.intersection(entry["sessions"])]
            timestamps = [row["timestamp"] for row in self.manifest.payload()["dataset"]
                          if row["timestamp"][:10] in membership]
            data = {"run_id": self.manifest.run_id, "manifest_hash": self.manifest.manifest_hash,
                    "policy_hash": self.manifest.payload()["policy_hash"], "sessions": sorted(membership),
                    "start_timestamp": min(timestamps) if timestamps else None,
                    "end_timestamp": max(timestamps) if timestamps else None,
                    "overlapping_runs": overlap, "warning": "FINAL_TEST_REUSED" if overlap else None}
            self._write_new(target / f"{uuid4()}.json", data)
            return data
        finally:
            lock.rmdir()

    def attempts(self, *, identity=None):
        """Run/split-scoped reconstruction; no mutable shared shadow or pipeline key."""
        states = {}
        for event in self.events(identity=identity):
            if event["event_type"] == "DECISION_ATTEMPT":
                states[event["attempt_id"]] = event["decision"]
            elif event["event_type"] == "ENTRY_EXECUTION":
                states[event["attempt_id"]] = event["entry_state"]
            elif event["event_type"] == "FINAL_OUTCOME":
                states[event["attempt_id"]] = event["outcome"]
        return list(states.values())

    def count_entries_on(self, day):
        return sum(bool(item.get("entry_execution")) and item["entry_at"][:10] == day.isoformat()
                   for item in self.attempts())

    def open_trades(self):
        # Unresolved exposure is deliberately still open exposure, never zero.
        return [item for item in self.attempts() if item.get("entry_execution")
                and item["state"] in {"OPEN", "UNRESOLVED_EXPOSURE"}]

    def has_fingerprint(self, fingerprint):
        return any(item.get("fingerprint") == fingerprint for item in self.open_trades())

    def latest(self):
        items = self.attempts()
        return items[-1] if items else None

    def finish(self, summary):
        history = self.events()  # Verify the persisted chain before sealing a summary.
        sealed = {**summary, "ledger_seal": {"event_count": len(history),
                  "terminal_event_hash": history[-1]["event_hash"] if history else None}}
        self._write_new(self.directory / "summary.json", sealed | {"summary_hash": digest(sealed)})
