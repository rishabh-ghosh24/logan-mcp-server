"""Validate every packaged example without requiring OCI credentials."""

from importlib.resources import files
from unittest.mock import MagicMock

import pytest
import yaml

from oci_logan_mcp.validator import QueryValidator


SUBSTITUTIONS = {"{field}": "Log Source", "{n}": "5", "{keyword}": "error", "{level}": "ERROR"}


def _packaged(name):
    entries = yaml.safe_load((files("oci_logan_mcp") / "data" / name).read_text())["queries"]
    assert entries, f"{name} must contain executable examples"
    return entries


@pytest.mark.parametrize("entry", _packaged("starter_queries.yaml") + _packaged("builtin_queries.yaml"), ids=lambda entry: entry["name"])
def test_packaged_query_passes_offline_syntax_rules(entry):
    query = entry["query"]
    for placeholder, value in SUBSTITUTIONS.items():
        query = query.replace(placeholder, value)
    assert "''" not in query and "{" not in query
    validator = QueryValidator(MagicMock())
    assert validator._validate_syntax(query) == []
    assert validator._validate_syntax(query + " | timestats count span=1h")


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["filter_by_severity", "error_by_host"])
async def test_host_templates_accept_a_discovered_grouping_field(name):
    class TenantSchema:
        async def get_all_field_names(self):
            return ["Severity", "Entity", "Log Source"]

    entry = next(entry for entry in _packaged("builtin_queries.yaml") if entry["name"] == name)
    query = entry["query"].replace("{field}", "Entity").replace("{level}", "ERROR")
    validator = QueryValidator(TenantSchema())
    result = await validator.validate(query)
    assert result.valid, result.errors
    assert set(validator._extract_field_references(query)) == {"Severity", "Entity"}
    unavailable = await validator.validate(query.replace("'Entity'", "'Unavailable Host Field'"))
    assert not unavailable.valid and unavailable.errors
