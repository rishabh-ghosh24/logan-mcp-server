"""Tests for authentication module."""

import pytest
from unittest.mock import patch, MagicMock

from oci_logan_mcp.auth import get_signer, validate_credentials
from oci_logan_mcp.config import OCIConfig


# ---------------------------------------------------------------
# get_signer dispatch
# ---------------------------------------------------------------


class TestGetSigner:
    """Tests for get_signer dispatch."""

    @patch("oci_logan_mcp.auth._get_config_file_signer")
    def test_config_file_auth_type(self, mock_fn):
        """config_file routes to _get_config_file_signer."""
        mock_fn.return_value = ({}, MagicMock())
        config = OCIConfig(auth_type="config_file")
        get_signer(config)
        mock_fn.assert_called_once_with(config)

    @patch("oci_logan_mcp.auth._get_instance_principal_signer")
    def test_instance_principal_auth_type(self, mock_fn):
        """instance_principal routes to _get_instance_principal_signer."""
        mock_fn.return_value = ({}, MagicMock())
        config = OCIConfig(auth_type="instance_principal")
        get_signer(config)
        mock_fn.assert_called_once()

    @patch("oci_logan_mcp.auth._get_resource_principal_signer")
    def test_resource_principal_auth_type(self, mock_fn):
        """resource_principal routes to _get_resource_principal_signer."""
        mock_fn.return_value = ({}, MagicMock())
        config = OCIConfig(auth_type="resource_principal")
        get_signer(config)
        mock_fn.assert_called_once()

    def test_unknown_auth_type_raises(self):
        """Unknown auth type -> ValueError."""
        config = MagicMock()
        config.auth_type = "kerberos"
        with pytest.raises(ValueError, match="Unknown auth type"):
            get_signer(config)


# ---------------------------------------------------------------
# Config file signer
# ---------------------------------------------------------------


class TestConfigFileSigner:
    """Tests for config file authentication."""

    @patch("oci_logan_mcp.auth.oci")
    def test_reads_config_and_creates_signer(self, mock_oci):
        """Reads config file and creates Signer."""
        mock_oci.config.from_file.return_value = {
            "tenancy": "ocid1.tenancy.test",
            "user": "ocid1.user.test",
            "fingerprint": "aa:bb:cc",
            "key_file": "/path/to/key.pem",
        }
        mock_oci.signer.Signer.return_value = MagicMock()

        config = OCIConfig(auth_type="config_file")
        oci_config, signer = get_signer(config)

        mock_oci.config.from_file.assert_called_once()
        mock_oci.signer.Signer.assert_called_once()
        assert oci_config["tenancy"] == "ocid1.tenancy.test"

    @patch("oci_logan_mcp.auth.oci")
    def test_passes_config_path_and_profile(self, mock_oci):
        """Config path and profile are passed to from_file."""
        mock_oci.config.from_file.return_value = {
            "tenancy": "t", "user": "u", "fingerprint": "f", "key_file": "k",
        }
        mock_oci.signer.Signer.return_value = MagicMock()

        config = OCIConfig(config_path="/custom/config", profile="PROD")
        get_signer(config)

        mock_oci.config.from_file.assert_called_once_with(
            file_location="/custom/config", profile_name="PROD"
        )


# ---------------------------------------------------------------
# Instance principal signer
# ---------------------------------------------------------------


class TestInstancePrincipalSigner:
    """Tests for instance principal authentication."""

    @patch("oci_logan_mcp.auth.oci")
    def test_creates_instance_principal_signer(self, mock_oci):
        """Creates InstancePrincipalsSecurityTokenSigner."""
        mock_signer = MagicMock()
        mock_signer.region = "us-ashburn-1"
        mock_signer.tenancy_id = "ocid1.tenancy.test"
        mock_oci.auth.signers.InstancePrincipalsSecurityTokenSigner.return_value = mock_signer

        config = OCIConfig(auth_type="instance_principal")
        oci_config, signer = get_signer(config)

        assert oci_config["region"] == "us-ashburn-1"
        assert oci_config["tenancy"] == "ocid1.tenancy.test"

    @patch("oci_logan_mcp.auth.oci")
    def test_config_no_tenancy_if_missing(self, mock_oci):
        """No tenancy_id attribute -> not in config."""
        mock_signer = MagicMock(spec=["region"])  # no tenancy_id attr
        mock_signer.region = "us-phoenix-1"
        mock_oci.auth.signers.InstancePrincipalsSecurityTokenSigner.return_value = mock_signer

        config = OCIConfig(auth_type="instance_principal")
        oci_config, _ = get_signer(config)

        assert "tenancy" not in oci_config
        assert oci_config["region"] == "us-phoenix-1"


# ---------------------------------------------------------------
# Resource principal signer
# ---------------------------------------------------------------


class TestResourcePrincipalSigner:
    """Tests for resource principal authentication."""

    @patch("oci_logan_mcp.auth.oci")
    def test_creates_resource_principal_signer(self, mock_oci):
        """Creates resource principal signer."""
        mock_signer = MagicMock()
        mock_signer.region = "us-ashburn-1"
        mock_oci.auth.signers.get_resource_principals_signer.return_value = mock_signer

        config = OCIConfig(auth_type="resource_principal")
        oci_config, signer = get_signer(config)

        assert oci_config["region"] == "us-ashburn-1"
        mock_oci.auth.signers.get_resource_principals_signer.assert_called_once()


# ---------------------------------------------------------------
# validate_credentials
# ---------------------------------------------------------------


class TestValidateCredentials:
    """Tests for credential validation."""

    @patch("oci_logan_mcp.auth.get_signer")
    @patch("oci_logan_mcp.auth.oci")
    def test_valid_credentials_return_true(self, mock_oci, mock_get_signer):
        """Valid credentials -> True."""
        mock_get_signer.return_value = ({"tenancy": "ocid1.tenancy.test"}, MagicMock())
        mock_oci.identity.IdentityClient.return_value.get_tenancy.return_value = MagicMock()

        assert validate_credentials(OCIConfig()) is True

    @patch("oci_logan_mcp.auth.get_signer")
    def test_invalid_credentials_return_false(self, mock_get_signer):
        """Exception -> False."""
        mock_get_signer.side_effect = Exception("Auth failed")
        assert validate_credentials(OCIConfig()) is False

    @patch("oci_logan_mcp.auth.get_signer")
    @patch("oci_logan_mcp.auth.oci")
    def test_signer_tenancy_id_fallback(self, mock_oci, mock_get_signer):
        """Uses signer.tenancy_id when not in config."""
        mock_signer = MagicMock()
        mock_signer.tenancy_id = "ocid1.tenancy.signer"
        mock_get_signer.return_value = ({}, mock_signer)
        mock_oci.identity.IdentityClient.return_value.get_tenancy.return_value = MagicMock()

        assert validate_credentials(OCIConfig()) is True
        mock_oci.identity.IdentityClient.return_value.get_tenancy.assert_called_once_with(
            "ocid1.tenancy.signer"
        )

    @patch("oci_logan_mcp.auth.get_signer")
    def test_no_tenancy_at_all_returns_true(self, mock_get_signer):
        """No tenancy anywhere -> True (signer creation succeeded)."""
        mock_signer = MagicMock(spec=[])  # no tenancy_id attribute
        mock_get_signer.return_value = ({}, mock_signer)

        assert validate_credentials(OCIConfig()) is True
