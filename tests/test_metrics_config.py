"""Monitoring defaults and validation remain independent of Log Analytics setup."""

import pytest
import yaml

from oci_logan_mcp.config import Settings, load_config, save_config


def test_metrics_only_config_round_trips_and_supplies_bounded_defaults(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("oci:\n  auth_type: resource_principal\nmonitoring:\n  region: us-phoenix-1\n  default_compartment_id: selected\n")
    settings = load_config(path)
    assert settings.log_analytics.namespace == ""
    assert settings.to_dict()["monitoring"] == {
        "default_compartment_id": "selected", "region": "us-phoenix-1",
        "max_window_hours": 24, "timeout_seconds": 60, "max_series": 20,
        "max_datapoints": 4000, "max_response_bytes": 262144,
    }
    save_config(settings, path)
    assert load_config(path).to_dict()["monitoring"] == settings.to_dict()["monitoring"]


@pytest.mark.parametrize("suffix,value,field,expected", [
    ("COMPARTMENT", "chosen", "default_compartment_id", "chosen"),
    ("REGION", "us-phoenix-1", "region", "us-phoenix-1"),
    ("MAX_WINDOW_HOURS", "12", "max_window_hours", 12),
    ("TIMEOUT", "0.25", "timeout_seconds", 0.25),
    ("MAX_SERIES", "50", "max_series", 50),
    ("MAX_DATAPOINTS", "5000", "max_datapoints", 5000),
    ("MAX_RESPONSE_BYTES", "300000", "max_response_bytes", 300000),
])
def test_monitoring_environment_override_wins_over_yaml(tmp_path, monkeypatch, suffix, value, field, expected):
    path = tmp_path / "config.yaml"
    path.write_text("monitoring:\n  region: us-ashburn-1\n")
    monkeypatch.setenv("OCI_LOGAN_MONITORING_" + suffix, value)
    result = load_config(path)
    assert result.to_dict()["monitoring"][field] == expected


@pytest.mark.parametrize("field,value", [
    ("max_window_hours", 0), ("max_window_hours", 25),
    ("max_series", True), ("max_series", 1.5), ("max_series", 101),
    ("max_datapoints", 0), ("max_datapoints", 10001),
    ("max_response_bytes", 1023), ("max_response_bytes", 1000001),
    ("timeout_seconds", float("nan")), ("timeout_seconds", float("inf")),
    ("timeout_seconds", -1), ("timeout_seconds", False),
])
def test_invalid_monitoring_config_is_rejected_before_execution(tmp_path, field, value):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"monitoring": {field: value}}))
    with pytest.raises(ValueError, match="monitoring"):
        load_config(path)


@pytest.mark.parametrize("value", ["nan", "0", "true"])
def test_invalid_monitoring_environment_is_not_silently_accepted(tmp_path, monkeypatch, value):
    monkeypatch.setenv("OCI_LOGAN_MONITORING_TIMEOUT", value)
    with pytest.raises(ValueError, match="monitoring"):
        load_config(tmp_path / "absent.yaml")
