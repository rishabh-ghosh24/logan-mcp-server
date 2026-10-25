"""The schema resource is a read-time digest: counts, the top sources by volume, pointers (RQ-4)."""
from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from oci_logan_mcp.cache import CacheManager
from oci_logan_mcp.config import CacheConfig
from oci_logan_mcp.schema_manager import SchemaManager

SCOPE = {"namespace": "testtenancy", "compartment_id": "ocid1.compartment.scp", "include_subcompartments": True}


def make_client(n_sources=30):
    client = MagicMock()
    client.list_log_sources = AsyncMock(return_value=[
        {"name": f"src{i}", "display_name": f"Source {i}", "description": "", "entity_types": []} for i in range(n_sources)])
    client.get_entities_summary = AsyncMock(return_value={
        "active_entities": 7801, "entities_with_logs_collected": 5413, "entities_with_management_agent": 17})
    client.list_entity_types = AsyncMock(return_value=[{"name": f"type{i}", "internal_name": f"t{i}"} for i in range(246)])
    return client


def make_engine(rows):
    engine = MagicMock()
    engine.execute = AsyncMock(return_value={
        "data": {"columns": [{"name": "Log Source"}, {"name": "count"}], "rows": rows}})
    return engine


VOLUME_ROWS = [[f"Source {i}", 1000 - i] for i in range(25)]  # 25 sources with data in the window


@pytest.mark.asyncio
async def test_digest_has_counts_top_twenty_and_pointers_and_nothing_stored():
    client, engine = make_client(), make_engine(VOLUME_ROWS)

    digest = await SchemaManager(client, CacheManager(CacheConfig(enabled=True))).get_schema_digest(engine, SCOPE)

    assert digest["scope"] == {**SCOPE, "window": "last_24_hours"}
    assert digest["counts"] == {"log_sources_defined": 30, "log_sources_with_data_in_window": 25,
                                "active_entities": 7801, "entities_with_logs_collected": 5413, "entity_types": 246}
    top = digest["top_sources_by_volume"]
    assert len(top) == 20
    assert top[0] == {"display_name": "Source 0", "name": "src0", "records": 1000}
    assert digest["more_sources"]["with_data_not_shown"] == 5
    assert digest["more_sources"]["defined_without_data_in_window"] == 5
    assert "list_log_sources" in digest["more_sources"]["note"]
    assert "list_fields" in digest["how_to_get_more"]["fields_of_a_source"]
    for absent in ("log_sources", "fields", "entities", "parsers", "labels"):
        assert absent not in digest
    assert "errors" not in digest
    engine.execute.assert_awaited_once()
    call = engine.execute.await_args.kwargs
    assert call["query"] == "* | stats count by 'Log Source' | sort -count"
    assert call["time_range"] == "last_24_hours" and call["compartment_id"] == SCOPE["compartment_id"]
    assert call["include_subcompartments"] is True and call["max_results"] == 500


@pytest.mark.asyncio
async def test_digest_fails_soft_part_by_part():
    client, engine = make_client(), make_engine(VOLUME_ROWS)
    client.get_entities_summary = AsyncMock(side_effect=RuntimeError("identity down"))
    engine.execute = AsyncMock(side_effect=TimeoutError("query timed out"))

    digest = await SchemaManager(client, CacheManager(CacheConfig(enabled=True))).get_schema_digest(engine, SCOPE)

    assert digest["counts"]["log_sources_defined"] == 30 and digest["counts"]["entity_types"] == 246
    assert "active_entities" not in digest["counts"] and "log_sources_with_data_in_window" not in digest["counts"]
    assert digest["top_sources_by_volume"] == []
    assert set(digest["errors"]) == {"volume_by_source", "entities_summary"}
    assert "query timed out" in digest["errors"]["volume_by_source"]


@pytest.mark.asyncio
async def test_volume_map_is_shared_for_five_minutes():
    client, engine = make_client(), make_engine(VOLUME_ROWS)
    manager = SchemaManager(client, CacheManager(CacheConfig(enabled=True)))

    first = await manager.get_volume_by_source(engine, SCOPE["compartment_id"], True)
    second = await manager.get_volume_by_source(engine, SCOPE["compartment_id"], True)
    await manager.get_schema_digest(engine, SCOPE)

    assert first == {f"Source {i}": 1000 - i for i in range(25)} and second == first
    engine.execute.assert_awaited_once()
    other = await manager.get_volume_by_source(engine, "ocid1.compartment.other", True)
    assert engine.execute.await_count == 2 and other == first


class TestClientWrappers:
    @pytest.fixture
    def client(self):
        from oci_logan_mcp.client import OCILogAnalyticsClient
        from oci_logan_mcp.config import Settings
        settings = Settings()
        settings.log_analytics.namespace = "testns"
        settings.log_analytics.default_compartment_id = "ocid1.compartment.default"
        settings.oci.auth_type = "config_file"
        with patch("oci_logan_mcp.client.get_signer") as signer, patch("oci_logan_mcp.client.oci"):
            signer.return_value = ({"tenancy": "ocid1.tenancy.test"}, MagicMock())
            c = OCILogAnalyticsClient(settings)
        c._rate_limiter = MagicMock(); c._rate_limiter.acquire = AsyncMock(); c._rate_limiter.reset = MagicMock()
        return c

    @pytest.mark.asyncio
    async def test_entities_summary_reads_the_three_counts(self, client):
        client._la_client = MagicMock()
        client._la_client.get_log_analytics_entities_summary.return_value = SimpleNamespace(data=SimpleNamespace(
            active_entities_count=7801, entities_with_has_logs_collected_count=5413, entities_with_management_agent_count=17))

        assert await client.get_entities_summary() == {
            "active_entities": 7801, "entities_with_logs_collected": 5413, "entities_with_management_agent": 17}
        client._la_client.get_log_analytics_entities_summary.assert_called_once_with(
            namespace_name="testns", compartment_id="ocid1.compartment.default")

    @pytest.mark.asyncio
    async def test_entity_types_are_listed_with_internal_names(self, client):
        client._la_client = MagicMock()
        rows = [SimpleNamespace(name="Host (Linux)", internal_name="omc_host_linux", category="Host")]
        with patch("oci_logan_mcp.client.list_call_get_all_results", return_value=SimpleNamespace(data=rows)):
            assert await client.list_entity_types() == [{"name": "Host (Linux)", "internal_name": "omc_host_linux", "category": "Host"}]
