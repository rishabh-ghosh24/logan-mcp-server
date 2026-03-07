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
    load_config,
    save_config,
    _parse_config,
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
