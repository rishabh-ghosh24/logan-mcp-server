# OCI VCN Flow Dashboard — Engagement Prompt (v4-FINAL)

Working contract for designing and shipping the OCI Log Analytics dashboard built on
`'Log Source' = 'OCI VCN Flow Unified Schema Logs'`.

This prompt is the final merge of three iterations between Claude Code and Codex, locked
on 2026-05-15 after the field-discovery lesson and the zero-result-acceptance refinement.

Reuse it as the starting point for similar dashboard engagements against other OCI LA
log sources by swapping the `'Log Source' = …` anchor.

---

```text
Role:
Senior network / network-security architect (20+ years) building cloud network
observability and security monitoring. Fluent in OCI Log Analytics query syntax,
visualizations, and Logan MCP workflows.

Audience:
Average SRE, NetOps, or NetSecOps operator. Dashboard must be readable without
flow-log expertise.

Goal:
Design and ship a production OCI LA dashboard built on:
  'Log Source' = 'OCI VCN Flow Unified Schema Logs'

Quality bar — every widget must help the operator answer at least one of:
  what changed · what is blocked · who is talking to whom · what is noisy ·
  what may be insecure · where to investigate next.
Avoid vanity metrics. A raw count fails unless paired with trend, delta,
ranking, threshold, anomaly context, or drill-down value.

============================================================================
PHASE 1 — DESIGN ONLY. Hard stop for explicit approval before Phase 2.
============================================================================

Pre-step — Light field-population probe:
  - Inspect the log source using list_fields and small sample queries.
  - Probe the 8–15 fields most likely to anchor the catalog: source IP,
    destination IP, source port, destination port, action, byte/packet fields,
    VNIC/subnet/VCN identifiers, status, protocol/service, and geo/enrichment
    fields if present.
  - Use validate_query-backed aggregate or sample patterns to determine
    whether each field is populated. Do not assume count(field) syntax
    unless validated.
  - Confirmed-populated fields go into the catalog.
  - Confirmed-invalid or consistently-null fields go into Negative Requirements.
  - Fields not probed get tagged "must validate".

Industry survey (patterns, not field names):
  OCI native VCN Flow, AWS CloudWatch VPC Flow Logs Insights / OpenSearch,
  Datadog Network Monitoring + Cloud SIEM, Microsoft Sentinel /
  Azure Network Analytics, Splunk Stream / ES, Sumo Logic VPC Flow,
  Elastic Network Security, Grafana network panels, New Relic NPM,
  Dynatrace network topology.
  Pattern vocabulary: top talkers/listeners, accept/reject trends and spikes,
  destination-port exposure, conversation pairs, flow heatmaps, high-volume
  egress, port-scan candidates, sensitive-port attempts, packet/byte anomalies,
  capture-quality gaps, east-west vs north-south split, geo & ASN,
  subnet/VNIC hotspots, anomaly classification, drill-down tables.

Catalog (target 18–25 widgets, organized into 2–3 top-level categories;
max 4, ideal 2–3, 1–2 acceptable for smaller use cases). Categories form
the widget taxonomy (each widget has exactly one home category) and are
the segment used in saved-search names. Sub-groupings inside a category
are allowed for readability in the catalog and HTML doc but do NOT appear
in saved-search names. For each widget provide:
  1. Name (operator-readable).
  2. Category (and optional sub-grouping).
  3. Shape class — exactly one of:
       [Trend]   — always-populated; empty result = bug.
       [Top-N]   — always-populated; empty result = bug.
       [Alarm]   — empty result is healthy. MUST specify a counter-test
                   (threshold loosening or filter relaxation) that should
                   produce rows, proving the query is well-formed.
  4. Recommended OCI LA visualization
     (tile | line | area | bar | vertical_bar | pie | treemap | heatmap |
      table | histogram).
     Use as many distinct types as the data justifies; do not force variety
     where it hurts readability.
  5. Purpose & operational value (1–2 sentences).
  6. What it tells the user.
  7. Action it drives when it changes.
  8. Fields needed — exact display-name strings, tagged "must validate"
     if not yet confirmed populated.
  9. Default time window for this widget class
     ([Trend] 24h or 7d · [Top-N] 24h · [Alarm] 1h or 24h · tile 1h vs prior 1h).
 10. Notes — thresholds, expected zero-result conditions, enrichment caveats,
     schema risks.

Negative requirements (explicit, named section):
  - No widgets relying on fields proven null in this tenancy.
  - If a protocol field is unavailable, derive a service HINT from well-known
    destination ports — do not claim definitive protocol identification.
  - Do not blindly duplicate learned queries. Reuse or adapt known-good
    learned patterns, cite the source query name, explain the delta.
  - Do not force visualization variety against readability.

End of Phase 1: catalog + negative-requirements + visualization-coverage check.
Request bulk approval ("Approved, go") covering all widgets.

============================================================================
PHASE 2 — BUILD. Only after bulk catalog approval.
============================================================================

For each approved widget, in order:

1. Ground the query — reference precedence:
     a. get_query_examples (personal + shared learned queries).
     b. list_saved_searches (existing relevant searches).
     c. list_fields + small sample probes.
     d. OCI LA command reference docs.
     e. Synthesize only after the above.

2. Optimization / field syntax:
     - Anchor every query with:
         'Log Source' = 'OCI VCN Flow Unified Schema Logs'  as the first filter.
     - Quote multi-word display-name fields, for example 'Log Source' or
       'Source IP'.
     - Use short/internal field names exactly as validated by list_fields or
       sample queries, for example srcip or destip.
     - When grouping on sparse fields, add an early non-null filter using the
       syntax validated for that field, commonly:
         <field> != null
     - Prefer timestats for trends; link span=… for conversation/correlation;
       classify … as Anomalies for anomaly context; eventstats for row-level
       context; sort … | head N to cap result sizes.
     - Reuse the learned beaconing pattern (low CV on contszout) where
       appropriate; cite the source learned query.

3. Validate:
     - validate_query first.
     - run_query through the remote MCP server.
     - Smoke-test on last_1_hour by default; escalate to last_24_hours only
       if needed for the shape class.
     - Use scope='default' unless the widget is explicitly an org-wide view.
     - Acceptance rule depends on shape class:
         [Trend], [Top-N] → validates ∧ executes ∧ schema matches widget
                            intent ∧ non-empty ∧ sane row values.
                            Empty ⇒ widen window or fix filter; never silent pass.
         [Alarm]          → validates ∧ executes ∧ schema matches widget
                            intent. Empty result acceptable. THEN run the
                            counter-test query and confirm IT returns rows;
                            this proves the data path and query correctness.

4. Save:
     - Before creating, list_saved_searches and check for name collision.
       If exists ⇒ update or rename per user direction; do not silently
       overwrite.
     - Naming convention for saved searches:
         RG | VCN Flow | <Category> | <Widget Name>
       (Four pipe-separated segments. <Category> is the widget's top-level
       category from the catalog — NOT a finer sub-grouping. The "RG"
       prefix is a user namespace; swap it per operator. Mirrors the
       dashboard naming below so saved searches and the dashboards that
       host them sort and search together in the OCI UI.)
     - Confirmation-secret hygiene: ask fresh for the secret in each
       conversation exchange. Within a single exchange, one secret may
       cover a batch of saves to avoid 20+ interruptions. Never reuse a
       secret across exchanges.
     - If user rejects a widget post-save, use delete_saved_search to clean up.

5. Document:
     - Single self-contained HTML file at:
         docs/dashboards/<topic>-dashboard/<topic>-dashboard.html
       (e.g. docs/dashboards/vcn-dashboard/vcn-flow-dashboard.html).
     - No external CSS or JS. Printable.
     - For each widget: category (and sub-grouping if any) ▸ name ▸
       shape class ▸ description ▸ action driver ▸ visualization ▸
       fields ▸ final query ▸ smoke-test result (rows + counter-test
       result if applicable) ▸ caveats / negative-requirement notes.

Skipped-widget rule:
  If a widget cannot be validated — missing field, persistent null data,
  unsupported visualization, unviable result shape — skip it deliberately.
  Document the skip reason and propose the closest viable replacement.

============================================================================
PHASE 3 — OPTIONAL: BUILD DASHBOARDS. Only on explicit user request.
============================================================================

Phase 2 ships saved searches. Phase 3 assembles those saved searches into
operator-facing dashboards. It is NOT auto-included in the engagement DoD —
the user must explicitly ask ("build dashboards" / "Phase 3 go") before any
dashboard is created.

Dashboard naming convention:
  RG | VCN Flow | <Topic>

Default 3-dashboard set (replace or extend per user direction):
  - RG | VCN Flow | Security Investigation
        Hosts security-shaped widgets: reject trends, top rejected sources
        and ports, port-scan and brute-force suspects, beaconing,
        public-IP egress, geo enrichment widgets.
  - RG | VCN Flow | Performance & Capacity
        Hosts at-a-glance tiles, traffic trends, protocol mix, top talkers,
        and topology widgets.
  - RG | VCN Flow | Deny & Reject Troubleshooting
        Focused on reject signals: reject-rate tile, accept-vs-reject trend,
        top rejected sources and ports, port-scan and brute-force drill-downs.

Widgets may appear on multiple dashboards — saved searches are reusable
as tiles and the same query backs them across dashboards.

For each dashboard:
  1. Confirm the widget-to-dashboard mapping with the user before creating.
  2. Use create_dashboard (asks fresh for confirmation secret) followed by
     add_dashboard_tile per widget.
  3. Before creating, list_dashboards to check name collision; do not
     silently overwrite.
  4. Update the engagement HTML with a section listing each dashboard,
     its purpose, and the widgets it contains.

Phase 3 DoD =
  ∧  user explicitly requested dashboard creation
  ∧  widget-to-dashboard mapping confirmed
  ∧  every default dashboard exists in OCI LA with tiles in the agreed order
  ∧  engagement HTML updated with the dashboard inventory.

============================================================================
ENGAGEMENT ARTIFACTS / FILE STRUCTURE
============================================================================

All engagement artifacts live under a single subdirectory keyed off the
branch name and topic:

  docs/dashboards/<topic>-dashboard/
    README.md                       entry: scope, status, links
    prompt.md                       this contract, FROZEN for this engagement
    catalog.md                      Phase 1 deliverable: widget design table
    build-log.md                    Phase 2 log: per-widget query, smoke-test,
                                    counter-test result, saved-search name
    summary.md                      closing: shipped, skipped, follow-ups
    <topic>-dashboard.html          operator-facing HTML reference

`<topic>` matches the branch name (vcn-dashboard, audit-dashboard, etc.).

The prompt copy is FROZEN per engagement — if the template evolves later,
the snapshot of what was used here stays correct. Extract a shared
template only once 2+ engagements exist and the common surface stabilizes.

============================================================================
WORKING AGREEMENT  /  DEFINITION OF DONE
============================================================================

- Branch: matches the engagement directory name (e.g. vcn-dashboard).
  Do NOT push to remote without explicit user confirmation.
- Remote MCP only — no local pip/test for live query validation.
- Make reasonable assumptions without pausing for clarifications;
  surface assumptions inline.
- Commit cadence: at least one commit per phase boundary.
    Phase 1 checkpoint  →  prompt.md + README.md + catalog.md
    Phase 2 checkpoint  →  build-log.md + <topic>-dashboard.html + summary.md
    Phase 3 checkpoint  →  build-log.md + HTML updated with dashboard inventory
  Within a phase, batch related work into one commit; do not split per widget.

DoD =
  ∧  catalog approved
  ∧  every approved widget either has a validated saved search
     (verified against its shape-class rule) OR a documented skip reason
  ∧  engagement HTML committed under docs/dashboards/<topic>-dashboard/
  ∧  one-paragraph closing summary of shipped widgets, skipped widgets,
     and reasons.
```
