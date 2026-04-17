"""Tests for DashboardService."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from oci_logan_mcp.dashboard_service import DashboardService
from oci_logan_mcp.cache import CacheManager
from oci_logan_mcp.config import CacheConfig


def make_client():
    client = AsyncMock()
    client.compartment_id = "ocid1.compartment.test"
    client.create_management_saved_search.return_value = {
        "id": "ocid1.mss.1", "display_name": "tile1"
    }
    client.create_management_dashboard.return_value = {
        "id": "ocid1.dash.1", "display_name": "My Dashboard"
    }
    return client


def make_svc(client=None):
    return DashboardService(client or make_client(), CacheManager(CacheConfig(enabled=True)))


class TestCreateDashboard:
    @pytest.mark.asyncio
    async def test_creates_saved_search_per_tile(self):
        client = make_client()
        client.create_management_saved_search.side_effect = [
            {"id": "ocid1.mss.1"}, {"id": "ocid1.mss.2"}, {"id": "ocid1.mss.3"}
        ]
        client.create_management_dashboard.return_value = {"id": "ocid1.dash.1"}
        svc = make_svc(client)

        result = await svc.create_dashboard(
            display_name="My Dashboard",
            tiles=[
                {"title": "Errors", "query": "* | stats count", "visualization_type": "bar"},
                {"title": "Warnings", "query": "* | stats count", "visualization_type": "line"},
                {"title": "Summary", "query": "* | stats count", "visualization_type": "table"},
            ],
        )

        assert client.create_management_saved_search.call_count == 3
        assert client.create_management_dashboard.call_count == 1
        assert result["dashboard_id"] == "ocid1.dash.1"
        assert len(result["tile_saved_search_ids"]) == 3

    @pytest.mark.asyncio
    async def test_cleans_up_on_dashboard_creation_failure(self):
        client = make_client()
        client.create_management_saved_search.side_effect = [
            {"id": "ocid1.mss.1"}, {"id": "ocid1.mss.2"}
        ]
        client.create_management_dashboard.side_effect = Exception("dashboard API error")
        svc = make_svc(client)

        with pytest.raises(Exception, match="dashboard API error"):
            await svc.create_dashboard(
                display_name="My Dashboard",
                tiles=[
                    {"title": "A", "query": "* | stats count", "visualization_type": "bar"},
                    {"title": "B", "query": "* | stats count", "visualization_type": "line"},
                ],
            )
        assert client.delete_management_saved_search.call_count == 2

    @pytest.mark.asyncio
    async def test_invalid_viz_type_raises(self):
        svc = make_svc()
        with pytest.raises(ValueError, match="visualization type"):
            await svc.create_dashboard(
                display_name="x",
                tiles=[{"title": "t", "query": "* | stats count", "visualization_type": "invalid"}],
            )

    @pytest.mark.asyncio
    async def test_empty_tiles_raises(self):
        svc = make_svc()
        with pytest.raises(ValueError, match="At least one tile"):
            await svc.create_dashboard(display_name="x", tiles=[])


class TestDeleteDashboard:
    @pytest.mark.asyncio
    async def test_reads_tile_ids_then_deletes_children_then_dashboard(self):
        client = AsyncMock()
        client.get_management_dashboard.return_value = {
            "id": "ocid1.dash.1",
            "tiles": [
                {"saved_search_id": "ocid1.mss.1"},
                {"saved_search_id": "ocid1.mss.2"},
            ],
            "_etag": "etag123",
        }
        svc = make_svc(client)
        call_order = []
        client.delete_management_saved_search.side_effect = lambda id_: call_order.append(f"mss:{id_}")
        client.delete_management_dashboard.side_effect = lambda id_: call_order.append(f"dash:{id_}")

        await svc.delete_dashboard("ocid1.dash.1")

        assert "mss:ocid1.mss.1" in call_order
        assert "mss:ocid1.mss.2" in call_order
        assert call_order[-1] == "dash:ocid1.dash.1"


class TestAddTile:
    @pytest.mark.asyncio
    async def test_adds_tile_with_etag(self):
        client = AsyncMock()
        client.get_management_dashboard.return_value = {
            "id": "ocid1.dash.1",
            "display_name": "My Dashboard",
            "description": "",
            "tiles": [],
            "_etag": "etag123",
        }
        client.create_management_saved_search.return_value = {"id": "ocid1.mss.new"}
        client.update_management_dashboard.return_value = {"id": "ocid1.dash.1"}
        svc = make_svc(client)

        result = await svc.add_tile(
            dashboard_id="ocid1.dash.1",
            title="New Tile",
            query="* | stats count",
            visualization_type="bar",
        )

        assert result["saved_search_id"] == "ocid1.mss.new"
        call_kwargs = client.update_management_dashboard.call_args
        assert call_kwargs[1].get("if_match") == "etag123" or "etag123" in str(call_kwargs)

    @pytest.mark.asyncio
    async def test_cleans_up_orphaned_saved_search_on_put_failure(self):
        client = AsyncMock()
        client.get_management_dashboard.return_value = {
            "id": "ocid1.dash.1", "tiles": [], "_etag": "e1",
            "display_name": "D", "description": "",
        }
        client.create_management_saved_search.return_value = {"id": "ocid1.mss.new"}
        client.update_management_dashboard.side_effect = Exception("412 conflict")
        svc = make_svc(client)

        with pytest.raises(Exception):
            await svc.add_tile("ocid1.dash.1", "T", "* | stats count", "bar")

        client.delete_management_saved_search.assert_called_once_with("ocid1.mss.new")


class TestTileGridLayout:
    def test_computes_positions(self):
        svc = make_svc()
        positions = svc._compute_tile_positions(3)
        assert len(positions) == 3
        for p in positions:
            assert "row" in p and "column" in p
