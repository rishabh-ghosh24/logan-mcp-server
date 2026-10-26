"""Query limits must apply equally to live and cached results."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from oci_logan_mcp.cache import CacheManager
from oci_logan_mcp.query_engine import QueryEngine


WINDOW = {"time_start": "2026-10-25T00:00:00Z", "time_end": "2026-10-25T00:15:00Z"}


def engine_with_rows():
    async def query(**arguments):
        limit = arguments.get("max_results") or 3
        return {"columns": [{"name": "value"}], "rows": [[i] for i in range(limit)],
                "total_count": 5, "is_partial": limit < 5}

    client = SimpleNamespace(compartment_id="ocid1.compartment.test", query=AsyncMock(side_effect=query))
    return QueryEngine(client, CacheManager(), Mock()), client


@pytest.mark.asyncio
@pytest.mark.parametrize("first_limit,second_limit", [(1, 3), (3, 1)])
async def test_different_limits_never_reuse_incompatible_cached_rows(first_limit, second_limit):
    engine, client = engine_with_rows()
    first = await engine.execute("* | fields value", max_results=first_limit, **WINDOW)
    second = await engine.execute("* | fields value", max_results=second_limit, **WINDOW)
    repeated = await engine.execute("* | fields value", max_results=second_limit, **WINDOW)

    assert first["data"]["rows"] == [[i] for i in range(first_limit)]
    assert second["data"]["rows"] == [[i] for i in range(second_limit)]
    assert repeated["data"] == second["data"]
    assert [first["source"], second["source"], repeated["source"]] == ["live", "live", "cache"]
    assert client.query.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_limit", [0, -1, True, 1.5, "1"])
async def test_invalid_limit_is_rejected_even_with_a_warm_cache(invalid_limit):
    engine, client = engine_with_rows()
    valid = await engine.execute("* | fields value", **WINDOW)
    assert valid["data"]["rows"] == [[0], [1], [2]]

    with pytest.raises(ValueError, match="max_results must be a positive integer"):
        await engine.execute("* | fields value", max_results=invalid_limit, **WINDOW)
    assert client.query.await_count == 1
