# Monitoring metrics

[Back to the README](../README.md)

Logs and metrics use one server and the same OCI identity. Each service initializes
when needed. Metrics work without a Log Analytics namespace; a service-specific
failure leaves the other service usable. Common authentication failure affects
both. The two `monitoring://` help resources remain available without working
authentication.

For metrics only, create `~/.oci-logan-mcp/config.yaml` with your authentication
method and Monitoring defaults. This instance-principal example needs no Log
Analytics section:

```yaml
oci:
  auth_type: instance_principal
monitoring:
  default_compartment_id: <compartment OCID>
  region: <OCI region>
  max_window_hours: 24
  timeout_seconds: 60
  max_series: 20
  max_datapoints: 4000
  max_response_bytes: 262144
```

On a laptop, use `auth_type: config_file` and the OCI config/profile described in
Quick Start. For combined operation, retain your existing `log_analytics` section
and add `monitoring`. The setup wizard continues to configure logs; edit this
section directly for metrics.

Environment overrides use the prefix `OCI_LOGAN_MONITORING_` with suffixes
`COMPARTMENT`, `REGION`, `MAX_WINDOW_HOURS`, `TIMEOUT`, `MAX_SERIES`,
`MAX_DATAPOINTS` and `MAX_RESPONSE_BYTES`. Invalid Monitoring settings block metrics
with a safe setting-name error while logs remain usable.

The explicit compartment wins, followed by the user's remembered/session choice,
Monitoring default, then Log Analytics default. Region comes from the call,
Monitoring config, authenticated OCI config, then signer. Subtree is false by
default and requires a tenancy-root scope when the tenancy is known. `set_namespace`
changes only the Log Analytics namespace. No service silently changes region or
compartment after a failure.

### Monitoring permissions

Grant discovery with `inspect metrics` and data queries with `read metrics`.
`read metrics` includes inspection. For example, an instance dynamic group that
needs both can use:

```text
Allow dynamic-group <dynamic-group-name> to read metrics in compartment <compartment-name>
```

Use `group` instead of `dynamic-group` for an API-key user's group. These tools
need no Compute-instance read, alarm read or administrative permission. Existing
log and alarm tools retain their separate IAM requirements. See the official
[Monitoring policy reference](https://docs.oracle.com/en-us/iaas/Content/Identity/policyreference/monitoringpolicyreference.htm).

### Discovery and queries

Start with `test_connection({"service":"metrics"})`; inspection success does not
prove data-query permission. `get_current_context` reports configuration, created
clients and the scope of previous permission checks without probing OCI. Use
`test_connection({"service":"all"})` for separate logs and metrics diagnostics.
Omitting `service` preserves the existing logs check.

```text
list_metric_namespaces({"limit":50})
list_metrics({"namespace":"oci_computeagent"})
list_metrics({"namespace":"oci_computeagent","metric_name":"CpuUtilization"})
search_metric_catalog({"query":"cpu"})
query_catalog_metric({"metric_key":"cpu","time_range":"last_1_hour"})
query_metrics({"namespace":"oci_computeagent","query":"CpuUtilization[5m].mean()","time_range":"last_24_hours"})
```

Without `metric_name`, discovery returns OCI's exact metric names only. Answer
availability questions with a bullet list of those names. Do not invent readable
labels, append descriptions or show dimensions unless the user asks for them.
For a follow-up about one metric, supply `metric_name` to retrieve its detailed
streams. Instance-specific discovery preserves the supplied dimension filters.

Each call returns one native page and costs one session query slot. The page's
`returned_count` counts projected names, not the whole inventory. When
`more_available` is true, pass `next_page_token` with the same scope and filters;
merge duplicate names across pages. Check `budget_remaining.queries` before
continuing, stop when five or fewer slots remain, and report the list as
incomplete. Do the same if a timeout or error prevents completion. This reserve
is guidance for the assistant; it does not change the server's hard budget.

Catalog search is local, reports
ambiguity and never executes a query. Catalog queries require an exact reviewed
key. Raw MQL supports any authorized namespace, including custom metrics outside
the catalog. The catalog adapts reviewed data from Argus and OCI-Mon; source
commits, official URLs and withheld definitions ship in its manifest. Source
review does not establish live coverage in your tenancy.

Catalog filters refuse quotes, backslashes and control characters until their
escaped meaning is verified against a matching live stream. Literal `*` and `|`
remain supported with equality. Use expert raw MQL for explicit `=~` fuzzy matching
or other advanced expressions.

The query interval and resolution are different. Raw resolution defaults to the
largest supported value no greater than every operand interval; catalog
resolution follows its interval. An explicit resolution cannot exceed an operand
interval. The response states which value was used. The server allows at most
24 hours per call. OCI retention measured from now is 7 days at 1m, 30 days at 5m
and 90 days at 1h or 1d. See the [MQL reference](https://docs.oracle.com/en-us/iaas/Content/Monitoring/Reference/mql.htm).

Results label compact `[timestamp,value]` pairs with `point_columns`. Zeros stay
zero and gaps stay gaps. Per-expression facts appear once in `expressions`; each
stream has an `expression_index` and retains its full identity and units. Defaults
are 20 series and 4,000 points across the response, with hard maxima of 100 series
and 10,000 points. The configured byte cap also applies. A window that cannot fit
one whole series is rejected before OCI with compatible alternatives. Runtime
caps omit whole series and disclose counts and reasons. A capped subset cannot
establish fleet-wide worst-resource or top-N claims.

For 24 hours, a 5m interval returns about 289 points per stream. Choose explicit
1h aggregation and resolution when hourly fleet trends answer the question:

```text
query_catalog_metric({"metric_key":"cpu","interval":"1h","resolution":"1h","aggregation":"mean","time_range":"last_24_hours"})
```

State that hourly means can hide short spikes, and report interval and coverage.
Narrow dimensions before increasing caps. A smaller response can reduce tokens
per call, but extra queries to recover omitted data can increase total tokens.
The [Monitoring guide](../src/oci_logan_mcp/data/monitoring_syntax.md) and
[starter calls](../src/oci_logan_mcp/data/monitoring_templates.yaml) also explain load
balancer component levels and recovery.

Monitoring requests share the session query budget with logs. Timeouts keep their
charge because OCI work may still run. Session byte and cost estimates describe
estimated Log Analytics work; Monitoring responses explicitly say their service
cost is not estimated. MQL and dimension values enter audit as hashes and lengths
under both retention modes. MQL never enters the Log Analytics learner or query
history. Authorized dimensions still appear in tool results.

Correlate logs and metrics using resource id, region, verified compartment and the
same window. Matching timestamps alone prove neither cause nor an incident.
Metrics charts/CSV, automatic MQL learning/sharing and alarm-history expansion
are deferred. Existing log workflows continue unchanged.

### Repeatable live validation

From an installed environment, use the repository's read-only validator:

```bash
python scripts/validate_metrics_live.py --config ~/.oci-logan-mcp/config.yaml \
  --python venv/bin/python --compartment-id <compartment-OCID> \
  --region <OCI-region> --output-dir /tmp/monitoring-validation-new
```

It uses a new private HOME and stores, compares one selected stream with a direct
SDK request over the same fixed UTC window and records each applicable starter.
Keep `private-results.json`, copied settings and `server.stderr` private. Share
`review-summary.json` for coverage. Exit 0 means required live gates passed, 1
means a query or comparison failed, and 2 means authorization/data prerequisites
remain pending. A CPU result does not certify every catalog row.
