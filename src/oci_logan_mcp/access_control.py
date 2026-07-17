# src/oci_logan_mcp/access_control.py
"""CAM access-control: per-user entity isolation for the Assurance team.

Single source of truth for authorization, modeled on read_only_guard.py.
Inert unless an AccessProfile is built (i.e. unless --enforce-access is set).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, List, Tuple

import yaml


class AccessConfigError(Exception):
    """Raised when access_control.yaml is missing or invalid (fail-closed)."""


CAM_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
MAX_CAM_ID_LENGTH = 64
ENTITY_FIELD_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_. -]*$")
MAX_ENTITY_FIELD_LENGTH = 128


def validate_cam_id(cam_id: object) -> str:
    """Return a canonical CAM id or raise on an unsafe identity."""
    if not isinstance(cam_id, str):
        raise AccessConfigError("CAM id must be a string")
    if len(cam_id) > MAX_CAM_ID_LENGTH or CAM_ID_RE.fullmatch(cam_id) is None:
        raise AccessConfigError(
            "CAM id must be 1-64 lowercase ASCII characters using letters, "
            "digits, '.', '_' or '-' without leading, trailing, or repeated separators"
        )
    return cam_id


def validate_customer_numbers(value: object, field_path: str) -> Tuple[int, ...]:
    """Accept only an exact YAML list of positive integers."""
    if not isinstance(value, list) or any(
        type(number) is not int or number <= 0 for number in value
    ):
        raise AccessConfigError(
            f"access_control.yaml field '{field_path}' must be a list of positive integers"
        )
    return tuple(value)


def validate_entity_field(value: object) -> str:
    """Return a field name that is safe inside a single-quoted query identifier."""
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= MAX_ENTITY_FIELD_LENGTH
        or value.strip() != value
        or ENTITY_FIELD_RE.fullmatch(value) is None
    ):
        raise AccessConfigError(
            "access_control.yaml field 'entity_field' must be a 1-128 character "
            "ASCII identifier using letters, digits, spaces, '.', '_' or '-'"
        )
    return value


@dataclass(frozen=True)
class CamEntry:
    customers: Tuple[int, ...]
    allow_delivery: bool
    resolved_entities: Tuple[str, ...] = ()


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
    if not isinstance(raw, dict):
        raise AccessConfigError("access_control.yaml top-level value must be a mapping")

    for required in ("compartment_id", "namespace"):
        if not raw.get(required):
            raise AccessConfigError(f"access_control.yaml missing required field '{required}'")

    defaults = _mapping(raw.get("defaults", {}), "defaults")
    default_allow_delivery = _bool_field(
        defaults, "allow_delivery", True, "defaults.allow_delivery"
    )

    cams: Dict[str, CamEntry] = {}
    for raw_cam_id, raw_entry in _mapping(raw.get("cams", {}), "cams").items():
        cam_id = validate_cam_id(raw_cam_id)
        entry = _mapping(
            raw_entry if raw_entry is not None else {},
            f"cams.{cam_id}",
        )
        customers = validate_customer_numbers(
            entry.get("customers", []),
            f"cams.{cam_id}.customers",
        )
        allow_delivery = _bool_field(
            entry,
            "allow_delivery",
            default_allow_delivery,
            f"cams.{cam_id}.allow_delivery",
        )
        resolved_entities = _resolved_entities_field(
            entry.get("resolved_entities"),
            customers,
            f"cams.{cam_id}.resolved_entities",
        )
        cams[cam_id] = CamEntry(
            customers=customers,
            allow_delivery=allow_delivery,
            resolved_entities=resolved_entities,
        )

    return AccessControlConfig(
        tenancy_id=str(raw.get("tenancy_id", "")),
        compartment_id=str(raw["compartment_id"]),
        namespace=str(raw["namespace"]),
        entity_field=validate_entity_field(raw.get("entity_field", "Entity")),
        default_allow_delivery=default_allow_delivery,
        cams=cams,
    )


def _mapping(value: object, field_path: str) -> dict:
    if not isinstance(value, dict):
        raise AccessConfigError(f"access_control.yaml field '{field_path}' must be a mapping")
    return value


def _bool_field(mapping: dict, key: str, default: bool, field_path: str) -> bool:
    if key not in mapping:
        return default
    value = mapping[key]
    if not isinstance(value, bool):
        raise AccessConfigError(
            f"access_control.yaml field '{field_path}' must be a boolean"
        )
    return value


def _resolved_entities_field(
    value: object,
    customers: Tuple[int, ...],
    field_path: str,
) -> Tuple[str, ...]:
    """Validate an optional root-controlled entity-resolution snapshot."""
    if value is None:
        return ()
    if (
        not isinstance(value, list)
        or not value
        or any(
            not isinstance(name, str)
            or not 1 <= len(name) <= 1024
            or name.strip() != name
            or not name.isprintable()
            for name in value
        )
        or len(set(value)) != len(value)
    ):
        raise AccessConfigError(
            f"access_control.yaml field '{field_path}' must be a non-empty "
            "list of unique printable entity names"
        )
    if any(
        not any(
            name == str(number) or name.startswith(f"{number}_")
            for number in customers
        )
        for name in value
    ):
        raise AccessConfigError(
            f"access_control.yaml field '{field_path}' contains an entity "
            "outside the CAM customer allocation"
        )
    return tuple(value)


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


class QueryNotAllowed(Exception):
    """Raised when a CAM query cannot be safely scoped (fail-closed)."""


class EntityAccessDenied(QueryNotAllowed):
    """Raised when a CAM explicitly requests an entity outside their allocation."""


# Exact allowlist of pipeline commands that operate only on the already-scoped
# record set (no sub-query, no source/time/entity re-selector). See spec 6.5.
CAM_QUERY_COMMANDS: FrozenSet[str] = frozenset({
    "stats", "timestats", "eventstats", "where", "eval", "sort",
    "head", "tail", "fields", "fieldsummary", "distinct",
    "top", "bottom", "rename",
})


def _split_top_level_pipes(query: str) -> List[str]:
    """Split on '|' that are not inside single/double quotes."""
    segments, buf, quote = [], [], None
    for ch in query:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif ch == "|":
            segments.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if quote is not None:
        raise QueryNotAllowed("Unbalanced quote in query; cannot scope safely.")
    segments.append("".join(buf))
    return segments


def _leading_token(segment: str) -> str:
    return segment.strip().split(None, 1)[0].lower() if segment.strip() else ""


_FIELD = r"(?:'[^']+'|\"[^\"]+\"|[A-Za-z_][\w.]*)"
_SCALAR = r"(?:'[^']*'|\"[^\"]*\"|-?\d+(?:\.\d+)?|[A-Za-z_][\w.]*)"
_INLIST = r"\(\s*" + _SCALAR + r"(?:\s*,\s*" + _SCALAR + r")*\s*\)"
_PREDICATE = (
    r"(?:"
    + _FIELD + r"\s*(?:=|!=|<>|<=|>=|<|>)\s*" + _SCALAR
    + r"|" + _FIELD + r"\s+in\s+" + _INLIST
    + r"|" + _FIELD + r"\s+(?:like|contains)\s+" + _SCALAR
    + r"|" + _FIELD + r"\s+is\s+(?:not\s+)?null"
    + r")"
)
_TERM = r"(?:not\s+)?(?:\(\s*" + _PREDICATE + r"\s*\)|" + _PREDICATE + r")"
_HEAD_GRAMMAR = re.compile(
    _TERM + r"(?:\s+(?:and|or)\s+" + _TERM + r")*",
    re.IGNORECASE,
)


def _head_is_search_expression(head: str) -> bool:
    """Full-match check that the head is a pure search/filter expression.

    Fail-closed: the ENTIRE head must be field predicates joined by and/or
    (optionally negated or single-paren-wrapped), or '*'. Any trailing or
    embedded command text causes a non-match and is rejected.
    """
    stripped = head.strip()
    if stripped in ("", "*"):
        return True
    return _HEAD_GRAMMAR.fullmatch(stripped) is not None


def validate_cam_query(query: str) -> None:
    """Reject anything that could open an unscoped data context. See spec 6.5."""
    if "[" in query or "]" in query:
        raise QueryNotAllowed("Sub-query brackets are not permitted in access-controlled mode.")
    segments = _split_top_level_pipes(query)
    head = segments[0].strip()
    # (c) head must be a pure search expression, not a command invocation
    head_token = _leading_token(head)
    if head_token and head_token != "*" and head_token in _ALL_KNOWN_COMMANDS:
        raise QueryNotAllowed(
            f"Query may not begin with the command '{head_token}'; the leading "
            f"segment must be a search expression."
        )
    if not _head_is_search_expression(head):
        raise QueryNotAllowed(
            "The leading query segment must be a field predicate or '*'; "
            "command-form heads are not permitted in access-controlled mode."
        )
    # (b) every pipeline command must be in the allowlist
    for seg in segments[1:]:
        cmd = _leading_token(seg)
        if cmd not in CAM_QUERY_COMMANDS:
            raise QueryNotAllowed(f"Pipeline command '{cmd}' is not permitted in access-controlled mode.")


# Known command keywords that must never start a query head (context-openers and
# any pipeline command). Used only by validate_cam_query rule (c).
_ALL_KNOWN_COMMANDS: FrozenSet[str] = CAM_QUERY_COMMANDS | frozenset({
    "searchlookup", "lookup", "createview", "map", "updatetable",
    "link", "classify", "addfields", "nlp", "cluster", "regex", "extract",
})


_SAFE_ENTITY_RE = re.compile(r"[A-Za-z0-9_.\- ]+")


def _quote_value(value: str) -> str:
    if _SAFE_ENTITY_RE.fullmatch(value) is None:
        raise QueryNotAllowed(
            f"Entity name {value!r} contains unsafe characters; cannot scope safely."
        )
    return f"'{value}'"


def _explicit_entity_values(query: str, entity_field: str) -> FrozenSet[str]:
    """Return quoted entity values explicitly selected by entity predicates.

    CAM query validation has already established that the head and ``where``
    pipeline expressions are simple predicates. We inspect equality and ``in``
    filters on the configured entity field and the Log Analytics ``entityname``
    alias: those are unambiguous requests for a particular customer's data.
    Other predicates remain safely intersected with the CAM's allocated entity
    set by :func:`scope_query`.
    """
    segments = _split_top_level_pipes(query)
    predicates = [segments[0]]
    for segment in segments[1:]:
        where = re.match(r"^\s*where\s+(.+)$", segment, re.IGNORECASE)
        if where:
            predicates.append(where.group(1))

    selector_fields = [entity_field]
    if entity_field.casefold() != "entityname":
        selector_fields.append("entityname")
    escaped_fields = "|".join(re.escape(value) for value in selector_fields)
    field = rf"(?:'(?:{escaped_fields})'|\"(?:{escaped_fields})\"|(?:{escaped_fields}))"
    boundary_before = r"(?<![A-Za-z0-9_.])"
    boundary_after = r"(?![A-Za-z0-9_.])"
    values = set()

    def is_negated(predicate: str, match_start: int) -> bool:
        preceding = predicate[:match_start].rstrip().lower()
        return preceding.endswith("not") or preceding.endswith("not (")

    equality = re.compile(
        rf"{boundary_before}{field}{boundary_after}\s*=\s*(['\"])([^'\"]*)\1",
        re.IGNORECASE,
    )
    in_list = re.compile(
        rf"{boundary_before}{field}{boundary_after}\s+in\s+\(([^)]*)\)",
        re.IGNORECASE,
    )
    for predicate in predicates:
        values.update(
            match.group(2) for match in equality.finditer(predicate)
            if not is_negated(predicate, match.start())
        )
        for match in in_list.finditer(predicate):
            if is_negated(predicate, match.start()):
                continue
            values.update(
                quoted_value
                for _, quoted_value in re.findall(
                    r"(['\"])([^'\"]*)\1", match.group(1)
                )
            )

    return frozenset(values)


def scope_query(query: str, entity_names: FrozenSet[str], entity_field: str) -> str:
    """Validate then rewrite a query so it is constrained to entity_names.

    Result: `'<field>' in (<entities>) [and (<head>)] | <rest>`. Raises
    QueryNotAllowed if the query is unsafe to scope.
    """
    if not entity_names:
        raise QueryNotAllowed("No entities resolved for this user; refusing to run query.")
    try:
        entity_field = validate_entity_field(entity_field)
    except AccessConfigError as exc:
        raise QueryNotAllowed(str(exc)) from exc
    validate_cam_query(query)
    requested_entities = _explicit_entity_values(query, entity_field)
    denied_entities = sorted(requested_entities - entity_names)
    if denied_entities:
        requested = ", ".join(repr(entity) for entity in denied_entities)
        raise EntityAccessDenied(
            f"You do not have access to data for {requested}. You can access data only "
            "for your assigned customer entities. Use list_entities to see your "
            "permitted entities, or contact your administrator if you need access."
        )
    values = ", ".join(_quote_value(e) for e in sorted(entity_names))
    predicate = f"'{entity_field}' in ({values})"

    segments = _split_top_level_pipes(query)
    head = segments[0].strip()
    tail = segments[1:]

    if head in ("", "*"):
        scoped_head = predicate
    else:
        scoped_head = f"{predicate} and ({head})"

    parts = [scoped_head] + [seg.strip() for seg in tail]
    return " | ".join(parts)


# --- Tool gating (default-deny). See spec 6.6. Keep in sync with the registry;
# the drift test in tests/test_access_control.py fails if a new tool is unclassified.
CAM_ALLOWED_TOOLS: FrozenSet[str] = frozenset({
    "run_query", "run_batch_queries", "get_log_summary",
    "run_saved_search",                       # scoped at the handler (Task 9)
    "list_entities",                          # filtered (Task 9)
    "list_log_groups", "list_log_sources", "list_fields", "list_labels",
    "list_parsers", "list_saved_searches", "list_compartments", "find_compartment",
    "validate_query", "explain_query", "get_query_examples",
    "visualize", "export_results",
    "save_learned_query", "get_preferences", "remember_preference",
    "setup_confirmation_secret",
    "get_current_context", "get_session_budget", "test_connection",
})

# Allowed only when the profile's allow_delivery is True.
CAM_CONDITIONAL_TOOLS: FrozenSet[str] = frozenset({
    "get_report_delivery_options", "prepare_report_delivery", "deliver_report",
    "send_to_slack", "send_to_telegram", "list_notification_topics",
})

CAM_BLOCKED_TOOLS: FrozenSet[str] = frozenset({
    "set_compartment", "set_namespace", "update_tenancy_context",
    "diff_time_windows", "pivot_on_entity", "ingestion_health",
    "parser_failure_triage", "investigate_incident",
    "investigate_and_generate_report", "generate_incident_report",
    "get_incident_report", "list_incident_reports", "why_did_this_fire",
    "find_rare_events", "trace_request_id", "related_dashboards_and_searches",
    "list_dashboards", "list_alerts", "list_playbooks", "get_playbook",
    "create_alert", "update_alert", "delete_alert",
    "create_saved_search", "update_saved_search", "delete_saved_search",
    "create_dashboard", "add_dashboard_tile", "delete_dashboard",
    "create_log_source_from_sample",
    "export_transcript", "record_investigation", "delete_playbook",
})


def is_tool_allowed(profile: AccessProfile, tool_name: str) -> bool:
    if tool_name in CAM_ALLOWED_TOOLS:
        return True
    if tool_name in CAM_CONDITIONAL_TOOLS:
        return profile.allow_delivery
    return False   # default-deny (blocked or unknown)


# --- Resource gating. See spec 6.12.
CAM_ALLOWED_RESOURCES: FrozenSet[str] = frozenset({
    "loganalytics://schema",            # entities filtered before return
    "loganalytics://query-templates",   # shared query text is shareable
    "loganalytics://syntax-guide",
    "loganalytics://reference-docs",
})
CAM_BLOCKED_RESOURCES: FrozenSet[str] = frozenset({
    "loganalytics://tenancy-context",   # bulk entity roster
    "loganalytics://recent-queries",
})


def is_resource_allowed(uri: str) -> bool:
    return uri in CAM_ALLOWED_RESOURCES


# --- Delivery destination lockdown. See spec 6.13.
# Keys that, if present in a delivery tool's args, would override the pre-approved
# destination. In CAM mode any of these is rejected.
_DESTINATION_OVERRIDE_KEYS: FrozenSet[str] = frozenset({
    "chat_id", "telegram_chat_id", "webhook", "webhook_url",
    "topic_ocid", "email_topic_ocid", "recipients",
})


def destination_override_blocked(args: dict) -> bool:
    """True if delivery args try to set an explicit destination (CAM mode rejects this)."""
    def _walk(d):
        for k, v in d.items():
            if k in _DESTINATION_OVERRIDE_KEYS:
                return True
            if isinstance(v, dict) and _walk(v):
                return True
            if isinstance(v, (list, tuple)):
                for item in v:
                    if isinstance(item, dict) and _walk(item):
                        return True
        return False
    return _walk(args or {})
