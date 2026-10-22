"""Validate every packaged example without requiring OCI credentials."""

from importlib.resources import files
from unittest.mock import MagicMock

import pytest
import yaml

from oci_logan_mcp.validator import QueryValidator


SUBSTITUTIONS = {"{field}": "Log Source", "{n}": "5", "{keyword}": "error", "{level}": "ERROR"}


def _packaged(name):
    return yaml.safe_load((files("oci_logan_mcp") / "data" / name).read_text())["queries"]


@pytest.mark.parametrize("entry", _packaged("starter_queries.yaml") + _packaged("builtin_queries.yaml"), ids=lambda entry: entry["name"])
def test_packaged_query_passes_offline_syntax_rules(entry):
    query = entry["query"]
    for placeholder, value in SUBSTITUTIONS.items():
        query = query.replace(placeholder, value)
    assert "''" not in query and "{" not in query
    assert QueryValidator(MagicMock())._validate_syntax(query) == []
