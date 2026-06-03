# src/oci_logan_mcp/access_control.py
"""CAM access-control: per-user entity isolation for the Assurance team.

Single source of truth for authorization, modeled on read_only_guard.py.
Inert unless an AccessProfile is built (i.e. unless --enforce-access is set).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Tuple

import yaml


class AccessConfigError(Exception):
    """Raised when access_control.yaml is missing or invalid (fail-closed)."""


@dataclass(frozen=True)
class CamEntry:
    customers: Tuple[int, ...]
    allow_delivery: bool


@dataclass(frozen=True)
class AccessControlConfig:
    tenancy_id: str
    compartment_id: str
    namespace: str
    entity_field: str
    default_allow_delivery: bool
    cams: Dict[str, CamEntry]


def load_access_config(path: Path) -> AccessControlConfig:
    """Load and validate access_control.yaml. Raises AccessConfigError on any problem."""
    if not Path(path).is_file():
        raise AccessConfigError(f"access_control.yaml not found at {path}")
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise AccessConfigError(f"access_control.yaml is not valid YAML: {exc}") from exc

    for required in ("compartment_id", "namespace"):
        if not raw.get(required):
            raise AccessConfigError(f"access_control.yaml missing required field '{required}'")

    defaults = raw.get("defaults") or {}
    default_allow_delivery = bool(defaults.get("allow_delivery", True))

    cams: Dict[str, CamEntry] = {}
    for cam_id, entry in (raw.get("cams") or {}).items():
        entry = entry or {}
        customers = tuple(int(n) for n in (entry.get("customers") or []))
        allow_delivery = bool(entry.get("allow_delivery", default_allow_delivery))
        cams[cam_id] = CamEntry(customers=customers, allow_delivery=allow_delivery)

    return AccessControlConfig(
        tenancy_id=str(raw.get("tenancy_id", "")),
        compartment_id=str(raw["compartment_id"]),
        namespace=str(raw["namespace"]),
        entity_field=str(raw.get("entity_field", "Entity")),
        default_allow_delivery=default_allow_delivery,
        cams=cams,
    )


from typing import FrozenSet, Iterable


def entity_matches(entity_name: str, number: int) -> bool:
    """True iff entity_name is `<number>` or starts with `<number>_`.

    The number is matched as the exact integer string at the very start; the
    portion after the first '_' (the customer name) is never inspected.
    """
    prefix = str(number)
    return entity_name == prefix or entity_name.startswith(prefix + "_")


def resolve_entities(numbers: Iterable[int], all_entity_names: Iterable[str]) -> FrozenSet[str]:
    """Return the subset of all_entity_names matching any of the given numbers."""
    nums = tuple(numbers)
    return frozenset(
        name for name in all_entity_names if any(entity_matches(name, n) for n in nums)
    )


from typing import List


@dataclass(frozen=True)
class AccessProfile:
    """Resolved per-process authorization context for one CAM."""
    user_id: str
    customer_numbers: Tuple[int, ...]
    entity_names: FrozenSet[str]
    entity_field: str
    compartment_id: str
    namespace: str
    allow_delivery: bool


def build_profile(
    config: AccessControlConfig, user_id: str, all_entity_names: List[str]
) -> AccessProfile:
    """Build a CAM's AccessProfile, failing closed on any misconfiguration.

    Raises AccessConfigError if the user is unknown, has no customers, or resolves
    to zero live entities. Callers must treat a raise as process-fatal.
    """
    entry = config.cams.get(user_id)
    if entry is None:
        raise AccessConfigError(
            f"--enforce-access set but user '{user_id}' is not in access_control.yaml"
        )
    if not entry.customers:
        raise AccessConfigError(f"CAM '{user_id}' has an empty customers list")

    entity_names = resolve_entities(entry.customers, all_entity_names)
    if not entity_names:
        raise AccessConfigError(
            f"CAM '{user_id}' customer numbers {list(entry.customers)} matched no live "
            f"entities in compartment {config.compartment_id}"
        )

    return AccessProfile(
        user_id=user_id,
        customer_numbers=entry.customers,
        entity_names=entity_names,
        entity_field=config.entity_field,
        compartment_id=config.compartment_id,
        namespace=config.namespace,
        allow_delivery=entry.allow_delivery,
    )
