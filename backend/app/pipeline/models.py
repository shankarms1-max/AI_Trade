from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

PIPELINE_VERSION = "phase9_v1"


class PipelineStatus(str, Enum):
    STARTED = "STARTED"
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class StepStatus(str, Enum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class PipelineRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    research_run_id: str | None = None
    policy_hash: str | None = None
    execution_mode: str | None = None
    market_snapshot_id: int
    pipeline_version: str = PIPELINE_VERSION
    started_at: datetime
    completed_at: datetime | None = None
    status: PipelineStatus
    feature_status: StepStatus = StepStatus.PENDING
    alpha_status: StepStatus = StepStatus.SKIPPED
    regime_status: StepStatus = StepStatus.PENDING
    ai_status: StepStatus = StepStatus.SKIPPED
    strategy_status: StepStatus = StepStatus.PENDING
    risk_status: StepStatus = StepStatus.PENDING
    shadow_status: StepStatus = StepStatus.PENDING
    feature_snapshot_id: int | None = None
    alpha_feature_snapshot_id: int | None = None
    regime_snapshot_id: int | None = None
    ai_research_id: int | None = None
    strategy_candidate_set_id: int | None = None
    approved_candidate_count: int = 0
    shadow_trade_id: int | None = None
    safe_error_type: str | None = None
    safe_error_message: str | None = None
    stage_timings_ms: dict[str, float] = Field(default_factory=dict)
