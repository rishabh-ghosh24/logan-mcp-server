# Logan MCP Demo Guide

Use this guide to demonstrate the Logan MCP server feature by feature, then close with a simple end-to-end incident story:

`investigate_incident` -> `generate_incident_report` -> `deliver_report`

Live data note: the demo story below was selected from `emdemo-logan` on 2026-04-27 using the default compartment.

## Demo Goals

- Show that the server is connected to OCI Log Analytics.
- Show that the agent can discover available data before querying.
- Show query, triage, reporting, and delivery features in a natural operator workflow.
- Avoid accidental changes during the early walkthrough. Save mutating features for the end and call out the confirmation guardrails.

## Before You Start

1. Confirm the server is healthy.

   ```text
   test_connection()
   get_current_context()
   ```

   Talk track: "First we prove this is not a static demo. The MCP server is live, authenticated, and pointed at a real namespace and compartment."

2. Show current data volume.

   ```text
   get_log_summary(time_range="last_24_hours")
   ```

   Live result on 2026-04-27:

   - 24,847,025 logs in the last 24 hours.
   - 39 active sources with data.
   - Top sources include `OCI VCN Flow Unified Schema Logs`, `ExaWatcher Top Logs`, `Kubernetes Core DNS Logs`, `Exadata Metrics`, `Linux Audit Logs`, `Kubernetes Container Generic Logs`, and `Linux Syslog Logs`.

## Feature Walkthrough

### 1. Health and Context

Use:

```text
test_connection()
get_current_context()
```

Shows whether config, Identity API, Log Analytics API, and sample query execution work. This is the "can we even use the server?" checkpoint.

### 2. Data Discovery

Use:

```text
get_log_summary(time_range="last_24_hours")
list_log_sources()
```

Shows what log sources exist and which ones currently have data. This helps the agent avoid empty or expensive guesses.

### 3. Query Logs

Use:

```text
run_query(
  query="* | stats count as count by 'Log Source' | sort -count | head 10",
  time_range="last_24_hours",
  max_results=20
)
```

Shows that the server can run real Logan queries and return structured rows, columns, metadata, estimates, and next-step hints.

### 4. Parser Failure Triage

Use:

```text
run_query(
  query="'Parse Failed' = 1 | stats count as failure_count by 'Log Source' | sort -failure_count | head 10",
  time_range="last_24_hours",
  max_results=20
)
```

Live result on 2026-04-27:

| Source | Parse failures in last 24h |
|---|---:|
| Kubernetes Kubelet Logs | 12,811 |
| ExaWatcher Top Logs | 3,205 |
| ExaWatcher VMStat Logs | 906 |
| ExaWatcher Meminfo Logs | 54 |

Talk track: "Instead of searching randomly, we can quickly find which sources are having parsing or data-quality trouble."

### 5. Recent High-Signal Query

Use:

```text
run_query(
  query="'Parse Failed' = 1 | stats count as failure_count by 'Log Source' | sort -failure_count | head 10",
  time_range="last_1_hour",
  max_results=20
)
```

Live result on 2026-04-27:

| Source | Parse failures in last 1h |
|---|---:|
| Kubernetes Kubelet Logs | 532 |
| ExaWatcher VMStat Logs | 2 |

Talk track: "This gives us a focused live incident seed: parser failures are happening right now, mostly in Kubernetes Kubelet logs."

### 6. Sample Evidence

Use:

```text
run_query(
  query="'Log Source' = 'Kubernetes Kubelet Logs' AND 'Parse Failed' = 1 | fields Time, 'Log Source', 'Original Log Content' | sort -Time | head 5",
  time_range="last_24_hours",
  max_results=10
)
```

Live evidence showed repeated kubelet messages around `prometheus-server` in the `otel-demo` namespace, including `CrashLoopBackOff` and HTTP probe failures. This turns a parser/data-quality finding into an operational story.

### 7. Ingestion Health

Use:

```text
ingestion_health(severity_filter="all")
```

Live result on 2026-04-27:

- 3 sources healthy.
- 0 sources stopped.
- 1,029 sources unknown in the freshness probe window.

Talk track: "This answers the first on-call question: is ingestion broken globally, or is this isolated?"

### 8. One-Call Investigation

Use:

```text
investigate_incident(
  query="'Parse Failed' = 1",
  time_range="last_1_hour",
  top_k=2
)
```

Live result on 2026-04-27:

- Investigation completed with `partial=false`.
- J2 reported 539 parser failures.
- Top source: `Kubernetes Kubelet Logs`.
- Top entity: host `oke-cfqzhq4c4qa-ncpncm7ivua-ssyzrpi2qxa-2` with 368 matching events.
- Timeline showed repeated kubelet `RemoveContainer` and `Error syncing pod` events.
- The story points to `prometheus-server` in `otel-demo` repeatedly failing health checks and restarting.

Talk track: "This is the main value. One tool call does the tedious first 20 minutes: ingestion check, parser-failure triage, anomaly comparison, top clusters, top entities, and a timeline."

### 9. Incident Report

Use the full object returned by `investigate_incident`.

```text
generate_incident_report(
  investigation=<investigate_incident response>,
  format="markdown",
  summary_length="short"
)
```

Shows deterministic report generation without calling another LLM. The output can be reviewed before sending.

### 10. Report Delivery

Only run this step when the recipient targets are approved.

```text
deliver_report(
  report={
    "title": "Kubelet parser failures and prometheus CrashLoopBackOff",
    "markdown": "<markdown returned by generate_incident_report>"
  },
  channels=["telegram", "email"],
  format="both",
  recipients={
    "telegram_chat_id": "<approved chat id>",
    "email_topic_ocid": "<approved ONS topic OCID>"
  }
)
```

Talk track: "The same investigation can become a shareable report and land where operators already work. Delivery is mutating, so we only run it with approved recipients."

### 11. Visualization and Export

Use after any query result:

```text
visualize(...)
export_results(...)
```

Shows how to create charts or export CSV/JSON evidence for handoff.

### 12. Alerts, Dashboards, and Saved Searches

Use only after explaining guardrails:

```text
create_alert(...)
list_alerts()
create_dashboard(...)
list_dashboards()
create_saved_search(...)
list_saved_searches()
```

Talk track: "These features close the loop by turning investigation knowledge into future monitoring. They are guarded because they create or change OCI resources."

### 13. Playbooks and Learned Queries

Use:

```text
record_investigation(...)
list_playbooks()
save_learned_query(...)
```

Shows how a useful investigation path becomes reusable for the next incident.

## Recommended End-to-End Demo Story

Use this story:

> "We see parser failures in live Kubernetes Kubelet logs. The agent investigates them, discovers the dominant host and repeating kubelet messages, and produces a report showing a `prometheus-server` pod in `otel-demo` repeatedly failing health checks and restarting."

Why this is the best simple story from current data:

- It is live, not synthetic.
- The seed query is simple: `'Parse Failed' = 1`.
- It has enough volume to be convincing: 532 Kubelet parse failures in the last hour and 12,811 in the last 24 hours.
- It exercises multiple features in one pass: data discovery, query execution, parser triage, ingestion health, anomaly comparison, clustering, entity ranking, timeline, report generation, and delivery.
- It has a human-readable root narrative: Kubernetes kubelet logs show `prometheus-server` health-check failures and `CrashLoopBackOff`.

## Suggested Demo Script

1. "Let us first prove the server is live."
   Run `test_connection()` and `get_current_context()`.

2. "Now let us see what data exists."
   Run `get_log_summary(time_range="last_24_hours")`.

3. "The compartment has 24.8M logs and 39 active sources. Let us look for something operationally interesting."
   Run the 24-hour parse-failure query.

4. "Kubernetes Kubelet logs dominate parse failures. Let us check whether this is happening right now."
   Run the 1-hour parse-failure query.

5. "Now we hand the seed to the investigation tool."
   Run `investigate_incident(query="'Parse Failed' = 1", time_range="last_1_hour", top_k=2)`.

6. "The investigation found the dominant source, host, repeated event patterns, and a timeline."
   Point out `Kubernetes Kubelet Logs`, host `oke-cfqzhq4c4qa-ncpncm7ivua-ssyzrpi2qxa-2`, and `prometheus-server` / `CrashLoopBackOff`.

7. "Now we turn this into an incident report."
   Run `generate_incident_report(...)`.

8. "Finally, with approved recipients, we can deliver it to Telegram and email."
   Show the `deliver_report(...)` call. Execute only if approved.

## Fallback Story

If the parser-failure signal changes before the demo, use this discovery path:

```text
get_log_summary(time_range="last_24_hours")
run_query(query="* | stats count as count by 'Log Source' | sort -count | head 10", time_range="last_24_hours")
run_query(query="'Parse Failed' = 1 | stats count as failure_count by 'Log Source' | sort -failure_count | head 10", time_range="last_24_hours")
```

Pick the highest-volume source with a clear sample message, then run:

```text
investigate_incident(query="<chosen seed>", time_range="last_1_hour", top_k=2)
```

Avoid an unscoped `*` investigation during a live demo unless the goal is specifically to show broad anomaly detection; a scoped seed usually produces a cleaner story.
