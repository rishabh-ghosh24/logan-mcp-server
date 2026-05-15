# VCN Flow Dashboard — Engagement Workspace

OCI Log Analytics dashboard built on **`'Log Source' = 'OCI VCN Flow Unified Schema Logs'`**, designed for SRE, NetOps, and NetSecOps operators.

## Status

| Phase | State | Output |
|---|---|---|
| Phase 1 — Design | ⏳ Awaiting bulk catalog approval | [catalog.md](catalog.md) |
| Phase 2 — Build queries + saved searches | Pending | `build-log.md`, `vcn-flow-dashboard.html` |
| Phase 3 — Build dashboards (optional) | Pending — explicit opt-in only | dashboards in OCI LA UI |

Branch: `vcn-dashboard`. Engagement contract: [prompt.md](prompt.md) (v4-FINAL, frozen).

## Files in this directory

| File | Purpose |
|---|---|
| [`prompt.md`](prompt.md) | The working contract — design rules, build rules, file layout, DoD. Frozen for this engagement. |
| [`catalog.md`](catalog.md) | Phase 1 deliverable — 24 widget designs organized into 2 categories, with shape class, viz, fields, default time window, counter-tests for alarms. |
| `build-log.md` | Phase 2 record (per widget): final query, validation outcome, smoke-test rows + counter-test result, saved-search name. |
| `summary.md` | Closing report — shipped vs skipped widgets, reasons, follow-ups. |
| `vcn-flow-dashboard.html` | Operator-facing HTML reference — self-contained, printable, no external CSS/JS. |

## Headline design choices

- **2 widget categories**: *Performance & Capacity* (14 widgets) and *Security & Investigation* (10 widgets).
- **3 dashboards** in Phase 3 (when invoked): *Performance & Capacity*, *Security Investigation*, *Deny & Reject Troubleshooting*. Dashboards remix widgets from both categories.
- **Saved-search naming**: `RG | VCN Flow | <Category> | <Widget Name>` — four pipe-separated segments.
- **Dashboard naming**: `RG | VCN Flow | <Topic>`.
- **Shape classes** (per widget): `[Trend]`, `[Top-N]`, or `[Alarm]`. `[Alarm]` widgets ship with a counter-test query so a zero-row result can be proven healthy rather than buggy.
- **All 10 OCI LA viz types used** across the 24 widgets.

## How to reuse this for a different log source

1. Copy [`prompt.md`](prompt.md) into a new `docs/dashboards/<new-topic>-dashboard/` directory.
2. Swap the `'Log Source' = …` anchor and the dashboard/saved-search prefix.
3. Run Phase 1 from scratch — including the field-population probe — against the new source.

When a 2nd dashboard exists, consider extracting a shared template at `docs/dashboards/prompt-template.md` and having per-engagement copies inherit from it.
