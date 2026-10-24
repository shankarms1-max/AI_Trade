"""Offline pacing/refresh regression; no broker calls or production evidence."""

import json
import os
import time
from datetime import timedelta

import pytest

from app.commodity_acts.request_budget import BudgetUnavailable, RequestBudget
from tests.test_commodity_contracts import capture, config

from tests import test_commodity_paper as fixtures

case = fixtures.case
research_case = fixtures.research_case


class Clock:
    def __init__(self):
        self.at = 1000.0

    def now(self):
        return self.at

    def sleep(self, seconds):
        self.at += seconds


def budget(path, clock, **kwargs):
    return RequestBudget(str(path), clock=clock.now, monotonic=clock.now, sleeper=clock.sleep, **kwargs)


def test_shared_budget_paces_independent_workers_and_restart(tmp_path):
    clock, calls = Clock(), []
    path = tmp_path / "budget"
    for _ in range(3):
        worker = budget(path, clock)
        worker.call(lambda: calls.append(clock.at))
    assert calls == [1000, 1001, 1002]
    assert set(json.loads(path.read_text())) == {"next_at", "cooldown_until"}


def test_failure_has_no_retry_and_both_workers_share_cooldown(tmp_path):
    clock, calls = Clock(), []
    path = tmp_path / "budget"

    def failed():
        calls.append(1)
        raise RuntimeError("PRIVATE_BROKER_DETAIL")

    with pytest.raises(BudgetUnavailable, match="COMMODITY_MARKET_REQUEST_FAILED") as error:
        budget(path, clock).call(failed)
    assert "PRIVATE" not in str(error.value) and len(calls) == 1
    with pytest.raises(BudgetUnavailable, match="COOLDOWN"):
        budget(path, clock).call(lambda: calls.append(2))
    assert calls == [1]
    clock.sleep(31)
    budget(path, clock).call(lambda: calls.append(3))
    assert calls == [1, 3]


def test_corrupt_or_future_budget_fails_closed_without_broker_call(tmp_path):
    clock, calls = Clock(), []
    path = tmp_path / "budget"
    first = budget(path, clock)
    first.call(lambda: None)
    clock.at = 900
    with pytest.raises(BudgetUnavailable, match="BUSY"):
        first.call(lambda: calls.append(1))
    path.write_text('{"next_at":"secret","cooldown_until":0}')
    with pytest.raises(BudgetUnavailable, match="CORRUPT"):
        first.call(lambda: calls.append(1))
    assert not calls


def test_missing_shared_cache_does_not_fall_back_to_unbounded_access(tmp_path):
    with pytest.raises(BudgetUnavailable, match="UNAVAILABLE"):
        RequestBudget(tmp_path / "absent" / "budget").call(lambda: pytest.fail("broker called"))


def test_master_refresh_is_bounded_and_revalidates_actual_rows():
    _, market, client = capture()
    market.capture(config().specification("CRUDEOILM"))
    assert sum(method == "search" for method, _ in client.calls) == 1
    client.at += timedelta(seconds=1800)
    renewed = market.capture(config().specification("CRUDEOILM"))
    assert sum(method == "search" for method, _ in client.calls) == 2
    assert json.loads(renewed.provenance["contract_evidence"])["verified_at"] == client.at.isoformat()


def test_failed_metadata_refresh_cannot_reuse_expired_cache():
    from app.commodity_acts.broker import CommodityDataError

    _, market, client = capture()
    client.at += timedelta(seconds=1800)
    client.rows = []
    with pytest.raises(CommodityDataError):
        market.capture(config().specification("CRUDEOILM"))


def _paced_process(path, results):
    limiter = RequestBudget(path, interval=0.05, max_wait=2)
    for _ in range(2):
        limiter.call(lambda: results.put(time.time()))


@pytest.mark.skipif(os.name == "nt", reason="Linux production file-lock concurrency")
def test_simultaneous_instruments_share_kernel_lock_and_spacing(tmp_path):
    import multiprocessing

    ctx = multiprocessing.get_context("fork")
    results = ctx.Queue()
    workers = [ctx.Process(target=_paced_process, args=(str(tmp_path / "shared"), results)) for _ in range(2)]
    for worker in workers:
        worker.start()
    starts = sorted(results.get(timeout=5) for _ in range(4))
    for worker in workers:
        worker.join(timeout=5)
        assert worker.exitcode == 0
    assert all(second - first >= 0.04 for first, second in zip(starts, starts[1:]))


def test_today_metrics_use_stored_entry_and_exit_days_without_mutation(case):
    from tests.test_commodity_paper import AT, positions, step
    from app.commodity_acts.paper_read import summary

    step(case, 0)
    step(case, 15)
    step(case, 1830)
    step(case, 1845)
    before = positions(case)
    row = summary(case[0], case[1]["CRUDEOILM"].config, AT + timedelta(seconds=1850))["instruments"][0]
    assert row["trades_today"] == 1
    assert row["realized_gross_today"] == before[0][1]["gross_realized_pnl"]
    tomorrow = summary(case[0], case[1]["CRUDEOILM"].config, AT + timedelta(days=1))["instruments"][0]
    assert tomorrow["trades_today"] == 0 and tomorrow["realized_gross_today"] is None
    assert positions(case) == before
