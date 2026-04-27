# Logan MCP Natural-Language Demo Guide

Use this guide to demonstrate Logan MCP as an operator-facing assistant, not as a raw tool API. The person driving the demo should type the prompts below into Codex chat while `emdemo-logan` is attached. Codex should choose the MCP tools and show the results.

End-to-end demo path:

`investigate_incident` -> `generate_incident_report` -> `deliver_report`

Live data note: the recommended story was selected from `emdemo-logan` on 2026-04-27 using the default compartment. Counts change over time, so treat the numbers as expected shape, not exact constants.

## Demo Rules

- Type the "What you type" prompt into Codex.
- Do not type raw MCP function calls during the main demo unless you are intentionally showing the lower-level API.
- Let Codex run the MCP tools and summarize the output.
- Use the default compartment unless the audience asks for tenancy-wide scope.
- Do not run delivery, alert, dashboard, saved-search, or log-source creation steps unless the recipient/resource targets are approved.

## Before You Start

### 1. Confirm The Server Is Healthy

What you type:

```text
Confirm the emdemo-logan server is healthy.
```

Expected behind the scenes:

```text
test_connection()
```

Expected result:

- Status should say `All systems operational`.
- Configuration should be `OK`.
- Identity API should return compartments.
- Log Analytics API should return log sources.
- Query execution should succeed with a sample query.
- The result already includes namespace, compartment, and tenancy context.

Presenter note:

`get_current_context()` is optional here. You only need it if you want to show the context separately. `test_connection()` already proves the server is connected and includes the active context, so the health demo should usually be one prompt.

Talk track:

"First we prove this is live. The MCP server is authenticated, can reach OCI Identity and Log Analytics, and can run a real query."

### 2. Show Current Data Volume

What you type:

```text
Show current data volume for the default compartment over the last 24 hours.
```

Expected behind the scenes:

```text
get_log_summary(time_range="last_24_hours", scope="default")
```

Expected result:

- Total logs should be around 24 to 25 million in the last 24 hours.
- Active sources should be around 38 to 39.
- Top sources should include high-volume sources like:
  - `OCI VCN Flow Unified Schema Logs`
  - `ExaWatcher Top Logs`
  - `Kubernetes Core DNS Logs`
  - `Exadata Metrics`
  - `Linux Audit Logs`
  - `Kubernetes Container Generic Logs`
  - `Linux Syslog Logs`

Validated example from 2026-04-27:

| Metric | Value |
|---|---:|
| Total logs, last 24h | 24,787,447 |
| Sources with data | 38 |

Talk track:

"This is why the assistant needs discovery first. There is a lot of data, so we want the agent to identify useful sources before running broad investigations."

## Feature Walkthrough

### 1. Data Discovery

What you type:

```text
What log data do we have in this compartment over the last 24 hours? Summarize the active sources.
```

Expected behind the scenes:

```text
get_log_summary(time_range="last_24_hours")
```

Expected result:

- A short summary of total log volume.
- A count of active log sources.
- A ranked list of top sources by volume.
- A recommendation to filter by `Log Source` for better performance.

Why this matters:

It shows that the agent starts by understanding available data instead of guessing queries.

### 2. Query Logs

What you type:

```text
Show me the top 10 log sources by volume in the last 24 hours.
```

Expected behind the scenes:

```text
run_query(
  query="* | stats count as count by 'Log Source' | sort -count | head 10",
  time_range="last_24_hours",
  max_results=20
)
```

Expected result:

- A table with `Log Source` and `count`.
- `OCI VCN Flow Unified Schema Logs` should usually be near the top.
- The response should include query metadata and may include estimate fields.

Talk track:

"This is the basic query path. The result is structured, so the assistant can reason over it instead of just dumping text."

### 3. Parser Failure Triage

What you type:

```text
Which log sources have parser failures in the last 24 hours? Show the top offenders.
```

Expected behind the scenes:

```text
run_query(
  query="'Parse Failed' = 1 | stats count as failure_count by 'Log Source' | sort -failure_count | head 10",
  time_range="last_24_hours",
  max_results=20
)
```

Expected result:

- A table of sources with parse failures.
- For the current demo data, expect `Kubernetes Kubelet Logs` to be the strongest signal.

Validated example from 2026-04-27:

| Source | Parse failures in last 24h |
|---|---:|
| Kubernetes Kubelet Logs | 12,811 |
| ExaWatcher Top Logs | 3,205 |
| ExaWatcher VMStat Logs | 906 |
| ExaWatcher Meminfo Logs | 54 |

Talk track:

"Now we have an operationally interesting lead: parser failures are concentrated in a small set of sources."

### 4. Recent Parser Failure Check

What you type:

```text
Are parser failures still happening right now? Check the last hour.
```

Expected behind the scenes:

```text
run_query(
  query="'Parse Failed' = 1 | stats count as failure_count by 'Log Source' | sort -failure_count | head 10",
  time_range="last_1_hour",
  max_results=20
)
```

Expected result:

- A smaller table focused on the last hour.
- For the current demo data, expect `Kubernetes Kubelet Logs` to dominate.

Validated example from 2026-04-27:

| Source | Parse failures in last 1h |
|---|---:|
| Kubernetes Kubelet Logs | 532 |
| ExaWatcher VMStat Logs | 2 |

Talk track:

"This confirms the issue is active, so it is a good candidate for the investigation demo."

### 5. Sample Evidence

What you type:

```text
Show me a few recent raw examples from Kubernetes Kubelet Logs where parsing failed.
```

Expected behind the scenes:

```text
run_query(
  query="'Log Source' = 'Kubernetes Kubelet Logs' AND 'Parse Failed' = 1 | fields Time, 'Log Source', 'Original Log Content' | sort -Time | head 5",
  time_range="last_24_hours",
  max_results=10
)
```

Expected result:

- Recent raw kubelet log lines.
- For the current demo data, expect messages involving:
  - `prometheus-server`
  - `otel-demo`
  - `CrashLoopBackOff`
  - HTTP readiness probe failures, such as status `503`

Talk track:

"This turns the numeric signal into a human-readable story. We can now see that the parser failures are connected to real kubelet events around a restarting Prometheus pod."

### 6. Ingestion Health

What you type:

```text
Is log ingestion healthy right now, or is this a broader ingestion problem?
```

Expected behind the scenes:

```text
ingestion_health(severity_filter="all")
```

Expected result:

- A summary of healthy, stopped, and unknown sources.
- In the current demo data, expect no stopped sources and many unknown sources because the freshness probe checks many configured sources, not only sources active in the last 24 hours.

Validated example from 2026-04-27:

| Status | Count |
|---|---:|
| Healthy | 3 |
| Stopped | 0 |
| Unknown | 1,029 |

Talk track:

"This answers the first on-call question: is ingestion globally broken, or are we looking at a narrower data-quality and workload issue?"

### 7. Investigation Mode

Switch to investigation mode for this step, or explicitly tell Codex to investigate rather than only query.

What you type:

```text
Use investigation mode. Investigate parser failures over the last hour. Focus on the top two anomalous sources and explain the likely story.
```

Expected behind the scenes:

```text
investigate_incident(
  query="'Parse Failed' = 1",
  time_range="last_1_hour",
  top_k=2
)
```

Expected result:

- A structured investigation, not just a raw query result.
- `partial` should be `false` for the clean demo path.
- J2 parser-failure count should be around the current one-hour total.
- Top source should be `Kubernetes Kubelet Logs`.
- Top entity should include host `oke-cfqzhq4c4qa-ncpncm7ivua-ssyzrpi2qxa-2` if the same data pattern is still present.
- Timeline should show repeated kubelet events such as `RemoveContainer`, `Error syncing pod`, and `CrashLoopBackOff`.
- The likely story should mention `prometheus-server` in `otel-demo` repeatedly failing health checks and restarting.

Validated example from 2026-04-27:

- Investigation completed with `partial=false`.
- J2 reported 539 parser failures.
- Top source: `Kubernetes Kubelet Logs`.
- Top entity: host `oke-cfqzhq4c4qa-ncpncm7ivua-ssyzrpi2qxa-2` with 368 matching events.
- Timeline showed repeated kubelet `RemoveContainer` and `Error syncing pod` events.

Talk track:

"This is the main value. One prompt turns discovery, parser triage, anomaly comparison, clustering, entity ranking, and timeline construction into a first-cut investigation."

### 8. Incident Report

What you type:

```text
Generate a short incident report from that investigation in Markdown. Include executive summary, timeline, findings, evidence, and next steps.
```

Expected behind the scenes:

```text
generate_incident_report(
  investigation=<investigate_incident response>,
  format="markdown",
  summary_length="short"
)
```

Expected result:

- A Markdown report titled `Incident Report`.
- Executive summary mentioning Kubernetes kubelet parser failures and `prometheus-server`.
- Timeline entries with real timestamps.
- Top findings with:
  - `Kubernetes Kubelet Logs`
  - cluster patterns like readiness probe failures
  - entity values like `host=<host name>`
- Evidence showing the seed query, time range, elapsed seconds, and budget snapshot.

Presenter check:

If the report says `unknown time` or `entity=unknown` for A1 output, the server is stale. The current deployed version renders A1 fields correctly.

Talk track:

"The report is deterministic and reviewable. It does not need another LLM call to invent prose."

### 9. Report Delivery

Do not execute this step unless the recipient targets are approved.

What you type:

```text
Deliver this report to the approved Telegram chat and email topic in both PDF and Markdown format.
```

Expected behind the scenes:

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

Expected result:

- Delivery status per channel.
- PDF path or delivery artifact metadata when PDF is generated.
- Telegram receives the PDF when configured.
- Email/ONS receives the Markdown summary when configured.

Talk track:

"This closes the loop. The agent can move from live investigation to a shareable operator report."

### 10. Visualization And Export

What you type:

```text
Create a simple chart of the parser failures by log source, and export the result as CSV.
```

Expected behind the scenes:

```text
visualize(...)
export_results(...)
```

Expected result:

- A simple chart, usually bar or table, showing parser failures by source.
- A CSV or JSON export path for evidence handoff.

Talk track:

"This is useful when the operator needs to paste evidence into a ticket or share a visual summary."

### 11. Alerts, Dashboards, And Saved Searches

Do not execute create/update/delete operations unless the target resources are approved.

What you type for a safe listing demo:

```text
Show existing Logan-managed alerts and available dashboards or saved searches related to Kubernetes Kubelet Logs.
```

Expected behind the scenes:

```text
list_alerts()
list_dashboards()
list_saved_searches()
related_dashboards_and_searches(source="Kubernetes Kubelet Logs")
```

Expected result:

- Existing alerts, dashboards, saved searches, or related learned queries if present.
- No resource mutation for the safe listing path.

Optional mutating prompt, only with approval:

```text
Create an alert for recurring Kubernetes Kubelet parser failures and send notifications to the approved ONS topic.
```

Expected result for mutating path:

- The server should require confirmation before creating OCI resources.

Talk track:

"Read operations are safe during the demo. Create, update, and delete operations are guarded because they change OCI resources."

### 12. Playbooks And Learned Queries

What you type:

```text
Save this investigation flow as a playbook so we can reuse it later.
```

Expected behind the scenes:

```text
record_investigation(...)
list_playbooks()
```

Expected result:

- A named playbook or listed playbook entry, if recording is enabled for the session.
- A reusable trail of the investigation steps.

Talk track:

"The assistant can preserve useful operator workflows instead of making the next person rediscover the same sequence."

## Recommended End-To-End Demo Story

Use this story:

> We see parser failures in live Kubernetes Kubelet logs. The assistant investigates them, discovers the dominant host and repeated kubelet messages, then produces a report showing a `prometheus-server` pod in `otel-demo` repeatedly failing health checks and restarting.

Why this is the best simple story from current data:

- It is live, not synthetic.
- The first input is simple natural language: "Which sources have parser failures?"
- It has enough volume to be convincing.
- It exercises data discovery, query execution, parser triage, ingestion health, investigation mode, clustering, entity ranking, timeline generation, report generation, and optional delivery.
- It has a human-readable narrative: Kubernetes kubelet logs show `prometheus-server` health-check failures and `CrashLoopBackOff`.

## Short Presenter Script

1. Type:

   ```text
   Confirm the emdemo-logan server is healthy.
   ```

   Expect: `All systems operational`.

2. Type:

   ```text
   Show current data volume for the default compartment over the last 24 hours.
   ```

   Expect: around 24 to 25 million logs and around 38 to 39 active sources.

3. Type:

   ```text
   Which log sources have parser failures in the last 24 hours? Show the top offenders.
   ```

   Expect: `Kubernetes Kubelet Logs` near the top.

4. Type:

   ```text
   Are parser failures still happening right now? Check the last hour.
   ```

   Expect: `Kubernetes Kubelet Logs` remains the main active source.

5. Type:

   ```text
   Show me a few recent raw examples from Kubernetes Kubelet Logs where parsing failed.
   ```

   Expect: kubelet messages mentioning `prometheus-server`, `otel-demo`, `CrashLoopBackOff`, or readiness probe failures.

6. Type:

   ```text
   Use investigation mode. Investigate parser failures over the last hour. Focus on the top two anomalous sources and explain the likely story.
   ```

   Expect: a structured investigation with top sources, entities, clusters, and timeline.

7. Type:

   ```text
   Generate a short incident report from that investigation in Markdown. Include executive summary, timeline, findings, evidence, and next steps.
   ```

   Expect: a clean Markdown incident report with real timestamps and host entity values.

8. Optional, only after recipient approval:

   ```text
   Deliver this report to the approved Telegram chat and email topic in both PDF and Markdown format.
   ```

   Expect: per-channel delivery status.

## Fallback Story

If the parser-failure signal changes before the demo, type:

```text
Find a good live incident story in the default compartment. Start with data volume, then look for concentrated errors or parser failures in the last 24 hours. Pick the cleanest story and explain why.
```

Expected behind the scenes:

```text
get_log_summary(time_range="last_24_hours")
run_query(query="* | stats count as count by 'Log Source' | sort -count | head 10", time_range="last_24_hours")
run_query(query="'Parse Failed' = 1 | stats count as failure_count by 'Log Source' | sort -failure_count | head 10", time_range="last_24_hours")
```

Then type:

```text
Use investigation mode on the best seed you found. Keep it scoped and explain the story in plain language.
```

Avoid an unscoped `*` investigation during a live demo unless the goal is specifically broad anomaly discovery. A scoped seed usually gives a cleaner story.
