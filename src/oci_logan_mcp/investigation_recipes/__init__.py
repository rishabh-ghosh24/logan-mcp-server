"""Built-in source-specific investigation recipes.

These are code-versioned probes for common Logan source families. They are
separate from recorded playbooks, which capture user audit trails.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional


@dataclass(frozen=True)
class InvestigationProbe:
    name: str
    query_tail: str
    required_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class InvestigationRecipe:
    recipe_id: str
    exact_sources: tuple[str, ...]
    family_prefixes: tuple[str, ...]
    probes: tuple[InvestigationProbe, ...]


RECIPES: tuple[InvestigationRecipe, ...] = (
    InvestigationRecipe(
        recipe_id="oci_vcn_flow_unified",
        exact_sources=("OCI VCN Flow Unified Schema Logs",),
        family_prefixes=("oci vcn flow",),
        probes=(
            InvestigationProbe(
                name="action_breakdown",
                query_tail="stats count as n by 'Action' | sort -n | head 10",
                required_fields=("Action",),
            ),
            InvestigationProbe(
                name="top_reject_destination_ports",
                query_tail=(
                    "where 'Action' = 'REJECT' | stats count as n by "
                    "'Destination Port' | sort -n | head 10"
                ),
                required_fields=("Action", "Destination Port"),
            ),
            InvestigationProbe(
                name="top_reject_source_ips",
                query_tail=(
                    "where 'Action' = 'REJECT' | stats count as n by "
                    "'Source IP' | sort -n | head 10"
                ),
                required_fields=("Action", "Source IP"),
            ),
        ),
    ),
    InvestigationRecipe(
        recipe_id="kubernetes_coredns",
        exact_sources=("Kubernetes Core DNS Logs",),
        family_prefixes=("kubernetes core dns",),
        probes=(
            InvestigationProbe(
                name="severity_split",
                query_tail="stats count as n by 'Severity' | sort -n | head 10",
                required_fields=("Severity",),
            ),
            InvestigationProbe(
                name="warning_clusters",
                query_tail="where 'Severity' = 'warning' | cluster | sort -Count | head 5",
                required_fields=("Severity",),
            ),
        ),
    ),
    InvestigationRecipe(
        recipe_id="exawatcher_top",
        exact_sources=("ExaWatcher Top Logs",),
        family_prefixes=("exawatcher top",),
        probes=(
            InvestigationProbe(
                name="top_process_clusters",
                query_tail="cluster | sort -Count | head 5",
                required_fields=(),
            ),
            InvestigationProbe(
                name="severity_split",
                query_tail="stats count as n by 'Severity' | sort -n | head 10",
                required_fields=("Severity",),
            ),
        ),
    ),
    InvestigationRecipe(
        recipe_id="kubernetes_workload",
        exact_sources=(),
        family_prefixes=("kubernetes ", "oke "),
        probes=(
            InvestigationProbe(
                name="severity_split",
                query_tail="stats count as n by 'Severity' | sort -n | head 10",
                required_fields=("Severity",),
            ),
            InvestigationProbe(
                name="error_clusters",
                query_tail="where 'Severity' in ('error', 'Error') | cluster | sort -Count | head 5",
                required_fields=("Severity",),
            ),
        ),
    ),
    InvestigationRecipe(
        recipe_id="linux_system",
        exact_sources=(),
        family_prefixes=("linux ",),
        probes=(
            InvestigationProbe(
                name="severity_split",
                query_tail="stats count as n by 'Severity' | sort -n | head 10",
                required_fields=("Severity",),
            ),
            InvestigationProbe(
                name="auth_failure_clusters",
                query_tail=(
                    "where 'Original Log Content' like '*fail*' | cluster | "
                    "sort -Count | head 5"
                ),
                required_fields=("Original Log Content",),
            ),
        ),
    ),
    InvestigationRecipe(
        recipe_id="oci_audit_events",
        exact_sources=("OCI Audit Logs", "OCI Events Logs"),
        family_prefixes=("oci audit", "oci events"),
        probes=(
            InvestigationProbe(
                name="failed_actions",
                query_tail=(
                    "where 'Response Status' >= 400 | stats count as n by "
                    "'Principal Name', 'Event Name' | sort -n | head 10"
                ),
                required_fields=("Response Status", "Principal Name", "Event Name"),
            ),
        ),
    ),
    InvestigationRecipe(
        recipe_id="oci_load_balancer",
        exact_sources=("OCI Load Balancer Access Logs", "OCI Load Balancer Error Logs"),
        family_prefixes=("oci load balancer",),
        probes=(
            InvestigationProbe(
                name="status_code_split",
                query_tail="stats count as n by 'Status Code' | sort -n | head 10",
                required_fields=("Status Code",),
            ),
        ),
    ),
    InvestigationRecipe(
        recipe_id="exawatcher_metrics",
        exact_sources=("ExaWatcher Meminfo Logs", "ExaWatcher VMStat Logs", "Exadata Metrics Logs"),
        family_prefixes=("exawatcher meminfo", "exawatcher vmstat", "exadata metrics"),
        probes=(
            InvestigationProbe(
                name="metric_clusters",
                query_tail="cluster | sort -Count | head 5",
                required_fields=(),
            ),
        ),
    ),
)


def select_recipe(source_name: str) -> Optional[InvestigationRecipe]:
    source = source_name or ""
    for recipe in RECIPES:
        if source in recipe.exact_sources:
            return recipe

    lowered = source.lower()
    matches: List[InvestigationRecipe] = []
    for recipe in RECIPES:
        if any(lowered.startswith(prefix) for prefix in recipe.family_prefixes):
            matches.append(recipe)
    return matches[0] if matches else None


def recipe_ids(recipes: Iterable[InvestigationRecipe] = RECIPES) -> List[str]:
    return [recipe.recipe_id for recipe in recipes]
