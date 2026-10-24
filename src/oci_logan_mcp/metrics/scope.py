"""Resolve and pin Monitoring scope without querying Identity."""
from collections.abc import Mapping
import re
from typing import Any
from ..config import Settings
from .models import MetricScope, MetricsError


def resolve_metric_scope(arguments: Mapping, settings: Settings, *,
                         shared_compartment_id: str | None, oci_config: Mapping,
                         signer: Any) -> MetricScope:
    compartment = (arguments.get('compartment_id') or shared_compartment_id
                   or settings.monitoring.default_compartment_id
                   or settings.log_analytics.default_compartment_id)
    if not isinstance(compartment, str) or not compartment.strip():
        raise MetricsError('missing_compartment', 'Choose a compartment_id before querying metrics.')
    region = (arguments.get('region') or settings.monitoring.region
              or oci_config.get('region') or getattr(signer, 'region', None))
    if not isinstance(region, str) or not re.fullmatch(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)+-\d+', region):
        raise MetricsError('invalid_region', 'Choose a valid OCI region in the request or configuration.')
    subtree = arguments.get('include_subcompartments', False)
    if type(subtree) is not bool:
        raise MetricsError('invalid_subtree_scope', 'include_subcompartments must be a boolean.')
    tenancy = oci_config.get('tenancy') or getattr(signer, 'tenancy_id', None) or None
    if subtree and tenancy and compartment != tenancy and not compartment.startswith('ocid1.tenancy.'):
        raise MetricsError('invalid_subtree_scope', 'Subtree queries require a tenancy root compartment_id.')
    return MetricScope(compartment, region, subtree, tenancy)
