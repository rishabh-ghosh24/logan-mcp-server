# L1 — `--read-only` Flag Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `--read-only` operating mode that blocks all mutating tools at a single chokepoint in the handler dispatch, returning a clear, agent-readable error. Also suppress the incidental tenancy-context auto-capture side-effect on allowed read tools so the promise is honest.

**Scope of the read-only promise (P0):** blocks OCI resource mutations, outbound notifications, tenancy/namespace/compartment config changes, secret writes, explicit preference/learned-query tool calls, and incidental writes to the **shared** tenancy-context file from allowed `list_*` tools. Per-user incidental writes (query log, result cache, per-user learned-query auto-save, preference usage tracking) continue — these help the user's own tooling and are not state an agent can weaponize via the MCP surface.

**Architecture:** New `read_only_guard.py` module owns the authoritative denylist of mutating tools and a single `raise_if_read_only()` check. `handlers.handle_tool_call` calls the guard **before** the existing confirmation gate — read-only mode rejects the call before the agent is ever prompted for a confirmation token. Three tenancy-context auto-capture call sites in allowed `list_*` handlers are guarded with `if not self.settings.read_only:`. Mode is selected via `--read-only` CLI flag or `OCI_LOGAN_MCP_READ_ONLY=1` env var, surfaced as `Settings.read_only: bool`.

**Tech Stack:** Python 3, pytest, dataclasses, argparse. No new runtime dependencies.

**Spec:** [../specs/agent-guardrails.md](../specs/agent-guardrails.md) · feature L1.

---

## File Structure

**Create:**
- `src/oci_logan_mcp/read_only_guard.py` — `MUTATING_TOOLS` frozenset, `ReadOnlyError`, `raise_if_read_only()`.
- `tests/test_read_only_guard.py` — unit tests for the guard module.

**Modify:**
- `src/oci_logan_mcp/config.py` — add `read_only: bool` to `Settings`; add env override for `OCI_LOGAN_MCP_READ_ONLY`.
- `src/oci_logan_mcp/__main__.py` — add `--read-only` CLI flag; set env var before `server_main()`.
- `src/oci_logan_mcp/handlers.py` — insert guard check in `handle_tool_call` before the confirmation gate.
- `tests/test_config.py` — cover new field + env override.
- `tests/test_handlers.py` — integration test that a mutating tool is blocked under `read_only=True`.
- `README.md` — document the flag, env var, and the denylist.

**Out of scope** (deferred to subsequent features):
- Per-tool read-only semantics (e.g. "allow this single mutation"). L1 is all-or-nothing.
- Audit-log coverage for read-only rejections beyond what the existing `AuditLogger` captures.
- UI/agent-side affordance to advertise read-only mode (N5 covers discoverability).

---

## Denylist rationale

Mutating = changes state in OCI, on disk (user state files), or on external systems. Read-only = inspection only.

**Mutating (blocked in read-only mode):**

| Tool | Why mutating |
|---|---|
| `set_compartment` | Writes session context |
| `set_namespace` | Writes session context |
| `update_tenancy_context` | Writes persistent tenancy config |
| `setup_confirmation_secret` | Writes a hashed secret to disk |
| `save_learned_query` | Writes to user-scoped query store |
| `remember_preference` | Writes to preference store |
| `create_alert` | Creates an OCI Monitoring alarm |
| `update_alert` | Modifies an OCI Monitoring alarm |
| `delete_alert` | Deletes an OCI Monitoring alarm |
| `create_saved_search` | Creates an OCI Log Analytics saved search |
| `update_saved_search` | Modifies an OCI Log Analytics saved search |
| `delete_saved_search` | Deletes an OCI Log Analytics saved search |
| `create_dashboard` | Creates an OCI Management Dashboard |
| `add_dashboard_tile` | Modifies an OCI Management Dashboard |
| `delete_dashboard` | Deletes an OCI Management Dashboard |
| `send_to_slack` | Outbound message to Slack |
| `send_to_telegram` | Outbound message to Telegram |

**NOT mutating (allowed):**
- All `list_*`, `run_*`, `validate_*`, `visualize`, `test_connection`, `find_compartment`, `get_*` tools.
- `export_results` — writes to local disk only, no external/service state change. Keep allowed so agents can still pull data in read-only mode.

**Drift risk:** Any new tool registered in `handlers.handle_tool_call` must be classified. Task 5 adds a drift-catching test that fails if a handler name is unknown to the guard module.

---

## Task 1: Add `read_only` field to `Settings` + env override

**Files:**
- Modify: `src/oci_logan_mcp/config.py:93-103` (Settings dataclass) and `:260-290` (`_apply_env_overrides`)
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
def test_settings_default_read_only_is_false():
    from oci_logan_mcp.config import Settings
    assert Settings().read_only is False


def test_env_override_read_only_true(monkeypatch, tmp_path):
    from oci_logan_mcp.config import load_config
    monkeypatch.setenv("OCI_LOGAN_MCP_READ_ONLY", "1")
    settings = load_config(config_path=tmp_path / "no.yaml")
    assert settings.read_only is True


@pytest.mark.parametrize("value,expected", [
    ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True),
    ("0", False), ("false", False), ("", False), ("no", False),
])
def test_env_override_read_only_parsing(monkeypatch, tmp_path, value, expected):
    from oci_logan_mcp.config import load_config
    monkeypatch.setenv("OCI_LOGAN_MCP_READ_ONLY", value)
    settings = load_config(config_path=tmp_path / "no.yaml")
    assert settings.read_only is expected
```

(Add `import pytest` at top of file if not already imported.)

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/test_config.py::test_settings_default_read_only_is_false tests/test_config.py::test_env_override_read_only_true -v
```

Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'read_only'`.

- [ ] **Step 3: Add `read_only` field to `Settings`**

In `src/oci_logan_mcp/config.py`, inside the `Settings` dataclass (after line 103 `notifications: ...`):

```python
    read_only: bool = False
```

- [ ] **Step 4: Add env override helper and wire it**

In `src/oci_logan_mcp/config.py`, inside `_apply_env_overrides` (after the notifications env block at ~line 288, before `return settings`):

```python
    if (raw := os.environ.get("OCI_LOGAN_MCP_READ_ONLY")) is not None and raw != "":
        normalized = raw.strip().lower()
        if normalized in ("1", "true", "yes", "on"):
            settings.read_only = True
        elif normalized in ("0", "false", "no", "off"):
            settings.read_only = False
        else:
            import logging
            logging.getLogger(__name__).warning(
                "Unrecognized OCI_LOGAN_MCP_READ_ONLY=%r; expected one of "
                "1/true/yes/on or 0/false/no/off. Leaving read_only unchanged.",
                raw,
            )
```

Also append a test to `tests/test_config.py`:

```python
def test_env_override_read_only_unrecognized_warns(monkeypatch, tmp_path, caplog):
    from oci_logan_mcp.config import load_config
    monkeypatch.setenv("OCI_LOGAN_MCP_READ_ONLY", "yez")
    with caplog.at_level("WARNING"):
        settings = load_config(config_path=tmp_path / "no.yaml")
    assert settings.read_only is False  # default preserved
    assert any("OCI_LOGAN_MCP_READ_ONLY" in rec.message for rec in caplog.records)
```

- [ ] **Step 5: Run tests to verify they pass**

```
pytest tests/test_config.py -v
```

Expected: PASS on the three new tests, no regressions.

- [ ] **Step 6: Commit**

```bash
git add src/oci_logan_mcp/config.py tests/test_config.py
git commit -m "feat(config): add read_only setting + OCI_LOGAN_MCP_READ_ONLY env override"
```

---

## Task 2: Create the guard module

**Files:**
- Create: `src/oci_logan_mcp/read_only_guard.py`
- Create: `tests/test_read_only_guard.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_read_only_guard.py`:

```python
"""Tests for the read-only guard."""

import pytest

from oci_logan_mcp.read_only_guard import (
    MUTATING_TOOLS,
    ReadOnlyError,
    raise_if_read_only,
)


def test_mutating_tools_is_frozenset():
    assert isinstance(MUTATING_TOOLS, frozenset)


def test_mutating_tools_contains_known_writers():
    expected_subset = {
        "set_compartment",
        "set_namespace",
        "update_tenancy_context",
        "setup_confirmation_secret",
        "save_learned_query",
        "remember_preference",
        "create_alert",
        "update_alert",
        "delete_alert",
        "create_saved_search",
        "update_saved_search",
        "delete_saved_search",
        "create_dashboard",
        "add_dashboard_tile",
        "delete_dashboard",
        "send_to_slack",
        "send_to_telegram",
    }
    assert expected_subset <= MUTATING_TOOLS


def test_mutating_tools_excludes_readers():
    readers = {
        "run_query",
        "run_saved_search",
        "list_fields",
        "list_saved_searches",
        "validate_query",
        "visualize",
        "get_current_context",
        "export_results",
    }
    assert readers.isdisjoint(MUTATING_TOOLS)


def test_raise_if_read_only_allows_non_mutating_when_enabled():
    # Should NOT raise
    raise_if_read_only("run_query", read_only=True)


def test_raise_if_read_only_allows_everything_when_disabled():
    raise_if_read_only("delete_alert", read_only=False)


def test_raise_if_read_only_blocks_mutating_when_enabled():
    with pytest.raises(ReadOnlyError) as exc:
        raise_if_read_only("delete_alert", read_only=True)
    assert "delete_alert" in str(exc.value)
    assert "read-only" in str(exc.value).lower()


def test_read_only_error_is_exception_subclass():
    assert issubclass(ReadOnlyError, Exception)
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/test_read_only_guard.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'oci_logan_mcp.read_only_guard'`.

- [ ] **Step 3: Implement the guard module**

Create `src/oci_logan_mcp/read_only_guard.py`:

```python
"""Read-only mode enforcement for MCP tool calls.

The denylist enumerated here is the single source of truth for what counts as
a mutating operation. A tool is mutating if it changes state in OCI, on disk
(user state files), or on an external system (Slack, Telegram).

Any new tool registered in handlers.handle_tool_call must be classified — see
the drift-catching test in tests/test_handlers.py.
"""

MUTATING_TOOLS: frozenset[str] = frozenset(
    {
        # Session / context mutations
        "set_compartment",
        "set_namespace",
        "update_tenancy_context",
        # User state writes
        "setup_confirmation_secret",
        "save_learned_query",
        "remember_preference",
        # OCI Monitoring alarms
        "create_alert",
        "update_alert",
        "delete_alert",
        # OCI Log Analytics saved searches
        "create_saved_search",
        "update_saved_search",
        "delete_saved_search",
        # OCI Management Dashboards
        "create_dashboard",
        "add_dashboard_tile",
        "delete_dashboard",
        # Outbound notifications
        "send_to_slack",
        "send_to_telegram",
    }
)


class ReadOnlyError(Exception):
    """Raised when a mutating tool is invoked under read-only mode."""


def raise_if_read_only(tool_name: str, *, read_only: bool) -> None:
    """Raise ReadOnlyError if read_only is True and tool_name is mutating."""
    if read_only and tool_name in MUTATING_TOOLS:
        raise ReadOnlyError(
            f"Tool '{tool_name}' is blocked because the server is running in "
            f"read-only mode. Restart without --read-only (or unset "
            f"OCI_LOGAN_MCP_READ_ONLY) to enable mutating operations."
        )
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/test_read_only_guard.py -v
```

Expected: PASS on all 7 tests.

- [ ] **Step 5: Commit**

```bash
git add src/oci_logan_mcp/read_only_guard.py tests/test_read_only_guard.py
git commit -m "feat(guard): add read_only_guard module with mutating tool denylist"
```

---

## Task 2.5: Suppress tenancy-context auto-capture under read-only

**Why:** Three allowed `list_*` tools (`list_log_sources`, `list_fields`, `list_compartments`) write to the shared tenancy-context YAML file as a side effect. In read-only mode the server must not touch shared state — guard each call site. Reads still return full data; only the persistence side-effect is suppressed.

**Files:**
- Modify: `src/oci_logan_mcp/handlers.py:294`, `:311`, `:627` (three `context_manager.update_*` call sites)
- Test: `tests/test_handlers.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_handlers.py`:

```python
@pytest.mark.asyncio
async def test_read_only_skips_tenancy_context_update_for_log_sources(
    handlers, settings, monkeypatch
):
    settings.read_only = True
    captured = {"called": False}

    async def fake_get_log_sources(compartment_id=None):
        return [{"name": "linux_syslog"}]

    monkeypatch.setattr(handlers.schema_manager, "get_log_sources", fake_get_log_sources)
    monkeypatch.setattr(
        handlers.context_manager,
        "update_log_sources",
        lambda sources: captured.__setitem__("called", True),
    )

    result = await handlers.handle_tool_call("list_log_sources", {})
    assert "linux_syslog" in result[0]["text"]
    assert captured["called"] is False


@pytest.mark.asyncio
async def test_non_read_only_still_updates_tenancy_context_for_log_sources(
    handlers, settings, monkeypatch
):
    settings.read_only = False
    captured = {"called": False}

    async def fake_get_log_sources(compartment_id=None):
        return [{"name": "linux_syslog"}]

    monkeypatch.setattr(handlers.schema_manager, "get_log_sources", fake_get_log_sources)
    monkeypatch.setattr(
        handlers.context_manager,
        "update_log_sources",
        lambda sources: captured.__setitem__("called", True),
    )

    await handlers.handle_tool_call("list_log_sources", {})
    assert captured["called"] is True
```

Add matching pairs for `list_fields` (stubs `get_fields`, targets `update_confirmed_fields`) and `list_compartments` (stubs `oci_client.list_compartments`, targets `update_compartments`). Six tests total.

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/test_handlers.py -k "tenancy_context" -v
```

Expected: FAIL — current handlers unconditionally call the update methods.

- [ ] **Step 3: Guard the three call sites**

In `src/oci_logan_mcp/handlers.py`:

At line 294 (inside `_list_log_sources`), replace:

```python
        # Auto-capture to tenancy context
        self.context_manager.update_log_sources(sources)
```

with:

```python
        # Auto-capture to tenancy context (suppressed in read-only mode)
        if not self.settings.read_only:
            self.context_manager.update_log_sources(sources)
```

Apply the same wrap at line 311 (`update_confirmed_fields`) and line 627 (`update_compartments`).

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/test_handlers.py -k "tenancy_context" -v
```

Expected: PASS on all six tests.

- [ ] **Step 5: Run full handler suite for regressions**

```
pytest tests/test_handlers.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/oci_logan_mcp/handlers.py tests/test_handlers.py
git commit -m "feat(handlers): suppress tenancy-context auto-capture under read-only"
```

---

## Task 3: Wire guard into `handle_tool_call`

**Files:**
- Modify: `src/oci_logan_mcp/handlers.py:84-224` (the `handle_tool_call` method)
- Test: `tests/test_handlers.py`

- [ ] **Step 1: Write the failing integration test**

Append to `tests/test_handlers.py` (use the existing `handlers` / `settings` fixtures — mirror the style of neighbouring tests):

```python
@pytest.mark.asyncio
async def test_read_only_blocks_mutating_tool(handlers, settings):
    settings.read_only = True
    result = await handlers.handle_tool_call("delete_alert", {"alarm_id": "ocid1.alarm.x"})
    assert len(result) == 1
    payload = json.loads(result[0]["text"])
    assert payload["status"] == "read_only_blocked"
    assert payload["tool"] == "delete_alert"
    assert "read-only" in payload["error"].lower()


@pytest.mark.asyncio
async def test_read_only_allows_reader(handlers, settings, monkeypatch):
    settings.read_only = True
    # Stub the reader to avoid OCI calls
    async def fake_list_saved_searches(args):
        return [{"type": "text", "text": "[]"}]
    monkeypatch.setattr(handlers, "_list_saved_searches", fake_list_saved_searches)
    result = await handlers.handle_tool_call("list_saved_searches", {})
    assert result == [{"type": "text", "text": "[]"}]


@pytest.mark.asyncio
async def test_read_only_disabled_does_not_block(handlers, settings, monkeypatch):
    settings.read_only = False
    # Stub a mutator so it doesn't actually hit OCI
    async def fake_delete_alert(args):
        return [{"type": "text", "text": "deleted"}]
    monkeypatch.setattr(handlers, "_delete_alert", fake_delete_alert)
    # Bypass confirmation gate for this test
    monkeypatch.setattr(handlers.confirmation_manager, "is_guarded", lambda name: False)
    result = await handlers.handle_tool_call("delete_alert", {})
    assert result == [{"type": "text", "text": "deleted"}]
```

(Ensure `import json` and `import pytest` are present at the top of the file; they already are.)

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/test_handlers.py::test_read_only_blocks_mutating_tool -v
```

Expected: FAIL — the handler currently does not check `settings.read_only`.

- [ ] **Step 3: Add the guard call in `handle_tool_call`**

In `src/oci_logan_mcp/handlers.py`, add the import at the top (after the other local imports, around line 27):

```python
from .read_only_guard import ReadOnlyError, raise_if_read_only
```

Then, inside `handle_tool_call`, the existing sequence is:

```
line 142:    handler = handlers.get(name)
line 143:    if not handler:
line 144:        return [{"type": "text", "text": f"Unknown tool: {name}"}]
line 145:
line 146:    user_id = self.user_store.user_id
line 147:
line 148:    # --- Confirmation gate for guarded operations ---
line 149:    if self.confirmation_manager.is_guarded(name):
```

Insert the read-only guard block **between line 146 and line 148** (i.e. after `user_id` is assigned, before the confirmation gate comment). The final sequence should be exactly:

```python
        handler = handlers.get(name)
        if not handler:
            return [{"type": "text", "text": f"Unknown tool: {name}"}]

        user_id = self.user_store.user_id

        # --- Read-only guard (runs BEFORE confirmation gate) ---
        try:
            raise_if_read_only(name, read_only=self.settings.read_only)
        except ReadOnlyError as e:
            if self.audit_logger:
                self.audit_logger.log(
                    user=user_id, tool=name, args=arguments,
                    outcome="read_only_blocked",
                )
            return [{"type": "text", "text": json.dumps({
                "status": "read_only_blocked",
                "tool": name,
                "error": str(e),
            }, indent=2)}]

        # --- Confirmation gate for guarded operations ---
        if self.confirmation_manager.is_guarded(name):
```

Do not reorder any other lines.

- [ ] **Step 4: Run the three new handler tests**

```
pytest tests/test_handlers.py::test_read_only_blocks_mutating_tool tests/test_handlers.py::test_read_only_allows_reader tests/test_handlers.py::test_read_only_disabled_does_not_block -v
```

Expected: PASS.

- [ ] **Step 5: Run full handler suite for regressions**

```
pytest tests/test_handlers.py -v
```

Expected: PASS for all existing tests.

- [ ] **Step 6: Commit**

```bash
git add src/oci_logan_mcp/handlers.py tests/test_handlers.py
git commit -m "feat(handlers): enforce read-only guard before confirmation gate"
```

---

## Task 4: CLI `--read-only` flag

**Files:**
- Modify: `src/oci_logan_mcp/__main__.py:17-76`
- Test: `tests/test_main_cli.py` (create if absent; check first)

- [ ] **Step 1: Check whether a CLI test file already exists**

```
ls tests/test_main_cli.py 2>/dev/null || ls tests/test___main__.py 2>/dev/null || echo "no CLI test file"
```

If neither exists, create `tests/test_main_cli.py`. If one exists, append to it.

- [ ] **Step 2: Write the failing test**

```python
"""CLI flag tests for oci_logan_mcp.__main__."""

import os
import sys
from unittest.mock import patch


def test_read_only_flag_sets_env_var(monkeypatch):
    from oci_logan_mcp import __main__ as main_mod
    monkeypatch.delenv("OCI_LOGAN_MCP_READ_ONLY", raising=False)

    captured = {}

    def fake_server_main():
        captured["env"] = os.environ.get("OCI_LOGAN_MCP_READ_ONLY")

    monkeypatch.setattr(sys, "argv", ["oci-logan-mcp", "--read-only"])
    monkeypatch.setattr(main_mod, "server_main", fake_server_main)
    main_mod.main()

    assert captured["env"] == "1"


def test_no_read_only_flag_leaves_env_unset(monkeypatch):
    from oci_logan_mcp import __main__ as main_mod
    monkeypatch.delenv("OCI_LOGAN_MCP_READ_ONLY", raising=False)

    captured = {}

    def fake_server_main():
        captured["env"] = os.environ.get("OCI_LOGAN_MCP_READ_ONLY")

    monkeypatch.setattr(sys, "argv", ["oci-logan-mcp"])
    monkeypatch.setattr(main_mod, "server_main", fake_server_main)
    main_mod.main()

    assert captured["env"] is None
```

- [ ] **Step 3: Run tests to verify they fail**

```
pytest tests/test_main_cli.py -v
```

Expected: FAIL — `--read-only` is an unrecognized argument.

- [ ] **Step 4: Add the flag to argparse**

In `src/oci_logan_mcp/__main__.py`, add after the `--reset-secret` argument (after line 47):

```python
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="Disable all mutating tools (alarms, saved searches, dashboards, "
             "notifications, preference writes). Reads remain allowed.",
    )
```

Then, in the `else:` branch that calls `server_main()` (around line 73-76), set the env var before invocation:

```python
    else:
        if args.user:
            os.environ["LOGAN_USER"] = args.user
        if args.read_only:
            os.environ["OCI_LOGAN_MCP_READ_ONLY"] = "1"
        server_main()
```

- [ ] **Step 5: Run tests to verify they pass**

```
pytest tests/test_main_cli.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/oci_logan_mcp/__main__.py tests/test_main_cli.py
git commit -m "feat(cli): add --read-only flag that sets OCI_LOGAN_MCP_READ_ONLY"
```

---

## Task 5: Drift-catching test (classification coverage)

**Why:** Without this, a future PR could register a new mutating tool in `handlers.handle_tool_call` and silently leave it unguarded. This test fails loudly in that case.

**Files:**
- Test: `tests/test_read_only_guard.py` (append)

- [ ] **Step 1: Write the drift-catching test**

Append to `tests/test_read_only_guard.py`:

```python
def test_all_registered_tools_are_classified():
    """Every tool dispatched in handle_tool_call must be either in
    MUTATING_TOOLS or in the known-readers allowlist below.

    If this test fails: you added a new handler. Either add it to
    MUTATING_TOOLS in read_only_guard.py, or add it to KNOWN_READERS below.
    """
    import ast
    import pathlib

    handlers_src = pathlib.Path("src/oci_logan_mcp/handlers.py").read_text()
    tree = ast.parse(handlers_src)

    # Locate the `handlers = {...}` Assign statement inside handle_tool_call
    # specifically — do NOT grab the first dict in the function, which could
    # match an unrelated literal added later.
    registered: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_tool_call"):
            continue
        for sub in ast.walk(node):
            if (
                isinstance(sub, ast.Assign)
                and len(sub.targets) == 1
                and isinstance(sub.targets[0], ast.Name)
                and sub.targets[0].id == "handlers"
                and isinstance(sub.value, ast.Dict)
            ):
                for key in sub.value.keys:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        registered.add(key.value)
                break
        break

    assert registered, (
        "Could not locate `handlers = {...}` assignment inside handle_tool_call. "
        "If the registry was refactored, update this test."
    )

    KNOWN_READERS = {
        "list_log_sources", "list_fields", "list_entities", "list_parsers",
        "list_labels", "list_saved_searches", "list_log_groups",
        "validate_query", "run_query", "run_saved_search", "run_batch_queries",
        "visualize", "export_results",
        "get_current_context", "list_compartments",
        "test_connection", "find_compartment",
        "get_query_examples", "get_log_summary",
        "get_preferences", "list_alerts", "list_dashboards",
    }

    unclassified = registered - MUTATING_TOOLS - KNOWN_READERS
    assert not unclassified, (
        f"Unclassified tools: {sorted(unclassified)}. "
        "Add each to MUTATING_TOOLS (in read_only_guard.py) or KNOWN_READERS (in this test)."
    )
```

- [ ] **Step 2: Run to confirm current state passes**

```
pytest tests/test_read_only_guard.py::test_all_registered_tools_are_classified -v
```

Expected: PASS — the current handler registry should be fully classified by the lists in this plan.

If this fails on first run, it means the actual registered tool names diverge from the classification above. Fix by reading `handlers.py:88-140` and reconciling before proceeding.

- [ ] **Step 3: Commit**

```bash
git add tests/test_read_only_guard.py
git commit -m "test(guard): catch drift when new handler is not classified"
```

---

## Task 6: README documentation

**Files:**
- Modify: `README.md` (find an appropriate section — "Configuration" or "Security" or add a new "Operating Modes" section)

- [ ] **Step 1: Add a "Read-only mode" subsection**

Add the following Markdown to `README.md` (adjust heading level to match surrounding section). Use a four-backtick outer fence in the plan only — in the final README file, the outer fence is just the literal markdown shown (the nested triple-backtick `bash` block renders as a code block inside the section).

````markdown
### Read-only mode

Start the server without any ability to mutate OCI resources or external systems:

```bash
oci-logan-mcp --read-only
# or
OCI_LOGAN_MCP_READ_ONLY=1 oci-logan-mcp
```

In read-only mode the following tools return a `read_only_blocked` error instead
of executing:

- `create_alert`, `update_alert`, `delete_alert`
- `create_saved_search`, `update_saved_search`, `delete_saved_search`
- `create_dashboard`, `add_dashboard_tile`, `delete_dashboard`
- `send_to_slack`, `send_to_telegram`
- `set_compartment`, `set_namespace`, `update_tenancy_context`
- `save_learned_query`, `remember_preference`, `setup_confirmation_secret`

All query, validation, listing, visualization and `export_results` tools remain
available. Use this mode when giving an untrusted agent, a newcomer, or an
automated process access to the server.
````

- [ ] **Step 2: Sanity-check the markdown renders**

```
grep -n "read-only" README.md
```

Expected: the new section is present.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: document --read-only flag"
```

---

## Final verification

- [ ] **Run the full suite**

```
pytest -v
```

Expected: all pre-existing tests pass; the six new tests (config x3, guard x8 including drift, handlers x3, CLI x2) all pass.

- [ ] **Smoke-check startup path is unchanged**

```
python -m oci_logan_mcp --help | grep -i read-only
```

Expected: `--read-only` appears in the help output.

- [ ] **Acceptance criteria check (from [spec](../specs/agent-guardrails.md) L1)**

- [ ] Flag/env var starts server in read-only mode.
- [ ] Mutating tools return a structured `read_only_blocked` error and never reach the OCI client.
- [ ] Read-path tools behave identically to the non-read-only mode.
- [ ] Drift-catching test prevents silent regressions.
- [ ] README documents the flag and the denylist.

---

## Notes for the executor

- TDD is non-negotiable: red → green → commit for every task.
- Do **not** refactor unrelated code. This plan is surgical — L1 only.
- `export_results` is intentionally NOT in `MUTATING_TOOLS`. Do not move it without discussion.
- If `tests/test_handlers.py` does not have a `handlers` fixture matching the shape assumed here, read the file and adjust the test to match the existing fixture pattern. Do not invent a new one. (The reviewer confirmed fixtures `settings` and `handlers` exist in the current file.)
- The spec ([../specs/agent-guardrails.md](../specs/agent-guardrails.md)) mentions `delete_learned_query` in its mutating-tool list, but that tool is **not registered** in `handlers.handle_tool_call` at the time of writing. It is therefore intentionally omitted from `MUTATING_TOOLS`. If it is registered later, add it in the same PR.
- Apply `superpowers:verification-before-completion` at the end — run the full suite fresh and report numbers, not intent.
