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
