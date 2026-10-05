"""Research writes must use the immutable file namespace, never shared SQL keys."""


def reject_shared_research_write(value):
    if (getattr(value, "research_run_id", None)
            or getattr(value, "execution_mode", None) == "ISOLATED_OFFLINE_REPLAY"):
        raise ValueError("ISOLATED_REPLAY_REQUIRES_RESEARCH_NAMESPACE")
