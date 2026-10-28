"""Use real wrapper behavior with recording factories at the external SDK boundary."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import logging
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import oci
import pytest

from oci_logan_mcp.client import OCILogAnalyticsClient
from oci_logan_mcp.config import Settings


class RecordingSdk:
    def __init__(self):
        self.la_calls = []
        self.identity_calls = []
        self.monitor_calls = []
        self.query_calls = []
        self.la_error = None
        self.monitor_error = None
        self.monitor_gate = None
        self.la = SimpleNamespace(query=self.query)

    def query(self, **kwargs):
        self.query_calls.append(kwargs)
        return SimpleNamespace(data=SimpleNamespace(columns=[], items=[{"count": 42}],
            total_count=1, is_partial_result=False), has_next_page=False, next_page=None)

    def make_la(self, **kwargs):
        self.la_calls.append(kwargs)
        if self.la_error:
            raise self.la_error
        return self.la

    def make_identity(self, **kwargs):
        self.identity_calls.append(kwargs)
        raise RuntimeError("identity-private-marker")

    def make_monitor(self, **kwargs):
        self.monitor_calls.append(kwargs)
        if self.monitor_gate:
            self.monitor_gate[0].set()
            assert self.monitor_gate[1].wait(3)
        if self.monitor_error:
            raise self.monitor_error
        return SimpleNamespace(config=kwargs["config"], timeout=kwargs.get("timeout"),
                               retry_strategy=kwargs.get("retry_strategy"), marker=73)


def make_client(monkeypatch, sdk, settings=None, config=None):
    settings = settings or Settings()
    # Patches end before any lazy access; the wrapper must retain these factories.
    with monkeypatch.context() as context:
        context.setattr("oci_logan_mcp.client.get_signer", lambda _: (
            config if config is not None else {"region": "us-ashburn-1", "tenancy": "root"}, object()))
        context.setattr(oci.log_analytics, "LogAnalyticsClient", sdk.make_la)
        context.setattr(oci.identity, "IdentityClient", sdk.make_identity)
        context.setattr(oci.monitoring, "MonitoringClient", sdk.make_monitor)
        return OCILogAnalyticsClient(settings)


def test_monitoring_does_not_construct_log_or_identity_clients(monkeypatch):
    sdk = RecordingSdk()
    sdk.la_error = RuntimeError("la-private-marker")
    client = make_client(monkeypatch, sdk)
    assert client.namespace == ""
    assert sdk.la_calls == sdk.identity_calls == []
    monitoring = client.monitoring_client_for_region("us-phoenix-1", timeout_seconds=60)
    assert monitoring.marker == 73
    assert monitoring.config["region"] == "us-phoenix-1"
    assert sdk.la_calls == sdk.identity_calls == []


def test_regional_clients_keep_distinct_config_timeout_and_no_retries(monkeypatch):
    sdk = RecordingSdk()
    client = make_client(monkeypatch, sdk)
    west = client.monitoring_client_for_region("us-phoenix-1", timeout_seconds=3)
    east = client.monitoring_client_for_region("us-ashburn-1", timeout_seconds=60)
    assert west is client.monitoring_client_for_region("us-phoenix-1", timeout_seconds=3)
    changed_timeout = client.monitoring_client_for_region("us-phoenix-1", timeout_seconds=7)
    assert changed_timeout is not west
    assert west.config["region"] == "us-phoenix-1"
    assert east.config["region"] == "us-ashburn-1"
    assert client._config["region"] == "us-ashburn-1"
    assert west.config is not east.config and west.config is not client._config
    assert west.timeout == (3, 3)
    assert east.timeout == (10, 60)
    assert changed_timeout.timeout == (7, 7)
    assert isinstance(west.retry_strategy, oci.retry.NoneRetryStrategy)
    assert len(sdk.monitor_calls) == 3


def test_concurrent_first_access_constructs_one_regional_client(monkeypatch):
    sdk = RecordingSdk()
    entered, release = threading.Event(), threading.Event()
    sdk.monitor_gate = (entered, release)
    client = make_client(monkeypatch, sdk)
    with ThreadPoolExecutor(max_workers=4) as pool:
        requests = [pool.submit(client.monitoring_client_for_region, "us-phoenix-1", timeout_seconds=60) for _ in range(4)]
        try:
            assert entered.wait(2)
            assert all(not request.done() for request in requests)
        finally:
            release.set()
        results = [request.result(3) for request in requests]
    assert all(result is results[0] for result in results)
    assert results[0].marker == 73
    assert len(sdk.monitor_calls) == 1


def test_constructor_failure_is_safe_and_does_not_poison_an_explicit_retry(monkeypatch):
    sdk = RecordingSdk()
    client = make_client(monkeypatch, sdk, config={"region": "us-ashburn-1"})
    assert client.tenancy_id == ""
    sdk.monitor_error = RuntimeError("monitoring-private-marker")
    with pytest.raises(RuntimeError, match="test_connection") as caught:
        client.monitoring_client_for_region("us-phoenix-1", timeout_seconds=60)
    assert "monitoring-private-marker" not in str(caught.value)
    sdk.monitor_error = None
    assert client.monitoring_client_for_region("us-phoenix-1", timeout_seconds=60).marker == 73
    assert len(sdk.monitor_calls) == 2


@pytest.mark.asyncio
async def test_late_log_query_uses_captured_factory_and_returns_real_rows(monkeypatch):
    sdk = RecordingSdk()
    settings = Settings()
    settings.log_analytics.namespace = "fixture"
    settings.log_analytics.default_compartment_id = "ocid1.compartment.default"
    client = make_client(monkeypatch, sdk, settings)
    assert not sdk.la_calls
    result = await client.query("* | stats count", "2026-10-20T00:00:00Z", "2026-10-20T01:00:00Z")
    assert result["rows"] == [[42]]
    assert len(sdk.la_calls) == len(sdk.query_calls) == 1
    assert sdk.query_calls[0]["namespace_name"] == "fixture"
    assert not sdk.identity_calls


@pytest.mark.asyncio
async def test_first_log_constructor_failure_is_flagged_without_disabling_monitoring(tmp_path, monkeypatch):
    from unittest.mock import MagicMock
    from oci_logan_mcp.handlers import MCPHandlers
    from oci_logan_mcp.user_store import UserStore
    from oci_logan_mcp.secret_store import SecretStore
    from oci_logan_mcp.server import build_call_tool_result

    sdk = RecordingSdk()
    sdk.la_error = RuntimeError("la-private-marker")
    settings = Settings()
    settings.log_analytics.namespace = "fixture"
    settings.log_analytics.default_compartment_id = "ocid1.compartment.default"
    settings.report_delivery.artifact_dir = tmp_path / "reports"
    settings.logging.log_path = tmp_path / "logs"
    settings.budget.enabled = False
    client = make_client(monkeypatch, sdk, settings)
    cache = MagicMock(get=MagicMock(return_value=None))
    handler = MCPHandlers(settings, client, cache, MagicMock(), MagicMock(),
        UserStore(tmp_path, user_id="fixture"), secret_store=SecretStore(tmp_path / "secret"))
    response = build_call_tool_result(await handler.handle_tool_call("run_query", {"query": "* | stats count"}))
    assert response.isError is True
    assert "test_connection" in response.content[0].text
    assert "la-private-marker" not in response.content[0].text
    assert sdk.la_calls
    assert client.monitoring_client_for_region("us-phoenix-1", timeout_seconds=60).marker == 73


@pytest.mark.asyncio
async def test_background_schema_refresh_needs_a_log_namespace(monkeypatch):
    from oci_logan_mcp.server import OCILogAnalyticsMCPServer
    server = OCILogAnalyticsMCPServer()
    server.settings = Settings()
    server.oci_client = SimpleNamespace(namespace="")
    server.context_manager = SimpleNamespace(refresh_schema=AsyncMock(return_value={"fields": 7}))
    await server._refresh_schema_background()
    assert server.context_manager.refresh_schema.await_count == 0
    server.settings.log_analytics.namespace = "fixture"
    server.oci_client.namespace = "fixture"
    await server._refresh_schema_background()
    assert server.context_manager.refresh_schema.await_count == 1
