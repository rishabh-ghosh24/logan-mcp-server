"""Native timeout failures retain budget after a query reaches SDK dispatch."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import oci
import pytest
from oci._vendor.requests.exceptions import ReadTimeout

from oci_logan_mcp.budget_tracker import BudgetExceededError, BudgetLimits, BudgetTracker
from oci_logan_mcp.cache import CacheManager
from oci_logan_mcp.client import OCILogAnalyticsClient
from oci_logan_mcp.config import Settings
from oci_logan_mcp.query_engine import QueryEngine
from oci_logan_mcp.query_estimator import QueryEstimate


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    ReadTimeout("synthetic read timeout"),
    oci.exceptions.RequestException(ReadTimeout("synthetic wrapped timeout")),
    oci.exceptions.ServiceError(408, "RequestTimeout", {}, "synthetic provider timeout"),
    oci.exceptions.ServiceError(504, "GatewayTimeout", {}, "synthetic provider timeout"),
], ids=["read", "wrapped-read", "provider-408", "provider-504"])
async def test_dispatched_native_timeout_keeps_the_whole_reservation(error):
    # Refunding an ambiguous timeout would make the second request execute again.
    with patch("oci_logan_mcp.client.get_signer", return_value=({"region": "us-ashburn-1"}, object())):
        client = OCILogAnalyticsClient(Settings())
    sdk_query = MagicMock(side_effect=error)
    client._la_client = SimpleNamespace(query=sdk_query)
    budget = BudgetTracker("native-timeout", BudgetLimits(max_queries_per_session=1))
    estimator = MagicMock()
    estimator.estimate = AsyncMock(return_value=QueryEstimate(512, 1, 0.25, 0, "high", "synthetic"))
    engine = QueryEngine(client, CacheManager(), MagicMock(), estimator, budget)

    with pytest.raises(type(error)):
        await engine.execute("* | stats count", use_cache=False)

    assert budget.snapshot().to_dict() == {"queries": 1, "bytes": 512, "cost_usd": 0.25}
    with pytest.raises(BudgetExceededError):
        await engine.execute("* | stats count", use_cache=False)
    assert sdk_query.call_count == 1
