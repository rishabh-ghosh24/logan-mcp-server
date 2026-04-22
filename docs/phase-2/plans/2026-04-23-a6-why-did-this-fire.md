# A6 — `why_did_this_fire` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (default for A6) or superpowers:subagent-driven-development if the task breakdown becomes mostly independent. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `why_did_this_fire(alarm_ocid, fire_time, window_before_seconds=None, window_after_seconds=60)` for Logan-managed alarms so one call explains the alert metadata, reruns the stored Logan query in the historical fire window, and returns scoped top contributing rows when safe.

**Architecture:** Add a small read-only postmortem tool module that composes existing primitives instead of inventing new OCI flows. Alarm metadata comes from `OCILogAnalyticsClient.get_alarm()`, query execution comes from the existing `QueryEngine`, and seed extraction reuses A1’s quote-aware helper from `investigate.py`. The handler only validates inputs and forwards the structured result; the tool owns window computation, structured error codes, and degraded-seed suppression.

**Tech Stack:** Python 3, pytest, existing OCI client + query engine, standard library `datetime`/`re`. No new runtime dependency for ISO-8601 duration parsing.

**Spec:** `docs/phase-2/specs/triage-toolkit.md` feature A6 plus the approved 2026-04-23 execution plan in the current thread.

---

## File Structure

**Create:**
- `src/oci_logan_mcp/alarm_postmortem.py` — `WhyDidThisFireTool` plus local helpers for duration parsing, error payloads, absolute-window computation, and row normalization.
- `tests/test_alarm_postmortem.py` — focused unit tests for error codes, duration parsing, degraded-seed behavior, and row normalization.

**Modify:**
- `src/oci_logan_mcp/client.py` — expose `pending_duration` from `get_alarm()`.
- `src/oci_logan_mcp/tools.py` — register the `why_did_this_fire` schema.
- `src/oci_logan_mcp/handlers.py` — construct the tool, add handler dispatch, validate inputs, and return structured JSON.
- `tests/test_client.py` — pin `get_alarm()` returning `pending_duration`.
- `tests/test_handlers.py` — add handler smoke/error-routing tests.
- `tests/test_read_only_guard.py` — add `why_did_this_fire` to `KNOWN_READERS`.
- `README.md` — document the new tool in the triage toolkit section.

**Do not modify:**
- `src/oci_logan_mcp/read_only_guard.py` — this tool is read-only and should stay out of `MUTATING_TOOLS`.
- `src/oci_logan_mcp/investigate.py` — reuse `_extract_seed_filter` as-is unless tests prove a shared helper extraction is necessary.

---

## Task 1: Client metadata for alarm postmortem

**Files:**
- Modify: `src/oci_logan_mcp/client.py`
- Modify: `tests/test_client.py`

- [ ] **Step 1: Write the failing test**

Add a client test asserting `get_alarm()` exposes `pending_duration` in its returned dict.

```python
@pytest.mark.asyncio
async def test_get_alarm_includes_pending_duration(client):
    alarm = MagicMock(
        id="ocid1.alarm.oc1..test",
        display_name="Test Alarm",
        lifecycle_state="ACTIVE",
        severity="CRITICAL",
        is_enabled=True,
        destinations=[],
        query="Cpu[1m].count() > 0",
        pending_duration="PT5M",
        freeform_tags={},
    )
    client.monitoring_client.get_alarm.return_value.data = alarm

    result = await client.get_alarm("ocid1.alarm.oc1..test")

    assert result["pending_duration"] == "PT5M"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_client.py -k pending_duration -v`
Expected: FAIL because `pending_duration` is absent from the returned payload.

- [ ] **Step 3: Write minimal implementation**

Update `OCILogAnalyticsClient.get_alarm()` to include:

```python
"pending_duration": getattr(alarm, "pending_duration", None),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_client.py -k pending_duration -v`
Expected: PASS

---

## Task 2: Tool core with structured errors and window logic

**Files:**
- Create: `src/oci_logan_mcp/alarm_postmortem.py`
- Create: `tests/test_alarm_postmortem.py`

- [ ] **Step 1: Write the failing tests**

Create focused tests for:
- ISO-8601 pending duration parsing:

```python
def test_parse_pending_duration_supports_minutes_and_seconds():
    assert _parse_pending_duration_seconds("PT5M") == 300
    assert _parse_pending_duration_seconds("PT30S") == 30
```

- Distinct structured errors:

```python
@pytest.mark.asyncio
async def test_rejects_non_logan_managed_alarm(tool):
    tool._client.get_alarm = AsyncMock(return_value={"freeform_tags": {"logan_kind": "monitoring_alarm"}})
    result = await tool.run(alarm_ocid="ocid1.alarm", fire_time="2026-04-23T10:00:00Z")
    assert result["status"] == "error"
    assert result["error_code"] == "alarm_not_logan_managed"
```

- Degraded seed suppression:

```python
@pytest.mark.asyncio
async def test_degraded_seed_omits_top_rows(tool):
    tool._client.get_alarm = AsyncMock(return_value=_logan_alarm(query="*"))
    tool._engine.execute = AsyncMock(return_value={"data": {"columns": [], "rows": []}})

    result = await tool.run(alarm_ocid="ocid1.alarm", fire_time="2026-04-23T10:00:00Z")

    assert result["seed"]["seed_filter_degraded"] is True
    assert result["top_contributing_rows"] == []
    assert result["top_contributing_rows_omitted_reason"] == "unscoped_seed_filter"
    assert tool._engine.execute.await_count == 1
```

- Safe top-rows query shape:

```python
@pytest.mark.asyncio
async def test_scoped_seed_runs_fixed_top_rows_query(tool):
    tool._client.get_alarm = AsyncMock(return_value=_logan_alarm(query="'Event' = 'error' | stats count"))
    tool._engine.execute = AsyncMock(side_effect=[_trigger_result(), _rows_result()])

    result = await tool.run(alarm_ocid="ocid1.alarm", fire_time="2026-04-23T10:00:00Z")

    top_call = tool._engine.execute.await_args_list[1].kwargs
    assert top_call["query"] == "('Event' = 'error') | fields Time, 'Log Source', Severity, 'Original Log Content' | sort -Time | head 50"
    assert result["top_contributing_rows"][0]["source"] == "Audit Logs"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_alarm_postmortem.py -v`
Expected: FAIL with `ModuleNotFoundError` or missing helpers.

- [ ] **Step 3: Write minimal implementation**

Create `src/oci_logan_mcp/alarm_postmortem.py` with:
- `_parse_pending_duration_seconds(value: str | None) -> int | None`
- `_to_utc_datetime(fire_time)`
- `_format_window(fire_dt, before_s, after_s)`
- `_normalize_top_rows(query_result)`
- `WhyDidThisFireTool.run(...)`

Implementation rules:
- Fetch alarm via `self._client.get_alarm(alarm_ocid)`.
- Reject unless:
  - `freeform_tags["logan_managed"] == "true"`
  - `freeform_tags["logan_kind"] == "monitoring_alarm"`
  - `freeform_tags["logan_query"]` exists
- Derive `window_before_seconds` from explicit arg, parsed `pending_duration`, or `300`.
- Run stored Logan query once for `trigger_query_result`.
- Reuse `_extract_seed_filter()` from `investigate.py`.
- If seed degrades to `"*"`, omit raw-row query.
- Else run exactly:

```python
rows_query = (
    f"({seed_filter}) | fields Time, 'Log Source', Severity, "
    f"'Original Log Content' | sort -Time | head 50"
)
```

- Return:
  - `alarm`
  - `evaluation`
  - `window`
  - `seed`
  - `trigger_query_result`
  - `top_contributing_rows`
  - `top_contributing_rows_omitted_reason` when applicable
  - `related_saved_search_id`
  - `dashboard_id`

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_alarm_postmortem.py -v`
Expected: PASS

---

## Task 3: Tool schema and handler wiring

**Files:**
- Modify: `src/oci_logan_mcp/tools.py`
- Modify: `src/oci_logan_mcp/handlers.py`
- Modify: `tests/test_handlers.py`

- [ ] **Step 1: Write the failing handler tests**

Add tests modeled after `diff_time_windows` / `investigate_incident`:

```python
class TestWhyDidThisFire:
    @pytest.mark.asyncio
    async def test_routes_to_why_did_this_fire_tool(self, handlers):
        handlers.why_did_this_fire_tool.run = AsyncMock(return_value={"alarm": {"alarm_id": "a1"}})

        result = await handlers.handle_tool_call(
            "why_did_this_fire",
            {"alarm_ocid": "ocid1.alarm", "fire_time": "2026-04-23T10:00:00Z"},
        )

        payload = json.loads(result[0]["text"])
        assert payload["alarm"]["alarm_id"] == "a1"
        handlers.why_did_this_fire_tool.run.assert_awaited_once_with(
            alarm_ocid="ocid1.alarm",
            fire_time="2026-04-23T10:00:00Z",
            window_before_seconds=None,
            window_after_seconds=60,
        )
```

Also add a validation test for missing `alarm_ocid` or `fire_time` returning a structured `{status: "error"}` payload.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_handlers.py -k why_did_this_fire -v`
Expected: FAIL because schema/handler/tool wiring is missing.

- [ ] **Step 3: Write minimal implementation**

In `src/oci_logan_mcp/tools.py`, add the schema:

```python
{
    "name": "why_did_this_fire",
    "description": (
        "For Logan-managed monitoring alarms, explain why the alarm fired by "
        "returning the stored Logan query, the historical fire window, the "
        "trigger query result over that window, and up to 50 scoped top "
        "contributing rows when the seed query is safely scoped."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "alarm_ocid": {"type": "string"},
            "fire_time": {"type": "string", "description": "ISO-8601 timestamp of the firing event."},
            "window_before_seconds": {"type": "integer", "minimum": 1},
            "window_after_seconds": {"type": "integer", "minimum": 0, "default": 60},
        },
        "required": ["alarm_ocid", "fire_time"],
    },
}
```

In `src/oci_logan_mcp/handlers.py`:
- construct `WhyDidThisFireTool(self.oci_client, self.query_engine)`
- add `"why_did_this_fire": self._why_did_this_fire`
- implement `_why_did_this_fire()` that validates required args and forwards to the tool

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_handlers.py -k why_did_this_fire -v`
Expected: PASS

---

## Task 4: Read-only classification and documentation

**Files:**
- Modify: `tests/test_read_only_guard.py`
- Modify: `README.md`

- [ ] **Step 1: Write the failing classification/doc test**

Update `KNOWN_READERS` to include `why_did_this_fire`, then run the read-only guard test before code/docs are complete so the new handler classification is pinned.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_read_only_guard.py -v`
Expected: FAIL after handler registration if `why_did_this_fire` is not yet in `KNOWN_READERS`.

- [ ] **Step 3: Write minimal implementation**

- Add `why_did_this_fire` to `KNOWN_READERS`.
- Add a README section under the triage toolkit describing:
  - Logan-managed alarms only
  - `fire_time` historical replay window
  - degraded seed suppression for unsafe raw-row expansion

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_read_only_guard.py -v`
Expected: PASS

---

## Task 5: Final verification sweep

**Files:**
- Verify only

- [ ] **Step 1: Run focused tests**

Run:

```bash
pytest tests/test_alarm_postmortem.py tests/test_client.py -k "pending_duration or WhyDidThisFire or postmortem" -v
pytest tests/test_handlers.py -k why_did_this_fire -v
pytest tests/test_read_only_guard.py -v
```

Expected: PASS

- [ ] **Step 2: Run broader affected suite**

Run:

```bash
pytest tests/test_handlers.py tests/test_client.py tests/test_alarm_service.py tests/test_alarm_postmortem.py -v
```

Expected: PASS

- [ ] **Step 3: Sanity-check no mutating classification drift**

Run:

```bash
python - <<'PY'
from oci_logan_mcp.read_only_guard import MUTATING_TOOLS
assert "why_did_this_fire" not in MUTATING_TOOLS
print("ok")
PY
```

Expected: prints `ok`

- [ ] **Step 4: Prepare for review**

Before PR:
- use `superpowers:requesting-code-review`
- process feedback with `superpowers:receiving-code-review`
- rerun `superpowers:verification-before-completion`
