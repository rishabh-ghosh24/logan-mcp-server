"""First-run setup wizard for OCI Log Analytics MCP Server."""

import sys
from pathlib import Path
from typing import List, Optional, Tuple, Any

from .config import Settings, save_config


def run_setup_wizard() -> Settings:
    """Interactive first-run configuration wizard.

    Returns:
        Configured Settings object.
    """
    print("\nWelcome to OCI Log Analytics MCP Server!")
    print("=" * 50)
    print("\nLet's configure your connection to OCI Log Analytics.\n")

    settings = Settings()

    # Step 1: Auth Type (ask first — determines what else is needed)
    print("Step 1/4: Authentication Type")
    print("  Select authentication method:")
    print("  1. Instance Principal (OCI VM — recommended for VM deployments)")
    print("  2. Config file (local development with ~/.oci/config)")
    print("  3. Resource Principal (OCI Functions)")
    auth_options = ["instance_principal", "config_file", "resource_principal"]
    auth_choice = _prompt_choice("  Choice", options=auth_options, default=0)
    settings.oci.auth_type = auth_options[auth_choice]

    # Step 2: OCI Config (only for config_file auth)
    if settings.oci.auth_type == "config_file":
        print("\nStep 2/4: OCI Configuration")
        settings.oci.config_path = _prompt_path(
            "  Path to OCI config file", default=Path.home() / ".oci" / "config"
        )
        settings.oci.profile = _prompt("  Profile name", default="DEFAULT")
    else:
        print("\nStep 2/4: OCI Configuration")
        print(f"  Using {settings.oci.auth_type} — no config file needed.")

    # Step 3: Namespace (try to fetch from OCI)
    print("\nStep 3/4: Log Analytics Namespace")
    namespace = _fetch_namespace(settings)
    if namespace:
        if _confirm(f"  Found namespace: {namespace}. Use this?"):
            settings.log_analytics.namespace = namespace
        else:
            settings.log_analytics.namespace = _prompt("  Enter namespace manually")
    else:
        settings.log_analytics.namespace = _prompt("  Enter namespace")

    # Step 4: Compartment (try to fetch from OCI)
    print("\nStep 4/4: Default Compartment")
    compartments = _fetch_compartments(settings)
    if compartments:
        print("  Available compartments:")
        for i, (name, ocid) in enumerate(compartments, 1):
            short_ocid = ocid[:40] + "..." if len(ocid) > 40 else ocid
            print(f"  {i}. {name} ({short_ocid})")
        print(f"  Or enter a compartment OCID directly.")
        selected = _prompt_compartment(
            "  Select compartment (number or OCID)", compartments, default=0
        )
        settings.log_analytics.default_compartment_id = selected
    else:
        settings.log_analytics.default_compartment_id = _prompt("  Enter compartment OCID")

    # Confirm
    print("\nConfirm Configuration")
    print(f"  Auth type:    {settings.oci.auth_type}")
    if settings.oci.auth_type == "config_file":
        print(f"  Config file:  {settings.oci.config_path}")
        print(f"  Profile:      {settings.oci.profile}")
    print(f"  Namespace:    {settings.log_analytics.namespace}")
    compartment_display = settings.log_analytics.default_compartment_id
    if len(compartment_display) > 50:
        compartment_display = compartment_display[:50] + "..."
    print(f"  Compartment:  {compartment_display}")

    if _confirm("\nSave this configuration?"):
        save_config(settings)
        print(f"\nConfiguration saved to ~/.oci-logan-mcp/config.yaml")
        print("MCP Server is ready!\n")
    else:
        print("\nConfiguration not saved. You can run the wizard again later.\n")

    return settings


def _prompt(message: str, default: Optional[str] = None) -> str:
    """Prompt user for text input."""
    if default:
        prompt_text = f"{message} [{default}]: "
    else:
        prompt_text = f"{message}: "

    try:
        value = input(prompt_text).strip()
        return value if value else (default or "")
    except (EOFError, KeyboardInterrupt):
        print("\nSetup cancelled.")
        sys.exit(1)


def _prompt_path(message: str, default: Optional[Path] = None) -> Path:
    """Prompt user for file path input."""
    default_str = str(default) if default else None
    value = _prompt(message, default=default_str)
    return Path(value).expanduser()


def _prompt_choice(message: str, options: list, default: int = 0) -> int:
    """Prompt user to choose from options."""
    default_display = default + 1
    prompt_text = f"{message} [{default_display}]: "

    try:
        value = input(prompt_text).strip()
        if not value:
            return default
        choice = int(value) - 1
        if 0 <= choice < len(options):
            return choice
        print(f"  Invalid choice. Using default: {default_display}")
        return default
    except ValueError:
        print(f"  Invalid input. Using default: {default_display}")
        return default
    except (EOFError, KeyboardInterrupt):
        print("\nSetup cancelled.")
        sys.exit(1)


def _prompt_compartment(message: str, compartments: list, default: int = 0) -> str:
    """Prompt user to select a compartment by number or enter an OCID directly.

    Returns the selected compartment OCID string.
    """
    default_display = default + 1
    prompt_text = f"{message} [{default_display}]: "

    try:
        value = input(prompt_text).strip()
        if not value:
            return compartments[default][1]

        # Check if user entered a raw OCID
        if value.startswith("ocid1."):
            return value

        # Try to parse as a number (list selection)
        try:
            choice = int(value) - 1
            if 0 <= choice < len(compartments):
                return compartments[choice][1]
            print(f"  Invalid choice. Using default: {default_display}")
            return compartments[default][1]
        except ValueError:
            # Not a number and not an OCID — use default
            print(f"  Invalid input. Using default: {default_display}")
            return compartments[default][1]

    except (EOFError, KeyboardInterrupt):
        print("\nSetup cancelled.")
        sys.exit(1)


def _confirm(message: str, default: bool = True) -> bool:
    """Prompt user for yes/no confirmation."""
    default_str = "Y/n" if default else "y/N"
    prompt_text = f"{message} [{default_str}]: "

    try:
        value = input(prompt_text).strip().lower()
        if not value:
            return default
        return value in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print("\nSetup cancelled.")
        sys.exit(1)


def _get_oci_clients(settings: Settings) -> Tuple[Any, Any]:
    """Create OCI clients using the selected auth type.

    Returns:
        Tuple of (identity_client, config_dict) or (None, None) on failure.
    """
    try:
        import oci

        if settings.oci.auth_type == "config_file":
            config = oci.config.from_file(
                file_location=str(settings.oci.config_path),
                profile_name=settings.oci.profile,
            )
            identity_client = oci.identity.IdentityClient(config)
            return identity_client, config

        elif settings.oci.auth_type == "instance_principal":
            signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
            config = {"region": signer.region}
            # Try to get tenancy ID from signer
            if hasattr(signer, "tenancy_id"):
                config["tenancy"] = signer.tenancy_id
            identity_client = oci.identity.IdentityClient(
                config=config, signer=signer
            )
            return identity_client, config

        elif settings.oci.auth_type == "resource_principal":
            signer = oci.auth.signers.get_resource_principals_signer()
            config = {"region": signer.region}
            identity_client = oci.identity.IdentityClient(
                config=config, signer=signer
            )
            return identity_client, config

    except ImportError:
        print("  OCI SDK not installed.")
    except Exception as e:
        print(f"  Could not create OCI client: {e}")

    return None, None


def _fetch_namespace(settings: Settings) -> Optional[str]:
    """Try to fetch Log Analytics namespace from OCI.

    Uses the Log Analytics API (get_namespace) to retrieve the actual
    namespace, which may differ from the tenancy name.
    """
    try:
        import oci

        print("  Fetching namespace from OCI...")

        if settings.oci.auth_type == "config_file":
            config = oci.config.from_file(
                file_location=str(settings.oci.config_path),
                profile_name=settings.oci.profile,
            )
            la_client = oci.log_analytics.LogAnalyticsClient(config)
            tenancy_id = config.get("tenancy")
        elif settings.oci.auth_type == "instance_principal":
            signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
            config = {"region": signer.region}
            la_client = oci.log_analytics.LogAnalyticsClient(
                config=config, signer=signer
            )
            tenancy_id = getattr(signer, "tenancy_id", None)
        elif settings.oci.auth_type == "resource_principal":
            signer = oci.auth.signers.get_resource_principals_signer()
            config = {"region": signer.region}
            la_client = oci.log_analytics.LogAnalyticsClient(
                config=config, signer=signer
            )
            tenancy_id = getattr(signer, "tenancy_id", None)
        else:
            return None

        if not tenancy_id:
            print("  Could not determine tenancy ID for namespace lookup.")
            return None

        # list_namespaces returns the actual LA namespace for this tenancy
        response = la_client.list_namespaces(compartment_id=tenancy_id)
        if response.data and hasattr(response.data, "items") and response.data.items:
            return response.data.items[0].namespace_name

        return None

    except Exception as e:
        print(f"  Could not fetch namespace: {e}")
        return None


def _fetch_compartments(settings: Settings) -> List[tuple]:
    """Try to fetch compartments from OCI."""
    try:
        print("  Fetching compartments from OCI...")
        identity_client, config = _get_oci_clients(settings)
        if identity_client is None:
            return []

        tenancy_id = config.get("tenancy")
        if not tenancy_id:
            print("  Could not determine tenancy ID. Enter compartment OCID manually.")
            return []

        compartments = [(f"root ({tenancy_id[:30]}...)", tenancy_id)]

        response = identity_client.list_compartments(
            compartment_id=tenancy_id,
            compartment_id_in_subtree=True,
            access_level="ACCESSIBLE",
            lifecycle_state="ACTIVE",
        )

        for comp in response.data:
            compartments.append((comp.name, comp.id))

        return compartments[:20]

    except Exception as e:
        print(f"  Could not fetch compartments: {e}")
        return []
