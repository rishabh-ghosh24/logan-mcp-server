"""First-run setup wizard for OCI Log Analytics MCP Server."""

import sys
from pathlib import Path
from typing import List, Optional

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

    # Step 1: OCI Config
    print("Step 1/5: OCI Configuration")
    settings.oci.config_path = _prompt_path(
        "  Path to OCI config file", default=Path.home() / ".oci" / "config"
    )
    settings.oci.profile = _prompt("  Profile name", default="DEFAULT")

    # Step 2: Auth Type
    print("\nStep 2/5: Authentication Type")
    print("  Select authentication method:")
    print("  1. Config file (local development)")
    print("  2. Instance Principal (OCI VM)")
    print("  3. Resource Principal (OCI Functions)")
    auth_options = ["config_file", "instance_principal", "resource_principal"]
    auth_choice = _prompt_choice("  Choice", options=auth_options, default=0)
    settings.oci.auth_type = auth_options[auth_choice]

    # Step 3: Namespace (try to fetch from OCI)
    print("\nStep 3/5: Log Analytics Namespace")
    namespace = _fetch_namespace(settings)
    if namespace:
        if _confirm(f"  Found namespace: {namespace}. Use this?"):
            settings.log_analytics.namespace = namespace
        else:
            settings.log_analytics.namespace = _prompt("  Enter namespace manually")
    else:
        settings.log_analytics.namespace = _prompt("  Enter namespace")

    # Step 4: Compartment (try to fetch from OCI)
    print("\nStep 4/5: Default Compartment")
    compartments = _fetch_compartments(settings)
    if compartments:
        print("  Available compartments:")
        for i, (name, ocid) in enumerate(compartments, 1):
            print(f"  {i}. {name}")
        selected = _prompt_choice("  Select default compartment", options=compartments, default=0)
        settings.log_analytics.default_compartment_id = compartments[selected][1]
    else:
        settings.log_analytics.default_compartment_id = _prompt("  Enter compartment OCID")

    # Step 5: Confirm
    print("\nStep 5/5: Confirm Configuration")
    print(f"  Namespace: {settings.log_analytics.namespace}")
    compartment_display = settings.log_analytics.default_compartment_id
    if len(compartment_display) > 50:
        compartment_display = compartment_display[:50] + "..."
    print(f"  Compartment: {compartment_display}")
    print(f"  Auth: {settings.oci.auth_type} (profile: {settings.oci.profile})")

    if _confirm("\n  Save this configuration?"):
        save_config(settings)
        print(f"\nConfiguration saved to ~/.oci-la-mcp/config.yaml")
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


def _fetch_namespace(settings: Settings) -> Optional[str]:
    """Try to fetch Log Analytics namespace from OCI."""
    try:
        import oci

        print("  Fetching namespace from OCI...")

        config = oci.config.from_file(
            file_location=str(settings.oci.config_path), profile_name=settings.oci.profile
        )

        identity_client = oci.identity.IdentityClient(config)
        tenancy = identity_client.get_tenancy(config["tenancy"]).data

        return tenancy.name.lower().replace(" ", "")

    except ImportError:
        print("  OCI SDK not installed. Please enter namespace manually.")
        return None
    except Exception as e:
        print(f"  Could not fetch namespace: {e}")
        return None


def _fetch_compartments(settings: Settings) -> List[tuple]:
    """Try to fetch compartments from OCI."""
    try:
        import oci

        print("  Fetching compartments from OCI...")

        config = oci.config.from_file(
            file_location=str(settings.oci.config_path), profile_name=settings.oci.profile
        )

        identity_client = oci.identity.IdentityClient(config)

        compartments = [(f"root ({config['tenancy'][:20]}...)", config["tenancy"])]

        response = identity_client.list_compartments(
            compartment_id=config["tenancy"],
            compartment_id_in_subtree=True,
            access_level="ACCESSIBLE",
            lifecycle_state="ACTIVE",
        )

        for comp in response.data:
            compartments.append((comp.name, comp.id))

        return compartments[:20]

    except ImportError:
        print("  OCI SDK not installed. Please enter compartment OCID manually.")
        return []
    except Exception as e:
        print(f"  Could not fetch compartments: {e}")
        return []
