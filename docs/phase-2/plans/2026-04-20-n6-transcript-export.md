# N6 — Transcript Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the existing audit log with (a) a stable `session_id` on every entry, (b) an `invoked` event for *every* tool call (not just confirmation-gated ones), and (c) a new `export_transcript` tool that writes matching audit entries to JSONL. Delivers a debugging/auditing surface and feeds future NL-to-query training data (K3 in P1).

**Architecture:**
- **Session identity is process-scoped.** `AuditLogger` gains a constructor arg `session_id: str`. `server.py` passes `uuid.uuid4().hex` at boot. Every audit entry carries the id. One id per server process — **documented as a debugging grouping, not an investigation boundary**. Per-investigation ids are deferred to P1.
- **Promotion path (`--promote-and-exit`) is out of scope for N6 P0.** `promote.py` does not currently instantiate `AuditLogger`, so there is nothing to thread a session id into. Tagging promotion runs requires wiring audit logging into `promote_all` — deferred to P1 along with the other transcript-completeness work. `__main__.py`'s `_reset_secret` path *does* build an `AuditLogger`; we give it a literal `session_id="cli-reset-secret"` so its entries are distinguishable.
- **Per-call `invoked` event.** `handlers.handle_tool_call` writes a single `invoked` audit event for every tool call, right after `user_id` is resolved and **before** the read-only guard and confirmation gate. Captures `session_id, timestamp, user, pid, tool, args (sanitized), outcome="invoked"`. Existing `executed` / `execution_failed` / `confirmation_*` events for guarded tools remain untouched — they carry completion data for those tools.
- **`export_transcript` tool.** Reads `~/.oci-logan-mcp/logs/audit.log` + rotated backups, filters by session id, writes JSONL to `config.transcript_dir` (default `~/.oci-logan-mcp/transcripts/`). Returns `{path, event_count}`.
- **No persistent completion-event capture for non-guarded tools in P0.** That's deferred — it'd require wrapping every handler in a try/finally with result-summary extraction, which multiplies test surface. P0 ships what's cheap and useful.
- **Redaction.** `redact=True` runs args/results through a new `redact_dict` helper added to `sanitize.py` in this branch. The helper recursively walks dict/list/str values and applies existing patterns (`OCID_RE`, `IPV4_RE`, `EMAIL_RE`, `UUID_RE`, `LONG_HEX_RE`, `SECRETISH_RE`). Stretch G1 will extend patterns later; N6 P0 ships the helper itself.

**Tech Stack:** Python 3, pytest, `uuid`, `json`. Reuses existing `file_lock` machinery from `audit.py`. No new runtime dependencies.

**Spec:** [../specs/agent-guardrails.md](../specs/agent-guardrails.md) · feature N6.

---

## File Structure

**Create:**
- `tests/test_transcript_export.py` — filter-by-session, include_results, redaction, roundtrip.

**Modify:**
- `src/oci_logan_mcp/audit.py` — add `session_id` constructor arg; include `session_id` on every entry; add `export_transcript` method.
- `src/oci_logan_mcp/sanitize.py` — add `redact_dict(obj)` helper that recursively masks sensitive strings (new public API).
- `src/oci_logan_mcp/server.py:219` — pass `session_id=uuid.uuid4().hex` at construction.
- `src/oci_logan_mcp/__main__.py` — in `_reset_secret`, pass `session_id="cli-reset-secret"` at construction. (Promotion path does not build an AuditLogger — left unchanged in P0; see "Out of scope" below.)
- `src/oci_logan_mcp/handlers.py` — write `invoked` event at the top of `handle_tool_call`; add `_export_transcript` handler + registration.
- `src/oci_logan_mcp/tools.py` — register `export_transcript` schema.
- `src/oci_logan_mcp/config.py` — add `transcript_dir: Path` to settings.
- `tests/test_audit.py` — assert `session_id` present on every entry.
- `tests/test_handlers.py` — assert `invoked` event fires for both guarded and non-guarded tool calls.
- `tests/test_read_only_guard.py` — add `export_transcript` to `KNOWN_READERS`.
- `tests/test_sanitize.py` (or create) — cover `redact_dict` behavior.

**Do NOT modify:**
- `file_lock.py` — reused as-is.
- `promote.py` — see "Out of scope" below.

**Out of scope (P1):**
- Per-call `completed` / result-summary capture for non-guarded tools.
- Client-supplied session ids (pass-through from MCP request metadata).
- Per-investigation session semantics (`start_investigation()` / `end_investigation()` tools).
- **Promotion-run audit coverage.** `promote.py::promote_all` does not currently emit audit entries. Adding a promotion-scoped `AuditLogger` would expand N6's blast radius — deferred. `export_transcript` against a promotion run will return `event_count: 0` in P0; that's expected.

---

## Task 1: `AuditLogger.__init__` accepts `session_id`

**Files:**
- Modify: `src/oci_logan_mcp/audit.py`
- Test: `tests/test_audit.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_audit.py`:

```python
def test_audit_logger_accepts_session_id(tmp_path):
    from oci_logan_mcp.audit import AuditLogger
    logger = AuditLogger(tmp_path, session_id="test-session-abc")
    logger.log(user="alice", tool="t", args={}, outcome="invoked")
    lines = (tmp_path / "audit.log").read_text().splitlines()
    import json
    entry = json.loads(lines[0])
    assert entry["session_id"] == "test-session-abc"


def test_audit_logger_without_session_id_defaults_to_unknown(tmp_path):
    """Backward compat: default session_id so existing callers don't break mid-refactor."""
    from oci_logan_mcp.audit import AuditLogger
    logger = AuditLogger(tmp_path)
    logger.log(user="a", tool="t", args={}, outcome="invoked")
    import json
    entry = json.loads((tmp_path / "audit.log").read_text().splitlines()[0])
    assert "session_id" in entry
    # Default can be "unknown" or similar — just assert presence.
    assert entry["session_id"]
```

- [ ] **Step 2: Run tests, verify fail**

```
pytest tests/test_audit.py -v -k "session_id"
```

Expected: FAIL — `AuditLogger.__init__() got an unexpected keyword argument 'session_id'`.

- [ ] **Step 3: Implement**

In `src/oci_logan_mcp/audit.py`:

Update constructor:

```python
    def __init__(self, log_dir: Path, session_id: str = "unknown") -> None:
        self._log_dir = log_dir
        self._log_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self._log_dir, 0o750)
        except OSError:
            pass
        self._log_path = self._log_dir / _AUDIT_FILENAME
        self._lock_path = self._log_dir / "audit.lock"
        self._thread_lock = threading.RLock()
        self._session_id = session_id
```

Update `log()` to include `session_id` on every entry:

```python
        entry: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "session_id": self._session_id,
            "user": user,
            "pid": os.getpid(),
            "tool": tool,
            "args": clean_args,
            "outcome": outcome,
        }
```

- [ ] **Step 4: Run tests**

```
pytest tests/test_audit.py -v
```

Expected: PASS — new tests + existing tests still pass.

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(n6): AuditLogger emits session_id on every entry"
```

---

## Task 2: Propagate session_id from `server.py` and `__main__.py`

**Files:**
- Modify: `src/oci_logan_mcp/server.py`
- Modify: `src/oci_logan_mcp/__main__.py`
- Test: new unit test

- [ ] **Step 1: Write the failing test**

Append to `tests/test_server_startup.py` (or create a new test file if that file is too noisy):

```python
def test_server_audit_logger_has_session_id(monkeypatch, tmp_path):
    """server.initialize_core wires a uuid session_id into the audit logger."""
    from oci_logan_mcp.server import OCILogAnalyticsMCPServer
    import asyncio

    # Point CONFIG_PATH at a temp location so initialize_core doesn't touch real config.
    # (Follow the existing fixture/mocking pattern used elsewhere in test_server_startup.py.)
    srv = OCILogAnalyticsMCPServer()
    # Minimal mocks — reuse whatever pattern the existing startup tests use.
    # The assertion we care about:
    #   after initialize_core, srv.audit_logger._session_id is a 32-char hex uuid.
    ...
```

> **Note to implementer:** the exact mocking pattern depends on existing conventions in `test_server_startup.py` — follow whatever is already there for `initialize_core` coverage. The key assertion is `len(srv.audit_logger._session_id) == 32` and all chars are hex.

- [ ] **Step 2: Run test, verify fail**

Expected: FAIL — session_id still defaults to `"unknown"`.

- [ ] **Step 3: Update server.py**

In `src/oci_logan_mcp/server.py` around line 219:

```python
        import uuid
        session_id = uuid.uuid4().hex
        self.audit_logger = AuditLogger(log_dir=base_dir / "logs", session_id=session_id)
        self._session_id = session_id
```

(Exposing `self._session_id` on the server lets future code — N5 — share it with the budget tracker.)

- [ ] **Step 4: Update __main__.py**

The only `AuditLogger` construction in `__main__.py` today is inside `_reset_secret` (currently around line 124). Update it to pass an identifying session id:

```python
            audit = AuditLogger(base_dir / "logs", session_id="cli-reset-secret")
```

> **Note on promotion:** `promote.py::promote_all` does not build an `AuditLogger`. We are not adding one in P0 — see "Out of scope" at the top of this plan. No code change under the `--promote-and-exit` flag in N6 P0.

- [ ] **Step 5: Run tests**

```
pytest tests/ -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git commit -am "feat(n6): propagate session_id from server + CLI paths"
```

---

## Task 3: Emit `invoked` event on every tool call

**Files:**
- Modify: `src/oci_logan_mcp/handlers.py` — insert `invoked` log at top of `handle_tool_call`.
- Test: `tests/test_handlers.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_handlers.py`:

```python
@pytest.mark.asyncio
async def test_invoked_event_fires_for_non_guarded_tool(fixtures):
    """Non-guarded tool (e.g. get_current_context) produces an invoked audit entry."""
    handlers = fixtures.handlers
    audit = fixtures.audit_logger  # test fixture exposes the AuditLogger instance

    await handlers.handle_tool_call("get_current_context", {})

    entries = _read_audit_entries(fixtures.audit_log_path)
    invoked = [e for e in entries if e.get("outcome") == "invoked" and e.get("tool") == "get_current_context"]
    assert len(invoked) == 1
    assert invoked[0]["session_id"]


@pytest.mark.asyncio
async def test_invoked_event_fires_before_read_only_block(fixtures_read_only):
    """Even blocked tools produce invoked + read_only_blocked (two entries)."""
    handlers = fixtures_read_only.handlers

    await handlers.handle_tool_call("delete_alert", {"alert_id": "ocid1"})

    entries = _read_audit_entries(fixtures_read_only.audit_log_path)
    outcomes = [e["outcome"] for e in entries if e.get("tool") == "delete_alert"]
    assert "invoked" in outcomes
    assert "read_only_blocked" in outcomes
    assert outcomes.index("invoked") < outcomes.index("read_only_blocked")


@pytest.mark.asyncio
async def test_invoked_event_sanitizes_confirmation_secret(fixtures):
    """invoked entry must not contain confirmation_secret in args."""
    handlers = fixtures.handlers
    await handlers.handle_tool_call(
        "delete_alert",
        {"alert_id": "x", "confirmation_token": "t", "confirmation_secret": "SHOULDNOTAPPEAR"},
    )
    entries = _read_audit_entries(fixtures.audit_log_path)
    invoked = [e for e in entries if e["outcome"] == "invoked" and e["tool"] == "delete_alert"]
    assert invoked
    raw = json.dumps(invoked[0])
    assert "SHOULDNOTAPPEAR" not in raw


def _read_audit_entries(path):
    import json
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
```

> **Fixture naming:** the existing `tests/test_handlers.py` already constructs handlers with a mock `audit_logger`. If a `fixtures` pytest fixture doesn't yet exist in the house style, add one (or follow whatever the file uses). The important asserts are:
> - An `invoked` entry is always written first.
> - `confirmation_secret` is stripped from args.

- [ ] **Step 2: Run tests, verify fail**

```
pytest tests/test_handlers.py::test_invoked_event_fires_for_non_guarded_tool -v
```

Expected: FAIL — no `invoked` entries written.

- [ ] **Step 3: Implement the invoked hook**

In `src/oci_logan_mcp/handlers.py`, inside `handle_tool_call`, right after `user_id = self.user_store.user_id` and **before** the read-only guard block:

```python
        # --- Always-on invocation audit (N6) ---
        if self.audit_logger:
            clean_args_for_invoked = {
                k: v for k, v in arguments.items()
                if k not in (
                    "confirmation_token",
                    "confirmation_secret",
                    "confirmation_secret_confirm",
                )
            }
            try:
                self.audit_logger.log(
                    user=user_id, tool=name, args=clean_args_for_invoked,
                    outcome="invoked",
                )
            except Exception as e:
                # Audit failures must not break tool calls.
                logger.warning("invoked audit entry failed: %s", e)
```

> **Note:** `AuditLogger._STRIP_KEYS` already filters confirmation secrets at the audit layer, so the local stripping above is belt-and-suspenders. Keep both — audit-layer stripping protects against bypass if a future caller forgets.

- [ ] **Step 4: Run tests**

```
pytest tests/test_handlers.py -v
```

Expected: PASS.

- [ ] **Step 5: Full suite**

```
pytest tests/ -q
```

Expected: all green. Existing `executed` / `execution_failed` tests still pass — they don't count event totals, just check for specific outcomes.

- [ ] **Step 6: Commit**

```bash
git commit -am "feat(n6): emit invoked audit event for every tool call"
```

---

## Task 4: Add `redact_dict` helper to `sanitize.py`

The transcript-export `redact=True` path needs a recursive dict/list/string redactor. The current `sanitize.py` only exposes `looks_sensitive`, `sanitize_query_text`, `sanitize_pattern`, and `normalize_query_text` — none fit. Add the missing helper here, backed by the existing regexes, before the transcript code depends on it.

**Files:**
- Modify: `src/oci_logan_mcp/sanitize.py`
- Create or modify: `tests/test_sanitize.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_sanitize.py` (or append if present):

```python
"""Tests for redact_dict."""

from oci_logan_mcp.sanitize import redact_dict


def test_redact_dict_masks_email_in_value():
    out = redact_dict({"user": "alice@example.com"})
    assert "alice@example.com" not in repr(out)


def test_redact_dict_masks_ocid():
    out = redact_dict({"resource": "ocid1.compartment.oc1..aaaaaaaexample"})
    assert "ocid1.compartment" not in repr(out)


def test_redact_dict_masks_ipv4():
    out = redact_dict({"source": "10.0.0.42"})
    assert "10.0.0.42" not in repr(out)


def test_redact_dict_masks_secretish_key_names():
    """Values under keys like 'password', 'token', 'api_key' are fully replaced."""
    out = redact_dict({"password": "hunter2", "api_key": "live-abc", "bearer": "xyz"})
    assert "hunter2" not in repr(out)
    assert "live-abc" not in repr(out)
    assert "xyz" not in repr(out)


def test_redact_dict_recurses_into_nested_structures():
    out = redact_dict({
        "nested": {"email": "bob@example.com"},
        "list": ["ocid1.alarm.oc1..xyz", "harmless"],
    })
    s = repr(out)
    assert "bob@example.com" not in s
    assert "ocid1.alarm" not in s
    # Harmless strings pass through unchanged.
    assert "harmless" in s


def test_redact_dict_leaves_plain_strings_alone():
    out = redact_dict({"name": "Linux Syslog", "count": 42})
    assert out["name"] == "Linux Syslog"
    assert out["count"] == 42


def test_redact_dict_handles_non_string_values():
    out = redact_dict({"n": None, "b": True, "i": 42, "f": 1.5})
    assert out == {"n": None, "b": True, "i": 42, "f": 1.5}


def test_redact_dict_handles_top_level_list():
    out = redact_dict(["alice@example.com", "ok"])
    assert "alice@example.com" not in repr(out)
    assert "ok" in repr(out)


def test_redact_dict_returns_same_shape():
    inp = {"a": {"b": ["x", "y"]}, "c": 1}
    out = redact_dict(inp)
    assert isinstance(out, dict)
    assert isinstance(out["a"], dict)
    assert isinstance(out["a"]["b"], list)
```

- [ ] **Step 2: Run tests, verify fail**

```
pytest tests/test_sanitize.py -v
```

Expected: FAIL — `ImportError: cannot import name 'redact_dict' from 'oci_logan_mcp.sanitize'`.

- [ ] **Step 3: Implement `redact_dict`**

In `src/oci_logan_mcp/sanitize.py`, add at the bottom:

```python
# --- Recursive dict/list/str redactor (N6 transcript export) ---

_SECRETISH_KEY_NAMES = frozenset({
    "password", "secret", "token", "bearer",
    "authorization", "api_key", "apikey", "auth",
    "confirmation_secret", "confirmation_secret_confirm", "confirmation_token",
})

_REDACTED = "<redacted>"


def _redact_string(value: str) -> str:
    """Mask sensitive substrings in a single string. Never returns None."""
    if not value:
        return value
    cleaned = sanitize_query_text(value)
    if cleaned is None:
        # SECRETISH_RE matched — whole value is sensitive.
        return _REDACTED
    if cleaned != value and looks_sensitive(cleaned):
        # Defensive: a partial sanitize still left something suspicious.
        return _REDACTED
    return cleaned


def redact_dict(obj):
    """Recursively redact sensitive substrings in dicts, lists, tuples, and strings.

    - Dict values whose key name matches a secretish term are fully replaced with
      "<redacted>".
    - String values are passed through `_redact_string`.
    - Non-string scalars (int, float, bool, None) are returned unchanged.
    - Tuples are returned as lists (JSON-serializable output).
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and k.strip().lower() in _SECRETISH_KEY_NAMES:
                out[k] = _REDACTED
            else:
                out[k] = redact_dict(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact_dict(v) for v in obj]
    if isinstance(obj, str):
        return _redact_string(obj)
    return obj
```

> **Design notes:**
> - We pass strings through the existing `sanitize_query_text` so behavior stays consistent with how we already redact query text elsewhere.
> - Key-based masking covers cases where a value *contains no regex match* but the key name says "this is a password" (e.g., a short alphanumeric token).
> - Tuples collapse to lists for JSON output — the transcript exporter emits JSON, so this is what the caller wants.

- [ ] **Step 4: Run tests**

```
pytest tests/test_sanitize.py -v
```

Expected: PASS — all 9 tests.

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(n6): add redact_dict helper to sanitize.py"
```

---

## Task 5: Transcript export helper in `audit.py`

Put the JSONL export logic on `AuditLogger` so handler code stays thin. Uses the `redact_dict` helper added in Task 4.

**Files:**
- Modify: `src/oci_logan_mcp/audit.py`
- Create: `tests/test_transcript_export.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_transcript_export.py`:

```python
"""Tests for AuditLogger.export_transcript."""

import json
from pathlib import Path

import pytest

from oci_logan_mcp.audit import AuditLogger


def test_export_filters_by_session(tmp_path):
    log_dir = tmp_path / "logs"
    out_dir = tmp_path / "transcripts"
    logger_s1 = AuditLogger(log_dir, session_id="s1")
    logger_s1.log(user="u", tool="run_query", args={"q": "*"}, outcome="invoked")
    logger_s1.log(user="u", tool="list_fields", args={}, outcome="invoked")

    # Simulate a second session by constructing a new logger pointed at the same file.
    logger_s2 = AuditLogger(log_dir, session_id="s2")
    logger_s2.log(user="u", tool="run_query", args={"q": "b"}, outcome="invoked")

    result = logger_s1.export_transcript(session_id="s1", out_dir=out_dir)
    assert result["event_count"] == 2
    lines = Path(result["path"]).read_text().splitlines()
    parsed = [json.loads(ln) for ln in lines]
    tools = [e["tool"] for e in parsed]
    assert tools == ["run_query", "list_fields"]
    assert all(e["session_id"] == "s1" for e in parsed)


def test_export_roundtrips_through_jq(tmp_path):
    """Every line must be valid JSON."""
    log_dir = tmp_path / "logs"
    out_dir = tmp_path / "transcripts"
    logger = AuditLogger(log_dir, session_id="s")
    for i in range(5):
        logger.log(user="u", tool=f"t{i}", args={"i": i}, outcome="invoked")

    result = logger.export_transcript(session_id="s", out_dir=out_dir)
    lines = Path(result["path"]).read_text().splitlines()
    assert len(lines) == 5
    for ln in lines:
        entry = json.loads(ln)
        assert "timestamp" in entry
        assert "session_id" in entry


def test_export_nonexistent_session_returns_zero(tmp_path):
    log_dir = tmp_path / "logs"
    out_dir = tmp_path / "transcripts"
    logger = AuditLogger(log_dir, session_id="present")
    logger.log(user="u", tool="x", args={}, outcome="invoked")
    result = logger.export_transcript(session_id="nope", out_dir=out_dir)
    assert result["event_count"] == 0


def test_export_reads_rotated_backups(tmp_path):
    """Events in audit.log.1 must be included in the export."""
    log_dir = tmp_path / "logs"
    out_dir = tmp_path / "transcripts"
    log_dir.mkdir()
    # Hand-write two files to simulate rotation.
    e1 = json.dumps({"timestamp": "t", "session_id": "s", "user": "u", "pid": 1, "tool": "old", "args": {}, "outcome": "invoked"})
    e2 = json.dumps({"timestamp": "t", "session_id": "s", "user": "u", "pid": 1, "tool": "new", "args": {}, "outcome": "invoked"})
    (log_dir / "audit.log.1").write_text(e1 + "\n")
    (log_dir / "audit.log").write_text(e2 + "\n")

    logger = AuditLogger(log_dir, session_id="s")
    result = logger.export_transcript(session_id="s", out_dir=out_dir)
    tools = [json.loads(ln)["tool"] for ln in Path(result["path"]).read_text().splitlines()]
    assert "old" in tools
    assert "new" in tools


def test_export_include_results_false_strips_result_preview(tmp_path):
    log_dir = tmp_path / "logs"
    out_dir = tmp_path / "transcripts"
    logger = AuditLogger(log_dir, session_id="s")
    logger.log(user="u", tool="x", args={}, outcome="executed", result_summary="sensitive preview")

    result = logger.export_transcript(session_id="s", out_dir=out_dir, include_results=False)
    content = Path(result["path"]).read_text()
    assert "sensitive preview" not in content


def test_export_redact_masks_known_patterns(tmp_path):
    """redact=True runs entries through sanitize.py patterns."""
    log_dir = tmp_path / "logs"
    out_dir = tmp_path / "transcripts"
    logger = AuditLogger(log_dir, session_id="s")
    logger.log(user="u", tool="x", args={"email": "alice@example.com"}, outcome="invoked")

    result = logger.export_transcript(session_id="s", out_dir=out_dir, redact=True)
    content = Path(result["path"]).read_text()
    # sanitize.py masks emails — exact replacement token depends on that module's current behavior.
    assert "alice@example.com" not in content
```

- [ ] **Step 2: Run tests, verify fail**

```
pytest tests/test_transcript_export.py -v
```

Expected: FAIL — `AttributeError: 'AuditLogger' object has no attribute 'export_transcript'`.

- [ ] **Step 3: Implement `export_transcript`**

In `src/oci_logan_mcp/audit.py`, add a new method on `AuditLogger`:

```python
    def export_transcript(
        self,
        session_id: str,
        out_dir: Path,
        include_results: bool = True,
        redact: bool = False,
    ) -> Dict[str, Any]:
        """Write matching audit entries to a timestamped JSONL file.

        Returns `{path: str, event_count: int}`.
        """
        out_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_path = out_dir / f"transcript-{session_id}-{timestamp}.jsonl"

        if redact:
            from .sanitize import redact_dict  # Added in Task 4.
        else:
            redact_dict = None

        count = 0
        with self._thread_lock:
            candidates = self._transcript_source_files()
            with open(out_path, "w", encoding="utf-8") as out:
                for src in candidates:
                    try:
                        with open(src, "r", encoding="utf-8") as f:
                            for line in f:
                                line = line.strip()
                                if not line:
                                    continue
                                try:
                                    entry = json.loads(line)
                                except Exception:
                                    continue
                                if entry.get("session_id") != session_id:
                                    continue
                                if not include_results:
                                    entry.pop("result_summary", None)
                                if redact_dict is not None:
                                    entry = redact_dict(entry)
                                out.write(json.dumps(entry, separators=(",", ":")) + "\n")
                                count += 1
                    except FileNotFoundError:
                        continue
        return {"path": str(out_path), "event_count": count}

    def _transcript_source_files(self) -> list[Path]:
        """Return current log plus rotated backups, newest first."""
        files: list[Path] = []
        if self._log_path.is_file():
            files.append(self._log_path)
        for i in range(1, _MAX_BACKUPS + 1):
            p = self._log_dir / f"{_AUDIT_FILENAME}.{i}"
            if p.is_file():
                files.append(p)
        # Read order must preserve chronological append order: oldest first.
        # Rotated files contain older events than the current one.
        files.reverse()
        return files
```

- [ ] **Step 4: Run tests**

```
pytest tests/test_transcript_export.py -v
```

Expected: PASS — all 6 tests.

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(n6): AuditLogger.export_transcript writes filtered JSONL"
```

---

## Task 6: Register `export_transcript` tool

**Files:**
- Modify: `src/oci_logan_mcp/tools.py`
- Modify: `src/oci_logan_mcp/handlers.py`
- Modify: `src/oci_logan_mcp/config.py` — add `transcript_dir`.
- Modify: `tests/test_read_only_guard.py` — extend `KNOWN_READERS`.
- Test: integration

- [ ] **Step 1: Write the failing test**

Append to `tests/test_handlers.py`:

```python
@pytest.mark.asyncio
async def test_export_transcript_tool_returns_path_and_count(fixtures):
    handlers = fixtures.handlers

    # Produce some events first.
    await handlers.handle_tool_call("get_current_context", {})
    await handlers.handle_tool_call("list_saved_searches", {})

    result = await handlers.handle_tool_call(
        "export_transcript", {"session_id": "current"},
    )
    payload = json.loads(result[0]["text"])
    assert "path" in payload
    assert "event_count" in payload
    assert payload["event_count"] >= 2

    import os
    assert os.path.isfile(payload["path"])
```

- [ ] **Step 2: Run test, verify fail**

Expected: FAIL — `Unknown tool: export_transcript`.

- [ ] **Step 3: Add `transcript_dir` to `Settings` (with round-trip support)**

In `src/oci_logan_mcp/config.py`, add under `Settings`:

```python
    transcript_dir: Path = field(default_factory=lambda: Path.home() / ".oci-logan-mcp" / "transcripts")
```

In `_parse_config`:

```python
    if td := data.get("transcript_dir"):
        settings.transcript_dir = Path(td)
```

In `Settings.to_dict()`, add `transcript_dir` so `save_config()` round-trips correctly:

```python
            "transcript_dir": str(self.transcript_dir),
```

> **Why this matters:** `save_config()` writes exactly what `to_dict()` returns. If `transcript_dir` is missing from `to_dict()`, any user with a custom path loses it the next time any code path writes config — a silent config-regression we absolutely do not want.

**Add a round-trip test** to `tests/test_config.py`:

```python
def test_transcript_dir_round_trips_through_save_and_load(tmp_path):
    from oci_logan_mcp.config import Settings, save_config, load_config
    cfg_path = tmp_path / "config.yaml"

    custom = tmp_path / "my-transcripts"
    s = Settings()
    s.transcript_dir = custom
    save_config(s, config_path=cfg_path)

    loaded = load_config(config_path=cfg_path)
    assert loaded.transcript_dir == custom
```

Run it to verify:

```
pytest tests/test_config.py::test_transcript_dir_round_trips_through_save_and_load -v
```

Expected: PASS.

- [ ] **Step 4: Register tool schema**

In `src/oci_logan_mcp/tools.py`, append:

```python
        {
            "name": "export_transcript",
            "description": "Export the current (or specified) session's tool-call transcript as JSONL. Returns the file path and event count. Pass session_id='current' for the running process's session.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session id to export, or 'current' for the running process.",
                        "default": "current",
                    },
                    "include_results": {
                        "type": "boolean",
                        "description": "Include result_summary fields (on confirmation-gated tools). Default true.",
                        "default": True,
                    },
                    "redact": {
                        "type": "boolean",
                        "description": "Run known PII/secret redaction patterns over the output. Default false.",
                        "default": False,
                    },
                },
            },
        },
```

- [ ] **Step 5: Implement handler**

In `src/oci_logan_mcp/handlers.py`:

```python
    async def _export_transcript(self, args: Dict) -> List[Dict]:
        if not self.audit_logger:
            return [{"type": "text", "text": json.dumps({
                "error": "Audit logger unavailable; transcript export disabled.",
            })}]
        sid = args.get("session_id", "current")
        if sid == "current":
            sid = self.audit_logger._session_id
        out_dir = self.settings.transcript_dir
        try:
            result = self.audit_logger.export_transcript(
                session_id=sid,
                out_dir=out_dir,
                include_results=bool(args.get("include_results", True)),
                redact=bool(args.get("redact", False)),
            )
        except Exception as e:
            logger.exception("export_transcript failed")
            return [{"type": "text", "text": json.dumps({"error": str(e)})}]
        return [{"type": "text", "text": json.dumps(result, indent=2)}]
```

Register in `handle_tool_call`:

```python
            "export_transcript": self._export_transcript,
```

- [ ] **Step 6: Update drift test**

In `tests/test_read_only_guard.py`, extend `KNOWN_READERS`:

```python
        "export_transcript",
```

- [ ] **Step 7: Run tests**

```
pytest tests/ -q
```

Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git commit -am "feat(n6): register export_transcript tool"
```

---

## Task 7: README section

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add documentation**

Insert a new section, e.g. under "Observability" or "Audit":

```markdown
### Transcript export

Every tool call is recorded in the audit log with a process-scoped `session_id`. Export the current session's trail as JSONL:

```
export_transcript(session_id="current")
```

Output file lands under `~/.oci-logan-mcp/transcripts/` by default (override via `transcript_dir` in config).

Flags:
- `include_results=false` — omit `result_summary` fields (useful when sharing).
- `redact=true` — apply built-in PII/secret masking before writing.

> **Note:** `session_id` is process-scoped — one id per server process. Long-lived servers aggregate many logical investigations under one id. Per-investigation session boundaries are a future enhancement (see N1 in the feature catalog).
```

- [ ] **Step 2: Commit**

```bash
git commit -am "docs(n6): document export_transcript and session_id semantics"
```

---

## Task 8: Manual verification

- [ ] **Step 1: Start the server**

```
oci-logan-mcp
```

Tail the audit log:

```
tail -f ~/.oci-logan-mcp/logs/audit.log
```

- [ ] **Step 2: Run a non-guarded tool from an MCP client**

```
get_current_context()
```

Expected: one new line in `audit.log` with `"outcome": "invoked"` and a 32-char hex `session_id`.

- [ ] **Step 3: Run a read-only-blocked tool (server in --read-only)**

Relaunch with `--read-only`, then:

```
delete_alert(alert_id="ocid1")
```

Expected: **two** new audit lines — one `invoked`, one `read_only_blocked`. Both carry the same `session_id`.

- [ ] **Step 4: Export the transcript**

```
export_transcript(session_id="current")
```

Expected: JSON response with a file path. Inspect with `jq .`:

```
jq . ~/.oci-logan-mcp/transcripts/transcript-<id>-*.jsonl | head
```

Every line parses. All entries share the same session_id.

- [ ] **Step 5: Export a nonexistent session**

```
export_transcript(session_id="does-not-exist")
```

Expected: `event_count: 0`, empty output file.

- [ ] **Step 6: Confirm promotion path is NOT audited in P0**

```
oci-logan-mcp --promote-and-exit
grep -c "" ~/.oci-logan-mcp/logs/audit.log
```

Expected: the line count does not change (promote_all emits no audit entries in P0). This is the documented P0 limitation, not a bug.

---

## Branch acceptance checklist for N6

- [ ] Every audit entry carries `session_id`.
- [ ] `server.py` passes a uuid; `__main__.py::_reset_secret` passes `"cli-reset-secret"`.
- [ ] `handle_tool_call` emits an `invoked` event before the read-only guard and confirmation gate.
- [ ] `invoked` args are stripped of confirmation secrets.
- [ ] `redact_dict` helper exists in `sanitize.py` with unit tests.
- [ ] `transcript_dir` is present in `Settings.to_dict()` and round-trips through `save_config` / `load_config`.
- [ ] `export_transcript` tool returns `{path, event_count}` and writes valid JSONL.
- [ ] Rotated backups are included in exports.
- [ ] `include_results=false` omits `result_summary`.
- [ ] `redact=true` masks emails, OCIDs, IPv4s, UUIDs, long hex ids, and secretish keys.
- [ ] Drift test (`test_all_registered_tools_are_classified`) passes.
- [ ] README documents the feature, the process-scoped session limit, and the P0 exclusion of promotion-run audit.
- [ ] Manual smoke confirms all of the above end-to-end.
