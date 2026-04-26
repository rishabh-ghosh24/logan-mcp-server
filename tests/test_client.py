"""Exhaustive tests for OCI Log Analytics client — especially pagination."""

import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, AsyncMock, patch, PropertyMock

import pytest

from oci_logan_mcp.client import OCILogAnalyticsClient
from oci_logan_mcp.config import Settings


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def settings():
    """Create test settings."""
    s = Settings()
    s.log_analytics.namespace = "testns"
    s.log_analytics.default_compartment_id = "ocid1.compartment.default"
    s.oci.auth_type = "config_file"
    s.query.max_results = 1000
    return s


@pytest.fixture
def client(settings):
    """Create an OCILogAnalyticsClient with mocked OCI SDK."""
    with patch("oci_logan_mcp.client.get_signer") as mock_signer, \
         patch("oci_logan_mcp.client.oci") as mock_oci:

        mock_signer.return_value = ({"tenancy": "ocid1.tenancy.test"}, MagicMock())

        c = OCILogAnalyticsClient(settings)
        # Reset rate limiter for tests (no actual sleeping)
        c._rate_limiter = MagicMock()
        c._rate_limiter.acquire = AsyncMock()
        c._rate_limiter.handle_rate_limit = AsyncMock()
        c._rate_limiter.reset = MagicMock()
        return c


def _make_paginated_response(items, has_next_page=False, next_page=None):
    """Create a mock response for list_call_get_all_results."""
    resp = MagicMock()
    resp.data = MagicMock()
    resp.data.items = items
    resp.has_next_page = has_next_page
    resp.next_page = next_page
    return resp


def _make_identity_response(items, has_next_page=False, next_page=None):
    """Create a mock response for Identity API (response.data IS the list)."""
    resp = MagicMock()
    resp.data = items  # Identity API: response.data is the list directly
    resp.has_next_page = has_next_page
    resp.next_page = next_page
    return resp


def _make_source(name, display_name=None, description="", entity_types=None, is_system=False):
    """Create a mock log source object."""
    s = MagicMock()
    s.name = name
    s.display_name = display_name or name
    s.description = description
    s.entity_types = entity_types or []
    s.is_system = is_system
    return s


def _make_field(name, display_name=None, data_type="STRING", description=""):
    f = MagicMock()
    f.name = name
    f.display_name = display_name or name
    f.data_type = data_type
    f.description = description
    return f


def _make_entity(name, entity_type="Host", lifecycle_state="ACTIVE"):
    e = MagicMock()
    e.name = name
    e.entity_type_name = entity_type
    e.management_agent_id = None
    e.lifecycle_state = lifecycle_state
    return e


def _make_parser(name, type_="REGEX"):
    p = MagicMock()
    p.name = name
    p.type = type_
    p.description = ""
    p.is_system = False
    return p


def _make_label(name, display_name=None, priority="NONE"):
    l = MagicMock()
    l.name = name
    l.display_name = display_name or name
    l.description = ""
    l.priority = priority
    return l


def _make_compartment(id_, name, lifecycle_state="ACTIVE"):
    c = MagicMock()
    c.id = id_
    c.name = name
    c.description = ""
    c.lifecycle_state = lifecycle_state
    return c


def _make_log_group(id_, display_name, compartment_id="ocid1.compartment.default"):
    g = MagicMock()
    g.id = id_
    g.display_name = display_name
    g.description = ""
    g.compartment_id = compartment_id
    return g


def _make_saved_search(id_, display_name, task_type="SAVED_SEARCH"):
    ss = MagicMock()
    ss.id = id_
    ss.display_name = display_name
    ss.task_type = task_type
    ss.lifecycle_state = "ACTIVE"
    return ss


# ---------------------------------------------------------------------------
# Pagination Tests (list_call_get_all_results)
# ---------------------------------------------------------------------------

class TestListLogSourcesPagination:
    """Test list_log_sources uses oci.pagination for full results."""

    @pytest.mark.asyncio
    async def test_returns_all_sources(self, client):
        """Should return all sources from paginated response."""
        sources = [_make_source(f"Source{i}") for i in range(100)]
        response = _make_paginated_response(sources)

        with patch("oci_logan_mcp.client.list_call_get_all_results", return_value=response):
            result = await client.list_log_sources()

        assert len(result) == 100
        assert result[0]["name"] == "Source0"
        assert result[99]["name"] == "Source99"

    @pytest.mark.asyncio
    async def test_source_fields_extracted(self, client):
        """Should extract all expected fields from source objects."""
        src = _make_source("MySrc", display_name="My Source", description="desc", is_system=True)
        response = _make_paginated_response([src])

        with patch("oci_logan_mcp.client.list_call_get_all_results", return_value=response):
            result = await client.list_log_sources()

        assert len(result) == 1
        assert result[0]["name"] == "MySrc"
        assert result[0]["display_name"] == "My Source"
        assert result[0]["description"] == "desc"
        assert result[0]["is_system"] is True
        assert result[0]["entity_types"] == []

    @pytest.mark.asyncio
    async def test_uses_compartment_override(self, client):
        """Should pass compartment_id override to the API call."""
        response = _make_paginated_response([])

        with patch("oci_logan_mcp.client.list_call_get_all_results", return_value=response) as mock_paginate:
            await client.list_log_sources(compartment_id="ocid1.compartment.override")

        call_kwargs = mock_paginate.call_args
        assert call_kwargs.kwargs["compartment_id"] == "ocid1.compartment.override"

    @pytest.mark.asyncio
    async def test_uses_default_compartment(self, client):
        """Should use default compartment when no override provided."""
        response = _make_paginated_response([])

        with patch("oci_logan_mcp.client.list_call_get_all_results", return_value=response) as mock_paginate:
            await client.list_log_sources()

        call_kwargs = mock_paginate.call_args
        assert call_kwargs.kwargs["compartment_id"] == "ocid1.compartment.default"

    @pytest.mark.asyncio
    async def test_empty_result(self, client):
        """Should handle empty source list gracefully."""
        response = _make_paginated_response([])

        with patch("oci_logan_mcp.client.list_call_get_all_results", return_value=response):
            result = await client.list_log_sources()

        assert result == []


class TestListFieldsPagination:
    """Test list_fields uses oci.pagination for full results."""

    @pytest.mark.asyncio
    async def test_returns_all_fields(self, client):
        """Should return all 1800+ fields across pages."""
        fields = [_make_field(f"Field{i}") for i in range(1800)]
        response = _make_paginated_response(fields)

        with patch("oci_logan_mcp.client.list_call_get_all_results", return_value=response):
            result = await client.list_fields()

        assert len(result) == 1800

    @pytest.mark.asyncio
    async def test_field_attributes(self, client):
        """Should extract field name, display_name, data_type, description."""
        f = _make_field("Severity", display_name="Severity Level", data_type="STRING", description="Log severity")
        response = _make_paginated_response([f])

        with patch("oci_logan_mcp.client.list_call_get_all_results", return_value=response):
            result = await client.list_fields()

        assert result[0]["name"] == "Severity"
        assert result[0]["display_name"] == "Severity Level"
        assert result[0]["data_type"] == "STRING"
        assert result[0]["description"] == "Log severity"

    @pytest.mark.asyncio
    async def test_filter_by_source_name(self, client):
        """Should pass source_name filter to the API."""
        response = _make_paginated_response([])

        with patch("oci_logan_mcp.client.list_call_get_all_results", return_value=response) as mock_paginate:
            await client.list_fields(source_name="Linux Syslog")

        call_kwargs = mock_paginate.call_args.kwargs
        assert call_kwargs.get("source_name") == "Linux Syslog"

    @pytest.mark.asyncio
    async def test_no_source_filter(self, client):
        """Should NOT pass source_name when not specified."""
        response = _make_paginated_response([])

        with patch("oci_logan_mcp.client.list_call_get_all_results", return_value=response) as mock_paginate:
            await client.list_fields()

        call_kwargs = mock_paginate.call_args.kwargs
        assert "source_name" not in call_kwargs


class TestCustomContentAndUpload:
    """Test wrappers used by create_log_source_from_sample."""

    @pytest.mark.asyncio
    async def test_import_custom_content_wraps_zip_bytes(self, client):
        response = MagicMock()
        response.data = MagicMock()
        response.headers = {"opc-request-id": "req1"}

        with patch("oci_logan_mcp.client.oci.util.to_dict", return_value={"ok": True}):
            client._la_client.import_custom_content.return_value = response
            result = await client.import_custom_content(b"zip-bytes", overwrite=True)

        call = client._la_client.import_custom_content.call_args
        assert call.kwargs["namespace_name"] == "testns"
        assert isinstance(call.kwargs["import_custom_content_file_body"], BytesIO)
        assert call.kwargs["import_custom_content_file_body"].getvalue() == b"zip-bytes"
        assert call.kwargs["is_overwrite"] is True
        assert result["data"] == {"ok": True}
        assert result["headers"]["opc-request-id"] == "req1"

    @pytest.mark.asyncio
    async def test_upsert_json_parser_uses_native_parser_model(self, client):
        response = MagicMock()
        response.data = MagicMock()
        response.headers = {"opc-request-id": "parser-req"}

        with patch("oci_logan_mcp.client.oci.util.to_dict", return_value={"parser": "ok"}):
            client._la_client.upsert_parser.return_value = response
            result = await client.upsert_json_parser(
                parser_name="App_JSON",
                display_name="App JSON",
                field_paths=[("alpha", "$.alpha")],
                field_mappings={"alpha": "udfs1"},
                example_content='{"alpha":"one"}',
            )

        call = client._la_client.upsert_parser.call_args
        details = call.kwargs["upsert_log_analytics_parser_details"]
        assert call.kwargs["namespace_name"] == "testns"
        assert details.name == "App_JSON"
        assert details.display_name == "App JSON"
        assert details.type == "JSON"
        assert details.header_content == "$:0"
        assert details.example_content == '{"alpha":"one"}'
        assert details.field_maps[0].parser_field_name == "udfs1"
        assert details.field_maps[0].structured_column_info == "$.alpha"
        assert result["data"] == {"parser": "ok"}

    @pytest.mark.asyncio
    async def test_upsert_delimited_parser_uses_native_parser_model(self, client):
        response = MagicMock()
        response.data = MagicMock()
        response.headers = {"opc-request-id": "parser-req"}

        with patch("oci_logan_mcp.client.oci.util.to_dict", return_value={"parser": "ok"}):
            client._la_client.upsert_parser.return_value = response
            result = await client.upsert_delimited_parser(
                parser_name="App_CSV",
                display_name="App CSV",
                field_paths=[("Source_IP", "1")],
                field_mappings={"Source_IP": "clnthostip"},
                header_content="Source IP",
                example_content="192.0.2.10\n",
            )

        call = client._la_client.upsert_parser.call_args
        details = call.kwargs["upsert_log_analytics_parser_details"]
        assert call.kwargs["namespace_name"] == "testns"
        assert details.name == "App_CSV"
        assert details.display_name == "App CSV"
        assert details.type == "DELIMITED"
        assert details.is_single_line_content is True
        assert details.field_delimiter == ","
        assert details.field_qualifier == '"'
        assert details.header_content == "Source IP"
        assert details.example_content == "192.0.2.10\n"
        assert details.field_maps[0].parser_field_sequence == 1
        assert details.field_maps[0].parser_field_name == "clnthostip"
        assert details.field_maps[0].structured_column_info is None
        assert result["data"] == {"parser": "ok"}

    @pytest.mark.asyncio
    async def test_upsert_log_source_binds_parser_and_entity_type(self, client):
        response = MagicMock()
        response.data = MagicMock()
        response.headers = {"opc-request-id": "source-req"}

        with patch("oci_logan_mcp.client.oci.util.to_dict", return_value={"source": "ok"}):
            client._la_client.upsert_source.return_value = response
            result = await client.upsert_log_source(
                source_name="App Logs",
                parser_name="App_JSON",
                display_name="App Logs",
                entity_type="omc_host_linux",
            )

        call = client._la_client.upsert_source.call_args
        details = call.kwargs["upsert_log_analytics_source_details"]
        assert call.kwargs["namespace_name"] == "testns"
        assert call.kwargs["is_ignore_warning"] is True
        assert details.name == "App Logs"
        assert details.type_name == "os_file"
        assert details.parsers[0].name == "App_JSON"
        assert details.parsers[0].parser_sequence == 1
        assert details.entity_types[0].entity_type == "omc_host_linux"
        assert result["data"] == {"source": "ok"}

    @pytest.mark.asyncio
    async def test_upload_log_file_sends_content_to_source_and_log_group(self, client):
        response = MagicMock()
        response.data = MagicMock()
        response.headers = {"opc-request-id": "req2"}

        with patch("oci_logan_mcp.client.oci.util.to_dict", return_value={"upload": "ok"}):
            client._la_client.upload_log_file.return_value = response
            result = await client.upload_log_file(
                source_name="App Logs",
                filename="sample.ndjson",
                log_group_id="ocid1.loganalyticsloggroup.oc1..test",
                content='{"event":"x"}\n',
                upload_name="sample-upload",
            )

        call = client._la_client.upload_log_file.call_args
        assert call.kwargs["namespace_name"] == "testns"
        assert call.kwargs["log_source_name"] == "App Logs"
        assert call.kwargs["filename"] == "sample.ndjson"
        assert call.kwargs["opc_meta_loggrpid"] == "ocid1.loganalyticsloggroup.oc1..test"
        assert call.kwargs["upload_log_file_body"].getvalue() == b'{"event":"x"}\n'
        assert call.kwargs["upload_name"] == "sample-upload"
        assert result["data"] == {"upload": "ok"}

    @pytest.mark.asyncio
    async def test_list_upload_files_returns_processing_status(self, client):
        item = MagicMock()
        item.reference = "file-ref"
        item.name = "sample.ndjson"
        item.status = "FAILED"
        item.total_chunks = 0
        item.chunks_consumed = 0
        item.chunks_success = 0
        item.chunks_fail = 0
        item.time_started = "2026-04-25T23:30:59+00:00"
        item.source_name = "App Logs"
        item.entity_type = "Host (Linux)"
        item.entity_name = "app-host"
        item.log_group_id = "ocid1.loganalyticsloggroup.oc1..test"
        item.log_group_name = "Testlogsources"
        item.failure_details = "Unexpected error"
        response = MagicMock()
        response.data = MagicMock()
        response.data.items = [item]
        client._la_client.list_upload_files.return_value = response

        result = await client.list_upload_files("upload-ref")

        client._la_client.list_upload_files.assert_called_once_with(
            namespace_name="testns",
            upload_reference="upload-ref",
        )
        assert result == [
            {
                "reference": "file-ref",
                "name": "sample.ndjson",
                "status": "FAILED",
                "total_chunks": 0,
                "chunks_consumed": 0,
                "chunks_success": 0,
                "chunks_fail": 0,
                "time_started": "2026-04-25T23:30:59+00:00",
                "source_name": "App Logs",
                "entity_type": "Host (Linux)",
                "entity_name": "app-host",
                "log_group_id": "ocid1.loganalyticsloggroup.oc1..test",
                "log_group_name": "Testlogsources",
                "failure_details": "Unexpected error",
            }
        ]


class TestListEntitiesPagination:
    """Test list_entities uses oci.pagination for full results."""

    @pytest.mark.asyncio
    async def test_returns_all_entities(self, client):
        """Should return all 6K+ entities across pages."""
        entities = [_make_entity(f"Entity{i}") for i in range(6000)]
        response = _make_paginated_response(entities)

        with patch("oci_logan_mcp.client.list_call_get_all_results", return_value=response):
            result = await client.list_entities()

        assert len(result) == 6000

    @pytest.mark.asyncio
    async def test_entity_type_filter(self, client):
        """Should pass entity_type_name filter."""
        response = _make_paginated_response([])

        with patch("oci_logan_mcp.client.list_call_get_all_results", return_value=response) as mock_paginate:
            await client.list_entities(entity_type="Database")

        call_kwargs = mock_paginate.call_args.kwargs
        assert call_kwargs["entity_type_name"] == ["Database"]

    @pytest.mark.asyncio
    async def test_entity_attributes(self, client):
        """Should extract entity attributes correctly."""
        e = _make_entity("myhost.example.com", entity_type="Host", lifecycle_state="ACTIVE")
        response = _make_paginated_response([e])

        with patch("oci_logan_mcp.client.list_call_get_all_results", return_value=response):
            result = await client.list_entities()

        assert result[0]["name"] == "myhost.example.com"
        assert result[0]["entity_type"] == "Host"
        assert result[0]["lifecycle_state"] == "ACTIVE"


class TestListParsersPagination:
    """Test list_parsers uses oci.pagination for full results."""

    @pytest.mark.asyncio
    async def test_returns_all_parsers(self, client):
        """Should return all 450+ parsers across pages."""
        parsers = [_make_parser(f"Parser{i}") for i in range(451)]
        response = _make_paginated_response(parsers)

        with patch("oci_logan_mcp.client.list_call_get_all_results", return_value=response):
            result = await client.list_parsers()

        assert len(result) == 451

    @pytest.mark.asyncio
    async def test_parser_attributes(self, client):
        """Should extract parser fields."""
        p = _make_parser("MyParser", type_="REGEX")
        response = _make_paginated_response([p])

        with patch("oci_logan_mcp.client.list_call_get_all_results", return_value=response):
            result = await client.list_parsers()

        assert result[0]["name"] == "MyParser"
        assert result[0]["type"] == "REGEX"


class TestListLabelsPagination:
    """Test list_labels uses oci.pagination for full results."""

    @pytest.mark.asyncio
    async def test_returns_all_labels(self, client):
        """Should return all 123+ labels across pages."""
        labels = [_make_label(f"Label{i}") for i in range(123)]
        response = _make_paginated_response(labels)

        with patch("oci_logan_mcp.client.list_call_get_all_results", return_value=response):
            result = await client.list_labels()

        assert len(result) == 123

    @pytest.mark.asyncio
    async def test_label_attributes(self, client):
        """Should extract label fields."""
        l = _make_label("Security", priority="HIGH")
        response = _make_paginated_response([l])

        with patch("oci_logan_mcp.client.list_call_get_all_results", return_value=response):
            result = await client.list_labels()

        assert result[0]["name"] == "Security"
        assert result[0]["priority"] == "HIGH"


class TestListSavedSearchesPagination:
    """Test list_saved_searches uses oci.pagination for full results."""

    @pytest.mark.asyncio
    async def test_returns_all_saved_searches(self, client):
        """Should return all 1415+ saved searches across pages."""
        searches = [_make_saved_search(f"ocid1.ss.{i}", f"Search{i}") for i in range(1415)]
        response = _make_paginated_response(searches)

        with patch("oci_logan_mcp.client.list_call_get_all_results", return_value=response):
            result = await client.list_saved_searches()

        assert len(result) == 1415

    @pytest.mark.asyncio
    async def test_passes_saved_search_task_type(self, client):
        """Should filter by SAVED_SEARCH task type."""
        response = _make_paginated_response([])

        with patch("oci_logan_mcp.client.list_call_get_all_results", return_value=response) as mock_paginate:
            await client.list_saved_searches()

        call_kwargs = mock_paginate.call_args.kwargs
        assert call_kwargs["task_type"] == "SAVED_SEARCH"

    @pytest.mark.asyncio
    async def test_handles_api_error_gracefully(self, client):
        """Should return empty list on API error."""
        with patch("oci_logan_mcp.client.list_call_get_all_results", side_effect=Exception("API Error")):
            result = await client.list_saved_searches()

        assert result == []

    @pytest.mark.asyncio
    async def test_no_orphan_em_bridges_call(self, client):
        """Ensure list_log_analytics_em_bridges is NOT called (removed orphan)."""
        response = _make_paginated_response([])

        with patch("oci_logan_mcp.client.list_call_get_all_results", return_value=response):
            await client.list_saved_searches()

        # em_bridges should never be called
        client._la_client.list_log_analytics_em_bridges.assert_not_called()


class TestListCompartmentsPagination:
    """Test list_compartments uses oci.pagination for Identity API."""

    @pytest.mark.asyncio
    async def test_returns_all_compartments(self, client):
        """Should return all compartments from Identity API."""
        compartments = [_make_compartment(f"ocid1.comp.{i}", f"Comp{i}") for i in range(50)]
        response = _make_identity_response(compartments)

        with patch("oci_logan_mcp.client.list_call_get_all_results", return_value=response):
            result = await client.list_compartments()

        assert len(result) == 50

    @pytest.mark.asyncio
    async def test_compartment_attributes(self, client):
        """Should extract compartment fields."""
        c = _make_compartment("ocid1.comp.prod", "Production", lifecycle_state="ACTIVE")
        response = _make_identity_response([c])

        with patch("oci_logan_mcp.client.list_call_get_all_results", return_value=response):
            result = await client.list_compartments()

        assert result[0]["id"] == "ocid1.comp.prod"
        assert result[0]["name"] == "Production"
        assert result[0]["lifecycle_state"] == "ACTIVE"

    @pytest.mark.asyncio
    async def test_no_tenancy_id_returns_empty(self, client):
        """Should return empty list when tenancy ID is not available."""
        client._config = {}  # No tenancy key
        client._signer = MagicMock(spec=[])  # No tenancy_id attr

        result = await client.list_compartments()
        assert result == []

    @pytest.mark.asyncio
    async def test_uses_signer_tenancy_id_fallback(self, client):
        """Should fall back to signer.tenancy_id for instance principal."""
        client._config = {}  # No tenancy in config
        client._signer = MagicMock()
        client._signer.tenancy_id = "ocid1.tenancy.from-signer"

        response = _make_identity_response([])

        with patch("oci_logan_mcp.client.list_call_get_all_results", return_value=response) as mock_paginate:
            await client.list_compartments()

        call_kwargs = mock_paginate.call_args.kwargs
        assert call_kwargs["compartment_id"] == "ocid1.tenancy.from-signer"


class TestListLogGroupsPagination:
    """Test list_log_groups uses oci.pagination for full results."""

    @pytest.mark.asyncio
    async def test_returns_all_log_groups(self, client):
        """Should return all 39+ log groups across pages."""
        groups = [_make_log_group(f"ocid1.lg.{i}", f"Group{i}") for i in range(39)]
        response = _make_paginated_response(groups)

        with patch("oci_logan_mcp.client.list_call_get_all_results", return_value=response):
            result = await client.list_log_groups()

        assert len(result) == 39

    @pytest.mark.asyncio
    async def test_log_group_attributes(self, client):
        """Should extract log group fields."""
        g = _make_log_group("ocid1.lg.prod", "Production Logs", compartment_id="ocid1.comp.prod")
        response = _make_paginated_response([g])

        with patch("oci_logan_mcp.client.list_call_get_all_results", return_value=response):
            result = await client.list_log_groups()

        assert result[0]["id"] == "ocid1.lg.prod"
        assert result[0]["display_name"] == "Production Logs"
        assert result[0]["compartment_id"] == "ocid1.comp.prod"


# ---------------------------------------------------------------------------
# Query Result Pagination Tests
# ---------------------------------------------------------------------------

class TestQueryPagination:
    """Test query result pagination in _execute_single_query."""

    @pytest.mark.asyncio
    async def test_single_page_query(self, client):
        """Should return results from single page when no pagination needed."""
        mock_data = MagicMock()
        mock_data.columns = []
        mock_data.items = []
        mock_data.total_count = 0
        mock_data.is_partial_result = False

        mock_response = MagicMock()
        mock_response.data = mock_data
        mock_response.has_next_page = False

        client._la_client.query = MagicMock(return_value=mock_response)

        result = await client._execute_single_query(
            "* | stats count", "2024-01-01T00:00:00Z", "2024-01-02T00:00:00Z",
            1000, "ocid1.compartment.test", True,
        )

        assert result["rows"] == []
        assert client._la_client.query.call_count == 1

    @pytest.mark.asyncio
    async def test_multi_page_query(self, client):
        """Should fetch all pages when has_next_page is True."""
        # Page 1
        page1_data = MagicMock()
        col = MagicMock()
        col.display_name = "count"
        col.internal_name = "count"
        col.value_type = "LONG"
        page1_data.columns = [col]
        item1 = MagicMock()
        item1.values = [100]
        page1_data.items = [item1]
        page1_data.total_count = 2
        page1_data.is_partial_result = False

        page1_response = MagicMock()
        page1_response.data = page1_data
        page1_response.has_next_page = True
        page1_response.next_page = "page2token"

        # Page 2
        page2_data = MagicMock()
        page2_data.columns = [col]
        item2 = MagicMock()
        item2.values = [200]
        page2_data.items = [item2]
        page2_data.total_count = 2
        page2_data.is_partial_result = False

        page2_response = MagicMock()
        page2_response.data = page2_data
        page2_response.has_next_page = False

        client._la_client.query = MagicMock(side_effect=[page1_response, page2_response])

        result = await client._execute_single_query(
            "* | head 2000", "2024-01-01T00:00:00Z", "2024-01-02T00:00:00Z",
            2000, "ocid1.compartment.test", True,
        )

        assert len(result["rows"]) == 2
        assert result["rows"][0] == [100]
        assert result["rows"][1] == [200]
        assert client._la_client.query.call_count == 2

    @pytest.mark.asyncio
    async def test_pagination_respects_max_results(self, client):
        """Should stop fetching pages when max_results is reached."""
        # Page 1 — returns 1000 rows, enough for max_results=1000
        page1_data = MagicMock()
        page1_data.columns = []
        items = []
        for i in range(1000):
            item = MagicMock()
            item.values = [i]
            items.append(item)
        page1_data.items = items
        page1_data.total_count = 5000
        page1_data.is_partial_result = True

        page1_response = MagicMock()
        page1_response.data = page1_data
        page1_response.has_next_page = True
        page1_response.next_page = "page2"

        client._la_client.query = MagicMock(return_value=page1_response)

        result = await client._execute_single_query(
            "* | head 5000", "2024-01-01T00:00:00Z", "2024-01-02T00:00:00Z",
            1000, "ocid1.compartment.test", True,
        )

        # Should have 1000 rows and NOT fetch page 2
        assert len(result["rows"]) == 1000
        assert client._la_client.query.call_count == 1

    @pytest.mark.asyncio
    async def test_pagination_trims_excess_rows(self, client):
        """Should trim rows to max_results when a page overshoots."""
        page1_data = MagicMock()
        page1_data.columns = []
        items = []
        for i in range(600):
            item = MagicMock()
            item.values = [i]
            items.append(item)
        page1_data.items = items
        page1_data.total_count = 1200
        page1_data.is_partial_result = True

        page1_response = MagicMock()
        page1_response.data = page1_data
        page1_response.has_next_page = True
        page1_response.next_page = "page2"

        page2_data = MagicMock()
        page2_data.columns = []
        items2 = []
        for i in range(600, 1200):
            item = MagicMock()
            item.values = [i]
            items2.append(item)
        page2_data.items = items2
        page2_data.total_count = 1200
        page2_data.is_partial_result = False

        page2_response = MagicMock()
        page2_response.data = page2_data
        page2_response.has_next_page = False

        client._la_client.query = MagicMock(side_effect=[page1_response, page2_response])
        client.settings.query.max_results = 1000

        result = await client._execute_single_query(
            "* | head 1200", "2024-01-01T00:00:00Z", "2024-01-02T00:00:00Z",
            1000, "ocid1.compartment.test", True,
        )

        assert len(result["rows"]) == 1000
        assert result["total_count"] == 1000

    @pytest.mark.asyncio
    async def test_429_retry(self, client):
        """Should retry on 429 rate limit error."""
        import oci

        error_429 = oci.exceptions.ServiceError(
            status=429, code="TooManyRequests",
            headers={}, message="Rate limited"
        )

        success_data = MagicMock()
        success_data.columns = []
        success_data.items = []
        success_data.total_count = 0
        success_data.is_partial_result = False

        success_response = MagicMock()
        success_response.data = success_data
        success_response.has_next_page = False

        client._la_client.query = MagicMock(side_effect=[error_429, success_response])

        result = await client._execute_single_query(
            "* | stats count", "2024-01-01T00:00:00Z", "2024-01-02T00:00:00Z",
            1000, "ocid1.compartment.test", True,
        )

        assert result["rows"] == []
        assert client._rate_limiter.handle_rate_limit.await_count == 1


# ---------------------------------------------------------------------------
# Entity Type Serialization Tests
# ---------------------------------------------------------------------------

class TestSerializeEntityTypes:
    """Test _serialize_entity_types helper."""

    def test_none_entity_types(self, client):
        assert client._serialize_entity_types(None) == []

    def test_entity_types_with_name_attr(self, client):
        et = MagicMock()
        et.name = "Host"
        assert client._serialize_entity_types([et]) == ["Host"]

    def test_entity_types_with_entity_type_name_attr(self, client):
        et = MagicMock(spec=["entity_type_name"])
        et.entity_type_name = "Database"
        assert client._serialize_entity_types([et]) == ["Database"]

    def test_entity_types_as_strings(self, client):
        assert client._serialize_entity_types(["Host", "VM"]) == ["Host", "VM"]

    def test_entity_types_mixed(self, client):
        et1 = MagicMock()
        et1.name = "Host"
        result = client._serialize_entity_types([et1, "VM"])
        assert result == ["Host", "VM"]


# ---------------------------------------------------------------------------
# Parse Query Response Tests
# ---------------------------------------------------------------------------

class TestParseQueryResponse:
    """Test _parse_query_response extraction."""

    @staticmethod
    def _load_query_aggregation_fixture(name: str):
        base = Path(__file__).parent / "fixtures"
        with open(base / name) as f:
            payload = json.load(f)

        def convert(value, keep_dict=False):
            if isinstance(value, dict):
                if keep_dict:
                    return {k: convert(v) for k, v in value.items()}
                return SimpleNamespace(**{
                    k: convert(v, keep_dict=(k == "items"))
                    for k, v in value.items()
                })
            if isinstance(value, list):
                return [convert(v, keep_dict=keep_dict) for v in value]
            return value

        return convert(payload)

    def test_extracts_columns(self, client):
        data = MagicMock()
        col = MagicMock()
        col.display_name = "Severity"
        col.internal_name = "severity"
        col.value_type = "STRING"
        data.columns = [col]
        data.items = []
        data.total_count = 0
        data.is_partial_result = False

        result = client._parse_query_response(data)
        assert len(result["columns"]) == 1
        assert result["columns"][0]["name"] == "Severity"
        assert result["columns"][0]["internal_name"] == "severity"

    def test_extracts_rows_from_values_list(self, client):
        data = MagicMock()
        data.columns = []
        item = MagicMock()
        item.values = ["ERROR", 42]
        data.items = [item]
        data.total_count = 1
        data.is_partial_result = False

        result = client._parse_query_response(data)
        assert result["rows"] == [["ERROR", 42]]

    def test_extracts_rows_from_dict_items(self, client):
        data = MagicMock()
        data.columns = []
        data.items = [{"severity": "ERROR", "count": 42}]
        data.total_count = 1
        data.is_partial_result = False

        result = client._parse_query_response(data)
        assert result["rows"] == [["ERROR", 42]]

    def test_aligns_dict_items_to_declared_columns(self, client):
        data = self._load_query_aggregation_fixture("query_aggregation_good_stats_log_source.json")

        result = client._parse_query_response(data)

        assert result["rows"] == [
            ["APEX Oracle Unified DB Audit Log Source", 63536],
            ["Exadata Metrics", 2155815],
        ]

    def test_fills_missing_sparse_dict_fields_with_none(self, client):
        data = self._load_query_aggregation_fixture("query_aggregation_stats_rare_severity_single_group.json")

        result = client._parse_query_response(data)

        assert result["rows"] == [[
            None,
            478150,
            1776824082000,
            1776910473000,
            None,
            None,
        ]]

    def test_parses_multi_group_rare_rows_with_display_name_keys(self, client):
        data = self._load_query_aggregation_fixture("query_aggregation_raw_rare_log_source.json")

        result = client._parse_query_response(data)

        assert result["rows"] == [
            ["OCI Audit Logs", 130, 0.00052547664],
            ["Kubernetes CronJob Object Logs", 288, 0.001164133],
        ]

    def test_empty_response(self, client):
        data = MagicMock()
        data.columns = None
        data.items = None
        data.total_count = 0
        data.is_partial_result = False

        result = client._parse_query_response(data)
        assert result["columns"] == []
        assert result["rows"] == []

    def test_is_partial_flag(self, client):
        data = MagicMock()
        data.columns = []
        data.items = []
        data.total_count = 0
        data.is_partial_result = True

        result = client._parse_query_response(data)
        assert result["is_partial"] is True


# ---------------------------------------------------------------------------
# Namespace / Compartment property tests
# ---------------------------------------------------------------------------

class TestClientProperties:
    """Test namespace and compartment_id properties."""

    def test_namespace_getter(self, client):
        assert client.namespace == "testns"

    def test_namespace_setter(self, client):
        client.namespace = "new-ns"
        assert client.namespace == "new-ns"

    def test_compartment_getter(self, client):
        assert client.compartment_id == "ocid1.compartment.default"

    def test_compartment_setter(self, client):
        client.compartment_id = "ocid1.compartment.new"
        assert client.compartment_id == "ocid1.compartment.new"


# ---------------------------------------------------------------------------
# Lazy OCI Client Tests
# ---------------------------------------------------------------------------

class TestLazyClients:
    def test_monitoring_client_created_on_first_access(self, client):
        with patch("oci.monitoring.MonitoringClient") as MockMon:
            MockMon.return_value = MagicMock()
            client._monitoring_client = None  # ensure not pre-set
            _ = client.monitoring_client
            assert MockMon.called

    def test_monitoring_client_reused_on_second_access(self, client):
        with patch("oci.monitoring.MonitoringClient") as MockMon:
            MockMon.return_value = MagicMock()
            client._monitoring_client = None  # ensure not pre-set
            _ = client.monitoring_client
            _ = client.monitoring_client
            assert MockMon.call_count == 1

    def test_dashx_client_lazy(self, client):
        with patch("oci.management_dashboard.DashxApisClient") as MockDash:
            MockDash.return_value = MagicMock()
            client._dashx_client = None  # ensure not pre-set
            _ = client.dashx_client
            assert MockDash.called

    def test_ons_client_lazy(self, client):
        with patch("oci.ons.NotificationControlPlaneClient") as MockONS:
            MockONS.return_value = MagicMock()
            client._ons_client = None  # ensure not pre-set
            _ = client.ons_client
            assert MockONS.called


# ---------------------------------------------------------------------------
# list_saved_searches freeform_tags Tests
# ---------------------------------------------------------------------------

class TestListSavedSearchesIncludesFreeformTags:
    @pytest.mark.asyncio
    async def test_freeform_tags_included(self, client):
        task = MagicMock()
        task.id = "ocid1.task.1"
        task.display_name = "My Search"
        task.task_type = "SAVED_SEARCH"
        task.lifecycle_state = "ACTIVE"
        task.freeform_tags = {"logan_managed": "true"}

        with patch("oci_logan_mcp.client.list_call_get_all_results") as mock_list:
            mock_resp = MagicMock()
            mock_resp.data = [task]
            mock_list.return_value = mock_resp
            results = await client.list_saved_searches()

        assert results[0]["freeform_tags"] == {"logan_managed": "true"}


# ---------------------------------------------------------------------------
# Fixture alias for new alarm/dashboard/ONS tests
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_client(client):
    """Alias for the client fixture used in alarm/dashboard/ONS tests."""
    return client


# ---------------------------------------------------------------------------
# Alarm, Dashboard, and ONS method tests
# ---------------------------------------------------------------------------

class TestAlertClientMethods:
    @pytest.mark.asyncio
    async def test_get_topic_calls_ons(self, mock_client):
        mock_client._ons_client = MagicMock()
        mock_client._ons_client.get_topic.return_value = MagicMock(data=MagicMock(topic_id="ocid1.topic.1", name="test", lifecycle_state="ACTIVE"))
        result = await mock_client.get_topic("ocid1.topic.1")
        mock_client._ons_client.get_topic.assert_called_once_with(topic_id="ocid1.topic.1")

    @pytest.mark.asyncio
    async def test_create_alarm_calls_monitoring(self, mock_client):
        mock_client._monitoring_client = MagicMock()
        mock_client._monitoring_client.create_alarm.return_value = MagicMock(
            data=MagicMock(id="ocid1.alarm.1", display_name="test",
                           freeform_tags={}, lifecycle_state="ACTIVE")
        )
        details = MagicMock()
        result = await mock_client.create_alarm(details)
        mock_client._monitoring_client.create_alarm.assert_called_once_with(
            create_alarm_details=details
        )
        assert result["id"] == "ocid1.alarm.1"

    @pytest.mark.asyncio
    async def test_delete_alarm_calls_monitoring(self, mock_client):
        mock_client._monitoring_client = MagicMock()
        mock_client._monitoring_client.delete_alarm.return_value = MagicMock()
        await mock_client.delete_alarm("ocid1.alarm.1")
        mock_client._monitoring_client.delete_alarm.assert_called_once_with(
            alarm_id="ocid1.alarm.1"
        )

    @pytest.mark.asyncio
    async def test_get_alarm_includes_pending_duration(self, mock_client):
        mock_client._monitoring_client = MagicMock()
        mock_client._monitoring_client.get_alarm.return_value = MagicMock(
            data=MagicMock(
                id="ocid1.alarm.1",
                display_name="test",
                lifecycle_state="ACTIVE",
                severity="CRITICAL",
                is_enabled=True,
                destinations=[],
                query="metric[1m].count() > 0",
                pending_duration="PT5M",
                compartment_id="ocid1.compartment.oc1..test",
                freeform_tags={},
            )
        )

        result = await mock_client.get_alarm("ocid1.alarm.1")

        mock_client._monitoring_client.get_alarm.assert_called_once_with(
            alarm_id="ocid1.alarm.1"
        )
        assert result["pending_duration"] == "PT5M"
        assert result["compartment_id"] == "ocid1.compartment.oc1..test"

    @pytest.mark.asyncio
    async def test_create_management_saved_search(self, mock_client):
        mock_client._dashx_client = MagicMock()
        mock_client._dashx_client.create_management_saved_search.return_value = MagicMock(
            data=MagicMock(id="ocid1.mss.1", display_name="test", freeform_tags={})
        )
        details = MagicMock()
        result = await mock_client.create_management_saved_search(details)
        assert result["id"] == "ocid1.mss.1"
