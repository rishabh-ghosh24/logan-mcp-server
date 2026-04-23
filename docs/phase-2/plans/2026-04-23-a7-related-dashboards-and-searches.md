# A7 — `related_dashboards_and_searches` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (default for A7) or superpowers:subagent-driven-development if the task breakdown becomes mostly independent. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `related_dashboards_and_searches(source=None, entity=None, field=None)` so one read-only call suggests the most relevant dashboards, saved searches, and learned queries for a source, entity, or field.

**Architecture:** Add a small read-only discovery tool that composes existing dashboard, saved-search, and learned-query loaders instead of inventing a new persistence layer. The handler validates the request and forwards `self.user_store.user_id`; the tool owns term normalization, deterministic scoring, saved-search detail rescoring, and result shaping.

**Tech Stack:** Python 3, pytest, `asyncio.gather`, existing `DashboardService`, `SavedSearchService`, `UnifiedCatalog`, and `fuzzy_match.py`.

**Spec:** `docs/phase-2/specs/triage-toolkit.md` feature A7 plus the approved 2026-04-23 execution plan in the current thread.

---

## File Structure

**Create:**
- `src/oci_logan_mcp/related_resources.py` — `RelatedDashboardsAndSearchesTool` plus local helpers for query-term extraction, deterministic scoring, and saved-search rescoring.
- `tests/test_related_resources.py` — focused unit tests for validation, ranking, learned-query filtering, and saved-search detail fetch behavior.

**Modify:**
- `src/oci_logan_mcp/tools.py` — register the `related_dashboards_and_searches` schema.
- `src/oci_logan_mcp/handlers.py` — construct the tool, add handler dispatch, validate inputs, and pass `self.user_store.user_id`.
- `tests/test_handlers.py` — add handler routing / validation tests.
- `tests/test_read_only_guard.py` — add `related_dashboards_and_searches` to `KNOWN_READERS`.
- `README.md` — document the new tool in the investigation toolkit section.

**Do not modify:**
- `src/oci_logan_mcp/read_only_guard.py` — this tool is read-only and should stay out of `MUTATING_TOOLS`.
- `src/oci_logan_mcp/catalog.py` — load personal + shared as-is; do not add new catalog merge behavior for A7.

---

## Task 1: Red tests for A7 ranking and validation

**Files:**
- Create: `tests/test_related_resources.py`

- [ ] **Step 1: Write the failing tests**

Add focused tests covering:
- missing all of `source`, `entity`, and `field` returns a structured error from the tool layer
- dashboards rank from `display_name` and `description`
- saved searches are shortlisted from listing metadata, then rescored with fetched query text
- learned queries include personal + shared only
- builtin/starter queries never appear
- reasons are stable and tied to the strongest match

Representative tests:

```python
@pytest.mark.asyncio
async def test_requires_at_least_one_search_input(tool):
    result = await tool.run(user_id="alice")
    assert result["status"] == "error"
    assert result["error_code"] == "missing_search_input"
```

```python
@pytest.mark.asyncio
async def test_saved_search_query_text_can_rescore_shortlisted_candidate(tool):
    tool._saved_search.list_searches = AsyncMock(return_value=[
        {"id": "ss-1", "display_name": "generic"},
        {"id": "ss-2", "display_name": "auth failures"},
    ])
    tool._saved_search.get_search_by_id = AsyncMock(side_effect=[
        {"id": "ss-1", "display_name": "generic", "query": "'Request ID' = 'abc'"},
        {"id": "ss-2", "display_name": "auth failures", "query": "'User Name' = 'alice'"},
    ])

    result = await tool.run(field="Request ID", user_id="alice")

    assert result["saved_searches"][0]["id"] == "ss-1"
    assert result["saved_searches"][0]["reason"] == "field matched query"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_related_resources.py -v`
Expected: FAIL with `ModuleNotFoundError` or missing tool implementation.

---

## Task 2: Minimal A7 implementation in a dedicated read-only tool module

**Files:**
- Create: `src/oci_logan_mcp/related_resources.py`
- Modify: `tests/test_related_resources.py`

- [ ] **Step 1: Write minimal implementation**

Create `RelatedDashboardsAndSearchesTool` with:
- `run(source=None, entity=None, field=None, user_id: str)` returning either a structured error or the 3 result buckets
- `_build_terms(source, entity, field)` that normalizes and deduplicates terms using `normalize_field_name()`
- `_score_candidate(...)` that applies:
  - `+3` exact normalized hit in primary text
  - `+2` exact normalized hit in secondary text
  - `+1` fuzzy fallback hit via `find_similar_fields(..., threshold=70)`
- `_rank_dashboards(...)` over `display_name` + `description`
- `_rank_saved_searches(...)` that:
  - lists searches
  - scores listing metadata first
  - keeps top 10
  - fetches details via `asyncio.gather`
  - rescored candidates using saved-search query text
- `_rank_learned_queries(...)` using:
  - `catalog.load_personal(user_id)`
  - `catalog.load_shared()`
  - no builtin/starter loaders

Keep output shape:

```python
{
    "dashboards": [{"id": "...", "name": "...", "score": 3, "reason": "..."}],
    "saved_searches": [...],
    "learned_queries": [...],
}
```

- [ ] **Step 2: Run focused tests**

Run: `python3 -m pytest tests/test_related_resources.py -v`
Expected: PASS

---

## Task 3: Tool schema and handler wiring

**Files:**
- Modify: `src/oci_logan_mcp/tools.py`
- Modify: `src/oci_logan_mcp/handlers.py`
- Modify: `tests/test_handlers.py`
- Modify: `tests/test_read_only_guard.py`

- [ ] **Step 1: Write the failing handler/schema tests**

Add tests asserting:
- `related_dashboards_and_searches` is registered in `get_tools()`
- handler routes to the new tool and passes `self.user_store.user_id`
- missing inputs return structured validation errors
- `KNOWN_READERS` includes the new tool

Representative handler test:

```python
@pytest.mark.asyncio
async def test_routes_to_related_resources_tool(self, handlers):
    handlers.related_dashboards_and_searches_tool.run = AsyncMock(return_value={
        "dashboards": [{"id": "dash-1", "name": "Audit Dashboard", "score": 3, "reason": "source matched display_name"}],
        "saved_searches": [],
        "learned_queries": [],
    })

    result = await handlers.handle_tool_call(
        "related_dashboards_and_searches",
        {"source": "Audit"},
    )

    payload = json.loads(result[0]["text"])
    assert payload["dashboards"][0]["id"] == "dash-1"
    handlers.related_dashboards_and_searches_tool.run.assert_awaited_once_with(
        source="Audit",
        entity=None,
        field=None,
        user_id="testuser",
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_handlers.py -k related_dashboards_and_searches -v`
Expected: FAIL because schema/handler wiring is missing.

- [ ] **Step 3: Write minimal wiring**

Add to `src/oci_logan_mcp/tools.py`:

```python
{
    "name": "related_dashboards_and_searches",
    "description": (
        "Suggest existing dashboards, saved searches, and learned queries that "
        "are related to a log source, entity, or field."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "source": {"type": "string"},
            "entity": {
                "type": "object",
                "properties": {
                    "type": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["type", "value"],
            },
            "field": {"type": "string"},
        },
    },
}
```

Add to `src/oci_logan_mcp/handlers.py`:
- tool construction in `__init__`
- handler dispatch entry in `handle_tool_call`
- `_related_dashboards_and_searches()` that validates at least one input and forwards `user_id`

Add `related_dashboards_and_searches` to `KNOWN_READERS` in `tests/test_read_only_guard.py`.

- [ ] **Step 4: Run focused wiring tests**

Run:
- `python3 -m pytest tests/test_handlers.py -k related_dashboards_and_searches -v`
- `python3 -m pytest tests/test_read_only_guard.py -v`

Expected: PASS

---

## Task 4: Docs and verification

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update README**

Add `related_dashboards_and_searches` to the investigation toolkit table and add a short subsection with one example request and the 3-bucket response summary.

- [ ] **Step 2: Run focused verification**

Run:
- `python3 -m pytest tests/test_related_resources.py tests/test_handlers.py -k related_dashboards_and_searches -v`
- `python3 -m pytest tests/test_tools.py tests/test_read_only_guard.py -v`

Expected: PASS

- [ ] **Step 3: Run full regression**

Run: `python3 -m pytest -q`
Expected: full suite PASS.

---

## Self-Review

- Spec coverage: validation, personal/shared-only learned queries, saved-search detail rescoring, schema/handler wiring, `KNOWN_READERS`, and README updates are all covered.
- Placeholder scan: no `TODO` / `TBD` / “handle appropriately” placeholders remain.
- Type consistency: output buckets always use `{id, name, score, reason}`; handler passes `entity` through unchanged and separately supplies `user_id`.
