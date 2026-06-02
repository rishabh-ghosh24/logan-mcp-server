# CAM Access Control - Layer 2 (Authorization Code) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the MCP-layer authorization that restricts each Assurance CAM to read-only access of only their assigned customer entities, gated behind `--enforce-access`, with zero change to general-purpose users.

**Architecture:** A new `access_control.py` module is the single source of truth (modeled on `read_only_guard.py`): it loads `access_control.yaml`, resolves a per-process `AccessProfile`, and provides pure functions for entity matching, query grammar validation + scoping, tool/resource gating, and delivery checks. Enforcement is wired at four points: the `handle_tool_call` gate (tools), `read_resource`/`list_resources` (resources), `OCILogAnalyticsClient` (query scoping + compartment/namespace pinning), and `server.initialize_core` (fail-closed startup). When `--enforce-access` is not set, no profile exists and every path behaves exactly as today.

**Tech Stack:** Python 3, FastMCP/stdio, PyYAML, pytest. Virtualenv is `venv` (never `.venv`). Tests run via `run_tests.py` or `pytest`.

**Spec:** `docs/superpowers/specs/2026-06-02-cam-access-control-design.md`

**Scope note:** This plan covers Layer 2 (Python authorization). Layer 1 (forced-command SSH, `cam-launch.sh`, `provision-cam.sh`/`deprovision-cam.sh`, docs) is a separate follow-up plan. Live smoke tests against `assurance-logan` run after both.

---

## File Structure

**Create:**
- `src/oci_logan_mcp/access_control.py` - the authorization module (config model, `AccessProfile`, entity matching, query validation + scoping, tool/resource gating, delivery checks). Single responsibility: "given a CAM identity, what may they do."
- `tests/test_access_control.py` - unit tests for the pure functions in the module.
- `tests/test_cam_enforcement.py` - integration tests for the wired enforcement (gate, resources, client scoping, startup, general-user invariance).

**Modify:**
- `src/oci_logan_mcp/config.py` - add `enforce_access: bool` to `Settings` and env/data wiring (parallel to `read_only`).
- `src/oci_logan_mcp/__main__.py` - add `--enforce-access` CLI flag.
- `src/oci_logan_mcp/server.py` - `initialize_core`: build + validate `AccessProfile` before stdio; attach to client; fail-closed.
- `src/oci_logan_mcp/handlers.py` - CAM tool gate in `handle_tool_call`; `list_entities` filtering; `run_saved_search` scoping; resource gating in `handle_resource_read`; auto-capture suppression; delivery destination checks; hold `self.access_profile`.
- `src/oci_logan_mcp/client.py` - hold `access_profile`; pin compartment/namespace and apply `scope_query` in OCI-facing methods.
- `src/oci_logan_mcp/server.py` (`list_tools`, `list_resources`) - advertise only CAM-permitted tools/resources.

**Conventions to follow:** mirror `read_only_guard.py` (frozenset constants + a drift test); mirror the audit/blocked-response shape used in `handle_tool_call` (`self._write_audit_event(..., blocked=True, block_reason=...)` then return a JSON `text` content).

---

## Task 1: Access-control config model and loader

**Files:**
- Create: `src/oci_logan_mcp/access_control.py`
- Test: `tests/test_access_control.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_access_control.py
"""Tests for the CAM access-control module."""
import textwrap
import pytest

from oci_logan_mcp.access_control import (
    AccessControlConfig,
    AccessConfigError,
    load_access_config,
)


def _write(tmp_path, body):
    p = tmp_path / "access_control.yaml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def test_load_minimal_config(tmp_path):
    path = _write(tmp_path, """
        tenancy_id: ocid1.tenancy.oc1..t
        compartment_id: ocid1.compartment.oc1..c
        namespace: ns123
        cams:
          cam_alice: { customers: [223, 66] }
          cam_bob:   { customers: [232], allow_delivery: false }
    """)
    cfg = load_access_config(path)
    assert isinstance(cfg, AccessControlConfig)
    assert cfg.compartment_id == "ocid1.compartment.oc1..c"
    assert cfg.namespace == "ns123"
    assert cfg.entity_field == "Entity"          # default
    assert cfg.default_allow_delivery is True     # default
    assert cfg.cams["cam_alice"].customers == (223, 66)
    assert cfg.cams["cam_alice"].allow_delivery is True   # inherits default
    assert cfg.cams["cam_bob"].allow_delivery is False    # per-cam override


def test_missing_file_raises(tmp_path):
    with pytest.raises(AccessConfigError):
        load_access_config(tmp_path / "nope.yaml")


def test_missing_required_field_raises(tmp_path):
    path = _write(tmp_path, """
        tenancy_id: ocid1.tenancy.oc1..t
        namespace: ns123
        cams: {}
    """)  # no compartment_id
    with pytest.raises(AccessConfigError):
        load_access_config(path)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_access_control.py -q`
Expected: FAIL (`ModuleNotFoundError: oci_logan_mcp.access_control`).

- [ ] **Step 3: Write minimal implementation**

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_access_control.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/oci_logan_mcp/access_control.py tests/test_access_control.py
git commit -m "feat(access-control): config model and loader for access_control.yaml"
```

---

## Task 2: Entity-number matching (anchored)

**Files:**
- Modify: `src/oci_logan_mcp/access_control.py`
- Test: `tests/test_access_control.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_access_control.py
from oci_logan_mcp.access_control import entity_matches, resolve_entities


def test_entity_matches_anchored():
    # number must be at the start, followed by '_' (or be the whole name)
    assert entity_matches("223_d360_silicone", 223)
    assert entity_matches("66_flsmidth_co_as", 66)
    assert entity_matches("223", 223)            # whole-name match
    # the digits inside the name part are never inspected
    assert not entity_matches("223_d360_silicone", 36)
    assert not entity_matches("223_d360_silicone", 360)
    # prefix collisions are rejected: next char must be '_'
    assert not entity_matches("232_elca", 2)
    assert not entity_matches("223_x", 22)
    assert not entity_matches("1400_indra", 140)
    assert entity_matches("1400_indra", 1400)


def test_resolve_entities_union():
    all_entities = ["223_d360_silicone", "66_flsmidth_co_as", "232_elca", "1400_indra"]
    assert resolve_entities((223, 66), all_entities) == frozenset(
        {"223_d360_silicone", "66_flsmidth_co_as"}
    )
    assert resolve_entities((9999,), all_entities) == frozenset()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_access_control.py -q`
Expected: FAIL (`ImportError: cannot import name 'entity_matches'`).

- [ ] **Step 3: Write minimal implementation**

```python
# append to src/oci_logan_mcp/access_control.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_access_control.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/oci_logan_mcp/access_control.py tests/test_access_control.py
git commit -m "feat(access-control): anchored entity-number matching and resolution"
```

---

## Task 3: AccessProfile and fail-closed build

**Files:**
- Modify: `src/oci_logan_mcp/access_control.py`
- Test: `tests/test_access_control.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_access_control.py
from oci_logan_mcp.access_control import AccessProfile, build_profile


def _cfg(tmp_path):
    return load_access_config(_write(tmp_path, """
        tenancy_id: ocid1.tenancy.oc1..t
        compartment_id: ocid1.compartment.oc1..c
        namespace: ns123
        cams:
          cam_alice: { customers: [223, 66] }
          cam_empty: { customers: [] }
    """))


ALL = ["223_d360_silicone", "66_flsmidth_co_as", "232_elca"]


def test_build_profile_resolves(tmp_path):
    prof = build_profile(_cfg(tmp_path), "cam_alice", ALL)
    assert isinstance(prof, AccessProfile)
    assert prof.entity_names == frozenset({"223_d360_silicone", "66_flsmidth_co_as"})
    assert prof.compartment_id == "ocid1.compartment.oc1..c"
    assert prof.namespace == "ns123"
    assert prof.allow_delivery is True


def test_build_profile_unknown_cam_fails_closed(tmp_path):
    with pytest.raises(AccessConfigError):
        build_profile(_cfg(tmp_path), "cam_ghost", ALL)


def test_build_profile_empty_customers_fails_closed(tmp_path):
    with pytest.raises(AccessConfigError):
        build_profile(_cfg(tmp_path), "cam_empty", ALL)


def test_build_profile_zero_resolved_fails_closed(tmp_path):
    # cam_alice's numbers don't match any live entity -> refuse (misconfig/typo)
    with pytest.raises(AccessConfigError):
        build_profile(_cfg(tmp_path), "cam_alice", ["999_other"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_access_control.py -q`
Expected: FAIL (`ImportError: cannot import name 'AccessProfile'`).

- [ ] **Step 3: Write minimal implementation**

```python
# append to src/oci_logan_mcp/access_control.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_access_control.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/oci_logan_mcp/access_control.py tests/test_access_control.py
git commit -m "feat(access-control): AccessProfile with fail-closed build"
```

---

## Task 4: Query grammar validation + scope_query

**Files:**
- Modify: `src/oci_logan_mcp/access_control.py`
- Test: `tests/test_access_control.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_access_control.py
from oci_logan_mcp.access_control import (
    CAM_QUERY_COMMANDS,
    QueryNotAllowed,
    scope_query,
    validate_cam_query,
)

ENTS = frozenset({"223_d360_silicone", "66_flsmidth_co_as"})


def test_validate_accepts_plain_reporting_query():
    validate_cam_query("* | stats count by 'Log Source' | sort -count | head 10")
    validate_cam_query("'Log Source' = 'Assurance_Image' | timestats count")


def test_validate_rejects_brackets_anywhere():
    with pytest.raises(QueryNotAllowed):
        validate_cam_query("* | addfields [ * | stats count ] as c")


def test_validate_rejects_command_form_head():
    # These commands may appear before the first pipe; CAM mode requires the head
    # to be a pure search/filter expression instead.
    for query in [
        "searchlookup table='t' | fields *",
        "lookup table='t'",
        "createview view='v' [ * | stats count ]",
        "map [ * | stats count ]",
        "updatetable table='t' [ * | stats count ]",
        "link Entity",
        "classify Severity",
        "addfields [ * | stats count ] as c",
    ]:
        with pytest.raises(QueryNotAllowed):
            validate_cam_query(query)


def test_validate_rejects_unknown_bare_command_form_head():
    # Fail closed: a bare leading token with command-style arguments is not a
    # field predicate, even if the command is not in our explicit command list.
    with pytest.raises(QueryNotAllowed):
        validate_cam_query("madeupcommand arg=value | stats count")


def test_validate_rejects_non_allowlisted_pipeline_command():
    with pytest.raises(QueryNotAllowed):
        validate_cam_query("* | classify Severity")


def test_scope_query_prepends_predicate():
    out = scope_query("* | stats count", ENTS, "Entity")
    assert out.startswith("'Entity' in (")
    assert "'223_d360_silicone'" in out and "'66_flsmidth_co_as'" in out
    assert out.endswith("| stats count")


def test_scope_query_wraps_non_star_head():
    out = scope_query("'Log Source' = 'X' | stats count", ENTS, "Entity")
    assert " and ('Log Source' = 'X') | stats count" in out


def test_scope_query_widen_attempt_becomes_empty_intersection():
    out = scope_query("Entity = '999_other' | stats count", ENTS, "Entity")
    # the user predicate is ANDed under the allowed-set predicate
    assert out.startswith("'Entity' in (")
    assert "and (Entity = '999_other')" in out


def test_scope_query_rejects_unsafe_via_validate():
    with pytest.raises(QueryNotAllowed):
        scope_query("searchlookup table='t'", ENTS, "Entity")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_access_control.py -q`
Expected: FAIL (`ImportError: cannot import name 'scope_query'`).

- [ ] **Step 3: Write minimal implementation**

```python
# append to src/oci_logan_mcp/access_control.py
import re

class QueryNotAllowed(Exception):
    """Raised when a CAM query cannot be safely scoped (fail-closed)."""


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


_SEARCH_HEAD_RE = re.compile(
    r"""^\s*(
        \*$
        |
        \([^)]*
        |
        not\s+
        |
        '[^']+'\s*(=|!=|<>|<=|>=|<|>|\bin\b|\blike\b|\bcontains\b|\bis\b)
        |
        "[^"]+"\s*(=|!=|<>|<=|>=|<|>|\bin\b|\blike\b|\bcontains\b|\bis\b)
        |
        [A-Za-z_][\w.]*\s*(=|!=|<>|<=|>=|<|>|\bin\b|\blike\b|\bcontains\b|\bis\b)
    )""",
    re.IGNORECASE | re.VERBOSE,
)


def _head_is_search_expression(head: str) -> bool:
    """Conservative check for a base search/filter expression.

    CAM mode does not need free-text or command-form heads for capacity reports.
    If this does not look like `*`, a parenthesized/filter expression, or a field
    predicate, reject it before injecting the entity predicate.
    """
    stripped = head.strip()
    if stripped in ("", "*"):
        return True
    return bool(_SEARCH_HEAD_RE.match(stripped))


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


def _quote_value(value: str) -> str:
    if "'" in value:
        raise QueryNotAllowed(f"Entity name {value!r} contains a quote; cannot scope safely.")
    return f"'{value}'"


def scope_query(query: str, entity_names: FrozenSet[str], entity_field: str) -> str:
    """Validate then rewrite a query so it is constrained to entity_names.

    Result: `'<field>' in (<entities>) [and (<head>)] | <rest>`. Raises
    QueryNotAllowed if the query is unsafe to scope.
    """
    if not entity_names:
        raise QueryNotAllowed("No entities resolved for this user; refusing to run query.")
    validate_cam_query(query)
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_access_control.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/oci_logan_mcp/access_control.py tests/test_access_control.py
git commit -m "feat(access-control): CAM query grammar validation and scope_query"
```

---

## Task 5: Tool and resource gating constants + drift test

**Files:**
- Modify: `src/oci_logan_mcp/access_control.py`
- Test: `tests/test_access_control.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_access_control.py
from oci_logan_mcp.access_control import (
    CAM_ALLOWED_TOOLS,
    CAM_CONDITIONAL_TOOLS,
    CAM_BLOCKED_TOOLS,
    CAM_ALLOWED_RESOURCES,
    is_tool_allowed,
    is_resource_allowed,
)
from oci_logan_mcp.tools import get_tools
from oci_logan_mcp.resources import get_resources


def test_tool_sets_partition_the_registry():
    registered = {t["name"] for t in get_tools()}
    classified = CAM_ALLOWED_TOOLS | CAM_CONDITIONAL_TOOLS | CAM_BLOCKED_TOOLS
    # every registered tool is classified exactly once; no stale names
    assert registered - classified == set(), f"unclassified: {registered - classified}"
    assert classified - registered == set(), f"stale: {classified - registered}"
    assert (CAM_ALLOWED_TOOLS & CAM_BLOCKED_TOOLS) == set()
    assert (CAM_ALLOWED_TOOLS & CAM_CONDITIONAL_TOOLS) == set()
    assert (CAM_CONDITIONAL_TOOLS & CAM_BLOCKED_TOOLS) == set()


def test_known_blocked_and_allowed():
    assert "set_compartment" in CAM_BLOCKED_TOOLS
    assert "investigate_incident" in CAM_BLOCKED_TOOLS
    assert "export_transcript" in CAM_BLOCKED_TOOLS
    assert "run_query" in CAM_ALLOWED_TOOLS
    assert "list_entities" in CAM_ALLOWED_TOOLS
    assert "deliver_report" in CAM_CONDITIONAL_TOOLS


def test_is_tool_allowed_respects_delivery_flag(tmp_path):
    prof_yes = build_profile(_cfg(tmp_path), "cam_alice", ALL)            # allow_delivery True
    assert is_tool_allowed(prof_yes, "run_query")
    assert is_tool_allowed(prof_yes, "deliver_report")
    assert not is_tool_allowed(prof_yes, "set_compartment")

    cfg2 = load_access_config(_write(tmp_path, """
        compartment_id: c
        namespace: ns
        cams:
          cam_alice: { customers: [223], allow_delivery: false }
    """))
    prof_no = build_profile(cfg2, "cam_alice", ALL)
    assert not is_tool_allowed(prof_no, "deliver_report")   # gated off


def test_resource_gating():
    assert is_resource_allowed("loganalytics://query-templates")
    assert is_resource_allowed("loganalytics://schema")        # filtered, but readable
    assert not is_resource_allowed("loganalytics://tenancy-context")
    assert not is_resource_allowed("loganalytics://recent-queries")


def test_resource_sets_partition_the_registry():
    from oci_logan_mcp.access_control import CAM_BLOCKED_RESOURCES

    registered = {r["uri"] for r in get_resources()}
    classified = CAM_ALLOWED_RESOURCES | CAM_BLOCKED_RESOURCES
    assert registered - classified == set(), f"unclassified: {registered - classified}"
    assert classified - registered == set(), f"stale: {classified - registered}"
    assert (CAM_ALLOWED_RESOURCES & CAM_BLOCKED_RESOURCES) == set()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_access_control.py -q`
Expected: FAIL (`ImportError: cannot import name 'CAM_ALLOWED_TOOLS'`).

- [ ] **Step 3: Write minimal implementation**

```python
# append to src/oci_logan_mcp/access_control.py

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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_access_control.py -q`
Expected: PASS. If `test_tool_sets_partition_the_registry` fails with unclassified names, add each to the correct set (do not leave any tool unclassified).

- [ ] **Step 5: Commit**

```bash
git add src/oci_logan_mcp/access_control.py tests/test_access_control.py
git commit -m "feat(access-control): tool/resource gating sets with registry drift test"
```

---

## Task 6: `--enforce-access` flag and config wiring

**Files:**
- Modify: `src/oci_logan_mcp/config.py` (around `Settings` at line 141-155 and env wiring at ~398-445)
- Modify: `src/oci_logan_mcp/__main__.py` (argparse, ~48-86)
- Test: `tests/test_access_control.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_access_control.py
from oci_logan_mcp.config import Settings


def test_settings_has_enforce_access_default_false():
    assert Settings().enforce_access is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_access_control.py::test_settings_has_enforce_access_default_false -q`
Expected: FAIL (`AttributeError: 'Settings' object has no attribute 'enforce_access'`).

- [ ] **Step 3: Implement config + CLI wiring**

In `src/oci_logan_mcp/config.py`, in the `Settings` dataclass (next to `read_only: bool = False`):

```python
    read_only: bool = False
    enforce_access: bool = False
    access_control_path: str = ""   # optional override; default resolved in server
```

In `config.py`, where env overrides are applied (near `OCI_LOGAN_MCP_READ_ONLY`, ~line 443), add:

```python
    if os.environ.get("OCI_LOGAN_MCP_ENFORCE_ACCESS"):
        settings.enforce_access = True
    if os.environ.get("OCI_LOGAN_MCP_ACCESS_CONFIG"):
        settings.access_control_path = os.environ["OCI_LOGAN_MCP_ACCESS_CONFIG"]
```

In `src/oci_logan_mcp/__main__.py`, add the argument (next to `--read-only`):

```python
    parser.add_argument(
        "--enforce-access",
        action="store_true",
        help="Enable CAM access control (Assurance). Requires access_control.yaml; "
             "refuses to start if the --user is not a configured CAM.",
    )
```

In `__main__.py`, in the server-start branch (next to where `--read-only` sets its env var, ~line 84):

```python
        if args.enforce_access:
            os.environ["OCI_LOGAN_MCP_ENFORCE_ACCESS"] = "1"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_access_control.py::test_settings_has_enforce_access_default_false -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/oci_logan_mcp/config.py src/oci_logan_mcp/__main__.py tests/test_access_control.py
git commit -m "feat(access-control): --enforce-access flag and config wiring"
```

---

## Task 7: Fail-closed startup wiring in server

**Files:**
- Modify: `src/oci_logan_mcp/server.py` (`initialize_core`, ~171-209)
- Modify: `src/oci_logan_mcp/handlers.py` (`MCPHandlers.__init__`, set `self.access_profile` before `ReportStore`)
- Test: `tests/test_cam_enforcement.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cam_enforcement.py
"""Integration tests for wired CAM enforcement."""
import textwrap
import pytest

from oci_logan_mcp.access_control import AccessConfigError, load_access_config


def _access_config_file(tmp_path, *, body=None):
    p = tmp_path / "ac.yaml"
    p.write_text(textwrap.dedent(body or """
        compartment_id: c
        namespace: ns
        cams:
          cam_alice: { customers: [223] }
    """), encoding="utf-8")
    return p


class _FakeClient:
    def __init__(self, settings):
        self.settings = settings
        self.namespace = "old_ns"
        self.compartment_id = "old_compartment"
        self.access_profile = None
        self.access_audit_logger = None

    async def list_entities(self, entity_type=None):
        return [{"name": "223_x"}, {"name": "999_other"}]


class _FakeSecretStore:
    def __init__(self, path):
        self.path = path

    def has_secret(self):
        return False

    def is_valid(self):
        return True


class _FakeHandlers:
    captured_profile = None

    def __init__(self, **kwargs):
        _FakeHandlers.captured_profile = kwargs.get("access_profile")


def _patch_initialize_core_dependencies(monkeypatch, tmp_path, settings):
    import oci_logan_mcp.server as server_mod

    monkeypatch.setattr(server_mod, "config_exists", lambda: True)
    monkeypatch.setattr(server_mod, "load_config", lambda: settings)
    monkeypatch.setattr(server_mod, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(server_mod, "OCILogAnalyticsClient", _FakeClient)
    monkeypatch.setattr(server_mod, "CacheManager", lambda cfg: object())
    monkeypatch.setattr(server_mod, "QueryLogger", lambda cfg: object())
    monkeypatch.setattr(server_mod, "ContextManager", lambda cfg: object())
    monkeypatch.setattr(server_mod, "PreferenceStore", lambda user_dir: object())
    monkeypatch.setattr(server_mod, "SecretStore", _FakeSecretStore)
    monkeypatch.setattr(server_mod, "AuditLogger", lambda log_dir, session_id: object())
    monkeypatch.setattr(server_mod, "MCPHandlers", _FakeHandlers)


@pytest.mark.asyncio
async def test_initialize_core_enforce_access_refuses_unknown_cam(monkeypatch, tmp_path):
    from oci_logan_mcp.config import Settings
    from oci_logan_mcp.server import OCILogAnalyticsMCPServer

    settings = Settings()
    settings.enforce_access = True
    settings.access_control_path = str(_access_config_file(tmp_path))
    monkeypatch.setenv("LOGAN_USER", "cam_ghost")
    _patch_initialize_core_dependencies(monkeypatch, tmp_path, settings)

    with pytest.raises(AccessConfigError):
        await OCILogAnalyticsMCPServer().initialize_core()


@pytest.mark.asyncio
async def test_initialize_core_builds_profile_from_user_store_identity(monkeypatch, tmp_path):
    from oci_logan_mcp.config import Settings
    from oci_logan_mcp.server import OCILogAnalyticsMCPServer

    settings = Settings()
    settings.enforce_access = True
    settings.access_control_path = str(_access_config_file(tmp_path))
    monkeypatch.setenv("LOGAN_USER", "cam_alice")
    _patch_initialize_core_dependencies(monkeypatch, tmp_path, settings)

    srv = OCILogAnalyticsMCPServer()
    await srv.initialize_core()

    assert srv.access_profile.user_id == "cam_alice"
    assert srv.access_profile.entity_names == frozenset({"223_x"})
    assert srv.oci_client.namespace == "ns"
    assert srv.oci_client.compartment_id == "c"
    assert srv.oci_client.access_profile is srv.access_profile
    assert _FakeHandlers.captured_profile is srv.access_profile


@pytest.mark.asyncio
async def test_initialize_core_enforce_access_zero_resolved_fails(monkeypatch, tmp_path):
    from oci_logan_mcp.config import Settings
    from oci_logan_mcp.server import OCILogAnalyticsMCPServer

    settings = Settings()
    settings.enforce_access = True
    settings.access_control_path = str(_access_config_file(tmp_path, body="""
        compartment_id: c
        namespace: ns
        cams:
          cam_alice: { customers: [555] }
    """))
    monkeypatch.setenv("LOGAN_USER", "cam_alice")
    _patch_initialize_core_dependencies(monkeypatch, tmp_path, settings)

    with pytest.raises(AccessConfigError):
        await OCILogAnalyticsMCPServer().initialize_core()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cam_enforcement.py -q`
Expected: FAIL because `Settings.enforce_access` is not wired into startup and `initialize_core` does not yet build/pass an `AccessProfile`.

- [ ] **Step 3: Wire fail-closed startup in `server.py`**

In `initialize_core`, after the OCI client is created and BEFORE the server begins serving, add CAM profile construction. Critically, when `enforce_access` is set, an OCI-client failure must be fatal (override the existing swallow-and-continue at ~198-201). Replace the client init block with:

```python
        try:
            self.oci_client = OCILogAnalyticsClient(self.settings)
            logger.info(
                f"Connected to OCI Log Analytics (namespace: {self.oci_client.namespace})"
            )
        except Exception as e:
            if self.settings.enforce_access:
                raise RuntimeError(
                    f"--enforce-access requires a working OCI client; init failed: {e}"
                ) from e
            logger.error(f"Failed to initialize OCI client: {e}")
            logger.warning("Server will start but OCI operations will fail")
            self.oci_client = None
```

Add `self.access_profile = None` early in `initialize_core`. Build the profile after the `UserStore` has been constructed (so CAM identity comes from the same source as learned queries, preferences, secrets, reports, and audit user ids), but before `MCPHandlers(...)` is constructed:

```python
        self.access_profile = None
        if self.settings.enforce_access:
            from .access_control import build_profile, load_access_config
            from .config import CONFIG_PATH
            ac_path = self.settings.access_control_path or str(
                CONFIG_PATH.parent / "access_control.yaml"
            )
            ac_config = load_access_config(ac_path)  # raises AccessConfigError -> fatal
            # Pin OCI scope to the access-control config
            self.oci_client.namespace = ac_config.namespace
            self.oci_client.compartment_id = ac_config.compartment_id
            all_entities = [
                e["name"] for e in (await self.oci_client.list_entities() or [])
            ]
            user_id = self.user_store.user_id
            self.access_profile = build_profile(ac_config, user_id, all_entities)
            self.oci_client.access_profile = self.access_profile   # client enforcement (Task 8)
            logger.info(
                f"CAM access control active for '{user_id}': "
                f"{len(self.access_profile.entity_names)} entities"
            )
```

`AccessConfigError` and any exception here must propagate out of `initialize_core` so the process exits non-zero before stdio serving.

- [ ] **Step 4: Pass the handlers/client the profile**

The handlers are constructed as `self.handlers = MCPHandlers(...)` at `server.py:274`. Pass the profile in the constructor, not by setting an attribute afterward; `MCPHandlers.__init__` constructs `ReportStore`, and CAM mode must be known before legacy shared reports can import.

```python
            self.handlers = MCPHandlers(
                settings=self.settings,
                oci_client=self.oci_client,
                cache=self.cache,
                query_logger=self.query_logger,
                context_manager=self.context_manager,
                user_store=self.user_store,
                preference_store=self.preference_store,
                secret_store=self.secret_store,
                audit_logger=self.audit_logger,
                access_profile=self.access_profile,
            )
```

In `MCPHandlers.__init__`, add `access_profile=None` to the signature and set it before any helper services are constructed:

```python
        self.access_profile = access_profile
```

- [ ] **Step 5: Run tests + manual smoke**

Run: `pytest tests/test_cam_enforcement.py -q`
Expected: PASS.
Manual: `OCI_LOGAN_MCP_ENFORCE_ACCESS=1 LOGAN_USER=cam_ghost python -m oci_logan_mcp` with a valid `access_control.yaml` should exit non-zero with an `AccessConfigError`-derived message.

- [ ] **Step 6: Commit**

```bash
git add src/oci_logan_mcp/server.py src/oci_logan_mcp/handlers.py tests/test_cam_enforcement.py
git commit -m "feat(access-control): fail-closed startup builds AccessProfile before serving"
```

---

## Task 8: Client-level compartment pinning + query scoping

**Files:**
- Modify: `src/oci_logan_mcp/client.py` (`OCILogAnalyticsClient.__init__`, `query` at ~154-252, `list_log_sources` at ~329)
- Test: `tests/test_cam_enforcement.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_cam_enforcement.py
from types import SimpleNamespace
import pytest

from oci_logan_mcp.access_control import AccessProfile


def _profile():
    return AccessProfile(
        user_id="cam_alice",
        customer_numbers=(223,),
        entity_names=frozenset({"223_x"}),
        entity_field="Entity",
        compartment_id="allowed_compartment",
        namespace="ns",
        allow_delivery=True,
    )


@pytest.mark.asyncio
async def test_client_query_scopes_and_pins_scope(monkeypatch):
    from oci_logan_mcp.client import OCILogAnalyticsClient

    client = OCILogAnalyticsClient.__new__(OCILogAnalyticsClient)
    client.settings = SimpleNamespace(query=SimpleNamespace(max_results=100))
    client._compartment_id = "default_compartment"
    client._namespace = "ns"
    client.access_profile = _profile()
    client.access_audit_logger = None
    captured = {}

    async def fake_execute(query_string, time_start, time_end, max_results,
                           compartment_id, include_subcompartments):
        captured.update({
            "query": query_string,
            "compartment_id": compartment_id,
            "include_subcompartments": include_subcompartments,
        })
        return {"rows": [], "columns": []}

    monkeypatch.setattr(client, "_execute_single_query", fake_execute)

    await OCILogAnalyticsClient.query(
        client,
        query_string="* | stats count",
        time_start="2026-06-01T00:00:00+00:00",
        time_end="2026-06-01T01:00:00+00:00",
        compartment_id="attacker_compartment",
        include_subcompartments=True,
    )

    assert captured["query"] == "'Entity' in ('223_x') | stats count"
    assert captured["compartment_id"] == "allowed_compartment"
    assert captured["include_subcompartments"] is False


@pytest.mark.asyncio
async def test_notification_topic_listing_does_not_walk_compartments_for_cam(monkeypatch):
    from oci_logan_mcp.client import OCILogAnalyticsClient

    client = OCILogAnalyticsClient.__new__(OCILogAnalyticsClient)
    client._compartment_id = "default_compartment"
    client.access_profile = _profile()
    client.access_audit_logger = None
    listed = []

    async def fake_list_topics(compartment_id):
        listed.append(compartment_id)
        return []

    async def fail_list_compartments():
        raise AssertionError("CAM notification topic listing must not enumerate compartments")

    monkeypatch.setattr(client, "_list_notification_topics_in_compartment", fake_list_topics)
    monkeypatch.setattr(client, "list_compartments", fail_list_compartments)

    await OCILogAnalyticsClient.list_notification_topics(
        client,
        compartment_id="attacker_compartment",
        include_subcompartments=True,
    )

    assert listed == ["allowed_compartment"]

```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cam_enforcement.py::test_client_query_scopes_and_pins_scope tests/test_cam_enforcement.py::test_notification_topic_listing_does_not_walk_compartments_for_cam -q`
Expected: FAIL because `OCILogAnalyticsClient.query()` does not scope/pin yet and notification topic listing still honors caller subtree walking.

- [ ] **Step 3: Implement client enforcement**

In `OCILogAnalyticsClient.__init__`, add `self.access_profile = None`.

In `client.query(...)`, at the very top of the method body (before any compartment resolution or `QueryDetails` construction at ~227), insert:

```python
        if self.access_profile is not None:
            from .access_control import scope_query
            query_string = scope_query(
                query_string,
                self.access_profile.entity_names,
                self.access_profile.entity_field,
            )
            # Pin scope: ignore caller overrides for CAMs.
            compartment_id = self.access_profile.compartment_id
            include_subcompartments = False
```

In every other OCI-facing method that accepts a `compartment_id` argument (e.g. `list_log_sources`, `list_entities`, notification/topic listing), pin the compartment at the top. If the method also accepts `include_subcompartments`, force it off in CAM mode:

```python
        if self.access_profile is not None:
            compartment_id = self.access_profile.compartment_id
            include_subcompartments = False
```

so a caller-supplied `compartment_id`, namespace, or subtree walk cannot redirect a CAM. (Namespace is already pinned at startup in Task 7.)

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_cam_enforcement.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/oci_logan_mcp/client.py tests/test_cam_enforcement.py
git commit -m "feat(access-control): client-level query scoping and compartment pinning"
```

---

## Task 9: Tool gate, list_entities filtering, run_saved_search scoping

**Files:**
- Modify: `src/oci_logan_mcp/handlers.py` (`handle_tool_call` gate after line 319; `_list_entities` ~756; `_run_saved_search` ~1002; uses the `access_profile` constructor arg from Task 7)
- Test: `tests/test_cam_enforcement.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_cam_enforcement.py
import json
from unittest.mock import AsyncMock


_HANDLER_METHODS = (
    "_list_log_sources", "_list_fields", "_list_entities", "_list_parsers",
    "_list_labels", "_list_saved_searches", "_list_log_groups",
    "_validate_query", "_run_query", "_run_saved_search", "_run_batch_queries",
    "_diff_time_windows", "_pivot_on_entity", "_ingestion_health",
    "_parser_failure_triage", "_investigate_incident",
    "_investigate_and_generate_report", "_generate_incident_report",
    "_get_report_delivery_options", "_prepare_report_delivery",
    "_list_notification_topics", "_get_incident_report", "_list_incident_reports",
    "_deliver_report", "_why_did_this_fire", "_find_rare_events",
    "_create_log_source_from_sample", "_trace_request_id",
    "_related_dashboards_and_searches", "_visualize", "_export_results",
    "_set_compartment", "_set_namespace", "_get_current_context",
    "_list_compartments", "_test_connection", "_find_compartment",
    "_get_query_examples", "_get_log_summary", "_setup_confirmation_secret",
    "_save_learned_query", "_update_tenancy_context", "_get_preferences",
    "_remember_preference", "_create_alert", "_list_alerts", "_update_alert",
    "_delete_alert", "_create_saved_search", "_update_saved_search",
    "_delete_saved_search", "_create_dashboard", "_list_dashboards",
    "_add_dashboard_tile", "_delete_dashboard", "_send_to_slack",
    "_send_to_telegram", "_explain_query", "_get_session_budget",
    "_export_transcript", "_record_investigation", "_list_playbooks",
    "_get_playbook", "_delete_playbook",
)


def _handler_with_profile():
    from oci_logan_mcp.handlers import MCPHandlers

    h = MCPHandlers.__new__(MCPHandlers)
    h.access_profile = _profile()
    h.settings = SimpleNamespace(read_only=False)
    h.user_store = SimpleNamespace(user_id="cam_alice")
    h.audit_logger = None
    h._write_audit_event = lambda **kwargs: True
    h._extract_audit_ref = lambda args: None
    h._audit_strictness = lambda name, args: "best_effort"
    h._clean_args_for_audit = lambda name, args: args
    h._summarize_tool_result = lambda result, elapsed_ms: {"success": True}
    h.confirmation_manager = SimpleNamespace(is_guarded_call=lambda name, args: False)
    for method in _HANDLER_METHODS:
        setattr(h, method, AsyncMock(return_value=[{"type": "text", "text": "{}"}]))
    return h


@pytest.mark.asyncio
async def test_handle_tool_call_blocks_disallowed_cam_tool():
    from oci_logan_mcp.handlers import MCPHandlers

    h = _handler_with_profile()
    result = await MCPHandlers.handle_tool_call(
        h, "investigate_incident", {"incident_id": "i-1"}
    )

    payload = json.loads(result[0]["text"])
    assert payload["status"] == "access_denied"
    h._investigate_incident.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_entities_filters_via_handler():
    from oci_logan_mcp.handlers import MCPHandlers

    h = MCPHandlers.__new__(MCPHandlers)
    h.access_profile = _profile()
    h.schema_manager = SimpleNamespace(
        get_entities=AsyncMock(return_value=[
            {"name": "223_x"},
            {"name": "999_other"},
        ])
    )

    result = await MCPHandlers._list_entities(h, {})
    payload = json.loads(result[0]["text"])

    assert [e["name"] for e in payload] == ["223_x"]


@pytest.mark.asyncio
async def test_run_saved_search_preserves_scope_and_time_args():
    from oci_logan_mcp.handlers import MCPHandlers

    h = MCPHandlers.__new__(MCPHandlers)
    h.saved_search = SimpleNamespace(
        get_search_by_name=AsyncMock(),
        get_search_by_id=AsyncMock(return_value={"query": "* | stats count"}),
    )
    h.query_engine = SimpleNamespace(execute=AsyncMock(return_value={"data": []}))
    h._resolve_scope = lambda args: ("allowed_compartment", False)

    await MCPHandlers._run_saved_search(
        h,
        {
            "id": "saved-1",
            "time_range": "last_24_hours",
            "time_start": "2026-06-01T00:00:00Z",
            "time_end": "2026-06-02T00:00:00Z",
        },
    )

    h.query_engine.execute.assert_awaited_once_with(
        query="* | stats count",
        time_range="last_24_hours",
        time_start="2026-06-01T00:00:00Z",
        time_end="2026-06-02T00:00:00Z",
        include_subcompartments=False,
        compartment_id="allowed_compartment",
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cam_enforcement.py::test_handle_tool_call_blocks_disallowed_cam_tool tests/test_cam_enforcement.py::test_list_entities_filters_via_handler tests/test_cam_enforcement.py::test_run_saved_search_preserves_scope_and_time_args -q`
Expected: FAIL because `handle_tool_call` has no CAM gate, `_list_entities` returns the full entity roster, and `_run_saved_search` ignores scope/time arguments.

- [ ] **Step 3: Add the gate to `handle_tool_call`**

Insert the CAM gate immediately after the unknown-tool check returns (after line 319, before the read-only guard at line 321):

```python
        # --- CAM access-control gate (runs after unknown-tool, before read-only) ---
        if self.access_profile is not None:
            from .access_control import is_tool_allowed
            if not is_tool_allowed(self.access_profile, name):
                self._write_audit_event(
                    user=user_id, tool=name, args=arguments,
                    outcome="access_denied", trace_id=trace_id,
                    audit_ref=audit_ref, audit_strictness=audit_strictness,
                    result_summary={"success": False, "error": "not permitted"},
                    blocked=True, block_reason="access_control",
                )
                return [{"type": "text", "text": json.dumps({
                    "status": "access_denied",
                    "tool": name,
                    "error": "This tool is not permitted in access-controlled (CAM) mode.",
                }, indent=2)}]
```

- [ ] **Step 4: Filter `_list_entities`**

In `_list_entities` (~756), filter the returned entities for CAMs:

```python
    async def _list_entities(self, args: Dict) -> List[Dict]:
        """List entities."""
        entities = await self.schema_manager.get_entities(
            entity_type=args.get("entity_type")
        )
        if self.access_profile is not None:
            allowed = self.access_profile.entity_names
            entities = [e for e in entities if e.get("name") in allowed]
        return [{"type": "text", "text": json.dumps(entities, indent=2)}]
```

- [ ] **Step 5: Scope `_run_saved_search`**

In `_run_saved_search` (~1002), pass scope through and stop hardcoding the time range. Replace the execute call so it uses `_resolve_scope` and `args` time params (the client also re-scopes the query string for CAMs, Task 8):

```python
        compartment_id, include_subs = self._resolve_scope(args)
        result = await self.query_engine.execute(
            query=query,
            time_range=args.get("time_range", "last_1_hour"),
            time_start=args.get("time_start"),
            time_end=args.get("time_end"),
            include_subcompartments=include_subs,
            compartment_id=compartment_id,
        )
```

- [ ] **Step 6: Run tests + drift test**

Run: `pytest tests/test_cam_enforcement.py tests/test_access_control.py -q`
Expected: PASS (incl. the registry drift test from Task 5).

- [ ] **Step 7: Commit**

```bash
git add src/oci_logan_mcp/handlers.py tests/test_cam_enforcement.py
git commit -m "feat(access-control): tool gate, list_entities filtering, run_saved_search scoping"
```

---

## Task 10: Resource gating and schema entity filtering

**Files:**
- Modify: `src/oci_logan_mcp/handlers.py` (`handle_resource_read` ~699)
- Modify: `src/oci_logan_mcp/server.py` (`list_resources` ~120, `list_tools` ~101)
- Test: `tests/test_cam_enforcement.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_cam_enforcement.py
@pytest.mark.asyncio
async def test_handle_resource_read_blocks_roster_resources():
    from oci_logan_mcp.handlers import MCPHandlers

    h = MCPHandlers.__new__(MCPHandlers)
    h.access_profile = _profile()

    result = await MCPHandlers.handle_resource_read(h, "loganalytics://tenancy-context")

    assert result["error"].startswith("Resource not permitted")


@pytest.mark.asyncio
async def test_schema_resource_filters_entities():
    from oci_logan_mcp.handlers import MCPHandlers

    h = MCPHandlers.__new__(MCPHandlers)
    h.access_profile = _profile()
    h.schema_manager = SimpleNamespace(
        get_full_schema=AsyncMock(return_value={
            "entities": [{"name": "223_x"}, {"name": "999_other"}],
            "fields": [{"name": "Log Source"}],
        })
    )

    schema = await MCPHandlers.handle_resource_read(h, "loganalytics://schema")

    assert [e["name"] for e in schema["entities"]] == ["223_x"]
    assert schema["fields"] == [{"name": "Log Source"}]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cam_enforcement.py::test_handle_resource_read_blocks_roster_resources tests/test_cam_enforcement.py::test_schema_resource_filters_entities -q`
Expected: FAIL because blocked resources are still readable and `loganalytics://schema` still returns the full entity roster.

- [ ] **Step 3: Gate `handle_resource_read`**

In `handle_resource_read` (~699), at the top:

```python
    async def handle_resource_read(self, uri: str) -> Any:
        """Handle resource read requests."""
        if self.access_profile is not None:
            from .access_control import is_resource_allowed
            if not is_resource_allowed(uri):
                return {"error": "Resource not permitted in access-controlled (CAM) mode."}
```

Then, for `loganalytics://schema`, filter entities before returning:

```python
        if uri == "loganalytics://schema":
            schema = await self.schema_manager.get_full_schema()
            if self.access_profile is not None:
                allowed = self.access_profile.entity_names
                schema["entities"] = [
                    e for e in schema.get("entities", []) if e.get("name") in allowed
                ]
            return schema
```

- [ ] **Step 4: Filter `list_tools` / `list_resources` in `server.py`**

In the `list_tools` handler (server.py ~101), filter `tool_defs` (the `get_tools()` list of dicts) before the existing `Tool(**kwargs)` loop, when a profile is active:

```python
        @self.server.list_tools()
        async def list_tools() -> list[Tool]:
            """Return list of available tools."""
            tool_defs = get_tools()
            prof = getattr(self.handlers, "access_profile", None)
            if prof is not None:
                from .access_control import is_tool_allowed
                tool_defs = [t for t in tool_defs if is_tool_allowed(prof, t["name"])]
            tools = []
            for t in tool_defs:
                # ... existing Tool(**kwargs) construction unchanged ...
                tools.append(Tool(**kwargs))
            return tools
```

In `list_resources` (server.py ~120), filter `resource_defs` (the `get_resources()` list) the same way:

```python
        @self.server.list_resources()
        async def list_resources() -> list[Resource]:
            """Return list of available resources."""
            resource_defs = get_resources()
            prof = getattr(self.handlers, "access_profile", None)
            if prof is not None:
                from .access_control import is_resource_allowed
                resource_defs = [r for r in resource_defs if is_resource_allowed(r["uri"])]
            return [
                Resource(uri=r["uri"], name=r["name"],
                         description=r["description"], mimeType=r["mimeType"])
                for r in resource_defs
            ]
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_cam_enforcement.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/oci_logan_mcp/handlers.py src/oci_logan_mcp/server.py tests/test_cam_enforcement.py
git commit -m "feat(access-control): resource gating, schema entity filtering, list filtering"
```

---

## Task 11: Catalog/report isolation, auto-capture suppression, delivery destination lockdown

**Files:**
- Modify: `src/oci_logan_mcp/handlers.py` (`_list_log_sources` ~728 and `_list_fields` ~738 auto-capture; delivery handlers `_send_to_telegram`/`_deliver_report`)
- Modify: `src/oci_logan_mcp/report_store.py` (`_import_legacy_shared_reports` ~271)
- Modify: `src/oci_logan_mcp/access_control.py` (`destination_override_blocked`)
- Test: `tests/test_cam_enforcement.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_cam_enforcement.py
@pytest.mark.asyncio
async def test_send_to_telegram_rejects_destination_override():
    from oci_logan_mcp.handlers import MCPHandlers

    h = MCPHandlers.__new__(MCPHandlers)
    h.access_profile = _profile()
    h.notification_service = SimpleNamespace(send_to_telegram=AsyncMock())

    result = await MCPHandlers._send_to_telegram(
        h, {"message": "capacity report", "chat_id": "12345"}
    )

    payload = json.loads(result[0]["text"])
    assert payload["status"] == "access_denied"
    h.notification_service.send_to_telegram.assert_not_awaited()


@pytest.mark.asyncio
async def test_deliver_report_rejects_recipient_override():
    from oci_logan_mcp.handlers import MCPHandlers

    h = MCPHandlers.__new__(MCPHandlers)
    h.access_profile = _profile()
    h.report_delivery_service = SimpleNamespace(deliver=AsyncMock())

    result = await MCPHandlers._deliver_report(
        h,
        {
            "report": {"markdown": "report body", "metadata": {}},
            "recipients": {"telegram_chat_id": "12345"},
        },
    )

    payload = json.loads(result[0]["text"])
    assert payload["status"] == "access_denied"
    h.report_delivery_service.deliver.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_fields_does_not_auto_capture_for_cam():
    from oci_logan_mcp.handlers import MCPHandlers

    h = MCPHandlers.__new__(MCPHandlers)
    h.access_profile = _profile()
    h.settings = SimpleNamespace(read_only=False)
    h.schema_manager = SimpleNamespace(
        get_fields=AsyncMock(return_value=[
            SimpleNamespace(
                name="Log Source",
                data_type="string",
                description="",
                possible_values=[],
                hint="",
            )
        ])
    )
    h.context_manager = SimpleNamespace(
        update_confirmed_fields=lambda fields: (_ for _ in ()).throw(
            AssertionError("CAM metadata reads must not update shared context")
        )
    )

    result = await MCPHandlers._list_fields(h, {})

    payload = json.loads(result[0]["text"])
    assert payload[0]["name"] == "Log Source"


def test_report_store_skips_legacy_shared_import_in_cam_mode(tmp_path):
    from oci_logan_mcp.report_store import ReportStore

    legacy_id = "rpt_" + ("a" * 32)
    legacy_dir = tmp_path / "store" / legacy_id
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "report.md").write_text("legacy result content", encoding="utf-8")
    (legacy_dir / "metadata.json").write_text(
        json.dumps({"report_id": legacy_id}), encoding="utf-8"
    )

    ReportStore(tmp_path, user_id="cam_alice", enforce_access=True)

    assert not (tmp_path / "users" / "cam_alice" / "store" / legacy_id).exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cam_enforcement.py::test_send_to_telegram_rejects_destination_override tests/test_cam_enforcement.py::test_deliver_report_rejects_recipient_override tests/test_cam_enforcement.py::test_list_fields_does_not_auto_capture_for_cam tests/test_cam_enforcement.py::test_report_store_skips_legacy_shared_import_in_cam_mode -q`
Expected: FAIL because delivery overrides are still honored, metadata reads still update shared context, and `ReportStore` does not yet accept `enforce_access`.

- [ ] **Step 3: Add the destination helper and suppression hooks**

In `access_control.py`:

```python
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
        return False
    return _walk(args or {})
```

In the delivery handlers (`_deliver_report`, `_send_to_telegram`, `_send_to_slack`), at the top, when `self.access_profile is not None`:

```python
        if self.access_profile is not None:
            from .access_control import destination_override_blocked
            if destination_override_blocked(args):
                return [{"type": "text", "text": json.dumps({
                    "status": "access_denied",
                    "error": "Custom delivery destinations are not permitted in CAM mode; "
                             "use the pre-approved destination.",
                }, indent=2)}]
```

In `_list_log_sources` and `_list_fields`, the auto-capture is currently `if not self.settings.read_only:`. Change both to also suppress for CAMs:

```python
        if not self.settings.read_only and self.access_profile is None:
            self.context_manager.update_log_sources(sources)   # (or update_confirmed_fields)
```

In `MCPHandlers.__init__`, pass the flag through when constructing `ReportStore`:

```python
        self.report_store = ReportStore(
            self.settings.report_delivery.artifact_dir,
            user_id=user_store.user_id,
            enforce_access=self.access_profile is not None,
        )
```

In `report_store.py` `_import_legacy_shared_reports`, skip when access control is active. Add an `enforce_access: bool = False` parameter to `ReportStore.__init__`, persist it as `self._enforce_access`, and short-circuit:

```python
    def _import_legacy_shared_reports(self) -> None:
        if self._enforce_access:
            return
        # ... existing body ...
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_cam_enforcement.py tests/test_access_control.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/oci_logan_mcp/access_control.py src/oci_logan_mcp/handlers.py src/oci_logan_mcp/report_store.py tests/test_cam_enforcement.py
git commit -m "feat(access-control): delivery destination lockdown, auto-capture + legacy report suppression"
```

---

## Task 12: Audit the effective scoped query and namespace the cache key

Implements spec §6.10 (audit records the effective scoped query; cache key
includes the profile id as defense-in-depth). The cache is in-memory per-process,
so this is hardening, not a fix for a present leak.

**Files:**
- Modify: `src/oci_logan_mcp/client.py` (`query`, the scoping block from Task 8)
- Modify: `src/oci_logan_mcp/query_engine.py` (`_make_cache_key` ~line 92 caller; the method definition)
- Modify: `src/oci_logan_mcp/server.py` (`initialize_core`, attach the audit logger to the client)
- Test: `tests/test_cam_enforcement.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_cam_enforcement.py
from datetime import datetime, timezone


def test_cache_key_namespaced_by_profile_behavior():
    from oci_logan_mcp.query_engine import QueryEngine

    engine = QueryEngine(
        oci_client=SimpleNamespace(access_profile=_profile()),
        cache=SimpleNamespace(),
        logger=SimpleNamespace(),
    )
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    end = datetime(2026, 6, 2, tzinfo=timezone.utc)

    key_a = engine._make_cache_key("* | stats count", start, end, False, "c")
    engine.oci_client.access_profile = AccessProfile(
        user_id="cam_bob",
        customer_numbers=(999,),
        entity_names=frozenset({"999_y"}),
        entity_field="Entity",
        compartment_id="allowed_compartment",
        namespace="ns",
        allow_delivery=True,
    )
    key_b = engine._make_cache_key("* | stats count", start, end, False, "c")

    assert key_a != key_b


@pytest.mark.asyncio
async def test_client_audits_effective_scoped_query(monkeypatch):
    from oci_logan_mcp.client import OCILogAnalyticsClient

    events = []
    client = OCILogAnalyticsClient.__new__(OCILogAnalyticsClient)
    client.settings = SimpleNamespace(query=SimpleNamespace(max_results=100))
    client._compartment_id = "default_compartment"
    client._namespace = "ns"
    client.access_profile = _profile()
    client.access_audit_logger = SimpleNamespace(log=lambda **kwargs: events.append(kwargs))

    async def fake_execute(query_string, time_start, time_end, max_results,
                           compartment_id, include_subcompartments):
        return {"rows": [], "columns": []}

    monkeypatch.setattr(client, "_execute_single_query", fake_execute)

    await OCILogAnalyticsClient.query(
        client,
        query_string="* | stats count",
        time_start="2026-06-01T00:00:00+00:00",
        time_end="2026-06-01T01:00:00+00:00",
        compartment_id="attacker_compartment",
        include_subcompartments=True,
    )

    assert events
    event = events[-1]
    assert event["user"] == "cam_alice"
    assert event["tool"] == "__access_control"
    assert event["outcome"] == "query_scoped"
    assert event["args"]["original_query"] == "* | stats count"
    assert event["args"]["effective_query"] == "'Entity' in ('223_x') | stats count"
    assert event["args"]["compartment_id"] == "allowed_compartment"
    assert event["args"]["include_subcompartments"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cam_enforcement.py::test_cache_key_namespaced_by_profile_behavior tests/test_cam_enforcement.py::test_client_audits_effective_scoped_query -q`
Expected: FAIL (cache key has no profile material; client does not audit the effective scoped query yet).

- [ ] **Step 3: Implement**

In `query_engine.py` `_make_cache_key`, prepend the access-profile user id (if any) to the existing key material so two identities never collide:

```python
    def _make_cache_key(self, query, start, end, include_subcompartments, compartment_id):
        prof = getattr(self.oci_client, "access_profile", None)
        user_part = prof.user_id if prof is not None else ""
        # ... existing key material, but include user_part in the string ...
        material = f"{user_part}|{query}|{start.isoformat()}|{end.isoformat()}|{include_subcompartments}|{compartment_id}"
        return material
```

In `client.py` `__init__`, add `self.access_audit_logger = None`. In `server.py`, after the `AuditLogger` is constructed, attach it to the client when the client exists:

```python
        if self.oci_client is not None:
            self.oci_client.access_audit_logger = self.audit_logger
```

In `client.py` `query`, in the Task-8 scoping block, preserve the original query and emit a structured audit event after computing the scoped `query_string`:

```python
        if self.access_profile is not None:
            from .access_control import scope_query
            original_query = query_string
            query_string = scope_query(
                query_string,
                self.access_profile.entity_names,
                self.access_profile.entity_field,
            )
            compartment_id = self.access_profile.compartment_id
            include_subcompartments = False
            audit_logger = getattr(self, "access_audit_logger", None)
            if audit_logger is not None:
                audit_logger.log(
                    user=self.access_profile.user_id,
                    tool="__access_control",
                    args={
                        "original_query": original_query,
                        "effective_query": query_string,
                        "compartment_id": compartment_id,
                        "include_subcompartments": include_subcompartments,
                    },
                    outcome="query_scoped",
                    result_summary={"success": True},
                )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_cam_enforcement.py::test_cache_key_namespaced_by_profile_behavior tests/test_cam_enforcement.py::test_client_audits_effective_scoped_query -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/oci_logan_mcp/query_engine.py src/oci_logan_mcp/client.py src/oci_logan_mcp/server.py tests/test_cam_enforcement.py
git commit -m "feat(access-control): audit effective scoped query, namespace cache key by profile"
```

---

## Task 13: General-user invariance test

**Files:**
- Test: `tests/test_cam_enforcement.py`

- [ ] **Step 1: Write the test**

```python
# append to tests/test_cam_enforcement.py
@pytest.mark.asyncio
async def test_client_without_profile_preserves_caller_query_and_scope(monkeypatch):
    from oci_logan_mcp.client import OCILogAnalyticsClient

    client = OCILogAnalyticsClient.__new__(OCILogAnalyticsClient)
    client.settings = SimpleNamespace(query=SimpleNamespace(max_results=100))
    client._compartment_id = "default_compartment"
    client._namespace = "ns"
    client.access_profile = None
    client.access_audit_logger = SimpleNamespace(log=AsyncMock())
    captured = {}

    async def fake_execute(query_string, time_start, time_end, max_results,
                           compartment_id, include_subcompartments):
        captured.update({
            "query": query_string,
            "compartment_id": compartment_id,
            "include_subcompartments": include_subcompartments,
        })
        return {"rows": [], "columns": []}

    monkeypatch.setattr(client, "_execute_single_query", fake_execute)

    await OCILogAnalyticsClient.query(
        client,
        query_string="Entity = '999_other' | stats count",
        time_start="2026-06-01T00:00:00+00:00",
        time_end="2026-06-01T01:00:00+00:00",
        compartment_id="caller_compartment",
        include_subcompartments=True,
    )

    assert captured["query"] == "Entity = '999_other' | stats count"
    assert captured["compartment_id"] == "caller_compartment"
    assert captured["include_subcompartments"] is True
    client.access_audit_logger.log.assert_not_called()


@pytest.mark.asyncio
async def test_handler_without_profile_does_not_apply_cam_tool_gate():
    from oci_logan_mcp.handlers import MCPHandlers

    h = _handler_with_profile()
    h.access_profile = None

    await MCPHandlers.handle_tool_call(h, "investigate_incident", {"incident_id": "i-1"})

    h._investigate_incident.assert_awaited_once_with({"incident_id": "i-1"})
```

- [ ] **Step 2: Run the full suite**

Run: `python run_tests.py` (or `pytest -q`)
Expected: PASS, including the existing `tests/test_read_only_guard.py` (unchanged) and all access-control tests.

- [ ] **Step 3: Commit**

```bash
git add tests/test_cam_enforcement.py
git commit -m "test(access-control): general-user invariance guard"
```

---

## Final verification

- [ ] Run the whole suite: `python run_tests.py`. Expected: all green.
- [ ] Run `tests/test_repo_conventions.py` (venv convention) to ensure no `.venv` paths were introduced.
- [ ] Manual fail-closed check: `OCI_LOGAN_MCP_ENFORCE_ACCESS=1 LOGAN_USER=cam_ghost python -m oci_logan_mcp` exits non-zero.
- [ ] Confirm general mode unchanged: start without `--enforce-access`; `list_tools` shows all tools; `set_compartment` works.

## Out of scope for this plan (follow-ups)
- **Layer 1** (separate plan): forced-command SSH, `cam-launch.sh`, `provision-cam.sh`/`deprovision-cam.sh`, `authorized_keys` hardening, docs.
- **Live smoke tests** against `assurance-logan` (after redeploying the current build to the VM - see spec §9 deployment drift).
- Estimator accuracy on the scoped query (spec §6.10, low priority).
