# Phase 2 — Product Roadmap

**Status:** Locked for execution
**Timeline:** One quarter (~12 weeks) from kickoff
**Team:** Solo developer (Rishabh) with Claude
**Companion docs:** [feature-catalog.md](feature-catalog.md) · [specs/](specs/)

---

## 1. Goal

Graduate `logan-mcp-server` from "a rich set of Logan query tools" into **"the default way an LLM agent investigates an OCI Log Analytics incident."** Phase 2 adds the orchestrator that stitches existing tools into a first-cut investigation, the guardrails that make agents trustworthy, the health checks that make the data trustworthy, and the report-delivery pipe that ships findings to where the user already is (Telegram + email).

---

## 2. Personas

Every P0 feature must serve at least two of these.

1. **Ravi — On-call SRE (primary).** Paged at 2am. Hands the agent an alarm and asks "what broke, where, since when." Cares about time-to-first-signal, confidence, low cognitive load. Abandons tools that hallucinate or are slow.
2. **Priya — Observability Platform Admin (buyer).** Runs Logan for the org. Cares about cost, query governance, ingestion health, safe rollout, multi-tenant trust. Without her, the tool never gets adopted org-wide.
3. **The Agent — LLM agents themselves.** We are an API for non-human callers. Must reward good agent behavior: cheap discovery, cheap validation, structured next-step hints, guardrails against runaway loops.

**Deprioritized this phase:** dashboard builders, SOC-primary-persona workflows (partially covered via stretch items).

---

## 3. Cross-cutting themes

The P0 tier is organized around four narratives. Every P0 feature ladders into one of these.

- **T1 — Triage Velocity.** Collapse "alert to root-cause hypothesis" from tens of minutes to under five. *(A1, A2, A4, A6 + stretch A3/A5/A7)*
- **T2 — Trustworthy Autonomous Agent.** Cost/ETA visibility, query budgets, read-only mode, next-step hints, audit trails. *(H1, N5, N2, L1, N6)*
- **T3 — Signal Quality at the Source.** Ingestion health and parser failure surfaces. The agent's answers are only as good as the data feeding it. *(J1, J2)*
- **T4 — Closing the Loop.** Investigation record → report → delivery. A1's output reaches the user via the channels they already live in. *(N1, N3, Report Delivery + stretch G1)*

---

## 4. P0 — Committed for Phase 2

**18 features total (14 core + 4 stretch).** Stretch items slip to P1 only if time runs short; they do not break the flagship narrative.

### P0 Core (14)

| Code | Feature | Theme | Size |
|---|---|---|---|
| **A1** | `investigate_incident` orchestrator | T1 | XL |
| **A2** | `diff_time_windows` | T1 | M |
| **A4** | `pivot_on_entity` | T1 | M |
| **A6** | `why_did_this_fire` | T1 | M |
| **H1** | `explain_query` (cost + ETA) | T2 | M |
| **N5** | Query budget per session | T2 | M |
| **N2** | Suggested next query | T2 | M |
| **L1** | `--read-only` mode flag | T2 | S |
| **N6** | Transcript export | T2 | S |
| **J1** | Ingestion-health (stoppage detection) | T3 | M |
| **J2** | Parser failure triage | T3 | M |
| **N1** | Investigation recorder (playbooks) | T4 | M |
| **N3** | Auto-generate incident report | T4 | M |
| **M-new** | Report Delivery (PDF via Telegram + email via ONS) | T4 | M |

### P0 Stretch (4 — slip to P1 if time runs short)

| Code | Feature | Theme | Size |
|---|---|---|---|
| **A3** | `find_rare_events` | T1 | M |
| **A5** | `trace_request_id` | T1 | M |
| **A7** | `related_dashboards_and_searches` | T1 | S |
| **G1** | PII redaction (optional, config-driven, off by default) | T4 | L |

---

## 5. Branch strategy

Three themed feature branches off `main`, plus this docs branch. One branch open at a time.

| Branch | Contents | Narrative |
|---|---|---|
| `docs/phase-2-roadmap` *(this branch)* | feature catalog + roadmap + specs | Documentation foundation |
| `feat/agent-guardrails` *(ship 1st)* | L1, H1, N5, N2, N6 | "Trust the agent" |
| `feat/triage-toolkit` *(ship 2nd)* | A1, A2, A4, A6, J1, J2 + stretch A3, A5, A7 | "Investigation flow" |
| `feat/reports-and-playbooks` *(ship 3rd)* | N1, N3, Report Delivery + stretch G1 | "Output + sharing" |

**Why this ordering:**
1. `feat/agent-guardrails` ships first because L1, H1, N5 are foundational — they protect everything built after and unblock safer dogfooding of later branches.
2. `feat/triage-toolkit` ships second. Primitives (A2, A4) build before A1; J1/J2 ship in parallel since they share ingestion plumbing.
3. `feat/reports-and-playbooks` ships last — consumes A1 output, so A1 must be merged first.

**Branches are delivery lanes, not batching buckets.** Each feature on a themed branch merges to `main` as its own small PR — the branch name is a narrative grouping for reviewers and for roadmap tracking, not a mega-PR that accumulates all of its features before merging. Rationale: review burden scales with PR size; small PRs keep review quality high, reduce conflict risk with parallel docs work, and keep `main` releasable at feature granularity. A themed branch is "done" when the last of its features has merged — not when a single squash lands.

---

## 6. Sequencing inside P0

Logical order, not calendar:

**Phase 2a — Guardrails (weeks 0–3)**
- **Week 0 quick wins:** L1 (day), N2 (add to existing tool responses — no new endpoints).
- **Weeks 1–2:** H1 + N5 designed and shipped together (same cost-accounting plumbing).
- **Week 3:** N6 transcript export (piggybacks on audit log).
- **Merge `feat/agent-guardrails` → main.**

**Phase 2b — Triage toolkit (weeks 3–8)**
- **Weeks 3–4 (parallel tracks):**
  - A2 (diff_time_windows) and A4 (pivot_on_entity) — independent primitives.
  - J1 + J2 — ingestion/parser plumbing, independent track.
- **Weeks 5–7:** A1 (investigate_incident) — consumes A2, A4, existing cluster/schema tools.
- **Week 8:** A6 (why_did_this_fire) — lightweight variant of A1 scoped to alarms.
- **Stretch (if time):** A3, A5, A7 — all wrappers around existing Logan capabilities, relatively cheap.
- **Merge `feat/triage-toolkit` → main.**

**Phase 2c — Reports & playbooks (weeks 8–12)**
- **Weeks 8–9:** N1 investigation recorder — reuses N6 transcript plumbing.
- **Weeks 9–10:** N3 incident report — consumes A1 output.
- **Weeks 10–11:** Report Delivery — PDF generation + Telegram attach + ONS email.
- **Stretch (if time):** G1 PII redaction (L-sized; realistic only if stretch window holds).
- **Merge `feat/reports-and-playbooks` → main.**

---

## 7. Dependencies

- **H1 + N5** — designed together. N5 enforces what H1 estimates.
- **A1** — depends on A2, A4, and existing `cluster`/`schema` tools. Don't start until A2+A4 merged.
- **A6** — depends on A1's primitives + existing alarm APIs.
- **N1** — reuses audit log and transcript plumbing from N6.
- **N3** — depends on A1's structured output shape.
- **Report Delivery** — depends on N3 (for report) and existing Telegram/ONS tools.
- **G1** — cross-cuts everything that outputs data. Ships last to avoid blocking all other features.

---

## 8. Success metrics

### Per P0 theme
- **T1 Triage Velocity:** median time from alarm-fire to first useful agent response ≤ 60s; A1 returns within 20s at p95.
- **T2 Trustworthy Agent:** 100% of `run_query` responses include `estimated_bytes`, `estimated_eta`, and `next_steps[]`; zero sessions exceed budget cap without hitting the guardrail; 0 "runaway agent" incidents in dogfood logs.
- **T3 Signal Quality:** J1 catches ≥ 1 real stoppage/drop before manual discovery in dogfood; parser failure rate from J2 surfaces used to resolve ≥ 2 parser bugs.
- **T4 Closing the loop:** ≥ 1 end-to-end flow demonstrated (alarm → A1 → N3 → PDF → Telegram + email) per month in dogfood.

### Phase-level leading indicators
- Tools called per session **rises** (agents do more).
- Bytes scanned per session **falls** (better primitives = less brute-force).
- Failed-query rate **falls** (H1 + N2 prevent dead-ends).
- Transcript-export (N6) count **rises** (auditing actually happens).

---

## 9. Non-goals for Phase 2

Explicit cuts to preserve focus:

- **Metrics & APM correlation (B1, B2)** — handled by the separate `oci-mon-mcp` server; cross-server correlation at the agent layer.
- **Dashboards as a first-class surface** — dashboards aren't the agent surface. E1, E2, E3, E5 pushed to P2; E4, E6 rejected.
- **SLO/burn-rate alerts (D7)** — SLOs are a full product category; don't half-build.
- **Forecasting (F2)** — dedicated ML product category; don't build inside a log MCP.
- **Parser/field-extraction authoring (G3, G4)** — Logan console already does this.
- **Broad integrations (M1, M2, M3, M4, M5, M6)** — Telegram + ONS email cover the current need; others wait.

Everything rejected is listed in [feature-catalog.md](feature-catalog.md#appendix-disposition-summary) with rationale.

---

## 10. Open questions (not blocking kickoff)

Answers would shape priorities mid-phase but don't gate P0 start:

1. **Regulated-tenant deal for G1?** If a compliance-regulated customer emerges mid-phase, G1 promotes from stretch → core and displaces one stretch item.
2. **Design-partner tenants?** Currently optimizing for Ravi (SRE) + Priya (admin). If a security/SOC team becomes the primary design partner, A3 and A5 should promote from stretch → core.
3. **Dogfooding cadence?** Internal-team dogfooding on a real Logan tenant is the single biggest quality lever for A1 and N2. Recommend scheduling a weekly dogfood session starting Week 1.

---

## 11. What we are *not* committing to

For clarity: features listed in P1/P2 in the feature catalog are **not** part of this phase. They are the backlog. They may pull forward if a compelling reason emerges, but they are not tracked as work for the next quarter.
