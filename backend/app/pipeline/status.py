from app.pipeline.models import PipelineRun, PipelineStatus, StepStatus


def failed_run(run: PipelineRun, exc: BaseException) -> PipelineRun:
    completed = sum(
        status == StepStatus.SUCCESS
        for status in (run.shadow_status, run.feature_status, run.alpha_status, run.regime_status,
                       run.strategy_status, run.risk_status)
    )
    return run.model_copy(update={
        "status": PipelineStatus.PARTIAL if completed else PipelineStatus.FAILED,
        "safe_error_type": type(exc).__name__[:120],
        "safe_error_message": "Research pipeline step failed",
    })
