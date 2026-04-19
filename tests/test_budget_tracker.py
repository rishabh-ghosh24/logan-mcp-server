"""Tests for BudgetTracker."""

import pytest
from oci_logan_mcp.budget_tracker import (
    BudgetTracker, BudgetLimits, BudgetExceededError, BudgetUsage,
)


@pytest.fixture
def limits():
    return BudgetLimits(
        enabled=True,
        max_queries_per_session=3,
        max_bytes_per_session=10_000,
        max_cost_usd_per_session=1.00,
    )


@pytest.fixture
def tracker(limits):
    return BudgetTracker(session_id="s1", limits=limits)


def test_tracker_starts_at_zero(tracker):
    usage = tracker.snapshot()
    assert usage.queries == 0
    assert usage.bytes == 0
    assert usage.cost_usd == 0.0


def test_check_under_budget_passes(tracker):
    tracker.check(estimated_bytes=1000, estimated_cost_usd=0.10)


def test_check_over_query_count_blocks(tracker):
    tracker.record(actual_bytes=100, actual_cost_usd=0.01)
    tracker.record(actual_bytes=100, actual_cost_usd=0.01)
    tracker.record(actual_bytes=100, actual_cost_usd=0.01)
    with pytest.raises(BudgetExceededError) as exc:
        tracker.check(estimated_bytes=100, estimated_cost_usd=0.01)
    assert "query count" in str(exc.value).lower() or "queries" in str(exc.value).lower()


def test_check_over_bytes_blocks(tracker):
    tracker.record(actual_bytes=9_000, actual_cost_usd=0.01)
    with pytest.raises(BudgetExceededError) as exc:
        tracker.check(estimated_bytes=5_000, estimated_cost_usd=0.01)
    assert "bytes" in str(exc.value).lower()


def test_check_over_cost_blocks(tracker):
    tracker.record(actual_bytes=100, actual_cost_usd=0.90)
    with pytest.raises(BudgetExceededError) as exc:
        tracker.check(estimated_bytes=100, estimated_cost_usd=0.20)
    assert "cost" in str(exc.value).lower()


def test_record_accumulates(tracker):
    tracker.record(actual_bytes=1000, actual_cost_usd=0.10)
    tracker.record(actual_bytes=2000, actual_cost_usd=0.20)
    u = tracker.snapshot()
    assert u.queries == 2
    assert u.bytes == 3000
    assert abs(u.cost_usd - 0.30) < 1e-6


def test_disabled_tracker_never_raises(limits):
    limits.enabled = False
    t = BudgetTracker("s", limits)
    for _ in range(10):
        t.record(actual_bytes=100_000, actual_cost_usd=1.00)
    t.check(estimated_bytes=100_000, estimated_cost_usd=10.00)


def test_remaining_reports_correctly(tracker):
    tracker.record(actual_bytes=2_500, actual_cost_usd=0.25)
    remaining = tracker.remaining()
    assert remaining["queries"] == 2
    assert remaining["bytes"] == 7_500
    assert abs(remaining["cost_usd"] - 0.75) < 1e-6


def test_override_skips_check(tracker):
    tracker.record(actual_bytes=100, actual_cost_usd=0.01)
    tracker.record(actual_bytes=100, actual_cost_usd=0.01)
    tracker.record(actual_bytes=100, actual_cost_usd=0.01)
    tracker.check(estimated_bytes=100, estimated_cost_usd=0.01, override=True)
