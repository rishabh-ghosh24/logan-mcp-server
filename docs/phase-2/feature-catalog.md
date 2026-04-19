# Phase 2 — Feature Catalog

Comprehensive list of features brainstormed by Agent A (monitoring/observability expert persona) for the next phase of `logan-mcp-server`. This catalog is the canonical reference: it preserves the full ideation output, Agent A's original conviction tags, size estimates, plain-English descriptions, and the disposition decisions made during founder review.

## How to read this

- **Code** — stable short code (A1, H1, etc.) used across all Phase 2 docs for traceability.
- **Conviction** — Agent A's original bet: 🔥 must-have, ⭐ high-value, 💡 nice-to-have.
- **Size** — rough build estimate: S (days), M (1–2 weeks), L (3+ weeks), XL (multi-month).
- **Disposition** — where the feature landed after founder review: `P0-core`, `P0-stretch`, `P1`, `P2`, or `Rejected`.
- **Notes** — clarifications, reframings, or scope decisions from discussion.

---

## A. Investigation & Triage

### A1 — `investigate_incident` (orchestrator)
- **Conviction:** 🔥 · **Size:** XL · **Disposition:** P0-core
- **What:** One tool call that accepts an alert/time-range and returns a ranked set of anomalous sources, top error clusters, changed entities, and a draft timeline.
- **User story:** As an on-call SRE, when I'm paged, I want to hand the agent an alert+range and get back a first-cut investigation so I skip 20 minutes of boilerplate queries.
- **Why it matters:** Flagship feature. Turns the MCP from "query tools" into "a teammate." Consumes A2 and A4 as primitives.

### A2 — `diff_time_windows`
- **Conviction:** 🔥 · **Size:** M · **Disposition:** P0-core
- **What:** Compare "last hour" vs. "same hour yesterday/last week" on a source or field distribution.
- **User story:** As an SRE, when something breaks, I want to instantly see what changed vs. normal without writing two queries and eyeballing them.
- **Why it matters:** Cheapest-to-build primitive in observability. Also a building block inside A1.

### A3 — `find_rare_events` (rarity scoring)
- **Conviction:** ⭐ · **Size:** M · **Disposition:** P0-stretch
- **What:** Return rows scoring low on historical frequency for a given source+field (first-seen IPs, user agents, processes).
- **User story:** As a SOC analyst, I want to surface first-seen field values in a window so I catch novel activity.
- **Notes:** Different from Logan's `cluster` (message similarity) and `outlier` (numeric distribution). A3 is **frequency-of-value** across history. Logan's `rare` command covers part of this — A3's value is as a packaged tool wrapper with good defaults for agents, not a new capability.

### A4 — `pivot_on_entity`
- **Conviction:** 🔥 · **Size:** M · **Disposition:** P0-core
- **What:** Given a host/user/request-id, one-call pull of everything about that entity across all sources in a time window.
- **User story:** As on-call, once I spot a suspicious entity, I want one call to gather everything touching it instead of listing sources myself.
- **Why it matters:** Second most-used agent primitive after running a query.

### A5 — `trace_request_id` (multi-source join)
- **Conviction:** ⭐ · **Size:** M · **Disposition:** P0-stretch
- **What:** Given a request-id, search all sources and produce an ordered event list.
- **User story:** As an SRE chasing a user complaint with a request-id, I want the server to handle the multi-source search without me enumerating sources.

### A6 — `why_did_this_fire` (alarm post-mortem)
- **Conviction:** 🔥 · **Size:** M · **Disposition:** P0-core
- **What:** Given an alarm OCID and fire-time, re-run the source query bracketed around the event, return top contributing rows and dashboard link.
- **User story:** As on-call, given a page, I want a one-call "why did this fire" without reconstructing context.
- **Why it matters:** Huge demo value — we own both sides of the alarm pipe.

### A7 — `related_dashboards_and_searches`
- **Conviction:** ⭐ · **Size:** S · **Disposition:** P0-stretch
- **What:** Given a source/entity/field, suggest existing dashboards, saved searches, learned queries.
- **User story:** As a responder, I want to find existing work on this topic before I duplicate it.

---

## B. Correlation & Pivoting

> **Disposition for all of Category B: Out of scope for Phase 2.**
> Metrics and APM correlation will be handled by the separate `oci-mon-mcp` server. The LLM agent will call both MCP servers and do cross-source correlation client-side. Merging the two servers may be reconsidered later but is not planned for this phase.

### B1 — Metrics correlation (`query_oci_metrics`)
- **Conviction:** 🔥 · **Size:** L · **Disposition:** Out-of-scope (deferred to `oci-mon-mcp`)
- **What:** Correlate a log spike with host CPU/memory/network metrics.

### B2 — APM trace lookup
- **Conviction:** ⭐ · **Size:** L · **Disposition:** Out-of-scope
- **What:** Align distributed trace spans with log timestamps.

### B3 — Cross-compartment correlation helper
- **Conviction:** ⭐ · **Size:** M · **Disposition:** P2
- **What:** Walk ancestors/descendants of a compartment and run the same query with per-compartment attribution.

### B4 — Entity topology (`get_entity_neighbors`)
- **Conviction:** 💡 · **Size:** L · **Disposition:** Rejected
- **What:** Return upstream/downstream entities (host→vm→db).
- **Why rejected:** Requires a graph substrate we don't have; out-of-scope for a log-analytics MCP.

### B5 — Event bookmarking / annotations
- **Conviction:** ⭐ · **Size:** M · **Disposition:** P2
- **What:** Attach notes to a (time, entity, query) tuple; retrievable later.

---

## C. Saved Content Lifecycle

### C1 — Saved-search versioning + diff
- **Conviction:** ⭐ · **Size:** M · **Disposition:** P2

### C2 — Ownership, tags, sharing scope on saved searches/dashboards
- **Conviction:** 🔥 · **Size:** M · **Disposition:** P1
- **What:** First-class owner, team, tag-list fields; filter lists by tag/team.

### C3 — Deprecation workflow
- **Conviction:** 💡 · **Size:** S · **Disposition:** P2

### C4 — Bulk import/export as YAML (GitOps)
- **Conviction:** ⭐ · **Size:** M · **Disposition:** P1
- **What:** Export dashboards/searches/alerts to a git repo; re-import on a new tenancy.

### C5 — `list_learned_queries` for admins
- **Conviction:** 💡 · **Size:** S · **Disposition:** P2

### C6 — `promote_now_dry_run`
- **Conviction:** ⭐ · **Size:** S · **Disposition:** P2

---

## D. Alerts & On-Call

### D1 — Alert grouping / dedup policy
- **Conviction:** 🔥 · **Size:** L · **Disposition:** P1
- **What:** Collapse alarms with the same fingerprint into one notification within a window.

### D2 — Alert suppression windows (maintenance)
- **Conviction:** 🔥 · **Size:** M · **Disposition:** P1
- **What:** Suppress matching alarms during a maintenance window with audit and auto-expiry.
- **Notes:** Originally P0 in the PM's first pass; moved to P1 during founder review to keep P0 tight. Should ship in the alerts batch of P1.

### D3 — Runbook linkage on alerts
- **Conviction:** 🔥 · **Size:** S · **Disposition:** P1
- **What:** Optional `runbook_url`/`runbook_markdown` field on `create_alert`; surface in notifications and `why_did_this_fire`.

### D4 — Alert noise report
- **Conviction:** ⭐ · **Size:** M · **Disposition:** P2

### D5 — Alert-to-saved-search linkage
- **Conviction:** ⭐ · **Size:** S · **Disposition:** P2

### D6 — Multi-threshold alarms (warn/crit)
- **Conviction:** ⭐ · **Size:** M · **Disposition:** P2

### D7 — Burn-rate SLO alerts
- **Conviction:** ⭐ · **Size:** L · **Disposition:** Rejected
- **Why rejected:** SLOs are a full product category. Either integrate with a dedicated SLO product or don't play — half-building erodes trust.

### D8 — Anomaly alert (no threshold)
- **Conviction:** ⭐ · **Size:** L · **Disposition:** Rejected
- **Why rejected:** Depends on F1 maturity. Revisit after F1 lands.

---

## E. Dashboards

> **Disposition context:** Dashboards are a secondary surface for an agent-first product. Ravi (on-call SRE) doesn't open dashboards at 2am; the agent queries directly. Dashboard features are deprioritized across the board.

### E1 — Dashboard variables / template params
- **Conviction:** 🔥 · **Size:** M · **Disposition:** P2
- **What:** Dropdown variables at the top of a dashboard (e.g., env=prod→qa) that flip all tiles.
- **Notes:** Distinct from log groups/compartments/tags (which partition *data*). Variables are *runtime visualization filters*. For an agent-first product, the agent can pass params to queries directly, so this is less critical.

### E2 — Tile drill-down config
- **Conviction:** ⭐ · **Size:** M · **Disposition:** P2

### E3 — Dashboard templating library
- **Conviction:** ⭐ · **Size:** M · **Disposition:** P2

### E4 — Dashboard health check
- **Conviction:** 💡 · **Size:** S · **Disposition:** Rejected
- **Why rejected:** Tiny value; reinforces wrong persona focus.

### E5 — Export-to-PNG/PDF
- **Conviction:** ⭐ · **Size:** M · **Disposition:** P2
- **Notes:** Report-delivery (see "Report Delivery" below) covers the PDF generation need for investigation reports — dashboard PNG/PDF is a different surface.

### E6 — Clone/fork dashboard
- **Conviction:** 💡 · **Size:** S · **Disposition:** Rejected
- **Why rejected:** OCI console does this; don't re-skin.

---

## F. Anomaly Detection / ML

### F1 — `log_pattern_cluster`
- **Conviction:** 🔥 · **Size:** L · **Disposition:** P1
- **What:** Group similar log messages into patterns (Splunk-style "Patterns" tab).
- **Notes:** Different from OCI **labels** (curated rule-based tags — answers "find things I already know to look for"). F1 is automatic text-similarity clustering ("I don't know what patterns exist — show me"). Logan has a `cluster` SQL command; F1 is an MCP tool wrapper with good defaults for agents.

### F2 — `forecast_metric`
- **Conviction:** ⭐ · **Size:** L · **Disposition:** Rejected
- **Why rejected:** Forecasting is a dedicated product category; don't build inside a Logan MCP.

### F3 — `detect_anomalies` window scan
- **Conviction:** ⭐ · **Size:** L · **Disposition:** P2

### F4 — First-seen / last-seen tracker
- **Conviction:** ⭐ · **Size:** M · **Disposition:** P2

### F5 — Persisted rarity baselines per source
- **Conviction:** 💡 · **Size:** L · **Disposition:** Rejected
- **Why rejected:** Large stateful addition; revisit only if F1 clustering proves load-bearing.

---

## G. Data Governance

### G1 — PII redaction policy authoring
- **Conviction:** 🔥 · **Size:** L · **Disposition:** P0-stretch
- **What:** Server-side regex + field-level masking rules applied to all outbound result sets.
- **Scope decisions (founder):**
  - **Off by default.** Most real workloads need to see user emails, failed-login usernames, etc.
  - **Config-driven / env-file based.** Operator opts in and declares which patterns/fields to redact.
  - **Applied to all outbound surfaces:** query results, export, Telegram/email reports, Slack, PDF.
  - No mandatory enforcement; this is a tool for regulated deployments to enable when needed.

### G2 — Retention / tier visibility
- **Conviction:** ⭐ · **Size:** S · **Disposition:** P2

### G3 — Parser authoring / test tool
- **Conviction:** ⭐ · **Size:** L · **Disposition:** Rejected
- **Why rejected:** Logan console already does this — don't re-skin.

### G4 — Field extraction / enrichment rule authoring
- **Conviction:** ⭐ · **Size:** L · **Disposition:** Rejected
- **Why rejected:** Same as G3.

### G5 — Sensitivity tagging on fields
- **Conviction:** 💡 · **Size:** M · **Disposition:** Rejected
- **Why rejected:** Covered indirectly by G1; standalone it's a governance product, not a tool.

---

## H. Performance & Cost

### H1 — `explain_query` (cost + ETA estimation)
- **Conviction:** 🔥 · **Size:** M · **Disposition:** P0-core
- **What:** Before running a query, return estimated bytes scanned, estimated cost, **and estimated runtime**.
- **Scope decisions (founder):**
  - Must include ETA, not just cost. Users reject queries that would take hours regardless of price.
  - If ETA exceeds a configurable threshold (e.g., 60s), agent should prompt the user before proceeding.
- **Why it matters:** Without this, an LLM agent can accidentally run a 30-day full-tenant scan. Admins will not approve rollout without it.

### H2 — Slow-query / expensive-query report
- **Conviction:** ⭐ · **Size:** M · **Disposition:** P2

### H3 — Query timeout + partial results
- **Conviction:** ⭐ · **Size:** S · **Disposition:** P2

### H4 — Auto-narrow time range when result limit hit
- **Conviction:** ⭐ · **Size:** M · **Disposition:** P2

### H5 — Query result cache TTL / invalidate surface
- **Conviction:** 💡 · **Size:** S · **Disposition:** P2

---

## I. Collaboration / Incident Management

### I1 — `create_incident_timeline`
- **Conviction:** 🔥 · **Size:** L · **Disposition:** P1
- **What:** Collect queries + annotations tied to an incident-id into an exportable timeline (Markdown + JSON).
- **Notes:** Natural extension of A1 + N1 + N3. A1 already produces structured output; I1 aggregates across multiple A1 runs.

### I2 — Handoff notes
- **Conviction:** ⭐ · **Size:** S · **Disposition:** P2

### I3 — Share query link with context
- **Conviction:** ⭐ · **Size:** S · **Disposition:** P2

### I4 — Comments on saved searches / dashboards
- **Conviction:** 💡 · **Size:** M · **Disposition:** Rejected
- **Why rejected:** Slack exists; don't build a collab product.

---

## J. Admin / Ops

### J1 — Ingestion-health tool (with stoppage detection)
- **Conviction:** 🔥 · **Size:** M · **Disposition:** P0-core
- **What:** For each log source: last-seen timestamp, hourly volume last 24h, parser-failure count, baseline comparison, stoppage alerts.
- **Scope decisions (founder):** The core value is **detecting stoppages and drops**, not just describing flow.
  - Compute baseline hourly volume per source.
  - Flag sources where last log is >Nx older than typical gap → STOPPED.
  - Flag sources where last-hour volume is <Y% of baseline → DROP.
  - Output: ranked list with severity, plus ingestion lag and parser-failure stats.

### J2 — Parser failure triage
- **Conviction:** 🔥 · **Size:** M · **Disposition:** P0-core
- **What:** List recent parse failures with sample raw lines, ranked by volume.
- **Notes:** Same ingestion plumbing as J1; ship together.

### J3 — Source onboarding wizard
- **Conviction:** ⭐ · **Size:** L · **Disposition:** P2

### J4 — Quota / limits visibility
- **Conviction:** ⭐ · **Size:** S · **Disposition:** P2

### J5 — Log-group topology tree
- **Conviction:** 💡 · **Size:** S · **Disposition:** P2

---

## K. Query UX

### K1 — Autosuggest fields for a source
- **Conviction:** 🔥 · **Size:** S · **Disposition:** P1
- **What:** `suggest_fields(source, partial="ht")` returns most-used fields (from learning pipeline) + schema.

### K2 — Query syntax fix-hints on validate
- **Conviction:** 🔥 · **Size:** M · **Disposition:** P1
- **What:** `validate_query` returns proposed corrections ("did you mean `Log Source = 'X'`?"), not bare errors.

### K3 — NL-to-query persistent feedback loop
- **Conviction:** 🔥 · **Size:** L · **Disposition:** P1
- **What:** Capture prompt → generated query → user acceptance/refinement; train the prompt catalog over time.
- **Notes:** Compounds with the existing learning pipeline. Requires telemetry from N2 to land first.

### K4 — Query explain plan
- **Conviction:** ⭐ · **Size:** M · **Disposition:** P2

### K5 — Query linter
- **Conviction:** ⭐ · **Size:** S · **Disposition:** P2

### K6 — Query snippet library at source-scope
- **Conviction:** 💡 · **Size:** S · **Disposition:** P2

---

## L. RBAC / Security

### L1 — Read-only mode flag (`--read-only`)
- **Conviction:** 🔥 · **Size:** S · **Disposition:** P0-core
- **What:** A single binary-level startup flag that disables every mutating tool.
- **Disabled tools:** create/update/delete for alerts, dashboards, saved searches, learned queries, preferences; send_to_slack, send_to_telegram, export, redaction-rule mutations, etc.
- **Still works:** run_query, list_*, get_*, validate_query, visualize, all read paths.
- **Why it matters:** Parallel deployments (full-access for admins, read-only for everyone else), safe agent experimentation, enterprise/audit adoption, dogfooding without risk.

### L2 — Scoped tokens / per-user compartment allowlist
- **Conviction:** ⭐ · **Size:** M · **Disposition:** P2

### L3 — Per-tool allowlist per user
- **Conviction:** ⭐ · **Size:** S · **Disposition:** P2

### L4 — Audit log rotation + shipping
- **Conviction:** ⭐ · **Size:** S · **Disposition:** P2

### L5 — Audit-log query tool
- **Conviction:** ⭐ · **Size:** S · **Disposition:** P2

### L6 — Confirmation secret MFA via OCI Vault
- **Conviction:** 💡 · **Size:** L · **Disposition:** Rejected
- **Why rejected:** Existing hashed-secret + resource-bound-token scheme is strong. Vault integration is a sales-led add-on, not a feature.

---

## M. Integrations

> **Disposition context:** All integrations beyond the existing Telegram tool are deferred. Phase 2 adds **Report Delivery** (below) which extends Telegram + OCI Notifications email for investigation reports. Other integrations (PagerDuty, Jira, ServiceNow, etc.) will be revisited later.

### M1 — PagerDuty / Opsgenie outputs
- **Conviction:** 🔥 · **Size:** M · **Disposition:** P2

### M2 — Jira / ServiceNow ticket creation
- **Conviction:** ⭐ · **Size:** M · **Disposition:** P2

### M3 — GitHub Issues output
- **Conviction:** 💡 · **Size:** S · **Disposition:** Rejected
- **Why rejected:** Generic webhook (M6) covers this — but M6 is also P2.

### M4 — OCI Streaming publisher
- **Conviction:** 💡 · **Size:** M · **Disposition:** Rejected
- **Why rejected:** Unclear job-to-be-done; cut until a customer asks by name.

### M5 — Object Storage export
- **Conviction:** ⭐ · **Size:** S · **Disposition:** P2

### M6 — Generic webhook
- **Conviction:** ⭐ · **Size:** S · **Disposition:** P2

### M-new — Report Delivery (PDF via Telegram + email via ONS)
- **Conviction:** 🔥 · **Size:** M · **Disposition:** P0-core
- **What:** Accepts a structured report (e.g., A1 output or N3 output), generates a PDF (and/or Markdown), delivers via:
  - Telegram (existing tool — attach file).
  - Email via OCI Notifications (ONS) — same channel already used by alarms.
- **Scope decisions (founder):** This is the integration the user actually wants right now. Other integrations wait.
- **Triggers:** on-demand from the agent, or scheduled (weekly/monthly summaries).
- **Bundles naturally with A1 and N3**: A1 investigates → N3 formats the report → Report Delivery ships it.

---

## N. Agentic Workflows

### N1 — Multi-step investigation recorder
- **Conviction:** 🔥 · **Size:** M · **Disposition:** P0-core
- **What:** Every tool call in a session is captured and replayable as a "playbook." Ad-hoc investigation becomes a reusable runbook.

### N2 — Suggested next query
- **Conviction:** 🔥 · **Size:** M · **Disposition:** P0-core
- **What:** After every `run_query`, return `next_steps: [...]` — pivot suggestions based on result shape (saw errors → group by status; saw spike → break down by entity).
- **Why it matters:** Cheapest, highest-leverage change to make agents smarter. PM promoted from dark-horse to P0.

### N3 — Auto-generate incident report
- **Conviction:** ⭐ · **Size:** M · **Disposition:** P0-core
- **What:** End-of-investigation synthesis: queries run + findings → Markdown (and/or PDF via Report Delivery) ready for Slack/Telegram/email.
- **Bundles with:** A1 (input) + Report Delivery (output).

### N4 — Session-scoped variables
- **Conviction:** ⭐ · **Size:** S · **Disposition:** P2
- **What:** `set_variable`/`get_variable` lets the agent stash a value across tool calls.
- **Founder decision:** convenience optimization, not must-have. Agents can pass args explicitly. Deprioritized.

### N5 — Query budget enforcement per session
- **Conviction:** ⭐ · **Size:** M · **Disposition:** P0-core
- **What:** Max N queries or M bytes scanned per session — prevents runaway agent loops.
- **Bundles with H1:** H1 tells you what a query will cost; N5 enforces the budget.

### N6 — Chain-of-tool transcript export
- **Conviction:** 💡 · **Size:** S · **Disposition:** P0-core
- **What:** Export tool call + response chain for a session as JSONL — audit and training data.
- **Founder decision:** Promoted from nice-to-have to P0; cheap to build, useful for both auditing and NL-to-query training data (feeds K3 in P1).

---

## Appendix: Disposition summary

### P0-core (14 features — committed for next phase)
A1, A2, A4, A6, H1, N1, N2, N3, N5, N6, L1, J1, J2, Report Delivery

### P0-stretch (4 features — slip to P1 if time runs short)
A3, A5, A7, G1

### P1 (ship after P0)
C2, C4, D1, D2, D3, F1, I1, K1, K2, K3

### P2 (backlog)
A-none, B3, B5, C1, C3, C5, C6, D4, D5, D6, E1, E2, E3, E5, F3, F4, G2, H2, H3, H4, H5, I2, I3, J3, J4, J5, K4, K5, K6, L2, L3, L4, L5, M1, M2, M5, M6, N4

### Rejected
B1, B2, B4 (metrics handled by separate MCP), D7, D8, E4, E6, F2, F5, G3, G4, G5, I4, L6, M3, M4
