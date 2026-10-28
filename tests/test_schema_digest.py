"""The schema resource is a read-time digest: counts, the top sources by volume, pointers (RQ-4)."""
from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from oci_logan_mcp.cache import CacheManager
from oci_logan_mcp.config import CacheConfig
from oci_logan_mcp.schema_manager import SchemaManager

SCOPE = {"namespace": "testns", "compartment_id": "ocid1.compartment.test", "include_subcompartments": True}


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


@pytest.mark.asyncio
async def test_a_failed_volume_query_leaves_the_no_data_count_unknown():
    client, engine = make_client(), make_engine(VOLUME_ROWS)
    engine.execute = AsyncMock(side_effect=TimeoutError("query timed out"))

    digest = await SchemaManager(client, CacheManager(CacheConfig(enabled=True))).get_schema_digest(engine, SCOPE)

    assert digest["more_sources"]["defined_without_data_in_window"] is None
    assert digest["more_sources"]["with_data_not_shown"] is None


@pytest.mark.parametrize("data", [
    {"rows": VOLUME_ROWS, "is_partial": True},
    {"rows": [[f"Source {i}", 1000 - i] for i in range(500)]},  # the row cap was reached: more may exist
])
@pytest.mark.asyncio
async def test_an_incomplete_volume_map_gives_lower_bounds_not_exact_counts(data):
    client, engine = make_client(n_sources=600), make_engine([])
    engine.execute = AsyncMock(return_value={"data": {"columns": [{"name": "Log Source"}, {"name": "count"}], **data}})

    digest = await SchemaManager(client, CacheManager(CacheConfig(enabled=True))).get_schema_digest(engine, SCOPE)

    rows = len(data["rows"])
    assert "log_sources_with_data_in_window" not in digest["counts"]
    assert digest["counts"]["log_sources_with_data_in_window_at_least"] == rows
    more = digest["more_sources"]
    assert more["defined_without_data_in_window"] is None
    assert more["with_data_not_shown_at_least"] == rows - 20 and "with_data_not_shown" not in more
    assert more["incomplete"]


@pytest.mark.asyncio
async def test_a_complete_empty_volume_map_is_an_exact_zero():
    client, engine = make_client(), make_engine([])

    digest = await SchemaManager(client, CacheManager(CacheConfig(enabled=True))).get_schema_digest(engine, SCOPE)

    assert digest["counts"]["log_sources_with_data_in_window"] == 0
    assert digest["more_sources"]["defined_without_data_in_window"] == 30
    assert "incomplete" not in digest["more_sources"]


@pytest.mark.asyncio
async def test_the_default_compartment_and_its_explicit_id_share_one_volume_map():
    client, engine = make_client(), make_engine(VOLUME_ROWS)
    client.compartment_id = SCOPE["compartment_id"]
    manager = SchemaManager(client, CacheManager(CacheConfig(enabled=True)))

    await manager.get_volume_by_source(engine, None, True)
    await manager.get_volume_by_source(engine, SCOPE["compartment_id"], True)

    engine.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrent_cold_reads_run_one_volume_query():
    import asyncio
    client, engine = make_client(), make_engine(VOLUME_ROWS)
    gate, result = asyncio.Event(), await engine.execute()
    engine.execute.reset_mock()

    async def slow(**kwargs):
        await gate.wait()
        return result
    engine.execute = AsyncMock(side_effect=slow)
    manager = SchemaManager(client, CacheManager(CacheConfig(enabled=True)))

    tasks = [asyncio.create_task(manager.get_volume_by_source(engine, SCOPE["compartment_id"], True)) for _ in range(2)]
    await asyncio.sleep(0)
    gate.set()
    first, second = await asyncio.gather(*tasks)

    assert first == second == {f"Source {i}": 1000 - i for i in range(25)}
    engine.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_volume_map_expires_after_five_minutes_whatever_the_query_ttl():
    import time
    client, engine = make_client(), make_engine(VOLUME_ROWS)
    manager = SchemaManager(client, CacheManager(CacheConfig(enabled=True, query_ttl_minutes=60)))

    await manager.get_volume_by_source(engine, SCOPE["compartment_id"], True)
    with patch("time.time", return_value=time.time() + 301):
        await manager.get_volume_by_source(engine, SCOPE["compartment_id"], True)

    assert engine.execute.await_count == 2


@pytest.mark.parametrize("enabled, phrase", [(True, "five minutes"), (False, "every read")])
@pytest.mark.asyncio
async def test_freshness_describes_the_real_reuse(enabled, phrase):
    client, engine = make_client(), make_engine(VOLUME_ROWS)

    digest = await SchemaManager(client, CacheManager(CacheConfig(enabled=enabled))).get_schema_digest(engine, SCOPE)

    assert phrase in digest["freshness"]


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
