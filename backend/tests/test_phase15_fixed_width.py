"""Fixed 400-point construction is scoped to new Phase 15 paper candidates."""
from dataclasses import replace
import json

import pytest
from sqlalchemy import select

from app.acts_paper.models import ActsPaperTrade
from app.acts_paper.service import verify_acts_paper_journal
from app.parallel_paper.config import ExperimentConfig, STRATEGIES
from app.parallel_paper.models import ParallelPaperTrade
from app.parallel_paper.service import verify_journals
from app.scalper.config import ScalperConfig
from app.scalper.strategy import build_candidates
from app.scalper_lab.engine import _construction_signal
from tests.test_mirror_exit_v1 import open_acts
from tests.test_parallel_paper_service import capture, raw, service
from tests.test_scalper import settings


@pytest.mark.parametrize("strategy", STRATEGIES)
@pytest.mark.parametrize("direction", ["BULL", "BEAR"])
def test_parallel_400_only_overrides_legacy_width_setting_without_relaxing_risk(strategy, direction):
    configuration = settings(scalper_allowed_widths="100,200")
    config = ExperimentConfig().execution(configuration, strategy)
    observation = raw(300)
    signal = _construction_signal(observation, direction)
    candidates = build_candidates(observation, signal, config).candidates
    assert config.allowed_widths == (400,)
    assert candidates and {candidate.spread_width for candidate in candidates} == {400}
    assert config.max_loss_per_trade == ScalperConfig.from_settings(configuration).max_loss_per_trade
    assert not build_candidates(observation, signal, replace(config, max_loss_per_trade=1000)).candidates


@pytest.mark.parametrize("direction", ["BULL", "BEAR"])
def test_no_400_hedge_does_not_fall_back_to_narrower_width(direction):
    config = ExperimentConfig().execution(settings(), "ORB_15")
    observation = raw(300)
    strikes, option_type = ((25000, 24900), "PE") if direction == "BULL" else ((25000, 25100), "CE")
    observation = observation.model_copy(update={"quotes": [quote for quote in observation.quotes
        if quote.strike in strikes and quote.option_type == option_type]})
    result = build_candidates(observation, _construction_signal(observation, direction), config)
    assert not result.candidates and result.rejection_counts["LONG_LEG_NOT_CAPTURED"] > 0


def test_all_parallel_new_projection_policies_and_candidates_record_400(engine, monkeypatch):
    import app.parallel_paper.service as module
    paper = service(engine, scalper_allowed_widths="100,200")
    observation = raw(300)
    snapshot_id = capture(engine, observation)
    def enter(strategy, *_):
        return {"strategy_id": strategy, "status": "ENTER", "direction": "BULL", "blockers": [],
                "entry_reason": "TEST_EXISTING_SIGNAL", "features": {
                    "breakout_timestamp": observation.captured_at.isoformat(),
                    "bar_end": observation.captured_at.isoformat()}}
    monkeypatch.setattr(module, "decide", enter)
    for strategy in STRATEGIES:
        assert paper._process(strategy, snapshot_id, observation, [], [], {}) == "PENDING_PAPER_ENTRY"
    with paper.sessions() as session:
        rows = session.scalars(select(ParallelPaperTrade)).all()
        assert len(rows) == 5
        for row in rows:
            doc = json.loads(row.document)
            assert doc["policy"]["execution"]["allowed_widths"] == [400]
            assert doc["candidate"]["spread_width"] == 400
    verify_journals(paper.sessions)


def test_acts_v2_new_projection_policy_and_candidate_record_400(engine):
    paper, _, candidate = open_acts(engine)
    assert paper.config.allowed_widths == (400,) and candidate.spread_width == 400
    with paper.sessions() as session:
        doc = json.loads(session.scalar(select(ActsPaperTrade.document)))
    assert doc["policy"]["config"]["allowed_widths"] == [400]
    assert doc["candidate"]["spread_width"] == 400
    verify_acts_paper_journal(paper.sessions)
