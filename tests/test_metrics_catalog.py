"""Metric catalog boundaries: provenance, safe dimensions and deterministic choices."""

from copy import deepcopy
from dataclasses import FrozenInstanceError

import pytest

from oci_logan_mcp.metrics.catalog import MetricCatalog
from oci_logan_mcp.metrics.models import MetricsError


def documents():
    """Independent two-metric data, including an intentionally ambiguous alias."""
    definitions = []
    candidates = []
    for key, name, label, aliases in (
        ("cpu", "CpuUtilization", "CPU utilization", ["processor", "load"]),
        ("memory", "MemoryUtilization", "Memory utilization", ["ram", "load", "cpu"]),
    ):
        definitions.append(dict(
            metric_key=key, namespace="oci_computeagent", metric_names=[name],
            label=label, aliases=aliases, unit="percent", resource_group=None,
            resource_name_dimension="resourceDisplayName",
            group_by_dimensions=["resourceId"],
            allowed_filter_dimensions=["resourceId", "resourceDisplayName"],
            allowed_aggregations=["mean", "max", "min"],
            default_aggregation="mean", default_interval="5m",
        ))
        candidates.append(dict(metric_key=key, status="corrected", reason="Reviewed fixture",
            verification=dict(namespace="oci_computeagent", resource_group=None, metrics={name: dict(
                unit="percent", dimensions=["resourceId", "resourceDisplayName"],
                source_urls=["https://docs.oracle.com/en-us/iaas/Content/Compute/References/computemetrics.htm"],
                reviewed="2026-10-24",
            )})))
    return {"version": "fixture", "definitions": definitions}, {"version": "fixture", "candidates": candidates}


def test_cpu_catalog_has_independent_official_facts():
    catalog = MetricCatalog.from_package()
    assert isinstance(catalog, MetricCatalog)
    cpu = catalog.require("cpu")
    assert cpu.namespace == "oci_computeagent"
    assert cpu.metric_names == ("CpuUtilization",)
    assert cpu.unit == "percent"
    assert "compartmentId" not in cpu.group_by_dimensions
    assert catalog.require("memory").metric_names == ("MemoryUtilization",)
    assert catalog.search("cpu")["matches"][0]["metric_key"] == "cpu"
    with pytest.raises(FrozenInstanceError):
        cpu.namespace = "changed"


@pytest.mark.parametrize("query,kind,keys", [
    ("CPU", "key", ["cpu"]),
    ("cpuutilization", "metric_name", ["cpu"]),
    ("processor", "alias", ["cpu"]),
    ("CPU utilization", "label", ["cpu"]),
    ("CpuU", "prefix", ["cpu"]),
    ("tilization", "substring", ["cpu", "memory"]),
    ("  load  ", "alias", ["cpu", "memory"]),
])
def test_search_prefers_best_category_and_preserves_ties(query, kind, keys):
    catalog = MetricCatalog.from_documents(*documents())
    assert isinstance(catalog, MetricCatalog)
    result = catalog.search(query)
    assert result["match_type"] == kind
    assert [item["metric_key"] for item in result["matches"]] == keys
    assert result["returned_count"] == len(keys)
    assert result["ambiguous"] is (len(keys) > 1)


def test_filtered_search_and_limit_do_not_make_ambiguity_disappear():
    catalog = MetricCatalog.from_documents(*documents())
    assert isinstance(catalog, MetricCatalog)
    result = catalog.search("load", namespace="oci_computeagent", limit=1)
    assert [row["metric_key"] for row in result["matches"]] == ["cpu"]
    assert result["ambiguous"] is True
    assert result["matched_count"] == 2
    assert catalog.search("load", namespace="custom")["matches"] == []
    assert catalog.search("load", resource_group="other")["matches"] == []
    assert catalog.search("absent")["returned_count"] == 0


@pytest.mark.parametrize("arguments", [{"query": ""}, {"query": "x", "limit": 0},
    {"query": "x", "limit": 101}, {"query": "x", "limit": True}])
def test_search_rejects_invalid_inputs_after_loading_valid_catalog(arguments):
    catalog = MetricCatalog.from_documents(*documents())
    assert isinstance(catalog, MetricCatalog)
    assert catalog.require("cpu").namespace == "oci_computeagent"
    with pytest.raises(MetricsError) as caught:
        catalog.search(**arguments)
    assert caught.value.code == "invalid_arguments"


def test_unknown_key_is_a_safe_error():
    catalog = MetricCatalog.from_documents(*documents())
    assert isinstance(catalog, MetricCatalog)
    assert catalog.require("cpu").metric_names == ("CpuUtilization",)
    with pytest.raises(MetricsError) as caught:
        catalog.require("private-marker")
    assert caught.value.code == "catalog_metric_not_found"
    assert "private-marker" not in str(caught.value)


@pytest.mark.parametrize("fault", ["group", "filter", "name", "source", "duplicate", "composite", "unit"])
def test_manifest_rejects_unverified_fields(fault):
    data, manifest = deepcopy(documents())
    if fault in ("group", "filter", "name"):
        field = {"group": "group_by_dimensions", "filter": "allowed_filter_dimensions",
                 "name": "resource_name_dimension"}[fault]
        data["definitions"][0][field] = "compartmentId" if fault == "name" else ["compartmentId"]
    elif fault == "source":
        manifest["candidates"][0]["verification"]["metrics"]["CpuUtilization"]["source_urls"] = []
    elif fault == "duplicate":
        data["definitions"].append(deepcopy(data["definitions"][0]))
    elif fault == "unit":
        data["definitions"][0]["unit"] = "bytes/s"
    else:
        data["definitions"][0]["metric_names"].append("MemoryUtilization")
        manifest["candidates"][0]["verification"]["metrics"]["MemoryUtilization"] = dict(
            unit="percent", dimensions=["resourceDisplayName"],
            source_urls=["https://docs.oracle.com/metric"], reviewed="2026-10-24")
    with pytest.raises(MetricsError) as caught:
        MetricCatalog.from_documents(data, manifest)
    assert caught.value.code == "catalog_invalid"


def test_packaged_manifest_accounts_for_all_candidates_without_shipping_withheld_rows():
    from importlib.resources import files
    import json
    manifest = json.loads((files("oci_logan_mcp") / "data/metric_catalog_manifest.json").read_text())
    catalog = MetricCatalog.from_package()
    assert isinstance(catalog, MetricCatalog)
    rows = manifest["candidates"]
    assert len(rows) == 81
    assert len({row["metric_key"] for row in rows}) == 81
    assert any(row["status"] == "withheld" for row in rows)
    for row in rows:
        if row["status"] == "withheld":
            with pytest.raises(MetricsError):
                catalog.require(row["metric_key"])
        else:
            assert catalog.require(row["metric_key"]).metric_key == row["metric_key"]
