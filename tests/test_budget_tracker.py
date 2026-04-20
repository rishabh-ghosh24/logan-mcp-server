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


def test_reserve_under_budget_passes(tracker):
    tracker.reserve(estimated_bytes=1000, estimated_cost_usd=0.10)
    snap = tracker.snapshot()
    assert snap.queries == 1
    assert snap.bytes == 1000
    assert abs(snap.cost_usd - 0.10) < 1e-6


def test_reserve_over_query_count_blocks_and_leaves_state_unchanged(tracker):
    for _ in range(3):
        tracker.reserve(estimated_bytes=100, estimated_cost_usd=0.01)
    snap_before = tracker.snapshot()
    with pytest.raises(BudgetExceededError) as exc:
        tracker.reserve(estimated_bytes=100, estimated_cost_usd=0.01)
    assert "query count" in str(exc.value).lower() or "queries" in str(exc.value).lower()
    # State must NOT mutate on a failed reserve.
    assert tracker.snapshot() == snap_before


def test_reserve_over_bytes_blocks(tracker):
    tracker.reserve(estimated_bytes=9_000, estimated_cost_usd=0.01)
    with pytest.raises(BudgetExceededError) as exc:
        tracker.reserve(estimated_bytes=5_000, estimated_cost_usd=0.01)
    assert "bytes" in str(exc.value).lower()


def test_reserve_over_cost_blocks(tracker):
    tracker.reserve(estimated_bytes=100, estimated_cost_usd=0.90)
    with pytest.raises(BudgetExceededError) as exc:
        tracker.reserve(estimated_bytes=100, estimated_cost_usd=0.20)
    assert "cost" in str(exc.value).lower()


def test_reserve_accumulates(tracker):
    tracker.reserve(estimated_bytes=1000, estimated_cost_usd=0.10)
    tracker.reserve(estimated_bytes=2000, estimated_cost_usd=0.20)
    u = tracker.snapshot()
    assert u.queries == 2
    assert u.bytes == 3000
    assert abs(u.cost_usd - 0.30) < 1e-6


def test_disabled_tracker_never_raises_and_does_not_track(limits):
    limits.enabled = False
    t = BudgetTracker("s", limits)
    for _ in range(10):
        t.reserve(estimated_bytes=100_000, estimated_cost_usd=1.00)
    # Disabled path is a no-op: nothing tracked, nothing enforced.
    assert t.snapshot().queries == 0


def test_remaining_reports_correctly(tracker):
    tracker.reserve(estimated_bytes=2_500, estimated_cost_usd=0.25)
    remaining = tracker.remaining()
    assert remaining["queries"] == 2
    assert remaining["bytes"] == 7_500
    assert abs(remaining["cost_usd"] - 0.75) < 1e-6


def test_override_skips_enforcement_but_still_records(tracker):
    for _ in range(3):
        tracker.reserve(estimated_bytes=100, estimated_cost_usd=0.01)
    tracker.reserve(estimated_bytes=100, estimated_cost_usd=0.01, override=True)
    # Override accepted over limit, usage still recorded.
    assert tracker.snapshot().queries == 4


def test_release_rolls_back_reservation(tracker):
    tracker.reserve(estimated_bytes=1000, estimated_cost_usd=0.10)
    tracker.release(bytes=1000, cost_usd=0.10)
    snap = tracker.snapshot()
    assert snap.queries == 0
    assert snap.bytes == 0
    assert snap.cost_usd == 0.0


def test_release_floors_at_zero_on_mismatch(tracker):
    # Releasing without a matching reserve must not underflow.
    tracker.release(bytes=9_999, cost_usd=99.0)
    snap = tracker.snapshot()
    assert snap.queries == 0
    assert snap.bytes == 0
    assert snap.cost_usd == 0.0


def test_reserve_is_atomic_under_concurrency(limits):
    """Key race-safety property: concurrent reserves that together would exceed
    the limit must result in exactly N successes (where N = cap) and the rest
    BudgetExceededError. check()+record() split couldn't guarantee this."""
    import threading
    limits.max_queries_per_session = 5
    limits.max_bytes_per_session = 10**9
    limits.max_cost_usd_per_session = 10**6
    t = BudgetTracker("s", limits)

    successes = []
    failures = []

    def worker():
        try:
            t.reserve(estimated_bytes=1, estimated_cost_usd=0.0)
            successes.append(1)
        except BudgetExceededError:
            failures.append(1)

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for th in threads: th.start()
    for th in threads: th.join()

    assert len(successes) == 5
    assert len(failures) == 45
    assert t.snapshot().queries == 5


@pytest.mark.asyncio
async def test_budget_preflight_blocks_on_cache_miss():
    from unittest.mock import AsyncMock, MagicMock
    from oci_logan_mcp.query_engine import QueryEngine
    from oci_logan_mcp.query_estimator import QueryEstimator
    from oci_logan_mcp.budget_tracker import BudgetTracker, BudgetLimits, BudgetExceededError
    from oci_logan_mcp.config import Settings

    settings = Settings()
    oci_client = MagicMock()
    oci_client.compartment_id = "c"
    oci_client.query = AsyncMock(return_value={"rows": [[1_000_000]], "columns": []})
    estimator = QueryEstimator(oci_client, settings)

    limits = BudgetLimits(
        enabled=True,
        max_queries_per_session=5,
        max_bytes_per_session=100,
        max_cost_usd_per_session=100.0,
    )
    tracker = BudgetTracker("s", limits)

    engine = QueryEngine(
        oci_client,
        MagicMock(get=MagicMock(return_value=None), set=MagicMock()),
        MagicMock(),
        estimator=estimator, budget_tracker=tracker,
    )

    with pytest.raises(BudgetExceededError):
        await engine.execute(query="'Log Source' = 'Linux Syslog'", time_range="last_1_hour")
    assert oci_client.query.await_count == 1


@pytest.mark.asyncio
async def test_budget_does_not_charge_on_cache_hit():
    from unittest.mock import AsyncMock, MagicMock
    from oci_logan_mcp.query_engine import QueryEngine
    from oci_logan_mcp.query_estimator import QueryEstimator
    from oci_logan_mcp.budget_tracker import BudgetTracker, BudgetLimits
    from oci_logan_mcp.config import Settings

    settings = Settings()
    oci_client = MagicMock()
    oci_client.compartment_id = "c"
    oci_client.query = AsyncMock()
    estimator = QueryEstimator(oci_client, settings)

    limits = BudgetLimits(enabled=True, max_queries_per_session=1,
                          max_bytes_per_session=1, max_cost_usd_per_session=0.01)
    tracker = BudgetTracker("s", limits)
    tracker.reserve(estimated_bytes=0, estimated_cost_usd=0.0)

    cached = {"result": {"rows": [], "columns": []},
              "estimate": {"estimated_bytes": 999, "estimated_rows": None,
                           "estimated_cost_usd": 1.0, "estimated_eta_seconds": 0.0,
                           "confidence": "medium", "rationale": ""}}
    cache = MagicMock(get=MagicMock(return_value=cached), set=MagicMock())

    engine = QueryEngine(oci_client, cache, MagicMock(),
                         estimator=estimator, budget_tracker=tracker)
    resp = await engine.execute(query="'Log Source' = 'x'", time_range="last_1_hour")
    assert resp["source"] == "cache"
    assert oci_client.query.await_count == 0
    assert tracker.snapshot().queries == 1


@pytest.mark.asyncio
async def test_budget_records_on_successful_live():
    from unittest.mock import AsyncMock, MagicMock
    from oci_logan_mcp.query_engine import QueryEngine
    from oci_logan_mcp.query_estimator import QueryEstimator
    from oci_logan_mcp.budget_tracker import BudgetTracker, BudgetLimits
    from oci_logan_mcp.config import Settings

    settings = Settings()
    oci_client = MagicMock()
    oci_client.compartment_id = "c"
    oci_client.query = AsyncMock(side_effect=[
        {"rows": [[1000]], "columns": []},
        {"rows": [["a"]], "columns": [{"name": "X"}]},
    ])
    estimator = QueryEstimator(oci_client, settings)
    tracker = BudgetTracker("s", BudgetLimits())

    engine = QueryEngine(
        oci_client,
        MagicMock(get=MagicMock(return_value=None), set=MagicMock()),
        MagicMock(),
        estimator=estimator, budget_tracker=tracker,
    )
    await engine.execute(query="'Log Source' = 'x'", time_range="last_1_hour")

    snap = tracker.snapshot()
    assert snap.queries == 1
    assert snap.bytes >= 0


@pytest.mark.asyncio
async def test_batch_queries_do_not_consume_budget_in_p0():
    from unittest.mock import AsyncMock, MagicMock
    from oci_logan_mcp.query_engine import QueryEngine
    from oci_logan_mcp.query_estimator import QueryEstimator
    from oci_logan_mcp.budget_tracker import BudgetTracker, BudgetLimits
    from oci_logan_mcp.config import Settings

    settings = Settings()
    oci_client = MagicMock()
    oci_client.compartment_id = "c"
    oci_client.query = AsyncMock(return_value={"rows": [], "columns": []})
    estimator = QueryEstimator(oci_client, settings)
    tracker = BudgetTracker("s", BudgetLimits())

    cache = MagicMock(get=MagicMock(return_value=None), set=MagicMock())
    engine = QueryEngine(oci_client, cache, MagicMock(),
                         estimator=estimator, budget_tracker=tracker)

    await engine.execute_batch([
        {"query": "a", "time_range": "last_1_hour"},
        {"query": "b", "time_range": "last_1_hour"},
    ])
    snap = tracker.snapshot()
    assert snap.queries == 0, (
        "P0 spec: run_batch_queries is unbudgeted."
    )


@pytest.mark.asyncio
async def test_query_engine_override_bypasses_budget():
    from unittest.mock import AsyncMock, MagicMock
    from oci_logan_mcp.query_engine import QueryEngine
    from oci_logan_mcp.query_estimator import QueryEstimator
    from oci_logan_mcp.budget_tracker import BudgetTracker, BudgetLimits
    from oci_logan_mcp.config import Settings

    settings = Settings()
    oci_client = MagicMock()
    oci_client.compartment_id = "c"
    oci_client.query = AsyncMock(side_effect=[
        {"rows": [[1_000_000]], "columns": []},
        {"rows": [], "columns": [{"name": "Time"}]},
    ])
    estimator = QueryEstimator(oci_client, settings)
    tracker = BudgetTracker("s", BudgetLimits(
        enabled=True, max_queries_per_session=1,
        max_bytes_per_session=1, max_cost_usd_per_session=0.01,
    ))
    engine = QueryEngine(
        oci_client, MagicMock(get=MagicMock(return_value=None), set=MagicMock()),
        MagicMock(), estimator=estimator, budget_tracker=tracker,
    )

    resp = await engine.execute(
        query="'Log Source' = 'x'", time_range="last_1_hour",
        use_cache=False, budget_override=True,
    )
    assert resp["source"] == "live"
    assert tracker.snapshot().queries == 1


@pytest.mark.asyncio
async def test_failed_live_query_releases_reservation():
    """When the OCI query raises after reserve(), the reservation must roll back."""
    from unittest.mock import AsyncMock, MagicMock
    from oci_logan_mcp.query_engine import QueryEngine
    from oci_logan_mcp.query_estimator import QueryEstimator
    from oci_logan_mcp.budget_tracker import BudgetTracker, BudgetLimits
    from oci_logan_mcp.config import Settings

    settings = Settings()
    oci_client = MagicMock()
    oci_client.compartment_id = "c"
    # First call: probe succeeds. Second call: actual query fails.
    oci_client.query = AsyncMock(side_effect=[
        {"rows": [[1000]], "columns": []},
        RuntimeError("OCI boom"),
    ])
    estimator = QueryEstimator(oci_client, settings)
    tracker = BudgetTracker("s", BudgetLimits())

    engine = QueryEngine(
        oci_client,
        MagicMock(get=MagicMock(return_value=None), set=MagicMock()),
        MagicMock(),
        estimator=estimator, budget_tracker=tracker,
    )

    with pytest.raises(RuntimeError, match="OCI boom"):
        await engine.execute(query="'Log Source' = 'x'", time_range="last_1_hour")

    # Reservation was rolled back — the failed query didn't consume budget.
    snap = tracker.snapshot()
    assert snap.queries == 0
    assert snap.bytes == 0
    assert snap.cost_usd == 0.0
