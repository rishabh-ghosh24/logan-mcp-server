"""IP pivots must cover each supported field and quote literal inputs."""

import pytest

from oci_logan_mcp.pivot_tool import PivotTool


class FieldBackend:
    """Return one real-shaped row only when its field and value were queried."""

    def __init__(self, populated_field):
        self.populated_field = populated_field
        self.queries = []

    async def execute(self, query, **kwargs):
        self.queries.append(query)
        matches = f"'{self.populated_field}' = '192.0.2.7'" in query
        if "| stats count by 'Log Source'" in query:
            return {"data": {"columns": [{"name": "Log Source"}, {"name": "count"}],
                             "rows": [["Access Logs", 1]] if matches else []}}
        return {"data": {"columns": [{"name": "Time"}, {"name": self.populated_field}],
                         "rows": [["2026-10-25T00:01:00Z", "192.0.2.7"]] if matches else []}}


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["Source IP", "Destination IP", "Host IP Address (Client)"])
async def test_ip_found_only_in_each_supported_field_is_discovered(field):
    backend = FieldBackend(field)
    result = await PivotTool(backend).run("ip", "192.0.2.7", {"time_range": "last_15_min"})

    assert result["stats"] == {"total_events": 1, "sources_matched": 1}
    assert result["cross_source_timeline"][0][field] == "192.0.2.7"
    assert result["entity"]["field"] == "Source IP"
    assert result["entity"]["fields"] == ["Source IP", "Destination IP", "Host IP Address (Client)"]
    assert result["metadata"]["sources_queried"] == ["Access Logs"]
    assert all("'IP Address'" not in query for query in backend.queries)


@pytest.mark.asyncio
async def test_absent_ip_has_empty_timeline_with_a_positive_control():
    tool = PivotTool(FieldBackend("Host IP Address (Client)"))
    present = await tool.run("ip", "192.0.2.7", {"time_range": "last_15_min"})
    absent = await tool.run("ip", "192.0.2.8", {"time_range": "last_15_min"})
    assert present["stats"]["total_events"] == 1
    assert absent["stats"] == {"total_events": 0, "sources_matched": 0}
    assert absent["cross_source_timeline"] == []
    assert absent["metadata"]["sources_queried"] == []


@pytest.mark.asyncio
async def test_quoted_source_field_and_value_remain_literal():
    class QuotedBackend:
        async def execute(self, query, **kwargs):
            assert query == "'Log Source' = 'QA\\'s Source' and 'User\\'s Field' = 'O\\'Brien\\\\folder'"
            return {"data": {"columns": [{"name": "Time"}, {"name": "User's Field"}],
                             "rows": [["2026-10-25T00:01:00Z", "O'Brien\\folder"]]}}

    result = await PivotTool(QuotedBackend()).run(
        "custom", "O'Brien\\folder", {"time_range": "last_15_min"},
        sources=["QA's Source"], field_name="User's Field",
    )
    assert result["stats"]["total_events"] == 1
    assert result["cross_source_timeline"][0]["User's Field"] == "O'Brien\\folder"


@pytest.mark.asyncio
async def test_discovery_quotes_values_before_adding_aggregation():
    class DiscoveryBackend:
        async def execute(self, query, **kwargs):
            assert query == "'User Name' = 'O\\'Brien' | stats count by 'Log Source'"
            return {"data": {"columns": [{"name": "Log Source"}, {"name": "count"}], "rows": []}}

    result = await PivotTool(DiscoveryBackend()).run("user", "O'Brien", {"time_range": "last_15_min"})
    assert result["stats"]["total_events"] == 0
    assert result["entity"]["value"] == "O'Brien"
