from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from time import perf_counter
from zoneinfo import ZoneInfo

from app.core.logging import get_logger
from app.pipeline.models import PipelineRun, PipelineStatus, StepStatus
from app.pipeline.repository import PipelineRepository
from app.pipeline.status import failed_run

logger = get_logger(__name__)
IST = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True)
class PipelineSteps:
    shadow_update: Callable[[int], Any]
    features: Callable[[int], Any]
    regime: Callable[[int], Any]
    strategy: Callable[[int], Any]
    risk: Callable[[int], Any]
    shadow_entry: Callable[[int], Any]
    ai: Callable[[int], Any] | None = None


class ResearchPipelineOrchestrator:
    """Runs persisted research phases in strict, no-future-leakage order."""

    def __init__(self, repository: PipelineRepository, steps: PipelineSteps,
                 operations: Any | None = None) -> None:
        self._repository = repository
        self._steps = steps
        self._operations = operations

    def _save(self, run: PipelineRun, **updates: Any) -> PipelineRun:
        return self._repository.save(run.model_copy(update=updates))

    def run(self, snapshot_id: int, *, run_ai: bool = False) -> PipelineRun:
        previous = self._repository.latest()
        pipeline_started = perf_counter()
        timings: dict[str, float] = {}
        run = PipelineRun(
            market_snapshot_id=snapshot_id,
            started_at=datetime.now(IST),
            status=PipelineStatus.STARTED,
            ai_status=StepStatus.PENDING if run_ai else StepStatus.SKIPPED,
        )
        run = self._repository.save(run)
        logger.info("PIPELINE_STARTED snapshot_id=%d", snapshot_id)
        active_status = "shadow_status"
        try:
            if not self._repository.raw_exists(snapshot_id):
                raise LookupError(f"market snapshot {snapshot_id} not found")

            # Existing positions see this snapshot before any new position can be created.
            stage_started = perf_counter()
            self._steps.shadow_update(snapshot_id)
            timings["shadow_pre_update"] = round((perf_counter() - stage_started) * 1000, 2)
            run = self._save(run, stage_timings_ms=timings.copy())
            logger.info("SHADOW_UPDATED snapshot_id=%d", snapshot_id)

            active_status = "feature_status"
            stage_started = perf_counter()
            self._steps.features(snapshot_id)
            timings["features"] = round((perf_counter() - stage_started) * 1000, 2)
            run = self._save(run, feature_status=StepStatus.SUCCESS,
                             stage_timings_ms=timings.copy(),
                             **self._repository.related_ids(snapshot_id))
            logger.info("FEATURES_BUILT snapshot_id=%d", snapshot_id)

            active_status = "regime_status"
            stage_started = perf_counter()
            self._steps.regime(snapshot_id)
            timings["regime"] = round((perf_counter() - stage_started) * 1000, 2)
            run = self._save(run, regime_status=StepStatus.SUCCESS,
                             stage_timings_ms=timings.copy(),
                             **self._repository.related_ids(snapshot_id))
            logger.info("REGIME_BUILT snapshot_id=%d", snapshot_id)

            if run_ai:
                stage_started = perf_counter()
                try:
                    if self._steps.ai is None:
                        raise RuntimeError("AI research runner is not configured")
                    self._steps.ai(snapshot_id)
                    timings["ai"] = round((perf_counter() - stage_started) * 1000, 2)
                    run = self._save(run, ai_status=StepStatus.SUCCESS,
                                     stage_timings_ms=timings.copy(),
                                     **self._repository.related_ids(snapshot_id))
                    logger.info("AI_BUILT snapshot_id=%d", snapshot_id)
                except Exception as exc:
                    timings["ai"] = round((perf_counter() - stage_started) * 1000, 2)
                    run = self._save(run, ai_status=StepStatus.FAILED,
                                     stage_timings_ms=timings.copy())
                    logger.warning("AI_RESEARCH_FAILED snapshot_id=%d error_type=%s",
                                   snapshot_id, type(exc).__name__)
            else:
                logger.info("AI_SKIPPED snapshot_id=%d", snapshot_id)

            active_status = "strategy_status"
            stage_started = perf_counter()
            self._steps.strategy(snapshot_id)
            timings["strategy"] = round((perf_counter() - stage_started) * 1000, 2)
            run = self._save(run, strategy_status=StepStatus.SUCCESS,
                             stage_timings_ms=timings.copy(),
                             **self._repository.related_ids(snapshot_id))
            logger.info("STRATEGY_BUILT snapshot_id=%d", snapshot_id)

            active_status = "risk_status"
            stage_started = perf_counter()
            risk_result = self._steps.risk(snapshot_id)
            timings["risk"] = round((perf_counter() - stage_started) * 1000, 2)
            run = self._save(
                run,
                risk_status=StepStatus.SUCCESS,
                approved_candidate_count=int(getattr(risk_result, "approved_count", 0)),
                stage_timings_ms=timings.copy(),
                **self._repository.related_ids(snapshot_id),
            )
            logger.info("RISK_BUILT snapshot_id=%d", snapshot_id)

            active_status = "shadow_status"
            stage_started = perf_counter()
            entry_result = self._steps.shadow_entry(snapshot_id)
            timings["shadow_entry"] = round((perf_counter() - stage_started) * 1000, 2)
            trade = getattr(entry_result, "trade", None)
            trade_id = getattr(trade, "id", None) if trade is not None else None
            run = self._save(run, shadow_status=StepStatus.SUCCESS, shadow_trade_id=trade_id,
                             stage_timings_ms=timings.copy())
            if bool(getattr(entry_result, "created", False)):
                logger.info("SHADOW_ENTRY_CREATED snapshot_id=%d", snapshot_id)

            timings["total_pipeline"] = round((perf_counter() - pipeline_started) * 1000, 2)
            run = self._save(
                run,
                status=PipelineStatus.SUCCESS,
                completed_at=datetime.now(IST),
                safe_error_type=None,
                safe_error_message=None,
                stage_timings_ms=timings.copy(),
            )
            logger.info("PIPELINE_SUCCESS snapshot_id=%d", snapshot_id)
            if self._operations is not None and previous and previous["status"] == "FAILED":
                from app.observability.models import EventSeverity
                self._operations.record_event(
                    "PIPELINE", EventSeverity.INFO, "PIPELINE_RECOVERED",
                    "Research pipeline recovered", snapshot_id=snapshot_id,
                    pipeline_run_id=run.id,
                )
            return run
        except Exception as exc:
            timings["total_pipeline"] = round((perf_counter() - pipeline_started) * 1000, 2)
            updates: dict[str, Any] = {active_status: StepStatus.FAILED}
            run = run.model_copy(update=updates)
            for field in ("feature_status", "regime_status", "strategy_status", "risk_status"):
                if getattr(run, field) == StepStatus.PENDING:
                    run = run.model_copy(update={field: StepStatus.SKIPPED})
            if run.shadow_status == StepStatus.PENDING:
                run = run.model_copy(update={"shadow_status": StepStatus.SKIPPED})
            run = failed_run(run, exc).model_copy(update={
                "completed_at": datetime.now(IST), "stage_timings_ms": timings.copy()
            })
            run = self._repository.save(run)
            event = "PIPELINE_PARTIAL" if run.status == PipelineStatus.PARTIAL else "PIPELINE_FAILED"
            logger.error("%s snapshot_id=%d error_type=%s", event, snapshot_id, type(exc).__name__)
            if self._operations is not None:
                from app.observability.models import EventSeverity, safe_exception
                error_type, message = safe_exception(exc)
                self._operations.record_event(
                    "PIPELINE", EventSeverity.ERROR, event, message,
                    error_type=error_type, snapshot_id=snapshot_id,
                    pipeline_run_id=run.id,
                )
            return run
