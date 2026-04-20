"""Tests for QueryEstimator."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from oci_logan_mcp.config import Settings
from oci_logan_mcp.query_estimator import QueryEstimator, QueryEstimate


@pytest.fixture
def settings():
    return Settings()


@pytest.fixture
def oci_client():
    client = MagicMock()
    client.query = AsyncMock()
    return client


@pytest.fixture
def estimator(oci_client, settings):
    return QueryEstimator(oci_client=oci_client, settings=settings)


def test_extract_log_sources_single():
    sources = QueryEstimator._extract_sources("'Log Source' = 'Linux Syslog'")
    assert sources == ["Linux Syslog"]


def test_extract_log_sources_multiple_or_clause():
    q = "'Log Source' in ('Linux Syslog', 'Apache HTTP Server') | head 10"
    sources = QueryEstimator._extract_sources(q)
    assert sorted(sources) == ["Apache HTTP Server", "Linux Syslog"]


def test_extract_log_sources_none_when_query_wildcards():
    assert QueryEstimator._extract_sources("* | head 10") == []


def test_has_filters_detected():
    assert QueryEstimator._has_filters("'Log Source' = 'x' and severity = 'ERROR'")
    assert QueryEstimator._has_filters("where user = 'bob'")
    assert not QueryEstimator._has_filters("* | head 1")


@pytest.mark.asyncio
async def test_estimate_unknown_source_returns_low_confidence(estimator, oci_client):
    oci_client.query.side_effect = RuntimeError("probe failed")
    est = await estimator.estimate("'Log Source' = 'Unknown'", "last_1_hour")
    assert isinstance(est, QueryEstimate)
    assert est.confidence == "low"
    assert est.estimated_bytes >= 0
    assert est.estimated_cost_usd is None or est.estimated_cost_usd >= 0


@pytest.mark.asyncio
async def test_estimate_never_raises_on_garbage_query(estimator):
    est = await estimator.estimate("", "last_1_hour")
    assert est.confidence == "low"


@pytest.mark.asyncio
async def test_estimate_with_working_probe_returns_medium_confidence(estimator, oci_client):
    oci_client.query.return_value = {"rows": [[1000]], "columns": []}
    est = await estimator.estimate("'Log Source' = 'Linux Syslog'", "last_1_hour")
    assert est.confidence == "medium"
    assert est.estimated_bytes > 0
    assert est.estimated_cost_usd is not None and est.estimated_cost_usd >= 0


@pytest.mark.asyncio
async def test_estimate_scales_linearly_with_time_range(estimator, oci_client):
    oci_client.query.return_value = {"rows": [[1000]], "columns": []}
    est_1h = await estimator.estimate("'Log Source' = 'Linux Syslog'", "last_1_hour")
    estimator._probe_cache.clear()
    oci_client.query.return_value = {"rows": [[1000]], "columns": []}
    est_24h = await estimator.estimate("'Log Source' = 'Linux Syslog'", "last_24_hours")
    assert 20 * est_1h.estimated_bytes <= est_24h.estimated_bytes <= 28 * est_1h.estimated_bytes


@pytest.mark.asyncio
async def test_probe_cache_reused_within_ttl(estimator, oci_client):
    oci_client.query.return_value = {"rows": [[100]], "columns": []}
    await estimator.estimate("'Log Source' = 'Linux Syslog'", "last_1_hour")
    await estimator.estimate("'Log Source' = 'Linux Syslog'", "last_1_hour")
    assert oci_client.query.await_count == 1


@pytest.mark.asyncio
async def test_probe_cache_expires(estimator, oci_client, monkeypatch):
    import oci_logan_mcp.query_estimator as qe_mod
    oci_client.query.return_value = {"rows": [[100]], "columns": []}
    await estimator.estimate("'Log Source' = 'Linux Syslog'", "last_1_hour")
    original_time = qe_mod.time.time()
    fake_time = original_time + estimator.settings.cost.probe_ttl_seconds + 1
    monkeypatch.setattr(qe_mod.time, "time", lambda: fake_time)
    await estimator.estimate("'Log Source' = 'Linux Syslog'", "last_1_hour")
    assert oci_client.query.await_count == 2


@pytest.mark.asyncio
async def test_filter_discount_reduces_bytes(estimator, oci_client):
    oci_client.query.return_value = {"rows": [[1000]], "columns": []}
    est_no_filter = await estimator.estimate("'Log Source' = 'x'", "last_1_hour")
    estimator._probe_cache.clear()
    oci_client.query.return_value = {"rows": [[1000]], "columns": []}
    est_filter = await estimator.estimate("'Log Source' = 'x' and severity = 'ERROR'", "last_1_hour")
    assert est_filter.estimated_bytes < est_no_filter.estimated_bytes


@pytest.mark.asyncio
async def test_run_query_carries_flat_estimate_fields_on_live():
    from unittest.mock import AsyncMock, MagicMock
    from oci_logan_mcp.query_engine import QueryEngine
    from oci_logan_mcp.query_estimator import QueryEstimator
    from oci_logan_mcp.config import Settings

    settings = Settings()
    oci_client = MagicMock()
    oci_client.compartment_id = "c"
    oci_client.query = AsyncMock()
    oci_client.query.side_effect = [
        {"rows": [[500]], "columns": []},
        {"rows": [], "columns": [{"name": "Time"}]},
    ]
    estimator = QueryEstimator(oci_client, settings)
    cache = MagicMock(get=MagicMock(return_value=None), set=MagicMock())

    engine = QueryEngine(oci_client, cache, MagicMock(), estimator=estimator)
    resp = await engine.execute(
        query="'Log Source' = 'Linux Syslog'",
        time_range="last_1_hour",
    )
    for key in ("estimated_bytes", "estimated_rows", "estimated_cost_usd",
                "estimated_eta_seconds", "estimate_confidence", "estimate_rationale"):
        assert key in resp, f"missing flat field: {key}"
    assert resp["estimate_confidence"] in {"low", "medium", "high"}


@pytest.mark.asyncio
async def test_cache_hit_replays_estimate_without_probing():
    from unittest.mock import AsyncMock, MagicMock
    from oci_logan_mcp.query_engine import QueryEngine
    from oci_logan_mcp.query_estimator import QueryEstimator
    from oci_logan_mcp.config import Settings

    settings = Settings()
    oci_client = MagicMock()
    oci_client.compartment_id = "c"
    oci_client.query = AsyncMock()

    estimator = QueryEstimator(oci_client, settings)
    cached_payload = {
        "result": {"rows": [["x"]], "columns": [{"name": "Time"}]},
        "estimate": {
            "estimated_bytes": 123, "estimated_rows": None,
            "estimated_cost_usd": 0.01, "estimated_eta_seconds": 0.5,
            "confidence": "medium", "rationale": "replayed from cache",
        },
    }
    cache = MagicMock(get=MagicMock(return_value=cached_payload), set=MagicMock())

    engine = QueryEngine(oci_client, cache, MagicMock(), estimator=estimator)
    resp = await engine.execute(
        query="'Log Source' = 'Linux Syslog'",
        time_range="last_1_hour",
    )
    assert resp["source"] == "cache"
    assert resp["estimated_bytes"] == 123
    assert resp["estimate_confidence"] == "medium"
    assert oci_client.query.await_count == 0


@pytest.mark.asyncio
async def test_next_steps_preserved_on_live_and_cache_paths():
    from unittest.mock import AsyncMock, MagicMock
    from oci_logan_mcp.query_engine import QueryEngine
    from oci_logan_mcp.query_estimator import QueryEstimator
    from oci_logan_mcp.config import Settings

    settings = Settings()
    oci_client = MagicMock()
    oci_client.compartment_id = "c"
    oci_client.query = AsyncMock(side_effect=[
        {"rows": [[500]], "columns": []},
        {"rows": [], "columns": [{"name": "Time"}]},
    ])
    estimator = QueryEstimator(oci_client, settings)
    cache = MagicMock(get=MagicMock(return_value=None), set=MagicMock())

    engine = QueryEngine(oci_client, cache, MagicMock(), estimator=estimator)
    live = await engine.execute(query="'Log Source' = 'x'", time_range="last_1_hour")
    assert "next_steps" in live
    assert isinstance(live["next_steps"], list)
    assert any(s["tool_name"] == "validate_query" for s in live["next_steps"])

    cached_bundle = {
        "result": {"rows": [], "columns": [{"name": "Time"}]},
        "estimate": {"estimated_bytes": 1, "estimated_rows": None,
                     "estimated_cost_usd": 0.0, "estimated_eta_seconds": 0.0,
                     "confidence": "medium", "rationale": ""},
    }
    cache.get = MagicMock(return_value=cached_bundle)
    cached = await engine.execute(query="'Log Source' = 'x'", time_range="last_1_hour")
    assert cached["source"] == "cache"
    assert "next_steps" in cached
    assert any(s["tool_name"] == "validate_query" for s in cached["next_steps"])


@pytest.mark.asyncio
async def test_live_path_caches_result_with_estimate_bundle():
    from unittest.mock import AsyncMock, MagicMock
    from oci_logan_mcp.query_engine import QueryEngine
    from oci_logan_mcp.query_estimator import QueryEstimator
    from oci_logan_mcp.config import Settings

    settings = Settings()
    oci_client = MagicMock()
    oci_client.compartment_id = "c"
    oci_client.query = AsyncMock(side_effect=[
        {"rows": [[500]], "columns": []},
        {"rows": [["x"]], "columns": [{"name": "Time"}]},
    ])
    estimator = QueryEstimator(oci_client, settings)
    cache = MagicMock(get=MagicMock(return_value=None), set=MagicMock())

    engine = QueryEngine(oci_client, cache, MagicMock(), estimator=estimator)
    await engine.execute(query="'Log Source' = 'x'", time_range="last_1_hour")

    assert cache.set.call_count == 1
    _key, payload = cache.set.call_args.args
    assert isinstance(payload, dict)
    assert "result" in payload and "estimate" in payload


# --- P0 limitation regression: source-less queries ---

@pytest.mark.asyncio
async def test_sourceless_query_returns_low_confidence_zero_bytes(estimator, oci_client):
    """Source-less queries cannot be probed; estimator must return confidence=low, bytes=0."""
    est = await estimator.estimate("* | head 100", "last_1_hour")
    assert est.confidence == "low"
    assert est.estimated_bytes == 0
    assert est.estimated_cost_usd is None or est.estimated_cost_usd == 0
    assert oci_client.query.await_count == 0, "no probe should fire for source-less query"


@pytest.mark.asyncio
async def test_sourceless_query_bypasses_bytes_cost_budget_but_query_count_blocks():
    """
    Regression for P0 limitation: bytes/cost budget is not checked when estimate is
    zero (source-less), but query-count limit still applies.
    """
    from unittest.mock import AsyncMock, MagicMock
    from oci_logan_mcp.query_engine import QueryEngine
    from oci_logan_mcp.query_estimator import QueryEstimator
    from oci_logan_mcp.budget_tracker import BudgetTracker, BudgetExceededError, BudgetLimits
    from oci_logan_mcp.config import Settings

    limits = BudgetLimits(
        enabled=True,
        max_queries_per_session=1,   # tight query-count limit
        max_bytes_per_session=10 * 1024 ** 3,
        max_cost_usd_per_session=5.00,
    )
    settings = Settings()

    oci_client = MagicMock()
    oci_client.compartment_id = "c"
    oci_client.query = AsyncMock(return_value={"rows": [], "columns": []})

    estimator_obj = QueryEstimator(oci_client, settings)
    budget = BudgetTracker(session_id="test-session", limits=limits)
    cache = MagicMock(get=MagicMock(return_value=None), set=MagicMock())
    engine = QueryEngine(oci_client, cache, MagicMock(), estimator=estimator_obj, budget_tracker=budget)

    # First source-less query: bytes/cost are 0 so budget allows it; query count increments
    resp = await engine.execute(query="* | head 10", time_range="last_1_hour")
    assert resp["estimate_confidence"] == "low"
    assert resp["estimated_bytes"] == 0

    # Second source-less query: query-count limit (1) is now exhausted → BudgetExceededError
    with pytest.raises(BudgetExceededError):
        await engine.execute(query="* | head 10", time_range="last_1_hour")
