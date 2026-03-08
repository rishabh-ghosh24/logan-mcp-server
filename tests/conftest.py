"""Shared test fixtures for OCI Log Analytics MCP Server."""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

from oci_logan_mcp.config import Settings


@pytest.fixture
def settings():
    """Create test Settings with known values."""
    s = Settings()
    s.log_analytics.namespace = "test-namespace"
    s.log_analytics.default_compartment_id = "ocid1.compartment.test"
    s.query.max_results = 1000
    s.query.default_time_range = "last_1_hour"
    return s


@pytest.fixture
def mock_oci_response():
    """Factory for creating mock OCI API responses.

    Usage:
        response = mock_oci_response(items=[...], has_next_page=False)
    """
    def _make(items=None, data=None, has_next_page=False, next_page=None):
        resp = MagicMock()
        if data is not None:
            resp.data = data
        elif items is not None:
            resp.data = MagicMock()
            resp.data.items = items
        else:
            resp.data = MagicMock()
            resp.data.items = []
        resp.has_next_page = has_next_page
        resp.next_page = next_page
        return resp
    return _make


@pytest.fixture
def mock_la_source():
    """Create a mock log source OCI object."""
    def _make(name="TestSource", display_name=None, description="", entity_types=None, is_system=False):
        src = MagicMock()
        src.name = name
        src.display_name = display_name or name
        src.description = description
        src.entity_types = entity_types or []
        src.is_system = is_system
        return src
    return _make


@pytest.fixture
def mock_la_field():
    """Create a mock field OCI object."""
    def _make(name="TestField", display_name=None, data_type="STRING", description=""):
        field = MagicMock()
        field.name = name
        field.display_name = display_name or name
        field.data_type = data_type
        field.description = description
        return field
    return _make


@pytest.fixture
def mock_la_entity():
    """Create a mock entity OCI object."""
    def _make(name="TestEntity", entity_type_name="Host", management_agent_id=None, lifecycle_state="ACTIVE"):
        entity = MagicMock()
        entity.name = name
        entity.entity_type_name = entity_type_name
        entity.management_agent_id = management_agent_id
        entity.lifecycle_state = lifecycle_state
        return entity
    return _make


@pytest.fixture
def mock_la_parser():
    """Create a mock parser OCI object."""
    def _make(name="TestParser", type_="REGEX", description="", is_system=False):
        parser = MagicMock()
        parser.name = name
        parser.type = type_
        parser.description = description
        parser.is_system = is_system
        return parser
    return _make


@pytest.fixture
def mock_la_label():
    """Create a mock label OCI object."""
    def _make(name="TestLabel", display_name=None, description="", priority="NONE"):
        label = MagicMock()
        label.name = name
        label.display_name = display_name or name
        label.description = description
        label.priority = priority
        return label
    return _make


@pytest.fixture
def mock_compartment():
    """Create a mock compartment OCI object."""
    def _make(id_="ocid1.compartment.test", name="TestCompartment", description="", lifecycle_state="ACTIVE"):
        comp = MagicMock()
        comp.id = id_
        comp.name = name
        comp.description = description
        comp.lifecycle_state = lifecycle_state
        return comp
    return _make


@pytest.fixture
def mock_log_group():
    """Create a mock log group OCI object."""
    def _make(id_="ocid1.loggroup.test", display_name="TestLogGroup", description="", compartment_id="ocid1.compartment.test"):
        lg = MagicMock()
        lg.id = id_
        lg.display_name = display_name
        lg.description = description
        lg.compartment_id = compartment_id
        return lg
    return _make


@pytest.fixture
def mock_saved_search():
    """Create a mock scheduled task (saved search) OCI object."""
    def _make(id_="ocid1.task.test", display_name="TestSearch", task_type="SAVED_SEARCH", lifecycle_state="ACTIVE"):
        ss = MagicMock()
        ss.id = id_
        ss.display_name = display_name
        ss.task_type = task_type
        ss.lifecycle_state = lifecycle_state
        return ss
    return _make


@pytest.fixture
def tmp_context_dir(tmp_path):
    """Create a temporary context directory."""
    context_dir = tmp_path / "context"
    context_dir.mkdir()
    return context_dir
