"""Immutable Monitoring contracts and safe failures."""

from dataclasses import dataclass


class MetricsError(ValueError):
    """A public error whose message contains no backend/user text."""

    def __init__(self, code, safe_message, *, status=None, request_id=None, details=None):
        super().__init__(safe_message)
        self.code = code
        self.status = status
        self.request_id = request_id
        self.details = details or {}


@dataclass(frozen=True)
class MetricDefinition:
    metric_key: str
    namespace: str
    label: str
    metric_names: tuple[str, ...]
    unit: str
    aliases: tuple[str, ...] = ()
    resource_group: str | None = None
    resource_name_dimension: str | None = None
    group_by_dimensions: tuple[str, ...] = ()
    allowed_filter_dimensions: tuple[str, ...] = ()
    allowed_aggregations: tuple[str, ...] = ("mean", "max", "min")
    default_aggregation: str = "mean"
    default_interval: str = "5m"
    source_urls: tuple[str, ...] = ()
