# N1 Investigation Recorder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add N1 playbook recording tools that capture current-process audit events into persistent, listable, retrievable, and deletable investigation playbooks.

**Architecture:** Keep N1 as a small record/catalog feature. `PlaybookStore` owns SQLite persistence under the current user's Logan base directory, `PlaybookRecorder` converts N6 audit entries into playbook steps, and `MCPHandlers` exposes four tools: `record_investigation`, `list_playbooks`, `get_playbook`, and `delete_playbook`. There is no replay, parameterization, or report generation in this PR.

**Tech Stack:** Python 3.11, standard-library `sqlite3`, existing `AuditLogger`, existing `UserStore`, existing MCP handler/tool schema patterns, pytest + pytest-asyncio.

---

## Scope

This PR implements only the N1 P0 contract from `docs/phase-2/specs/reports-and-playbooks.md`:

- `record_investigation(name, description=None, since=None, until=None)`
- `list_playbooks()`
- `get_playbook(playbook_id)`
- `delete_playbook(playbook_id)`

Explicitly out of scope:

- replaying playbooks
- auto-parameterization
- report generation
- PDF/email/Telegram report delivery
- client-supplied investigation/session ids

## File Structure

- Create: `src/oci_logan_mcp/playbook_store.py`
  - SQLite-backed storage for playbooks.
  - Stores metadata columns plus `steps_json`.
  - Returns plain dicts so handlers can JSON-serialize without custom encoders.
- Create: `src/oci_logan_mcp/playbook_recorder.py`
  - Reads audit entries from `AuditLogger`.
  - Filters the current process session by `[since, until]`.
  - Converts audit entries into N1 steps and persists via `PlaybookStore`.
- Modify: `src/oci_logan_mcp/audit.py`
  - Add a small public `iter_entries(session_id=None)` reader to avoid duplicating log/rotation parsing in N1.
- Modify: `src/oci_logan_mcp/handlers.py`
  - Instantiate `PlaybookStore` and `PlaybookRecorder`.
  - Register four new handlers.
  - Validate input and return structured JSON errors.
- Modify: `src/oci_logan_mcp/tools.py`
  - Register four MCP tool schemas.
- Modify: `src/oci_logan_mcp/read_only_guard.py`
  - Classify `record_investigation` and `delete_playbook` as mutating because they write/delete user state on disk.
- Modify: `tests/test_read_only_guard.py`
  - Classify `list_playbooks` and `get_playbook` as readers.
- Create: `tests/test_playbook_store.py`
- Create: `tests/test_playbook_recorder.py`
- Modify: `tests/test_handlers.py`
- Modify: `tests/test_tools.py`
- Modify: `README.md`

---

### Task 1: Add SQLite PlaybookStore

**Files:**
- Create: `src/oci_logan_mcp/playbook_store.py`
- Test: `tests/test_playbook_store.py`

- [ ] **Step 1: Write failing store tests**

Create `tests/test_playbook_store.py`:

```python
"""Tests for N1 playbook SQLite storage."""

import pytest

from oci_logan_mcp.playbook_store import PlaybookNotFoundError, PlaybookStore


def _playbook(name="CPU incident", created_at="2026-04-24T10:00:00+00:00"):
    return {
        "id": "pb_123",
        "name": name,
        "description": "Captured triage",
        "owner": "testuser",
        "created_at": created_at,
        "source_process_session_id": "session-a",
        "window": {
            "since": "2026-04-24T09:00:00+00:00",
            "until": "2026-04-24T10:00:00+00:00",
        },
        "steps": [
            {
                "tool": "run_query",
                "args": {"query": "*"},
                "ts": "2026-04-24T09:15:00+00:00",
                "outcome": "invoked",
            }
        ],
        "warning": "",
    }


def test_save_get_roundtrip(tmp_path):
    store = PlaybookStore(tmp_path / "playbooks.sqlite3")

    saved = store.save(_playbook())
    loaded = store.get(saved["id"])

    assert loaded == saved


def test_list_orders_newest_first(tmp_path):
    store = PlaybookStore(tmp_path / "playbooks.sqlite3")
    first = _playbook(name="old", created_at="2026-04-24T09:00:00+00:00")
    first["id"] = "pb_old"
    second = _playbook(name="new", created_at="2026-04-24T10:00:00+00:00")
    second["id"] = "pb_new"

    store.save(first)
    store.save(second)

    assert [p["id"] for p in store.list()] == ["pb_new", "pb_old"]
    assert store.list()[0]["step_count"] == 1
    assert "steps" not in store.list()[0]


def test_delete_removes_playbook(tmp_path):
    store = PlaybookStore(tmp_path / "playbooks.sqlite3")
    store.save(_playbook())

    assert store.delete("pb_123") is True
    assert store.delete("pb_123") is False
    with pytest.raises(PlaybookNotFoundError):
        store.get("pb_123")
```

- [ ] **Step 2: Run store tests and verify failure**

Run:

```bash
pytest tests/test_playbook_store.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'oci_logan_mcp.playbook_store'`.

- [ ] **Step 3: Implement PlaybookStore**

Create `src/oci_logan_mcp/playbook_store.py`:

```python
"""SQLite storage for N1 investigation playbooks."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List


class PlaybookNotFoundError(KeyError):
    """Raised when a requested playbook id does not exist."""


class PlaybookStore:
    """Persist playbooks for one Logan user."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS playbooks (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    source_process_session_id TEXT NOT NULL,
                    since_ts TEXT NOT NULL,
                    until_ts TEXT NOT NULL,
                    warning TEXT NOT NULL,
                    steps_json TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def save(self, playbook: Dict[str, Any]) -> Dict[str, Any]:
        steps_json = json.dumps(playbook["steps"], separators=(",", ":"))
        window = playbook["window"]
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO playbooks (
                    id, name, description, owner, created_at,
                    source_process_session_id, since_ts, until_ts,
                    warning, steps_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    playbook["id"],
                    playbook["name"],
                    playbook.get("description", ""),
                    playbook["owner"],
                    playbook["created_at"],
                    playbook["source_process_session_id"],
                    window["since"],
                    window["until"],
                    playbook.get("warning", ""),
                    steps_json,
                ),
            )
            conn.commit()
        return dict(playbook)

    def get(self, playbook_id: str) -> Dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM playbooks WHERE id = ?",
                (playbook_id,),
            ).fetchone()
        if row is None:
            raise PlaybookNotFoundError(playbook_id)
        return self._row_to_playbook(row)

    def list(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, name, description, owner, created_at,
                       source_process_session_id, since_ts, until_ts,
                       warning, steps_json
                FROM playbooks
                ORDER BY created_at DESC
                """
            ).fetchall()

        items = []
        for row in rows:
            steps = json.loads(row["steps_json"])
            items.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "description": row["description"],
                    "owner": row["owner"],
                    "created_at": row["created_at"],
                    "source_process_session_id": row["source_process_session_id"],
                    "window": {"since": row["since_ts"], "until": row["until_ts"]},
                    "step_count": len(steps),
                    "warning": row["warning"],
                }
            )
        return items

    def delete(self, playbook_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM playbooks WHERE id = ?", (playbook_id,))
            conn.commit()
            return cur.rowcount > 0

    @staticmethod
    def _row_to_playbook(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "owner": row["owner"],
            "created_at": row["created_at"],
            "source_process_session_id": row["source_process_session_id"],
            "window": {"since": row["since_ts"], "until": row["until_ts"]},
            "steps": json.loads(row["steps_json"]),
            "warning": row["warning"],
        }
```

- [ ] **Step 4: Run store tests**

Run:

```bash
pytest tests/test_playbook_store.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit store**

```bash
git add src/oci_logan_mcp/playbook_store.py tests/test_playbook_store.py
git commit -m "feat(n1): add playbook store"
```

---

### Task 2: Add AuditLogger.iter_entries and PlaybookRecorder

**Files:**
- Modify: `src/oci_logan_mcp/audit.py`
- Create: `src/oci_logan_mcp/playbook_recorder.py`
- Test: `tests/test_playbook_recorder.py`

- [ ] **Step 1: Write failing recorder tests**

Create `tests/test_playbook_recorder.py`:

```python
"""Tests for N1 investigation recording from audit entries."""

from datetime import datetime, timezone

from oci_logan_mcp.audit import AuditLogger
from oci_logan_mcp.playbook_recorder import PlaybookRecorder
from oci_logan_mcp.playbook_store import PlaybookStore


def _recorder(tmp_path):
    audit = AuditLogger(tmp_path / "audit", session_id="session-a")
    store = PlaybookStore(tmp_path / "playbooks.sqlite3")
    return audit, store, PlaybookRecorder(audit_logger=audit, store=store, owner="testuser")


def test_record_filters_current_session_events_by_window(tmp_path):
    audit, store, recorder = _recorder(tmp_path)
    audit.log(user="testuser", tool="run_query", args={"query": "*"}, outcome="invoked")
    audit.log(user="testuser", tool="list_fields", args={}, outcome="invoked")

    playbook = recorder.record(
        name="incident",
        description="two steps",
        since="2026-01-01T00:00:00+00:00",
        until="2099-01-01T00:00:00+00:00",
    )

    assert playbook["name"] == "incident"
    assert playbook["description"] == "two steps"
    assert playbook["owner"] == "testuser"
    assert playbook["source_process_session_id"] == "session-a"
    assert [step["tool"] for step in playbook["steps"]] == ["run_query", "list_fields"]
    assert playbook["steps"][0]["args"] == {"query": "*"}
    assert store.get(playbook["id"]) == playbook


def test_record_zero_events_adds_warning(tmp_path):
    _, _, recorder = _recorder(tmp_path)

    playbook = recorder.record(
        name="empty",
        description=None,
        since="2026-01-01T00:00:00+00:00",
        until="2026-01-01T00:01:00+00:00",
    )

    assert playbook["steps"] == []
    assert playbook["warning"] == "No audit events matched the requested time window."


def test_record_rejects_since_after_until(tmp_path):
    _, _, recorder = _recorder(tmp_path)

    try:
        recorder.record(
            name="bad",
            description=None,
            since="2026-01-01T01:00:00+00:00",
            until="2026-01-01T00:00:00+00:00",
        )
    except ValueError as exc:
        assert "since must be before until" in str(exc)
    else:
        raise AssertionError("Expected ValueError")


def test_defaults_capture_process_lifetime(tmp_path):
    audit, _, recorder = _recorder(tmp_path)
    audit.log(user="testuser", tool="test_connection", args={}, outcome="invoked")

    playbook = recorder.record(name="default-window")

    assert len(playbook["steps"]) == 1
    assert datetime.fromisoformat(playbook["window"]["since"]) <= datetime.fromisoformat(
        playbook["steps"][0]["ts"]
    )
    assert datetime.fromisoformat(playbook["steps"][0]["ts"]) <= datetime.fromisoformat(
        playbook["window"]["until"]
    )
```

- [ ] **Step 2: Run recorder tests and verify failure**

Run:

```bash
pytest tests/test_playbook_recorder.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'oci_logan_mcp.playbook_recorder'`.

- [ ] **Step 3: Add public audit entry iterator**

In `src/oci_logan_mcp/audit.py`, add `Iterator` to the typing import:

```python
from typing import Any, Dict, Iterator, List
```

Add this method after `export_transcript`:

```python
    def iter_entries(self, session_id: str | None = None) -> Iterator[Dict[str, Any]]:
        """Yield audit entries from current and rotated logs, oldest first."""
        with self._thread_lock:
            candidates = self._transcript_source_files()
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
                            if session_id is not None and entry.get("session_id") != session_id:
                                continue
                            yield entry
                except FileNotFoundError:
                    continue
```

- [ ] **Step 4: Implement PlaybookRecorder**

Create `src/oci_logan_mcp/playbook_recorder.py`:

```python
"""Record N6 audit events into N1 investigation playbooks."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .audit import AuditLogger
from .playbook_store import PlaybookStore


_PROCESS_STARTED_AT = datetime.now(timezone.utc)


def _parse_iso_datetime(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


class PlaybookRecorder:
    """Build and persist playbooks from current-process audit events."""

    def __init__(self, audit_logger: AuditLogger, store: PlaybookStore, owner: str) -> None:
        self._audit_logger = audit_logger
        self._store = store
        self._owner = owner

    def record(
        self,
        name: str,
        description: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
    ) -> Dict[str, Any]:
        since_dt = _parse_iso_datetime(since) if since else _PROCESS_STARTED_AT
        until_dt = _parse_iso_datetime(until) if until else datetime.now(timezone.utc)
        if since_dt > until_dt:
            raise ValueError("since must be before until")

        steps = []
        for entry in self._audit_logger.iter_entries(session_id=self._audit_logger.session_id):
            ts_raw = entry.get("timestamp")
            if not isinstance(ts_raw, str):
                continue
            try:
                ts = _parse_iso_datetime(ts_raw)
            except ValueError:
                continue
            if ts < since_dt or ts > until_dt:
                continue
            steps.append(
                {
                    "tool": entry.get("tool", ""),
                    "args": entry.get("args", {}),
                    "ts": _iso(ts),
                    "outcome": entry.get("outcome", ""),
                }
            )

        warning = ""
        if not steps:
            warning = "No audit events matched the requested time window."

        playbook = {
            "id": f"pb_{uuid.uuid4().hex}",
            "name": name,
            "description": description or "",
            "owner": self._owner,
            "created_at": _iso(datetime.now(timezone.utc)),
            "steps": steps,
            "source_process_session_id": self._audit_logger.session_id,
            "window": {"since": _iso(since_dt), "until": _iso(until_dt)},
            "warning": warning,
        }
        return self._store.save(playbook)
```

- [ ] **Step 5: Run recorder tests**

Run:

```bash
pytest tests/test_playbook_recorder.py tests/test_playbook_store.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit recorder**

```bash
git add src/oci_logan_mcp/audit.py src/oci_logan_mcp/playbook_recorder.py tests/test_playbook_recorder.py
git commit -m "feat(n1): record audit events as playbooks"
```

---

### Task 3: Register N1 MCP tools and handlers

**Files:**
- Modify: `src/oci_logan_mcp/handlers.py`
- Modify: `src/oci_logan_mcp/tools.py`
- Modify: `src/oci_logan_mcp/read_only_guard.py`
- Modify: `tests/test_handlers.py`
- Modify: `tests/test_tools.py`
- Modify: `tests/test_read_only_guard.py`

- [ ] **Step 1: Write failing tool schema tests**

Add to `tests/test_tools.py`:

```python
def test_playbook_tool_schemas():
    tools = {t["name"]: t for t in get_tools()}
    assert "record_investigation" in tools
    assert "list_playbooks" in tools
    assert "get_playbook" in tools
    assert "delete_playbook" in tools

    record_schema = tools["record_investigation"]["inputSchema"]
    assert record_schema["required"] == ["name"]
    assert "since" in record_schema["properties"]
    assert "until" in record_schema["properties"]

    get_schema = tools["get_playbook"]["inputSchema"]
    assert get_schema["required"] == ["playbook_id"]
```

Run:

```bash
pytest tests/test_tools.py::test_playbook_tool_schemas -q
```

Expected: FAIL because the tools are not registered.

- [ ] **Step 2: Register tool schemas**

In `src/oci_logan_mcp/tools.py`, add four tool definitions near `export_transcript`:

```python
        {
            "name": "record_investigation",
            "description": (
                "Record the current process's audit events in a time window as "
                "a named investigation playbook. P0 records only; it does not replay."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Human-readable playbook name."},
                    "description": {"type": "string", "description": "Optional description."},
                    "since": {
                        "type": "string",
                        "description": "Inclusive ISO-8601 start time. Defaults to server process start.",
                    },
                    "until": {
                        "type": "string",
                        "description": "Inclusive ISO-8601 end time. Defaults to now.",
                    },
                },
                "required": ["name"],
            },
        },
        {
            "name": "list_playbooks",
            "description": "List recorded investigation playbooks for the current user.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "get_playbook",
            "description": "Return one recorded investigation playbook with its full step list.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "playbook_id": {"type": "string", "description": "Playbook id returned by list_playbooks."}
                },
                "required": ["playbook_id"],
            },
        },
        {
            "name": "delete_playbook",
            "description": "Delete one recorded investigation playbook for the current user.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "playbook_id": {"type": "string", "description": "Playbook id to delete."}
                },
                "required": ["playbook_id"],
            },
        },
```

Run:

```bash
pytest tests/test_tools.py::test_playbook_tool_schemas -q
```

Expected: PASS.

- [ ] **Step 3: Write failing handler tests**

Add to `tests/test_handlers.py`:

```python
class TestInvestigationPlaybooks:
    @pytest.mark.asyncio
    async def test_record_investigation_routes_to_recorder(self, handlers):
        handlers.playbook_recorder.record = MagicMock(
            return_value={"id": "pb_1", "name": "incident", "steps": []}
        )

        result = await handlers.handle_tool_call(
            "record_investigation",
            {"name": "incident", "description": "desc"},
        )

        payload = json.loads(result[0]["text"])
        assert payload["id"] == "pb_1"
        handlers.playbook_recorder.record.assert_called_once_with(
            name="incident",
            description="desc",
            since=None,
            until=None,
        )

    @pytest.mark.asyncio
    async def test_record_investigation_requires_name(self, handlers):
        result = await handlers.handle_tool_call("record_investigation", {})

        payload = json.loads(result[0]["text"])
        assert payload["status"] == "error"
        assert payload["error_code"] == "missing_name"

    @pytest.mark.asyncio
    async def test_list_playbooks_routes_to_store(self, handlers):
        handlers.playbook_store.list = MagicMock(return_value=[{"id": "pb_1"}])

        result = await handlers.handle_tool_call("list_playbooks", {})

        payload = json.loads(result[0]["text"])
        assert payload == {"playbooks": [{"id": "pb_1"}]}

    @pytest.mark.asyncio
    async def test_get_playbook_returns_not_found(self, handlers):
        from oci_logan_mcp.playbook_store import PlaybookNotFoundError

        handlers.playbook_store.get = MagicMock(side_effect=PlaybookNotFoundError("pb_missing"))

        result = await handlers.handle_tool_call("get_playbook", {"playbook_id": "pb_missing"})

        payload = json.loads(result[0]["text"])
        assert payload["status"] == "error"
        assert payload["error_code"] == "playbook_not_found"

    @pytest.mark.asyncio
    async def test_delete_playbook_returns_deleted_flag(self, handlers):
        handlers.playbook_store.delete = MagicMock(return_value=True)

        result = await handlers.handle_tool_call("delete_playbook", {"playbook_id": "pb_1"})

        payload = json.loads(result[0]["text"])
        assert payload == {"deleted": True, "playbook_id": "pb_1"}
```

Run:

```bash
pytest tests/test_handlers.py::TestInvestigationPlaybooks -q
```

Expected: FAIL because handlers are not wired.

- [ ] **Step 4: Wire handler dependencies**

In `src/oci_logan_mcp/handlers.py`, add imports:

```python
from .playbook_recorder import PlaybookRecorder
from .playbook_store import PlaybookNotFoundError, PlaybookStore
```

In `MCPHandlers.__init__`, after the existing `RelatedDashboardsAndSearchesTool` assignment block for `self.related_dashboards_and_searches_tool`, add:

```python
        playbook_db_path = (
            user_store.base_dir / "users" / user_store.user_id / "playbooks.sqlite3"
        )
        self.playbook_store = PlaybookStore(playbook_db_path)
        if audit_logger is not None:
            self.playbook_recorder = PlaybookRecorder(
                audit_logger=audit_logger,
                store=self.playbook_store,
                owner=user_store.user_id,
            )
        else:
            self.playbook_recorder = None
```

In the `handlers` dict inside `handle_tool_call`, add:

```python
            "record_investigation": self._record_investigation,
            "list_playbooks": self._list_playbooks,
            "get_playbook": self._get_playbook,
            "delete_playbook": self._delete_playbook,
```

Add handler methods near `_export_transcript`:

```python
    async def _record_investigation(self, args: Dict) -> List[Dict]:
        name = args.get("name")
        if not isinstance(name, str) or not name.strip():
            return [{"type": "text", "text": json.dumps({
                "status": "error",
                "error_code": "missing_name",
                "error": "name is required",
            }, indent=2)}]
        if self.playbook_recorder is None:
            return [{"type": "text", "text": json.dumps({
                "status": "error",
                "error_code": "audit_logger_unavailable",
                "error": "Audit logger unavailable; investigation recording disabled.",
            }, indent=2)}]
        try:
            result = self.playbook_recorder.record(
                name=name.strip(),
                description=args.get("description"),
                since=args.get("since"),
                until=args.get("until"),
            )
        except ValueError as e:
            return [{"type": "text", "text": json.dumps({
                "status": "error",
                "error_code": "invalid_time_window",
                "error": str(e),
            }, indent=2)}]
        return [{"type": "text", "text": json.dumps(result, indent=2)}]

    async def _list_playbooks(self, args: Dict) -> List[Dict]:
        return [{"type": "text", "text": json.dumps({
            "playbooks": self.playbook_store.list(),
        }, indent=2)}]

    async def _get_playbook(self, args: Dict) -> List[Dict]:
        playbook_id = args.get("playbook_id")
        if not isinstance(playbook_id, str) or not playbook_id.strip():
            return [{"type": "text", "text": json.dumps({
                "status": "error",
                "error_code": "missing_playbook_id",
                "error": "playbook_id is required",
            }, indent=2)}]
        try:
            result = self.playbook_store.get(playbook_id.strip())
        except PlaybookNotFoundError:
            return [{"type": "text", "text": json.dumps({
                "status": "error",
                "error_code": "playbook_not_found",
                "error": f"Playbook '{playbook_id}' was not found.",
            }, indent=2)}]
        return [{"type": "text", "text": json.dumps(result, indent=2)}]

    async def _delete_playbook(self, args: Dict) -> List[Dict]:
        playbook_id = args.get("playbook_id")
        if not isinstance(playbook_id, str) or not playbook_id.strip():
            return [{"type": "text", "text": json.dumps({
                "status": "error",
                "error_code": "missing_playbook_id",
                "error": "playbook_id is required",
            }, indent=2)}]
        deleted = self.playbook_store.delete(playbook_id.strip())
        return [{"type": "text", "text": json.dumps({
            "deleted": deleted,
            "playbook_id": playbook_id.strip(),
        }, indent=2)}]
```

Run:

```bash
pytest tests/test_handlers.py::TestInvestigationPlaybooks -q
```

Expected: PASS.

- [ ] **Step 5: Classify read-only behavior**

In `src/oci_logan_mcp/read_only_guard.py`, add `record_investigation` and `delete_playbook` under user state writes:

```python
        "record_investigation",
        "delete_playbook",
```

In `tests/test_read_only_guard.py`, add `list_playbooks` and `get_playbook` to `KNOWN_READERS`:

```python
        "list_playbooks",
        "get_playbook",
```

Add a parameterized read-only case in `tests/test_handlers.py` if the existing read-only tests do not automatically cover denylisted tools:

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name,args", [
    ("record_investigation", {"name": "incident"}),
    ("delete_playbook", {"playbook_id": "pb_1"}),
])
async def test_playbook_mutations_blocked_in_read_only(settings, handlers, tool_name, args):
    settings.read_only = True

    result = await handlers.handle_tool_call(tool_name, args)

    payload = json.loads(result[0]["text"])
    assert payload["status"] == "read_only_blocked"
```

Run:

```bash
pytest tests/test_read_only_guard.py tests/test_handlers.py::test_playbook_mutations_blocked_in_read_only -q
```

Expected: PASS.

- [ ] **Step 6: Commit handler/tool wiring**

```bash
git add src/oci_logan_mcp/handlers.py src/oci_logan_mcp/tools.py src/oci_logan_mcp/read_only_guard.py tests/test_handlers.py tests/test_tools.py tests/test_read_only_guard.py
git commit -m "feat(n1): expose investigation playbook tools"
```

---

### Task 4: Document N1 and verify the branch

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add README section**

In `README.md`, after the transcript export section, add:

````markdown
### Investigation playbooks

Record the current process's audit trail as a named investigation playbook:

```json
{
  "tool": "record_investigation",
  "name": "CPU spike triage",
  "since": "2026-04-24T09:00:00Z",
  "until": "2026-04-24T10:00:00Z"
}
```

P0 playbooks are record/catalog artifacts only. They capture the tool calls already present in the N6 audit log and can be listed, fetched, or deleted with `list_playbooks`, `get_playbook`, and `delete_playbook`. Replay, parameterization, and report generation are separate follow-up features.
````

Also add the four tools to the "What You Can Do" table under a new or existing workflow/export row.

- [ ] **Step 2: Run focused verification**

Run:

```bash
pytest tests/test_playbook_store.py tests/test_playbook_recorder.py tests/test_handlers.py::TestInvestigationPlaybooks tests/test_tools.py::test_playbook_tool_schemas tests/test_read_only_guard.py -q
```

Expected: PASS.

- [ ] **Step 3: Run full suite**

Run:

```bash
pytest -q
```

Expected: all tests pass.

- [ ] **Step 4: Commit docs**

```bash
git add README.md
git commit -m "docs(n1): document investigation playbooks"
```

---

## Self-Review

- Spec coverage: covers N1 P0 record/list/get/delete, time-window capture, current-process session id, zero-event warning, read-only classification, and no replay.
- Intentional deferrals: N3 report generation, Report Delivery, G1 redaction, replay, parameterization, and client-supplied session ids remain outside this PR.
- Type consistency: tool argument names match the spec and handler tests: `name`, `description`, `since`, `until`, and `playbook_id`.
- Storage choice: uses standard-library SQLite as required by the Phase 2 reports/playbooks spec.
- Risk focus: explicit read-only classification prevents user-state writes in read-only deployments while keeping `list_playbooks` and `get_playbook` available as read tools.
