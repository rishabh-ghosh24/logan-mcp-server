# Learning, budgets and safety

[Back to the README](../README.md)

## Configuration overrides

The default configuration file is `~/.oci-logan-mcp/config.yaml`.
`OCI_LA_MCP_CONFIG` selects another file. Environment values override the loaded
server settings, for example:

```bash
export OCI_LA_NAMESPACE=your-namespace
export OCI_LA_COMPARTMENT=ocid1.compartment.oc1..example
export OCI_CONFIG_PROFILE=DEFAULT
```

`OCI_LA_AUTH_TYPE` selects authentication and `OCI_CONFIG_PATH` selects the OCI
API-key configuration file. Desktop clients may not inherit variables from your
terminal; use the server configuration file or the client's environment settings.
Monitoring has its own [configuration overrides](monitoring.md).

## Learning

Each user has separate query and preference files under `~/.oci-logan-mcp/users/<username>/`. Select a name with `--user firstname.lastname` or `LOGAN_USER`; the system user is the fallback.

- Successful complex queries are scored and auto-saved. Trivial queries are skipped.
- Automatic learning keeps at most 3 variants per query shape and 20 automatic entries per log source. It removes entries with the fewest recorded successes, then the lowest usage and oldest use.
- The personal store holds at most 200 queries. Automatic eviction protects manual saves; a store filled with manual queries skips additional automatic saves.
- `save_learned_query` marks an explicit save as manual and gives it a promotion-eligible interest score. Within the relevant learned examples, explicit saves rank ahead of automatic saves in `get_query_examples`.
- Preferences track field usage, time ranges and disambiguation choices. `set_compartment` remembers your default compartment without rewriting the shared configuration file.
- Shared examples show `source`, `user_count` and `success_count`. Execution counts describe recorded use, not proof that a query is correct for another environment.

If a name conflicts with a built-in or shared query, `save_learned_query` returns a `collision_warning`. Use `rename_to` for another name or `force=true` to override the collision.

Shared-template promotion is an administrator-run feature for trusted team deployments. See [promoting shared templates](team-deployment.md#promoting-shared-templates) for thresholds, scheduling and content review.

## Cost + ETA estimation

`run_query` responses carry `estimated_bytes`, `estimated_rows`, `estimated_cost_usd`, `estimated_eta_seconds`, `estimate_confidence` and `estimate_rationale`.

`explain_query` estimates the requested query instead of executing it in full. Estimation can issue count probes against OCI and caches those probes. A cached query response reuses its recorded estimate. Estimates are advisory and do not represent an OCI bill.

A whole-query deadline covers SDK calls, pagination, rate-limit waits and retries. Set `query.timeout_seconds` in the configuration. A timeout releases the caller, but an SDK thread and OCI request may continue until the HTTP call returns. The server does not add an explicit in-flight query cap beyond its existing rate limiting. Timed-out queries retain their budget reservation because work may still be running.

## Session query budget

The query execution path applies these per-session limits:

| Limit | Default |
|---|---|
| `max_queries_per_session` | 100 |
| `max_bytes_per_session` | 10 GiB |
| `max_cost_usd_per_session` | $5.00 |

Call `get_session_budget` to inspect usage and remaining allowance. Cached query responses do not consume a new query reservation. Several investigation and visualization tools use the same query path.

`run_batch_queries` accepts at most 10 queries and reserves budget atomically for each live item. Cache hits reuse results without another reservation. A refusal appears in that item's result as `BudgetExceededError`; a batch has no budget override. Use a confirmed `run_query` call to override an individual query. Do not treat the session budget as a tenancy spending limit.

Queries without a `'Log Source'` filter have low-confidence volume estimates and may report zero estimated bytes and cost. Their query-count limit still applies, but the byte and cost limits cannot bound an unknown scan size.

A `run_query` call with `budget_override=true` follows the confirmation flow. An override still records usage.

```yaml
budget:
  enabled: true
  max_queries_per_session: 100
  max_bytes_per_session: 10737418240
  max_cost_usd_per_session: 5.00
```

Set `budget.enabled: false` to disable these limits.

## Destructive Operation Safety

All 69 tools remain advertised. Writes are enabled by default, with confirmation required for guarded operations.

### Read-only mode

```bash
oci-logan-mcp --read-only
# Environment variable alternative:
OCI_LOGAN_MCP_READ_ONLY=1 oci-logan-mcp
```

Read-only mode blocks OCI resource changes, outbound delivery, preference and
learned-query saves, confirmation-secret setup, and playbook creation/deletion.
It explicitly blocks `generate_incident_report`, `record_investigation`,
`prepare_report_delivery`, `set_compartment`, `set_namespace` and
`update_tenancy_context`.

Queries, listing, validation, charts and `export_results` still work. Use
supported per-query scope arguments, such as `compartment_id` and
`include_subcompartments`, instead of changing session defaults. Monitoring
queries also accept a per-call region. Cache, history and audit files can still
be written. OCI IAM controls the data the process can access.

The combined `investigate_and_generate_report` workflow remains allowed as an
explicit exception in the current read-only policy. That exception does not
enable the separate `generate_incident_report` tool or outbound delivery.

Tool hints follow this same classification. Local writes alone do not establish
that a tool is allowed: exports and transcripts remain available, while the
report-generation, recording and delivery-preparation tools named above are
blocked.

### Guarded operations

Guarded tools include alert, saved-search and dashboard creation, updates and deletion; dashboard tile changes; log-source onboarding; and playbook deletion. A budget override also requires confirmation.

1. The first call returns an action summary and a single-use confirmation token.
2. The follow-up supplies that token and the user's confirmation secret.

This is **two-step server-side confirmation**. It helps a well-behaved client avoid accidental execution. It does not provide an independent approval channel against a compromised client that can submit both values.

Tokens validate the tool, submitted arguments, and effective namespace, compartment and subcompartment setting. Scope changes wait for running operations. Changing the default scope between preview and execution requires a new confirmation token.

Token expiry defaults to 300 seconds:

```yaml
guardrails:
  token_expiry_seconds: 300
```

### Per-user confirmation secrets

Secrets require at least eight characters and are stored as salted `hashlib.scrypt` hashes in `~/.oci-logan-mcp/users/<username>/confirmation_secret.hash`.

Startup succeeds without a secret. Unguarded tools remain available; guarded operations require first-time setup through `setup_confirmation_secret`. To replace a forgotten or existing secret, use an interactive terminal:

```bash
oci-logan-mcp --user firstname.lastname --reset-secret
```

The former `OCI_LA_CONFIRMATION_SECRET` variable is no longer used.

### History and audit records

Operational logs redact confirmation secrets on root handlers present at server startup. Embedding applications that add handlers later must apply `install_secret_redaction` to those handlers.

Private query history is stored at `~/.oci-logan-mcp/users/<username>/history/queries.log`, with POSIX mode `0600` and rotation. It does not propagate through the operational logger. `logging.query_logging: false` disables file history; the recent-query resource remains in memory for the session.

Audit records are stored separately at `~/.oci-logan-mcp/logs/audit.log`. New records contain `args_redacted`, not a duplicate raw `args` field. The local single-user default retains redacted argument text so investigation playbooks and transcripts remain useful.

Set `audit.store_arguments: false`, or `OCI_LOGAN_AUDIT_STORE_ARGUMENTS=0`, to replace query/free-text arguments with hashes and lengths, limit result summaries to selected numeric/boolean fields, and fingerprint errors. Such records carry `text_retained: false`. Playbooks recorded from them carry `text_omitted: true` and a warning that omitted arguments require review before replay. This audit setting does not disable private query history.

### Transcript export

Use `export_transcript(session_id="current")` to export the current process's audit trail. Output defaults to `~/.oci-logan-mcp/transcripts/`; `transcript_dir` changes the destination.

- `include_results=false` omits result summaries.
- `redact=true` applies additional built-in masking.
- The export preserves `text_retained` so readers can identify omitted text.

A session ID belongs to a server process and can contain multiple investigations. Promotion runs do not currently emit MCP audit events.

### Failure behavior

Guarded operations fail when the secret is missing, corrupt or wrong, or when a token is expired, reused or does not match the submitted operation. Required audit failures prevent the operation from executing. Read-only calls use best-effort auditing.

Report security issues through the process in [SECURITY.md](../SECURITY.md).
