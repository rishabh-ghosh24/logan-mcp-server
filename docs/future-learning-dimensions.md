# Future Learning Dimensions

Ideas for expanding the learning system beyond queries. Out of scope for v0.5
(which narrowly focuses on query promotion correctness + invisible learning
infrastructure). Consider for v0.6+.

## Current state (v0.5)

The learning system only tracks and promotes **queries**:

| Thing | Storage | Cross-user promotion? |
|---|---|---|
| Queries (text, metrics, metadata) | `user_store` → `promote.py` → `shared/promoted_queries.yaml` | ✅ Yes |
| User preferences (default compartment, time range) | `preferences.py` → `PreferenceStore` | ❌ Per-user, by design |
| Tenancy context (log sources, compartments, confirmed fields) | `context_manager.py` | ❌ Per-user |
| Recent query history | `query_logger.py` | ❌ Per-user, audit only |
| OCI saved searches | OCI's own primitive | N/A (OCI-managed) |

## Candidates for future learning

### 1. Field usage patterns

Track which fields users project / filter / group by most often. Promote
"commonly-projected fields" as suggestions when the LLM generates queries for
a given log source.

**Why valuable:** LLMs currently guess at field names from schema hints.
Learning which fields users actually care about would cut hallucinations.

**Plumbing needed:** parse fields out of executed queries, aggregate per
log_source, promote top-N after threshold.

### 2. Chart / visualization preferences

Track which chart types (bar, line, pie, heatmap, KPI) users accept vs. reject
for given data shapes (time series, top-N, cross-tab).

**Why valuable:** today the LLM picks chart types by heuristic. Learning
actual preferences per data shape would improve visualization UX.

**Plumbing needed:** capture chart-type selection + data shape + user
follow-up (accept / switch / reject). Aggregate per-shape preferences.

### 3. Dashboard patterns

Track which dashboard structures (tile count, layout, chart mix) prove
successful across users. Promote winning patterns as starter dashboards.

**Why valuable:** dashboards are high-effort to create from scratch. Sharing
proven layouts via the learning system would speed first-time creation.

**Plumbing needed:** extend promotion pipeline from query-only to
dashboard-config entries. Would likely need a separate catalog source
(`SourceType.DASHBOARD_TEMPLATE`?).

### 4. Failed query corrections

Track pairs: "user tried query X, it failed, they fixed it to Y." Promote the
X → Y correction pattern so future LLMs avoid the same mistake.

**Why valuable:** failure-to-success transitions encode domain knowledge the
LLM otherwise has to rediscover. Massive signal for reducing hallucinations.

**Plumbing needed:** detect the pattern in `query_logger.py` — consecutive
failed query from the same user followed by a successful similar query.
Sanitize + promote the delta (not the raw queries).

### 5. Natural-language → query mappings

Track which NL phrases successfully produced which queries (i.e., which
queries satisfied the user based on follow-up behavior). Over time, build a
phrase → query-shape mapping.

**Why valuable:** direct signal for "when user says X, suggest Y." Would
enable much faster first-try success.

**Plumbing needed:** capture NL context from MCP tool-call arguments,
associate with resulting query, track user acceptance (did they run it
again? ask for a different version?). Promote high-confidence phrase→query
pairs.

## Design principles (carry forward)

Anything added here should follow the same principles that landed in v0.5:

1. **Invisible mechanism** — users don't browse the learning, they benefit
   via improved LLM suggestions.
2. **Explicit save path only** — if explicit save is ever useful, trigger on
   user intent phrases, never as an inventory dashboard.
3. **Provenance with trust** — each learning source tagged with trust level
   (per-user vs. promoted vs. curated).
4. **Sanitization before sharing** — strip OCIDs, tenant-specific identifiers
   before cross-user promotion.
5. **Admin-triggered promotion** — cron-style, not runtime daemon.

## Scope boundary reminder

The learning system proper is for **cross-user knowledge transfer via
promotion**. User-owned persistence (named queries they want to re-run
themselves) belongs in the OCI `saved_search` primitive, not in the learning
system. These are two different concepts; don't merge them.
