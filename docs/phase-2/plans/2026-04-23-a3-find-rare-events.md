# A3 — `find_rare_events` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (default for A3) or superpowers:subagent-driven-development if the work unexpectedly splits into mostly independent tasks. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Decide A3 correctly, not optimistically: add a checked-in live validation probe for Logan's native `rare` command, and only ship `find_rare_events(source, field, time_range, rarity_threshold_percentile=5.0, history_days=30)` if the probe proves the returned data shape is clean enough to populate `rare_values` without heuristics.

**Architecture:** Keep A3 as a thin wrapper around Logan's native `rare` command. First add a probe script that executes representative `rare` queries and asserts that the response rows align with the declared columns and expose the value/count data needed for the wrapper. If that probe fails, stop and explicitly defer A3 with backlog/docs updates rather than inventing an approximation. If it passes, add a focused `RareEventsTool` module, wire the tool schema and handler, and annotate `rare` results with one follow-up history query.

**Tech Stack:** Python 3, pytest, existing `QueryEngine`, `MCPHandlers`, `tools.py`, `read_only_guard` coverage, and the official Oracle Log Analytics `rare`/`stats` query syntax.

**Spec:** `docs/phase-2/specs/triage-toolkit.md` feature A3 plus the approved 2026-04-23 Phase 2 execution plan in the current thread.

---

## File Structure

**Create:**
- `scripts/validate_rare_query.py` — live validation probe for native `rare` query behavior.
- `docs/phase-2/plans/2026-04-23-a3-find-rare-events.md` — this plan.

**Create only if the probe passes:**
- `src/oci_logan_mcp/rare_events.py` — `RareEventsTool` wrapper around native `rare`.
- `tests/test_rare_events.py` — focused unit tests for the wrapper.

**Modify only if the probe passes:**
- `src/oci_logan_mcp/tools.py` — register the `find_rare_events` schema.
- `src/oci_logan_mcp/handlers.py` — construct the tool, add handler dispatch, and validate inputs.
- `tests/test_handlers.py` — add handler routing / validation tests.
- `tests/test_tools.py` — assert the new tool schema.
- `tests/test_read_only_guard.py` — add `find_rare_events` to `KNOWN_READERS`.
- `README.md` — document the tool.

**Modify in both probe outcomes:**
- `docs/phase-2/backlog.md` — track any explicit defer follow-ups that remain after the probe result.
- `docs/phase-2/roadmap.md` — mark A3 as shipped or explicitly deferred for this phase, whichever the probe proves.

**Do not modify:**
- `src/oci_logan_mcp/read_only_guard.py` — A3 is read-only and must stay out of `MUTATING_TOOLS`.
- unrelated triage tools (`trace_lookup.py`, `related_resources.py`, etc.) unless the probe exposes a shared query-parsing bug that is required for A3 correctness.

---

## Task 1: Probe-native `rare` behavior before any wrapper code

**Files:**
- Create: `scripts/validate_rare_query.py`

- [ ] **Step 1: Write the failing probe script**

Create a script that:
- loads the repo's runtime/config the same way local Logan tools do
- runs at least two representative native `rare` queries against the configured tenancy
- prints the query, declared columns, raw rows, and a pass/fail verdict
- exits non-zero when any probed query returns row widths that do not align with the declared columns or omits the rare value/count data the wrapper would need

Representative probe targets:

```python
PROBE_CASES = [
    {
        "label": "raw-rare",
        "query": (
            "'Log Source' = 'Linux Syslog Logs' "
            "| rare limit = 5 showcount = true showpercent = true Severity"
        ),
        "time_range": "last_24_hours",
    },
    {
        "label": "stats-rare",
        "query": (
            "'Log Source' = 'Linux Syslog Logs' "
            "| stats count as count_in_range, earliest(Time) as first_seen, latest(Time) as last_seen by Severity "
            "| rare limit = 5 showcount = true showpercent = true Severity"
        ),
        "time_range": "last_24_hours",
    },
]
```

Validation rule sketch:

```python
def row_shape_is_usable(columns: list[dict], rows: list[list[object]]) -> bool:
    if not columns:
        return False
    expected = len(columns)
    if not rows:
        return True
    return all(len(row) == expected for row in rows)
```

- [ ] **Step 2: Run the probe to verify the current branch state**

Run: `python3 scripts/validate_rare_query.py`
Expected: either
- PASS with usable row shapes for both probe cases, or
- FAIL with an explicit mismatch report showing why A3 must defer

- [ ] **Step 3: Commit the probe scaffold**

```bash
git add scripts/validate_rare_query.py docs/phase-2/plans/2026-04-23-a3-find-rare-events.md
git commit -m "chore: add A3 rare query validation probe"
```

---

## Task 2A: Ship the wrapper only if the probe passes cleanly

**Files:**
- Create: `src/oci_logan_mcp/rare_events.py`
- Create: `tests/test_rare_events.py`
- Modify: `src/oci_logan_mcp/tools.py`
- Modify: `src/oci_logan_mcp/handlers.py`
- Modify: `tests/test_handlers.py`
- Modify: `tests/test_tools.py`
- Modify: `tests/test_read_only_guard.py`
- Modify: `README.md`
- Modify: `docs/phase-2/roadmap.md`

- [ ] **Step 1: Write the failing unit tests**

Add focused tests covering:
- required arguments: `source`, `field`, `time_range`
- native `rare` query composition for the current window
- follow-up history annotation query for `count_in_history`, `first_seen`, `last_seen`
- normalized return shape:

```python
{
    "source": "Linux Syslog Logs",
    "field": "Severity",
    "time_range": {"time_range": "last_24_hours"},
    "history_days": 30,
    "rarity_threshold_percentile": 5.0,
    "rare_values": [
        {
            "value": "critical",
            "count_in_range": 3,
            "count_in_history": 9,
            "first_seen": "2026-03-25T10:00:00+00:00",
            "last_seen": "2026-04-23T00:15:00+00:00",
            "percent_in_range": 0.4,
        }
    ],
}
```

- [ ] **Step 2: Run focused tests to verify they fail**

Run: `python3 -m pytest tests/test_rare_events.py -v`
Expected: FAIL with `ModuleNotFoundError` or missing `RareEventsTool`.

- [ ] **Step 3: Write the minimal implementation**

Create `RareEventsTool` with:
- a current-window query using native `rare`
- one follow-up history query grouped by the requested field across `history_days`
- parsing that maps the validated response columns into:
  - `value`
  - `count_in_range`
  - `percent_in_range`
  - `count_in_history`
  - `first_seen`
  - `last_seen`

Minimal implementation sketch:

```python
class RareEventsTool:
    def __init__(self, query_engine):
        self._engine = query_engine

    async def run(
        self,
        source: str,
        field: str,
        time_range: dict,
        rarity_threshold_percentile: float = 5.0,
        history_days: int = 30,
    ) -> dict:
        current_query = (
            f"'Log Source' = '{source}' "
            f"| rare limit = -1 showcount = true showpercent = true '{field}'"
        )
        current = await self._engine.execute(query=current_query, **time_range)
        history = await self._engine.execute(
            query=(
                f"'Log Source' = '{source}' "
                f"| stats count as count_in_history, earliest(Time) as first_seen, latest(Time) as last_seen by '{field}'"
            ),
            time_range=f"last_{history_days}_days",
        )
        return self._merge(current, history, rarity_threshold_percentile, source, field, time_range, history_days)
```

- [ ] **Step 4: Add tool schema and handler wiring**

Register `find_rare_events` in `tools.py` and `handlers.py`, validate:
- `source` is a non-empty string
- `field` is a non-empty string
- `time_range` is a non-empty object
- `rarity_threshold_percentile`, if present, is a positive number
- `history_days`, if present, is a positive integer

- [ ] **Step 5: Add read-only/documentation coverage**

Add `find_rare_events` to `KNOWN_READERS`, update README, and mark A3 shipped in `docs/phase-2/roadmap.md`.

- [ ] **Step 6: Run focused and full verification**

Run:
- `python3 -m pytest tests/test_rare_events.py tests/test_handlers.py::TestFindRareEvents tests/test_tools.py tests/test_read_only_guard.py -v`
- `python3 -m pytest -q`

Expected: PASS

- [ ] **Step 7: Re-run the live probe before PR**

Run: `python3 scripts/validate_rare_query.py`
Expected: PASS

- [ ] **Step 8: Commit the shipped A3 implementation**

```bash
git add src/oci_logan_mcp/rare_events.py src/oci_logan_mcp/tools.py src/oci_logan_mcp/handlers.py tests/test_rare_events.py tests/test_handlers.py tests/test_tools.py tests/test_read_only_guard.py README.md docs/phase-2/roadmap.md
git commit -m "feat: add find_rare_events tool"
```

---

## Task 2B: Explicitly defer A3 if the probe fails

**Files:**
- Modify: `docs/phase-2/backlog.md`
- Modify: `docs/phase-2/roadmap.md`
- Modify: `README.md` (only if it currently implies A3 shipped)

- [ ] **Step 1: Record the defer reason**

Add an explicit backlog entry describing the probe failure, for example:

```markdown
- **A3-F1 — native `rare` response shape not yet usable through current query path**
  - **Status:** open
  - **Why deferred:** live probe on 2026-04-23 showed native `rare` executing, but returned rows whose widths did not align with declared columns, so `find_rare_events` would require heuristics instead of a trustworthy wrapper.
  - **Next step:** fix or bypass the current query-response parsing so `rare` results can be consumed deterministically, then re-run `scripts/validate_rare_query.py`.
```

- [ ] **Step 2: Mark roadmap status honestly**

Update `docs/phase-2/roadmap.md` so A3 is clearly marked as deferred/not shipped for this phase completion rather than left ambiguous.

- [ ] **Step 3: Re-run the probe and full test suite**

Run:
- `python3 scripts/validate_rare_query.py`
- `python3 -m pytest -q`

Expected:
- probe still FAILS with the documented shape mismatch
- test suite PASSes

- [ ] **Step 4: Commit the defer-only branch**

```bash
git add docs/phase-2/backlog.md docs/phase-2/roadmap.md scripts/validate_rare_query.py docs/phase-2/plans/2026-04-23-a3-find-rare-events.md
git commit -m "docs: defer A3 after rare query validation"
```

---

## Self-Review Checklist

- Probe script is checked in and is the first executable gate.
- The branch never ships a guessed approximation of `rare`.
- If A3 ships, it does so only after a clean live probe on this branch state.
- If A3 defers, backlog and roadmap make that explicit.
- Any new deferred follow-ups are added to `docs/phase-2/backlog.md`.

---

## Execution Note

Current live evidence before implementation planning:
- Oracle docs confirm native `rare` syntax exists.
- Live `run_query` probes executed successfully, but returned row shapes that did not align cleanly with the declared columns in the current server path.

That evidence is not a substitute for the checked-in probe; it is the reason the probe must be the first gate.
