"""Native unit labels stay intact while known percent spellings agree."""
from dataclasses import replace

import pytest

from oci_logan_mcp.metrics.catalog import MetricCatalog
from tests.test_metrics_serialization import native, normalize


@pytest.mark.parametrize("native_unit,catalog_unit", [
    ("Percent", "percent"),
    ("PERCENT", "percent"),
    ("pErCeNt", "percent"),
    ("percent", "Percent"),
])
def test_percent_capitalization_keeps_native_unit_and_points_without_a_conflict(native_unit, catalog_unit):
    definition = replace(MetricCatalog.from_package().require("cpu"), unit=catalog_unit)
    row = normalize([native(metadata={"unit": native_unit})], definition=definition)[0]
    assert row["unit"] == native_unit and row["unit_source"] == "oci"
    assert row["metadata"]["unit"] == native_unit
    assert row["warnings"] == []
    assert row["points"] == [
        ["2026-10-24T10:00:00Z", 0],
        ["2026-10-24T10:10:00Z", 1.2345678901234567],
    ]


@pytest.mark.parametrize("native_unit,catalog_unit", [("bytes", "percent"), ("mB", "MB")])
def test_real_unit_differences_keep_the_native_value_and_conflict(native_unit, catalog_unit):
    definition = replace(MetricCatalog.from_package().require("cpu"), unit=catalog_unit)
    row = normalize([native(metadata={"unit": native_unit})], definition=definition)[0]
    assert row["unit"] == native_unit and row["unit_source"] == "oci"
    assert row["metadata"]["unit"] == native_unit
    assert row["warnings"] == ["unit_conflict"]
    assert row["points"][0] == ["2026-10-24T10:00:00Z", 0]
