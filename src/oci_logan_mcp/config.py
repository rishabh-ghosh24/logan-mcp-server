"""Configuration dataclasses and file loading for OCI Log Analytics MCP Server."""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Literal, Optional

import yaml


# Default config file location
CONFIG_PATH = Path.home() / ".oci-logan-mcp" / "config.yaml"

# Legacy config directory (for migration)
_LEGACY_CONFIG_DIR = Path.home() / ".oci-la-mcp"


# --- Dataclasses ---


@dataclass
class OCIConfig:
    """OCI authentication configuration."""

    config_path: Path = field(default_factory=lambda: Path.home() / ".oci" / "config")
    profile: str = "DEFAULT"
    auth_type: Literal["config_file", "instance_principal", "resource_principal"] = "config_file"


@dataclass
class LogAnalyticsConfig:
    """Log Analytics service configuration."""

    namespace: str = ""
    default_compartment_id: str = ""
    default_log_group_id: Optional[str] = None


@dataclass
class QueryConfig:
    """Query execution configuration."""

    default_time_range: str = "last_1_hour"
    max_results: int = 1000
    timeout_seconds: int = 60


@dataclass
class CacheConfig:
    """Caching configuration."""

    enabled: bool = True
    query_ttl_minutes: int = 5
    schema_ttl_minutes: int = 15


@dataclass
class LoggingConfig:
    """Logging configuration."""

    query_logging: bool = True
    log_path: Path = field(default_factory=lambda: Path.home() / ".oci-logan-mcp" / "logs")
    log_level: str = "INFO"


@dataclass
class GuardrailsConfig:
    """Query guardrails configuration."""

    max_time_range_days: int = 7
    warn_on_large_results: bool = True
    large_result_threshold: int = 10000
    token_expiry_seconds: int = 300


@dataclass
class SlackConfig:
    webhook_url: str = ""


@dataclass
class TelegramConfig:
    bot_token: str = ""
    default_chat_id: str = ""


@dataclass
class NotificationsConfig:
    slack: SlackConfig = field(default_factory=SlackConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)


@dataclass
class CostConfig:
    cost_per_gb_usd: float = 0.05
    eta_throughput_mbps: float = 50.0
    eta_high_threshold_seconds: float = 60.0
    probe_ttl_seconds: int = 900
    filter_selectivity_discount: float = 0.2


@dataclass
class BudgetConfig:
    enabled: bool = True
    max_queries_per_session: int = 100
    max_bytes_per_session: int = 10 * 1024**3
    max_cost_usd_per_session: float = 5.00


@dataclass
class Settings:
    """Main settings container."""

    oci: OCIConfig = field(default_factory=OCIConfig)
    log_analytics: LogAnalyticsConfig = field(default_factory=LogAnalyticsConfig)
    query: QueryConfig = field(default_factory=QueryConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    guardrails: GuardrailsConfig = field(default_factory=GuardrailsConfig)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)
    cost: CostConfig = field(default_factory=CostConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    read_only: bool = False

    def to_dict(self) -> dict:
        """Convert settings to dictionary for serialization."""
        return {
            "oci": {
                "config_path": str(self.oci.config_path),
                "profile": self.oci.profile,
                "auth_type": self.oci.auth_type,
            },
            "log_analytics": {
                "namespace": self.log_analytics.namespace,
                "default_compartment_id": self.log_analytics.default_compartment_id,
                "default_log_group_id": self.log_analytics.default_log_group_id,
            },
            "query": {
                "default_time_range": self.query.default_time_range,
                "max_results": self.query.max_results,
                "timeout_seconds": self.query.timeout_seconds,
            },
            "cache": {
                "enabled": self.cache.enabled,
                "query_ttl_minutes": self.cache.query_ttl_minutes,
                "schema_ttl_minutes": self.cache.schema_ttl_minutes,
            },
            "logging": {
                "query_logging": self.logging.query_logging,
                "log_path": str(self.logging.log_path),
                "log_level": self.logging.log_level,
            },
            "guardrails": {
                "max_time_range_days": self.guardrails.max_time_range_days,
                "warn_on_large_results": self.guardrails.warn_on_large_results,
                "large_result_threshold": self.guardrails.large_result_threshold,
            },
            "notifications": {
                "slack": {
                    "webhook_url": self.notifications.slack.webhook_url,
                },
                "telegram": {
                    "bot_token": self.notifications.telegram.bot_token,
                    "default_chat_id": self.notifications.telegram.default_chat_id,
                },
            },
            "cost": {
                "cost_per_gb_usd": self.cost.cost_per_gb_usd,
                "eta_throughput_mbps": self.cost.eta_throughput_mbps,
                "eta_high_threshold_seconds": self.cost.eta_high_threshold_seconds,
                "probe_ttl_seconds": self.cost.probe_ttl_seconds,
                "filter_selectivity_discount": self.cost.filter_selectivity_discount,
            },
            "budget": {
                "enabled": self.budget.enabled,
                "max_queries_per_session": self.budget.max_queries_per_session,
                "max_bytes_per_session": self.budget.max_bytes_per_session,
                "max_cost_usd_per_session": self.budget.max_cost_usd_per_session,
            },
        }


# --- Config Loading ---


def load_config(config_path: Optional[Path] = None) -> Settings:
    """Load configuration from file, with environment variable overrides.

    Args:
        config_path: Optional path to config file. Uses default if not specified.

    Returns:
        Settings object with loaded configuration.
    """
    settings = Settings()

    # Migrate legacy config directory if needed
    _migrate_legacy_config_dir()

    # Check for config path override from environment
    if env_config_path := os.environ.get("OCI_LA_MCP_CONFIG"):
        config_path = Path(env_config_path)
    elif config_path is None:
        config_path = CONFIG_PATH

    # Load from file if exists
    if config_path.exists():
        with open(config_path) as f:
            data = yaml.safe_load(f) or {}
            settings = _parse_config(data)

    # Apply environment variable overrides
    settings = _apply_env_overrides(settings)

    return settings


def _parse_config(data: Dict[str, Any]) -> Settings:
    """Parse configuration dictionary into Settings object."""
    settings = Settings()

    if oci_data := data.get("oci"):
        settings.oci = OCIConfig(
            config_path=Path(oci_data.get("config_path", str(settings.oci.config_path))),
            profile=oci_data.get("profile", settings.oci.profile),
            auth_type=oci_data.get("auth_type", settings.oci.auth_type),
        )

    if la_data := data.get("log_analytics"):
        settings.log_analytics = LogAnalyticsConfig(
            namespace=la_data.get("namespace", settings.log_analytics.namespace),
            default_compartment_id=la_data.get(
                "default_compartment_id", settings.log_analytics.default_compartment_id
            ),
            default_log_group_id=la_data.get("default_log_group_id"),
        )

    if query_data := data.get("query"):
        settings.query = QueryConfig(
            default_time_range=query_data.get(
                "default_time_range", settings.query.default_time_range
            ),
            max_results=query_data.get("max_results", settings.query.max_results),
            timeout_seconds=query_data.get("timeout_seconds", settings.query.timeout_seconds),
        )

    if cache_data := data.get("cache"):
        settings.cache = CacheConfig(
            enabled=cache_data.get("enabled", settings.cache.enabled),
            query_ttl_minutes=cache_data.get("query_ttl_minutes", settings.cache.query_ttl_minutes),
            schema_ttl_minutes=cache_data.get(
                "schema_ttl_minutes", settings.cache.schema_ttl_minutes
            ),
        )

    if logging_data := data.get("logging"):
        settings.logging = LoggingConfig(
            query_logging=logging_data.get("query_logging", settings.logging.query_logging),
            log_path=Path(logging_data.get("log_path", str(settings.logging.log_path))),
            log_level=logging_data.get("log_level", settings.logging.log_level),
        )

    if guardrails_data := data.get("guardrails"):
        settings.guardrails = GuardrailsConfig(
            max_time_range_days=guardrails_data.get(
                "max_time_range_days", settings.guardrails.max_time_range_days
            ),
            warn_on_large_results=guardrails_data.get(
                "warn_on_large_results", settings.guardrails.warn_on_large_results
            ),
            large_result_threshold=guardrails_data.get(
                "large_result_threshold", settings.guardrails.large_result_threshold
            ),
            token_expiry_seconds=guardrails_data.get(
                "token_expiry_seconds", settings.guardrails.token_expiry_seconds
            ),
        )

    if cost_data := data.get("cost"):
        settings.cost = CostConfig(
            cost_per_gb_usd=cost_data.get("cost_per_gb_usd", settings.cost.cost_per_gb_usd),
            eta_throughput_mbps=cost_data.get("eta_throughput_mbps", settings.cost.eta_throughput_mbps),
            eta_high_threshold_seconds=cost_data.get(
                "eta_high_threshold_seconds", settings.cost.eta_high_threshold_seconds
            ),
            probe_ttl_seconds=cost_data.get("probe_ttl_seconds", settings.cost.probe_ttl_seconds),
            filter_selectivity_discount=cost_data.get(
                "filter_selectivity_discount", settings.cost.filter_selectivity_discount
            ),
        )

    if budget_data := data.get("budget"):
        settings.budget = BudgetConfig(
            enabled=budget_data.get("enabled", settings.budget.enabled),
            max_queries_per_session=budget_data.get(
                "max_queries_per_session", settings.budget.max_queries_per_session
            ),
            max_bytes_per_session=budget_data.get(
                "max_bytes_per_session", settings.budget.max_bytes_per_session
            ),
            max_cost_usd_per_session=budget_data.get(
                "max_cost_usd_per_session", settings.budget.max_cost_usd_per_session
            ),
        )

    if notif_data := data.get("notifications"):
        if slack_data := notif_data.get("slack"):
            settings.notifications.slack = SlackConfig(
                webhook_url=slack_data.get("webhook_url", ""),
            )
        if tg_data := notif_data.get("telegram"):
            settings.notifications.telegram = TelegramConfig(
                bot_token=tg_data.get("bot_token", ""),
                default_chat_id=tg_data.get("default_chat_id", ""),
            )

    return settings


def _apply_env_overrides(settings: Settings) -> Settings:
    """Override settings with environment variables."""
    env_mappings = {
        "OCI_LA_NAMESPACE": ("log_analytics", "namespace"),
        "OCI_LA_COMPARTMENT": ("log_analytics", "default_compartment_id"),
        "OCI_CONFIG_PATH": ("oci", "config_path"),
        "OCI_CONFIG_PROFILE": ("oci", "profile"),
        "OCI_LA_AUTH_TYPE": ("oci", "auth_type"),
        "OCI_LA_TIMEOUT": ("query", "timeout_seconds"),
        "OCI_LA_LOG_LEVEL": ("logging", "log_level"),
    }

    for env_var, (section, key) in env_mappings.items():
        if value := os.environ.get(env_var):
            section_obj = getattr(settings, section)

            if key == "config_path":
                value = Path(value)
            elif key == "timeout_seconds":
                value = int(value)

            setattr(section_obj, key, value)

    if v := os.environ.get("SLACK_WEBHOOK_URL"):
        settings.notifications.slack.webhook_url = v
    if v := os.environ.get("TELEGRAM_BOT_TOKEN"):
        settings.notifications.telegram.bot_token = v
    if v := os.environ.get("TELEGRAM_CHAT_ID"):
        settings.notifications.telegram.default_chat_id = v

    if (raw := os.environ.get("OCI_LOGAN_MCP_READ_ONLY")) is not None and raw != "":
        normalized = raw.strip().lower()
        if normalized in ("1", "true", "yes", "on"):
            settings.read_only = True
        elif normalized in ("0", "false", "no", "off"):
            settings.read_only = False
        else:
            logging.getLogger(__name__).warning(
                "Unrecognized OCI_LOGAN_MCP_READ_ONLY=%r; expected one of "
                "1/true/yes/on or 0/false/no/off. Leaving read_only unchanged.",
                raw,
            )

    return settings


def _migrate_legacy_config_dir() -> None:
    """Migrate from legacy ~/.oci-la-mcp/ to ~/.oci-logan-mcp/ if needed."""
    new_dir = CONFIG_PATH.parent
    if _LEGACY_CONFIG_DIR.exists() and not new_dir.exists():
        import shutil
        import logging

        logger = logging.getLogger(__name__)
        shutil.move(str(_LEGACY_CONFIG_DIR), str(new_dir))
        logger.info(
            f"Migrated config directory from {_LEGACY_CONFIG_DIR} to {new_dir}"
        )


def save_config(settings: Settings, config_path: Optional[Path] = None) -> None:
    """Save settings to configuration file."""
    if config_path is None:
        config_path = CONFIG_PATH

    config_path.parent.mkdir(parents=True, exist_ok=True)

    data = settings.to_dict()
    with open(config_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


def config_exists(config_path: Optional[Path] = None) -> bool:
    """Check if configuration file exists."""
    if config_path is None:
        config_path = CONFIG_PATH
    return config_path.exists()
