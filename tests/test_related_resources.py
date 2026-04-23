"""Tests for related_dashboards_and_searches (A7) module."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from oci_logan_mcp.catalog import CatalogEntry, SourceType
from oci_logan_mcp.related_resources import RelatedDashboardsAndSearchesTool


def _make_tool():
    dashboard_service = MagicMock()
    dashboard_service.list_dashboards = AsyncMock(return_value=[])

    saved_search = MagicMock()
    saved_search.list_searches = AsyncMock(return_value=[])
    saved_search.get_search_by_id = AsyncMock(return_value={})

    catalog = MagicMock()
    catalog.load_personal = MagicMock(return_value=[])
    catalog.load_shared = MagicMock(return_value=[])
    catalog.load_builtins = MagicMock(return_value=[])
    catalog.load_starters = MagicMock(return_value=[])

    return (
        RelatedDashboardsAndSearchesTool(
            dashboard_service=dashboard_service,
            saved_search_service=saved_search,
            catalog=catalog,
        ),
        dashboard_service,
        saved_search,
        catalog,
    )


class TestRelatedDashboardsAndSearchesTool:
    @pytest.mark.asyncio
    async def test_requires_at_least_one_search_input(self):
        tool, _dashboard_service, _saved_search, _catalog = _make_tool()

        result = await tool.run(user_id="alice")

        assert result["status"] == "error"
        assert result["error_code"] == "missing_search_input"

    @pytest.mark.asyncio
    async def test_invalid_entity_returns_structured_error(self):
        tool, _dashboard_service, _saved_search, _catalog = _make_tool()

        result = await tool.run(entity="host-1", user_id="alice")

        assert result["status"] == "error"
        assert result["error_code"] == "invalid_entity"

    @pytest.mark.asyncio
    async def test_dashboards_rank_by_display_name_then_description(self):
        tool, dashboard_service, _saved_search, _catalog = _make_tool()
        dashboard_service.list_dashboards = AsyncMock(return_value=[
            {
                "id": "dash-1",
                "display_name": "Audit Dashboard",
                "description": "Overview of audit activity",
            },
            {
                "id": "dash-2",
                "display_name": "Operations Overview",
                "description": "Audit trail widgets for user actions",
            },
            {
                "id": "dash-3",
                "display_name": "Compute Overview",
                "description": "Host metrics",
            },
        ])

        result = await tool.run(source="Audit", user_id="alice")

        assert [item["id"] for item in result["dashboards"]] == ["dash-1", "dash-2"]
        assert result["dashboards"][0]["score"] > result["dashboards"][1]["score"]
        assert result["dashboards"][0]["reason"] == "source matched display_name"

    @pytest.mark.asyncio
    async def test_multi_input_reason_uses_strongest_matching_term(self):
        tool, dashboard_service, _saved_search, _catalog = _make_tool()
        dashboard_service.list_dashboards = AsyncMock(return_value=[
            {
                "id": "dash-1",
                "display_name": "Audit Dashboard",
                "description": "User Name activity widgets",
            },
        ])

        result = await tool.run(source="Audit", field="User Name", user_id="alice")

        assert result["dashboards"][0]["id"] == "dash-1"
        assert result["dashboards"][0]["reason"] == "source matched display_name"

    @pytest.mark.asyncio
    async def test_fuzzy_matching_uses_primary_fields(self):
        tool, dashboard_service, _saved_search, _catalog = _make_tool()
        dashboard_service.list_dashboards = AsyncMock(return_value=[
            {
                "id": "dash-1",
                "display_name": "Request Identifier Dashboard",
                "description": "Operations overview",
            },
        ])

        result = await tool.run(field="Requset Identifier", user_id="alice")

        assert result["dashboards"][0]["score"] == 1
        assert result["dashboards"][0]["reason"] == "field fuzzy matched display_name"

    @pytest.mark.asyncio
    async def test_saved_search_query_text_can_rescore_shortlisted_candidate(self):
        tool, _dashboard_service, saved_search, _catalog = _make_tool()
        saved_search.list_searches = AsyncMock(return_value=[
            {"id": "ss-1", "display_name": "Generic search"},
            {"id": "ss-2", "display_name": "Auth failures"},
        ])
        saved_search.get_search_by_id = AsyncMock(side_effect=[
            {
                "id": "ss-1",
                "display_name": "Generic search",
                "query": "'Request ID' = 'abc'",
            },
            {
                "id": "ss-2",
                "display_name": "Auth failures",
                "query": "'User Name' = 'alice'",
            },
        ])

        result = await tool.run(field="Request ID", user_id="alice")

        assert result["saved_searches"][0]["id"] == "ss-1"
        assert result["saved_searches"][0]["reason"] == "field matched query"

    @pytest.mark.asyncio
    async def test_saved_search_detail_fetches_are_capped_at_ten(self):
        tool, _dashboard_service, saved_search, _catalog = _make_tool()
        saved_search.list_searches = AsyncMock(return_value=[
            {"id": f"ss-{idx}", "display_name": f"search {idx}"}
            for idx in range(12)
        ])
        saved_search.get_search_by_id = AsyncMock(side_effect=[
            {
                "id": f"ss-{idx}",
                "display_name": f"search {idx}",
                "query": "'Request ID' = 'abc'",
            }
            for idx in range(10)
        ])

        await tool.run(field="Request ID", user_id="alice")

        assert saved_search.get_search_by_id.await_count == 10

    @pytest.mark.asyncio
    async def test_empty_corpora_return_empty_buckets(self):
        tool, _dashboard_service, _saved_search, _catalog = _make_tool()

        result = await tool.run(field="Request ID", user_id="alice")

        assert result == {
            "dashboards": [],
            "saved_searches": [],
            "learned_queries": [],
        }

    @pytest.mark.asyncio
    async def test_learned_queries_include_personal_and_shared_only(self):
        tool, _dashboard_service, _saved_search, catalog = _make_tool()
        catalog.load_personal.return_value = [
            CatalogEntry(
                entry_id="personal-1",
                name="request tracing",
                query="'Request ID' = 'abc'",
                description="Trace a request id",
                source=SourceType.PERSONAL,
            )
        ]
        catalog.load_shared.return_value = [
            CatalogEntry(
                entry_id="shared-1",
                name="request failures",
                query="'Request ID' = 'abc' and 'Event' = 'error'",
                description="Shared request troubleshooting",
                source=SourceType.SHARED,
            )
        ]
        catalog.load_builtins.return_value = [
            CatalogEntry(
                entry_id="builtin-1",
                name="builtin request",
                query="'Request ID' = 'abc'",
                description="Builtin",
                source=SourceType.BUILTIN,
            )
        ]
        catalog.load_starters.return_value = [
            CatalogEntry(
                entry_id="starter-1",
                name="starter request",
                query="'Request ID' = 'abc'",
                description="Starter",
                source=SourceType.STARTER,
            )
        ]

        result = await tool.run(field="Request ID", user_id="alice")

        assert [item["id"] for item in result["learned_queries"]] == [
            "personal-1",
            "shared-1",
        ]
        catalog.load_personal.assert_called_once_with("alice")
        catalog.load_shared.assert_called_once_with()
        catalog.load_builtins.assert_not_called()
        catalog.load_starters.assert_not_called()
