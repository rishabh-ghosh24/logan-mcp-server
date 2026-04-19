"""Tests for configuration module."""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from oci_logan_mcp.config import (
    Settings,
    OCIConfig,
    LogAnalyticsConfig,
    QueryConfig,
    CacheConfig,
    LoggingConfig,
    GuardrailsConfig,
    NotificationsConfig,
    SlackConfig,
    TelegramConfig,
    load_config,
    save_config,
    _parse_config,
    _apply_env_overrides,
)


class TestSettings:
    """Tests for Settings dataclass."""

    def test_default_settings(self):
        """Test default settings are created correctly."""
        settings = Settings()

        assert settings.oci.profile == "DEFAULT"
        assert settings.oci.auth_type == "config_file"
        assert settings.query.max_results == 1000
        assert settings.cache.enabled is True

    def test_settings_to_dict(self):
        """Test settings serialization."""
        settings = Settings()
        data = settings.to_dict()

        assert "oci" in data
        assert "log_analytics" in data
        assert "query" in data
        assert data["oci"]["profile"] == "DEFAULT"


class TestConfigLoader:
    """Tests for configuration loading."""

    def test_parse_empty_config(self):
        """Test parsing empty configuration."""
        settings = _parse_config({})
        assert settings.oci.profile == "DEFAULT"

    def test_parse_partial_config(self):
        """Test parsing partial configuration."""
        data = {
            "oci": {"profile": "CUSTOM"},
            "query": {"max_results": 500},
        }
        settings = _parse_config(data)

        assert settings.oci.profile == "CUSTOM"
        assert settings.query.max_results == 500
        # Other defaults should be preserved
        assert settings.cache.enabled is True

    @patch.dict("os.environ", {"OCI_LA_NAMESPACE": "test-namespace"})
    def test_env_override(self):
        """Test environment variable override."""
        from oci_logan_mcp.config import _apply_env_overrides

        settings = Settings()
        settings = _apply_env_overrides(settings)

        assert settings.log_analytics.namespace == "test-namespace"


class TestOCIConfig:
    """Tests for OCI configuration."""

    def test_default_config_path(self):
        """Test default OCI config path."""
        config = OCIConfig()
        assert config.config_path == Path.home() / ".oci" / "config"

    def test_auth_types(self):
        """Test valid auth types."""
        for auth_type in ["config_file", "instance_principal", "resource_principal"]:
            config = OCIConfig(auth_type=auth_type)
            assert config.auth_type == auth_type


class TestNotificationsConfig:
    def test_defaults_are_empty(self):
        s = Settings()
        assert s.notifications.slack.webhook_url == ""
        assert s.notifications.telegram.bot_token == ""
        assert s.notifications.telegram.default_chat_id == ""

    def test_parse_config_slack(self):
        data = {"notifications": {"slack": {"webhook_url": "https://hooks.slack.com/test"}}}
        s = _parse_config(data)
        assert s.notifications.slack.webhook_url == "https://hooks.slack.com/test"

    def test_parse_config_telegram(self):
        data = {"notifications": {"telegram": {"bot_token": "123:ABC", "default_chat_id": "-999"}}}
        s = _parse_config(data)
        assert s.notifications.telegram.bot_token == "123:ABC"
        assert s.notifications.telegram.default_chat_id == "-999"

    def test_env_override_slack(self, monkeypatch):
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/env")
        s = _apply_env_overrides(Settings())
        assert s.notifications.slack.webhook_url == "https://hooks.slack.com/env"

    def test_env_override_telegram_token(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok123")
        s = _apply_env_overrides(Settings())
        assert s.notifications.telegram.bot_token == "tok123"

    def test_env_override_telegram_chat(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100999")
        s = _apply_env_overrides(Settings())
        assert s.notifications.telegram.default_chat_id == "-100999"

    def test_to_dict_includes_notifications(self):
        s = Settings()
        s.notifications.slack.webhook_url = "https://test"
        d = s.to_dict()
        assert d["notifications"]["slack"]["webhook_url"] == "https://test"
        assert "telegram" in d["notifications"]


class TestConfirmationConfig:
    """Tests for confirmation secret and token expiry config."""

    def test_guardrails_token_expiry_default(self):
        g = GuardrailsConfig()
        assert g.token_expiry_seconds == 300

    def test_guardrails_token_expiry_from_yaml(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("guardrails:\n  token_expiry_seconds: 120\n")
        settings = load_config(config_file)
        assert settings.guardrails.token_expiry_seconds == 120


def test_settings_default_read_only_is_false():
    from oci_logan_mcp.config import Settings
    assert Settings().read_only is False


def test_env_override_read_only_true(monkeypatch, tmp_path):
    from oci_logan_mcp.config import load_config
    monkeypatch.setenv("OCI_LOGAN_MCP_READ_ONLY", "1")
    settings = load_config(config_path=tmp_path / "no.yaml")
    assert settings.read_only is True


@pytest.mark.parametrize("value,expected", [
    ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True),
    ("0", False), ("false", False), ("", False), ("no", False),
])
def test_env_override_read_only_parsing(monkeypatch, tmp_path, value, expected):
    from oci_logan_mcp.config import load_config
    monkeypatch.setenv("OCI_LOGAN_MCP_READ_ONLY", value)
    settings = load_config(config_path=tmp_path / "no.yaml")
    assert settings.read_only is expected


def test_env_override_read_only_unrecognized_warns(monkeypatch, tmp_path, caplog):
    from oci_logan_mcp.config import load_config
    monkeypatch.setenv("OCI_LOGAN_MCP_READ_ONLY", "yez")
    with caplog.at_level("WARNING"):
        settings = load_config(config_path=tmp_path / "no.yaml")
    assert settings.read_only is False  # default preserved
    assert any("OCI_LOGAN_MCP_READ_ONLY" in rec.message for rec in caplog.records)


def test_settings_has_cost_config_defaults():
    from oci_logan_mcp.config import Settings
    s = Settings()
    assert s.cost.cost_per_gb_usd == 0.05
    assert s.cost.eta_throughput_mbps == 50.0
    assert s.cost.eta_high_threshold_seconds == 60.0
    assert s.cost.probe_ttl_seconds == 900
    assert 0 < s.cost.filter_selectivity_discount <= 1


def test_settings_has_budget_config_defaults():
    from oci_logan_mcp.config import Settings
    s = Settings()
    assert s.budget.enabled is True
    assert s.budget.max_queries_per_session == 100
    assert s.budget.max_bytes_per_session == 10 * 1024**3
    assert s.budget.max_cost_usd_per_session == 5.00


def test_cost_and_budget_loaded_from_yaml(tmp_path):
    import yaml
    from oci_logan_mcp.config import load_config
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "cost": {"cost_per_gb_usd": 0.10, "probe_ttl_seconds": 120},
        "budget": {"enabled": False, "max_queries_per_session": 5},
    }))
    s = load_config(config_path=cfg_path)
    assert s.cost.cost_per_gb_usd == 0.10
    assert s.cost.probe_ttl_seconds == 120
    assert s.budget.enabled is False
    assert s.budget.max_queries_per_session == 5


def test_cost_budget_round_trips_through_save_and_load(tmp_path):
    from oci_logan_mcp.config import Settings, save_config, load_config
    cfg_path = tmp_path / "config.yaml"
    s = Settings()
    s.cost.cost_per_gb_usd = 0.12
    s.budget.max_queries_per_session = 50
    save_config(s, config_path=cfg_path)
    loaded = load_config(config_path=cfg_path)
    assert loaded.cost.cost_per_gb_usd == 0.12
    assert loaded.budget.max_queries_per_session == 50

