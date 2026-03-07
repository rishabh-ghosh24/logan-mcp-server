"""OCI authentication handlers for different auth types."""

from typing import Tuple, Any

import oci

from .config import OCIConfig


def get_signer(config: OCIConfig) -> Tuple[dict, Any]:
    """Get OCI configuration and signer based on auth type.

    Args:
        config: OCI configuration object with auth settings.

    Returns:
        Tuple of (oci_config dict, signer object).

    Raises:
        ValueError: If unknown auth type specified.
        oci.exceptions.ConfigFileNotFound: If config file not found.
        oci.exceptions.InvalidConfig: If config is invalid.
    """
    if config.auth_type == "config_file":
        return _get_config_file_signer(config)
    elif config.auth_type == "instance_principal":
        return _get_instance_principal_signer()
    elif config.auth_type == "resource_principal":
        return _get_resource_principal_signer()
    else:
        raise ValueError(f"Unknown auth type: {config.auth_type}")


def _get_config_file_signer(config: OCIConfig) -> Tuple[dict, Any]:
    """Get signer from OCI config file."""
    oci_config = oci.config.from_file(
        file_location=str(config.config_path), profile_name=config.profile
    )

    signer = oci.signer.Signer(
        tenancy=oci_config["tenancy"],
        user=oci_config["user"],
        fingerprint=oci_config["fingerprint"],
        private_key_file_location=oci_config["key_file"],
        pass_phrase=oci_config.get("pass_phrase"),
    )

    return oci_config, signer


def _get_instance_principal_signer() -> Tuple[dict, Any]:
    """Get signer for Instance Principal authentication.

    For use when running on an OCI compute instance.
    """
    signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
    oci_config = {"region": signer.region}
    # Include tenancy ID if available (needed for list_compartments, etc.)
    if hasattr(signer, "tenancy_id") and signer.tenancy_id:
        oci_config["tenancy"] = signer.tenancy_id
    return oci_config, signer


def _get_resource_principal_signer() -> Tuple[dict, Any]:
    """Get signer for Resource Principal authentication.

    For use when running in OCI Functions.
    """
    signer = oci.auth.signers.get_resource_principals_signer()
    oci_config = {"region": signer.region}
    return oci_config, signer


def validate_credentials(config: OCIConfig) -> bool:
    """Validate that credentials are working.

    Args:
        config: OCI configuration object.

    Returns:
        True if credentials are valid, False otherwise.
    """
    try:
        oci_config, signer = get_signer(config)
        tenancy_id = oci_config.get("tenancy")
        if not tenancy_id and hasattr(signer, "tenancy_id"):
            tenancy_id = signer.tenancy_id
        if not tenancy_id:
            # Can't validate without tenancy ID, but signer creation succeeded
            return True
        identity_client = oci.identity.IdentityClient(config=oci_config, signer=signer)
        identity_client.get_tenancy(tenancy_id)
        return True
    except Exception:
        return False
