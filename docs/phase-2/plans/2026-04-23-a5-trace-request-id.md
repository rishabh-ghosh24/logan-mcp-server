# A5 — `trace_request_id` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (default for A5) or superpowers:subagent-driven-development if the task breakdown becomes mostly independent. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `trace_request_id(request_id, time_range, id_fields=None)` so one read-only call searches common request-id / trace-id fields across all sources and returns a single ordered event stream.

**Architecture:** Add a thin read-only wrapper around `PivotTool.run(entity_type="custom", field_name=...)` instead of inventing a second source-discovery path. The tool probes candidate id fields in order, soft-skips unknown field names, merges the returned timelines, de-duplicates cross-field duplicates, and returns ordered events plus matched sources. Because `next_steps` intentionally stubbed around A5 while the tool did not exist, this plan also flips those hints from `pivot_on_entity` to `trace_request_id` now that the downstream tool will be real.

**Tech Stack:** Python 3, pytest, existing `PivotTool`, `BudgetTracker`, MCP handler/tool schema wiring, and `next_steps.py`.

**Spec:** `docs/phase-2/specs/triage-toolkit.md` feature A5 plus the approved 2026-04-23 Phase 2 execution plan in the current thread.

---

## File Structure

**Create:**
- `src/oci_logan_mcp/trace_lookup.py` — `TraceRequestIdTool` and its merge / dedup helpers.
- `tests/test_trace_lookup.py` — focused unit tests for candidate-field probing, soft misses, dedup, and ordering.

**Modify:**
- `src/oci_logan_mcp/tools.py` — register the `trace_request_id` schema.
- `src/oci_logan_mcp/handlers.py` — construct the tool, add handler dispatch, and validate `request_id`, `time_range`, and optional `id_fields`.
- `src/oci_logan_mcp/next_steps.py` — switch id-field suggestions from the A5 stub behavior to the real `trace_request_id` tool.
- `tests/test_handlers.py` — add handler routing / validation tests.
- `tests/test_tools.py` — assert the new tool schema.
- `tests/test_next_steps.py` — update the request-id / trace-id hint expectations now that A5 exists.
- `tests/test_read_only_guard.py` — add `trace_request_id` to `KNOWN_READERS`.
- `README.md` — document the new tool in the investigation toolkit section.

**Do not modify:**
- `src/oci_logan_mcp/read_only_guard.py` — this tool is read-only and should stay out of `MUTATING_TOOLS`.
- `src/oci_logan_mcp/pivot_tool.py` — `entity_type="custom"` plus `field_name` already exists; A5 should compose it, not expand its surface area.

---

## Task 1: Red tests for A5 core lookup behavior

**Files:**
- Create: `tests/test_trace_lookup.py`

- [ ] **Step 1: Write the failing tests**

Add focused tests covering:
- default candidate field probe order: `Request ID`, `Trace ID`, `traceId`, `x-request-id`
- unknown / invalid candidate fields are soft misses, not fatal errors
- successful results from multiple candidate fields merge into one ordered `events` list
- dedup prefers record-id fields (`_id`, `id`, `Record ID`) when present
- fallback fingerprint dedup does not collapse distinct rows that merely share timestamp/message
- caller-supplied `id_fields` overrides the default order exactly

Representative tests:

```python
@pytest.mark.asyncio
async def test_default_id_fields_are_probed_in_order(tool):
    tool._pivot.run = AsyncMock(side_effect=[
        PivotTool._empty_result("custom", "req-42", "Request ID", {"time_range": "last_1_hour"}),
        {
            "entity": {"type": "custom", "value": "req-42", "field": "Trace ID"},
            "by_source": [],
            "cross_source_timeline": [{"Time": "2026-04-23T10:00:00Z", "source": "App Logs", "_id": "r1"}],
            "stats": {"total_events": 1, "sources_matched": 1},
            "partial": False,
            "metadata": {},
        },
    ])

    await tool.run(request_id="req-42", time_range={"time_range": "last_1_hour"})

    assert [call.kwargs["field_name"] for call in tool._pivot.run.await_args_list] == [
        "Request ID",
        "Trace ID",
        "traceId",
        "x-request-id",
    ][:len(tool._pivot.run.await_args_list)]
```

```python
@pytest.mark.asyncio
async def test_unknown_field_errors_are_soft_misses(tool):
    tool._pivot.run = AsyncMock(side_effect=[
        RuntimeError("Unknown field name: Trace ID"),
        {
            "entity": {"type": "custom", "value": "req-42", "field": "traceId"},
            "by_source": [],
            "cross_source_timeline": [{"Time": "2026-04-23T10:00:00Z", "source": "App Logs", "_id": "r1"}],
            "stats": {"total_events": 1, "sources_matched": 1},
            "partial": False,
            "metadata": {},
        },
    ])

    result = await tool.run(
        request_id="req-42",
        time_range={"time_range": "last_1_hour"},
        id_fields=["Trace ID", "traceId"],
    )

    assert result["events"][0]["_id"] == "r1"
```

```python
@pytest.mark.asyncio
async def test_record_id_dedup_beats_timestamp_message_collisions(tool):
    tool._pivot.run = AsyncMock(side_effect=[
        {
            "entity": {"type": "custom", "value": "req-42", "field": "Request ID"},
            "by_source": [],
            "cross_source_timeline": [
                {"Time": "2026-04-23T10:00:00Z", "source": "App Logs", "_id": "r1", "Message": "started"},
                {"Time": "2026-04-23T10:00:00Z", "source": "App Logs", "_id": "r2", "Message": "started"},
            ],
            "stats": {"total_events": 2, "sources_matched": 1},
            "partial": False,
            "metadata": {},
        }
    ])

    result = await tool.run(request_id="req-42", time_range={"time_range": "last_1_hour"})

    assert [event["_id"] for event in result["events"]] == ["r1", "r2"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_trace_lookup.py -v`
Expected: FAIL with `ModuleNotFoundError` or missing `TraceRequestIdTool`.

---

## Task 2: Minimal A5 implementation in a dedicated lookup module

**Files:**
- Create: `src/oci_logan_mcp/trace_lookup.py`
- Modify: `tests/test_trace_lookup.py`

- [ ] **Step 1: Write minimal implementation**

Create `TraceRequestIdTool` with:
- `DEFAULT_ID_FIELDS = ["Request ID", "Trace ID", "traceId", "x-request-id"]`
- `run(request_id, time_range, id_fields=None)` returning:

```python
{
    "request_id": "req-42",
    "events": [...],
    "sources_matched": ["App Logs", "Audit Logs"],
}
```

- helper methods:
  - `_candidate_fields(id_fields)` — validate / normalize the field list
  - `_is_soft_field_miss(exc)` — true for unknown-field / invalid-field query failures
  - `_dedup_key(event)` — prefer `_id`, `id`, `Record ID`; else fingerprint normalized row + source
  - `_merge_results(results)` — merge `cross_source_timeline`, de-dup, and sort by timestamp

Minimal implementation sketch:

```python
class TraceRequestIdTool:
    DEFAULT_ID_FIELDS = ["Request ID", "Trace ID", "traceId", "x-request-id"]
    RECORD_ID_FIELDS = ("_id", "id", "Record ID")

    def __init__(self, pivot_tool):
        self._pivot = pivot_tool

    async def run(self, request_id: str, time_range: dict, id_fields: list[str] | None = None):
        results = []
        for field_name in self._candidate_fields(id_fields):
            try:
                result = await self._pivot.run(
                    entity_type="custom",
                    entity_value=request_id,
                    time_range=time_range,
                    field_name=field_name,
                )
            except Exception as exc:
                if self._is_soft_field_miss(exc):
                    logger.debug("Skipping unknown trace field %s: %s", field_name, exc)
                    continue
                raise
            results.append(result)
        events, sources = self._merge_results(results)
        return {
            "request_id": request_id,
            "events": events,
            "sources_matched": sources,
        }
```

`_merge_results()` should:
- extend from each `cross_source_timeline`
- keep the first occurrence per dedup key
- sort by `(timestamp is None, timestamp or "")`
- return `sorted(set(sources))`

- [ ] **Step 2: Run focused tests**

Run: `python3 -m pytest tests/test_trace_lookup.py -v`
Expected: PASS

---

## Task 3: Tool schema and handler wiring

**Files:**
- Modify: `src/oci_logan_mcp/tools.py`
- Modify: `src/oci_logan_mcp/handlers.py`
- Modify: `tests/test_handlers.py`
- Modify: `tests/test_tools.py`
- Modify: `tests/test_read_only_guard.py`

- [ ] **Step 1: Write the failing handler/schema tests**

Add tests asserting:
- `trace_request_id` is registered in `get_tools()`
- handler routes to the new tool and forwards `request_id`, `time_range`, and optional `id_fields`
- missing / invalid `request_id` returns structured error
- missing / non-dict `time_range` returns structured error
- invalid `id_fields` types return structured error
- `BudgetExceededError` returns the standard `{status: "budget_exceeded", budget: ...}` payload
- `KNOWN_READERS` includes `trace_request_id`

Representative tests:

```python
@pytest.mark.asyncio
async def test_trace_request_id_routes_through_handler(self, handlers):
    handlers.trace_request_id_tool.run = AsyncMock(return_value={
        "request_id": "req-42",
        "events": [{"timestamp": "2026-04-23T10:00:00Z", "source": "App Logs"}],
        "sources_matched": ["App Logs"],
    })

    result = await handlers.handle_tool_call(
        "trace_request_id",
        {"request_id": "req-42", "time_range": {"time_range": "last_1_hour"}},
    )

    payload = json.loads(result[0]["text"])
    assert payload["request_id"] == "req-42"
    handlers.trace_request_id_tool.run.assert_awaited_once_with(
        request_id="req-42",
        time_range={"time_range": "last_1_hour"},
        id_fields=None,
    )
```

```python
def test_trace_request_id_schema():
    tools = {t["name"]: t for t in get_tools()}
    spec = tools["trace_request_id"]
    props = spec["inputSchema"]["properties"]

    assert "request_id" in props
    assert "time_range" in props
    assert "id_fields" in props
    assert spec["inputSchema"]["required"] == ["request_id", "time_range"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
- `python3 -m pytest tests/test_handlers.py -k trace_request_id -v`
- `python3 -m pytest tests/test_tools.py -k trace_request_id -v`

Expected: FAIL because handler/schema wiring is missing.

- [ ] **Step 3: Write minimal wiring**

Add to `src/oci_logan_mcp/tools.py`:

```python
{
    "name": "trace_request_id",
    "description": (
        "Search common request-id and trace-id fields across all matching log "
        "sources and return a single ordered event stream."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "request_id": {"type": "string"},
            "time_range": {
                "type": "object",
                "description": "Time window: {time_range: '...'} OR {time_start, time_end} (ISO 8601)",
            },
            "id_fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional. Override the default field probe order.",
            },
        },
        "required": ["request_id", "time_range"],
    },
}
```

Add to `src/oci_logan_mcp/handlers.py`:
- import `TraceRequestIdTool`
- construct `self.trace_request_id_tool = TraceRequestIdTool(self.pivot_tool)`
- register `"trace_request_id": self._trace_request_id` in `handle_tool_call`
- implement `_trace_request_id()` with structured validation and budget handling

Handler implementation shape:

```python
async def _trace_request_id(self, args: Dict) -> List[Dict]:
    request_id = args.get("request_id")
    if not request_id or not isinstance(request_id, str):
        return [{"type": "text", "text": json.dumps(
            {"status": "error", "error": "request_id is required and must be a string"},
            indent=2,
        )}]

    time_range = args.get("time_range")
    if not isinstance(time_range, dict) or not time_range:
        return [{"type": "text", "text": json.dumps(
            {"status": "error", "error": "time_range is required and must be an object"},
            indent=2,
        )}]

    id_fields = args.get("id_fields")
    if id_fields is not None and (
        not isinstance(id_fields, list) or any(not isinstance(v, str) or not v.strip() for v in id_fields)
    ):
        return [{"type": "text", "text": json.dumps(
            {"status": "error", "error": "id_fields must be a list of non-empty strings"},
            indent=2,
        )}]

    try:
        result = await self.trace_request_id_tool.run(
            request_id=request_id,
            time_range=time_range,
            id_fields=id_fields,
        )
    except BudgetExceededError as e:
        payload = {
            "status": "budget_exceeded",
            "error": str(e),
            "partial": None,
            "budget": self._budget_tracker.snapshot().to_dict(),
        }
        return [{"type": "text", "text": json.dumps(payload, indent=2, default=str)}]
    return [{"type": "text", "text": json.dumps(result, indent=2, default=str)}]
```

Add `trace_request_id` to `KNOWN_READERS` in `tests/test_read_only_guard.py`.

- [ ] **Step 4: Run focused wiring tests**

Run:
- `python3 -m pytest tests/test_handlers.py -k trace_request_id -v`
- `python3 -m pytest tests/test_tools.py -k trace_request_id -v`
- `python3 -m pytest tests/test_read_only_guard.py -v`

Expected: PASS

---

## Task 4: Replace the A5 stub behavior in `next_steps`

**Files:**
- Modify: `src/oci_logan_mcp/next_steps.py`
- Modify: `tests/test_next_steps.py`

- [ ] **Step 1: Write the failing next-step tests**

Update the current request-id / trace-id tests so they assert the real A5 hint now that the tool exists:

```python
def test_request_id_field_suggests_trace_request_id():
    result = {
        "data": {
            "rows": [["2026-04-20T10:00:00Z", "abc-123-def"]],
            "columns": [{"name": "Time"}, {"name": "Request ID"}],
        },
        "metadata": {},
    }
    steps = suggest("*", result)
    tools = [s.tool_name for s in steps]
    assert "trace_request_id" in tools
    trace = next(s for s in steps if s.tool_name == "trace_request_id")
    assert trace.suggested_args["request_id"] == "abc-123-def"
    assert trace.suggested_args["time_range"] == {"time_range": "last_1_hour"}
```

```python
def test_empty_id_field_does_not_suggest_trace_request_id():
    result = {
        "data": {
            "rows": [["2026-04-20", None], ["2026-04-20", ""]],
            "columns": [{"name": "Time"}, {"name": "Request ID"}],
        },
        "metadata": {},
    }
    steps = suggest("*", result)
    assert not any(s.tool_name == "trace_request_id" for s in steps)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_next_steps.py -v`
Expected: FAIL because the heuristic still emits `pivot_on_entity`.

- [ ] **Step 3: Write minimal heuristic change**

In `src/oci_logan_mcp/next_steps.py`, change `_h_request_id()` to return:

```python
return [NextStep(
    tool_name="trace_request_id",
    suggested_args={
        "request_id": str(sample),
        "time_range": {"time_range": "last_1_hour"},
        "id_fields": [id_col_name],
    },
    reason=f"Result has a '{id_col_name}' field — trace this id across all sources.",
)]
```

Do not remove the existing large-result / diff-window / validate-query heuristics.

- [ ] **Step 4: Run next-step tests**

Run: `python3 -m pytest tests/test_next_steps.py -v`
Expected: PASS

---

## Task 5: README and full verification

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update README**

Add `trace_request_id` to the investigation toolkit section with one short example:

```text
trace_request_id(
  request_id="abc-123-def",
  time_range={"time_range": "last_1_hour"}
)
```

Describe the response as:
- one ordered `events` list
- de-duplicated cross-source results
- `sources_matched`

- [ ] **Step 2: Run focused verification**

Run:
- `python3 -m pytest tests/test_trace_lookup.py tests/test_handlers.py -k trace_request_id -v`
- `python3 -m pytest tests/test_tools.py tests/test_read_only_guard.py tests/test_next_steps.py -v`

Expected: PASS

- [ ] **Step 3: Run full regression**

Run: `python3 -m pytest -q`
Expected: full suite PASS.

---

## Self-Review

- Spec coverage: A5 lookup behavior, candidate-field probing, soft-miss handling, dedup, schema/handler wiring, `KNOWN_READERS`, README, and the repo-required `next_steps` stub flip are all covered.
- Placeholder scan: no `TODO` / `TBD` / “handle appropriately” placeholders remain.
- Type consistency: handler and tool both use `request_id`, `time_range`, and `id_fields`; `next_steps` emits the same argument names the tool schema expects.
