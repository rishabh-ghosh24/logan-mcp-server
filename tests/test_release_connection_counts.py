"""Connection diagnostics report aggregated records rather than result rows."""
from unittest.mock import AsyncMock

import pytest

from tests.test_metrics_service import call, make_handlers


@pytest.mark.asyncio
@pytest.mark.parametrize("rows,total_count,expected", [([[126246]], 1, 126246), ([[0]], 1, 0), ([], 0, 0)])
async def test_connection_reports_the_count_value_in_both_public_locations(tmp_path, monkeypatch, rows, total_count, expected):
    handlers, _ = make_handlers(tmp_path, monkeypatch, la_namespace="logs")
    handlers.oci_client.list_compartments = AsyncMock(return_value=[])
    handlers.oci_client.list_log_sources = AsyncMock(return_value=[])
    handlers.query_engine.execute = AsyncMock(return_value={"data": {
        "columns": [{"name": "Count", "internal_name": "func1_count", "type": "LONG"}],
        "rows": rows, "total_count": total_count, "is_partial": False,
    }})
    payload, failed = await call(handlers, "test_connection", {})
    assert not failed and payload["status"] == "All systems operational"
    assert payload["sample_query"]["result_count"] == expected
    execution = next(check for check in payload["checks"] if check["name"] == "Query execution")
    assert execution["status"] == f"OK - {expected:,} logs in last hour"
