# `chronic_baseline` track for `investigate_incident` — Design Document

**Status:** Design approved, pending writing-plans.
**Branch:** `goofy-mestorf-606e96` (worktree off `main`).
**Touches:** `src/oci_logan_mcp/investigate.py`, `src/oci_logan_mcp/config.py`, `src/oci_logan_mcp/report_generator.py` (small summary phrasing reuse only), `tests/test_investigate.py`.
**Related spec:** `docs/phase-2/specs/2026-04-22-a1-investigate-incident-design.md` (the A1 base design this track extends).

> **For agentic workers:** This is a design spec. Produce a TDD implementation plan via `superpowers:writing-plans`, then execute. Do NOT start coding from this doc directly.

---

## 1. Goal

Make `investigate_incident` surface high-volume steady-state error patterns that the existing anomaly-ranking track (DiffTool `pct_change` over current vs comparison window) is structurally blind to.

**Concrete failure case the design must cover:** OKE Control Plane Logs over `last_24_hours` — 34,475 error-like events (kube-apiserver gRPC dial failure to an etcd endpoint at ~24/min). Day-over-day delta is near zero, so `_rank_anomalous_sources` returns `[]`, no per-source drill-down runs, and the report names no clusters or entities. The investigation correctly says "nothing changed" and incorrectly implies "nothing's wrong."

The fix is a second ranking track that scores sources by **absolute error-like volume** in the current window, independent of trend. Its outputs flow into the same per-source drill-down path as anomaly entries, so chronic-only sources get clusters / entities / timelines populated normally.

## 2. Non-goals

- Replacing or modifying the existing anomaly track. Anomaly ranking by `pct_change` continues unchanged.
- Defining new severity semantics. The error-like substring filter is a heuristic, not a classifier.
- Auto-tuning the volume threshold by `time_range`. The threshold is intentionally absolute — see §6.
- Adding a public `run()` parameter for tuning. Configurability is settings-only for the first implementation.

## 3. Architecture

### 3.1 Track registration

A new track function `_run_chronic_baseline_track` joins `_run_core_tracks` as a **peer** of `diff` (not a sub-step). It executes concurrently in the same `asyncio.gather` and is gated by `config.run_chronic_baseline`:

| mode      | run_chronic_baseline |
|-----------|----------------------|
| `quick`   | `False`              |
| `standard`| `True`               |
| `deep`    | `True`               |

When skipped (quick mode), the report's top-level `chronic_baseline_sources` is `[]`. No `not_run` shape is introduced; the empty list is the disabled signal.

### 3.2 Ranking query

A single ranking query runs against the **current investigation window** only. There is no comparison window — that's the whole point of this track. Query shape:

```
seed_clause   = "(<seed_filter>) and " if seed_filter != "*" else ""
focus_clause  = " and 'Log Source' in (<quoted, escaped list>)" if focus_sources else ""
error_clause  = "(" + " or ".join(f"'Original Log Content' like '%{t}%'" for t in terms) + ")"
ranking_query = f"{seed_clause}{error_clause}{focus_clause} | stats count as n by 'Log Source' | sort -n | head {top_k}"
```

Notes:
- Logan matching is case-insensitive natively, so `error`, `Error`, `ERROR`, `Failed` all match a single `%error%` / `%fail%` literal.
- `seed_filter` extraction reuses `_extract_seed_filter` (existing pre-pipe clause extractor).
- Source-name escaping in the focus clause uses the same single-quote-doubling pattern as `_compose_source_scoped_query`. Source names are user input and may contain quotes.
- Term-list values are sanitized at config load: lowercase ASCII alpha only, no quotes/wildcards. Defaults are 12 known-safe substrings, so injection isn't a real risk in practice; validation is belt-and-braces against future tunability.
- The `compartment_id` and `time_range` parameters thread through identically to the diff track.

### 3.3 Parser and threshold

`_parse_chronic_response(response, threshold)` reads the `Log Source` and `n` columns. The threshold is applied **defensively in Python** even though `head` already caps results — entries with `n < threshold` are dropped here. The parser never emits below-threshold rows. Output shape per entry:

```python
{
  "source": str,
  "error_like_count": int,
  "error_like_share_of_seed": Optional[float],
}
```

`error_like_share_of_seed` is best-effort: if the seed-track result already exposes a total event count cheaply, divide; otherwise emit `None`. **Do not run an extra query just to compute this field.** It is a context aid, not a gating signal.

### 3.4 Merge step

After both tracks complete, `_merge_chronic_with_anomalous(anomalous_sources, chronic_sources) -> merged` produces the unified candidate list:

- Each anomaly entry gets `reasons: ["anomaly"]`.
- Each chronic entry gets `reasons: ["chronic_baseline"]` and carries `error_like_count` and `error_like_share_of_seed`.
- Sources appearing in both: a single entry, `reasons: ["anomaly", "chronic_baseline"]`, with both numeric field groups merged.
- Order: anomaly entries first (preserving existing ranking by absolute `pct_change`), then chronic-only entries (by `error_like_count` desc).

`top_k` is applied per track before merge. The merged list may exceed `top_k` because the two tracks are independent; this is intentional. Existing budget / partial / per-source-concurrency handling provides the cost ceiling on drill-down.

### 3.5 Drill-down

Drill-down (`_drill_down_one_source`) iterates the merged list unchanged. Chronic-only sources flow through the same cluster + entity + timeline pipeline as anomaly entries — this is the core of the bug fix. Per-source query composition uses the same `_compose_source_scoped_query` helper; chronic-track scoping does **not** affect drill-down (drill-down keeps the seed filter only, not the error-OR clause). This keeps drill-down output comparable across the two tracks.

## 4. Output schema changes

### 4.1 New top-level field

```python
"chronic_baseline_sources": List[{
    "source": str,
    "error_like_count": int,
    "error_like_share_of_seed": Optional[float],
}]
```

This is the raw chronic ranking output before merge. Empty list when the track is disabled (quick mode) or no source crosses the threshold. On track failure, also empty list — failure is signalled via `partial_reasons` only.

### 4.2 Per-source entry additions

Each entry in `anomalous_sources` (the merged list — name preserved for backward read-compat) gains:

```python
"reasons": List[str]                          # ["anomaly"], ["chronic_baseline"], or both
"error_like_count": Optional[int]             # None for anomaly-only entries
"error_like_share_of_seed": Optional[float]   # None for anomaly-only entries
```

Existing fields (`current_count`, `comparison_count`, `pct_change`, `top_error_clusters`, `top_entities`, `timeline`, `errors`) are preserved and nullable for chronic-only entries. Existing consumers can ignore the new fields.

### 4.3 Failure signalling

The chronic track is treated like any other peer track in `_run_core_tracks`. Stable `partial_reasons` strings:

| Outcome                       | `partial_reasons` entry        |
|-------------------------------|--------------------------------|
| `BudgetExceededError`         | `budget_exceeded` (existing)   |
| `asyncio.TimeoutError`        | `chronic_baseline_timeout`     |
| Other exception               | `chronic_baseline_errors`      |

In all failure cases, `chronic_baseline_sources = []` and the anomaly-track flow continues unaffected. The investigation produces whatever it would have produced without this feature.

### 4.4 Summary text

`_templated_summary` gains one appended sentence when **any** merged entry has `"chronic_baseline"` in its `reasons` list — including overlap entries. Counting only chronic-only entries would hide cases where a source is both anomalous *and* operationally heavy:

```python
chronic_count = sum(1 for s in merged if "chronic_baseline" in s["reasons"])
if chronic_count:
    top = next(s for s in merged if "chronic_baseline" in s["reasons"])
    parts.append(
        f"Chronic baseline: {chronic_count} source(s) with high error-like volume "
        f"(top: {top['source']} {top['error_like_count']} events)."
    )
```

## 5. Configuration

New dataclass in `config.py`:

```python
@dataclass
class ChronicBaselineConfig:
    """Chronic-baseline track for investigate_incident — see
    docs/superpowers/specs/2026-05-09-chronic-baseline-track-design.md."""
    enabled: bool = True
    error_like_terms: Tuple[str, ...] = (
        "error", "fail", "fatal", "critical", "exception", "timeout",
        "reject", "deny", "drop", "nxdomain", "servfail", "refused",
    )
    count_threshold: int = 1000   # absolute event count over the investigation window
```

Wired into `Settings` as `chronic_baseline: ChronicBaselineConfig = field(default_factory=ChronicBaselineConfig)`. `to_dict` and the YAML loader gain a parallel section, mirroring `IngestionHealthConfig`.

Mode wiring lives in `_InvestigationModeConfig`:

```python
run_chronic_baseline: bool   # False for quick, True for standard/deep
```

`_mode_config` returns the existing per-mode struct with the new flag set per §3.1.

## 6. Threshold rationale

`count_threshold = 1000` is the first default. Reasoning:

- Over `last_1_hour` (default `time_range`), 1000 events is ~17/min — well above background log noise on most sources, well below the OKE case (24/min × 60 = 1440).
- Over `last_24_hours`, the same threshold means ~42/hour — lenient, but lets steady incidents surface.

The threshold is intentionally **not auto-scaled** by window length. Absolute volume is what makes a finding "operationally serious"; a source that's quiet in absolute terms is rarely interesting even if its share-of-seed-volume is high. Cost and noise are controlled by:

- Mode gating (`quick` skips the track entirely).
- Per-track `top_k` (`head N` in the ranking query).

If field experience shows the default is too lenient or too strict, tune via `settings.chronic_baseline.count_threshold` — no code change required.

## 7. Interaction rules

- **`focus_sources`**: when set, both tracks restrict to this list. Chronic ranking gains `and 'Log Source' in (...)` with proper escaping. Sources outside `focus_sources` cannot appear in `chronic_baseline_sources` or in the merged list. This matches existing anomaly-track behavior.
- **`top_k`**: applied per track via `| head {top_k}`. Merge step preserves both, dedupes by source name. Merged candidate count may exceed `top_k`; this is intentional (two independent tracks).
- **`mode`**: see §3.1. Quick skips chronic; standard and deep run it.
- **Backward compatibility**: existing report consumers see strictly additive new fields. The `reasons` field is always present; existing entries get `["anomaly"]`. No existing field's type or semantics changes.

## 8. Test plan

Tests live in `tests/test_investigate.py` and follow the existing fake-engine pattern.

**Unit tests for pure helpers**

1. `_compose_chronic_baseline_query`:
   - wildcard seed (no leading `(*) and`)
   - simple seed (parenthesized seed clause)
   - `focus_sources` set (in-clause appended, single-quote escaping verified)
   - custom term list from settings
   - source-name and term escaping (embedded quotes doubled)
2. `_parse_chronic_response`:
   - happy path — multiple rows, sorted output preserved
   - malformed columns — empty list
   - threshold filter (defensive) — below-threshold rows dropped even if returned by query
   - null counts — row skipped
   - `share_of_seed` populated when seed total available; `None` otherwise (no extra query made)
3. `_merge_chronic_with_anomalous`:
   - anomaly-only — pass through, `reasons=["anomaly"]`
   - chronic-only — entries appended after anomalies, `reasons=["chronic_baseline"]`
   - overlap — single entry per source, `reasons=["anomaly", "chronic_baseline"]`, numeric fields from both
   - ordering invariant — anomaly entries always precede chronic-only entries
   - empty inputs — empty output

**Integration tests on `InvestigateIncidentTool.run()`**

4. **OKE-style scenario (the primary bug fix)**: anomaly delta empty, chronic returns one source (`error_like_count=34475`) over threshold. Assertions:
   - that source appears in the merged `anomalous_sources` with `reasons=["chronic_baseline"]`
   - **drill-down ran for it** — `top_error_clusters`, `top_entities`, and `timeline` are populated, not empty (this is the core regression-prevention assertion)
   - `chronic_baseline_sources` at top level lists the source
   - summary contains the chronic-baseline sentence
5. **Both tracks fire on the same source**: source appears once in merged list with `reasons=["anomaly", "chronic_baseline"]`, both numeric field groups present, drill-down ran once.
6. **`quick` mode skips the track**: no chronic ranking query is made by the fake engine; `chronic_baseline_sources` is `[]`; no chronic-baseline sentence in the summary.
7. **Below-threshold filter**: chronic raw response has 5 sources; only 1 above the configured threshold reaches merge and drill-down.
8. **Chronic track failure modes** — three sub-tests:
   - `asyncio.TimeoutError` → `partial_reasons` contains `chronic_baseline_timeout`, anomaly flow unaffected
   - generic exception → `partial_reasons` contains `chronic_baseline_errors`, anomaly flow unaffected
   - `BudgetExceededError` → `partial_reasons` contains `budget_exceeded`, anomaly flow unaffected
9. **`focus_sources` constrains both tracks**: focus list of `[X, Y]`; the chronic ranking query string contains `and 'Log Source' in ('X','Y')`; sources outside `focus_sources` never appear in merged output even if returned by a misbehaving fake engine.

**Backward-compat**

10. All pre-existing `tests/test_investigate.py` cases pass without modification. The new `reasons` field on existing anomaly entries is additive.

## 9. Open questions deferred to implementation

- Where exactly to place `_run_chronic_baseline_track` in the `_run_core_tracks` track-spec list. Implementation will follow the existing `track_specs` list pattern and add a conditional append, identical to how `run_ingestion_health` and `run_parser_failures` are gated.
- Whether to log the composed chronic query at debug level — likely yes for parity with existing tracks, but a follow-up if it's not a one-line addition.

## 10. Out of scope (possible follow-ups)

- A `Severity`-based fast path (option C from brainstorming). Defer until field data shows whether structured severity coverage is good enough to be worth a hybrid query. The pure-substring approach is the conservative first cut.
- Per-source rate-of-change *and* absolute-volume scoring (e.g. "high volume AND increasing"). The current design treats the two signals as independent ranking tracks. A combined score is a possible P1.
- A user-facing `chronic_threshold` parameter on `run()`. Settings-only is sufficient for v1.
