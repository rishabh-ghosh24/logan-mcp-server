# Release notes

[Back to the README](README.md)

## 1.3.0 (unreleased)

### Logs and metrics in one server

- Add five OCI Monitoring tools and two help resources for discovery, reviewed
  catalog queries and raw MQL. Metrics work without Log Analytics configured.
- Return exact metric names by default from `list_metrics`. A named follow-up
  returns stream dimensions. Preserve instance filters and native paging, with
  a query-slot charge for every page.
- Include 23 reviewed catalog definitions with source provenance and withhold
  unverified candidates. Raw MQL can query other authorized namespaces.
- Bound metric responses by series, points and bytes. Default to 4,000 compact
  timestamp/value pairs and derive omitted resolution from the query intervals.
- Keep service failures separate and report logs/metrics readiness independently.
  Document separate discovery and query IAM permissions.

### Smaller discovery responses

- Replace the full schema resource with a read-time digest: scope, window,
  counts, the top 20 sources by volume and pointers to discovery tools.
- Report incomplete or failed volume counts as lower bounds or unknown, not
  exact zeros, and record schema resource reads in the audit log.
- Add keyword matching and default 50-row limits to source, field, entity and
  saved-search discovery. Report total, returned and truncated counts.
- Prefer sources with recent data, keep source display/internal names, compact
  JSON responses, and omit redundant field hints and entity-type payloads.
- Fetch larger entity pages outside the event loop and resolve display source
  names before applying the SDK's supported field-source filter.

### Saved searches, dashboards and alerts

- Create native saved searches containing the query and widget configuration;
  continue accepting older scheduled-task IDs.
- Validate and translate five-field alert schedules to OCI syntax. Compare the
  extracted value using `last()`, with a window that covers the schedule.
- Discover console-made log alerts through OCI linkage. Show their creator,
  schedule, live condition and threshold without rewriting console conditions.
- Refresh linked Logan alarms when a query changes. Look up dependencies before
  writing and report partial results if a later linked update fails.
- Refuse saved-search deletion while a detection rule depends on it. Recheck
  dependencies live before deletion; listing markers follow the catalog cache.
- Delete an alarm without deleting its saved search. Keep rules that other
  alarms still read, including metrics used in compound conditions, and verify
  missing rules through current OCI inventory.

### Query and output correctness

- Include the row limit in query cache keys and reject invalid limits before
  reading the cache, so a small cached result cannot hide later rows.
- Search Source IP, Destination IP and Host IP Address (Client) for IP pivots,
  with quoted values escaped before execution.
- Reject blank scope changes without changing the active context.
- Render timestamp-typed line, area and heatmap axes in UTC without modifying
  the query's returned data.
- Support `network`, `audit` and `general` when retrieving learned examples,
  matching the categories accepted when saving them.
- Escape HTML-sensitive query output in Telegram messages.
- Fix invalid starter queries, use discovered grouping fields for host templates,
  and diagnose common Log Analytics syntax errors before execution.

### Safety and learning

- Bind confirmation tokens to the submitted operation and effective scope;
  wait for in-flight operations before changing scope.
- Keep writes enabled by default with confirmation for guarded operations;
  retain optional `--read-only` mode.
- Run query SDK work outside the event loop under one deadline. Limit batches
  to ten queries and charge each live item to the session budget. Timed-out
  requests retain their budget reservation.
- Isolate per-user query history, remove duplicate raw audit arguments and add
  configurable audit text retention. Redact confirmation secrets on log handlers.
- Protect manual saves, rank them ahead of automatic variants, cap automatic
  learning and record execution outcomes across query entry points.
- Preserve quoted query text during normalization, separate scope-specific
  learning outcomes, sanitize shared metadata and withdraw invalid patterns only
  when store scans support that decision.
- Reject unsafe user identifiers, remember compartments per user and resolve
  configuration paths consistently.

### Installation and protocol

- Require Python 3.11 or newer, MCP SDK `>=1.22.0,<2` and OCI SDK `>=2.104.2`.
- Repair wheel packaging, include the log and Monitoring catalogs and use one
  package version for the CLI and MCP initialization.
- Restore resource reads, advertise tool titles/annotations and return MCP error
  flags for blocked, unknown or failed calls.
- Add installed-wheel/stdin-stdout protocol checks and configure a Linux/macOS
  CI matrix for Python 3.11 through 3.14.

### Verification and known limits

Release qualification used Oracle Linux with Python 3.11 and 3.14, real OCI
queries, disposable resource lifecycles, alert firing/recovery and received
Notifications emails. This does not establish every platform or tenancy.

- Real Slack and Telegram delivery remains unverified; HTTP contracts and safe
  error handling were tested.
- Native Windows is unverified. Hosted CI passes on Linux and macOS with Python
  3.11 through 3.14, using simulated OCI calls.
- Some clients cache tool schemas. Reconnect after upgrading; restart the client
  if it has no reconnect control.
- Transport is stdio, optionally through SSH. Hosted HTTPS/OAuth is deferred.
- SSH connections can drop during network interruptions or client IP changes;
  the Claude app then needs a restart. Local installations are unaffected by
  this SSH limitation. HTTP transport is the planned fix.
- Metrics charts/CSV, automatic MQL learning/sharing and alarm-history expansion
  are deferred. Log chart/export tools remain available.
- Investigation drill-down uses the seed's pre-pipe filter. Put required scoping
  there rather than relying on a later pipeline filter.

## 1.2.0 (2026-04-17)

#### Internal architecture
- Unified query catalog across builtin templates, starter examples, personal learned queries, and shared promoted queries. Single `UnifiedCatalog` module with surface-specific precedence rules.
- Stable `entry_id` (UUID4) on every personal learned query with legacy backfill migration.
- Cross-user promotion dedup now uses canonical key `(name.lower(), normalized_query_text)`: fixes the multi-user aggregation bug where two users saving the same query were counted separately.
- Shared-catalog lock (`shared/catalog.lock`) protects promoted-queries writes from racing with concurrent user saves.

#### User-visible
- `save_learned_query` gains optional `force` and `rename_to` parameters for resolving collisions with built-in or community queries.
- Onboarding (`get_query_examples`) now includes top-N community favorites alongside starter examples.
- Scheduled promotion guide at `docs/cron-scheduling.md`.

#### Removed
- `list_learned_queries` MCP tool: learning is now invisible infrastructure.
- `delete_learned_query` MCP tool: orphaned without list.
- `ContextManager.{save,list,delete}_learned_query`, `get_all_templates`, and `record_query_usage`: replaced by `UserStore` + `UnifiedCatalog`.
- `src/oci_logan_mcp/templates/query_templates.yaml`: unwired dead file.

#### Behavior changes
- `QueryAutoSaver` now requires `user_store` (no fallback to ContextManager).
- Servers running without user_store configured will fail fast at startup.

---

| Version | Summary |
|---|---|
| **0.5.0** | **Dashboard creation:** Programmatic OCI Management Dashboard creation with proper LA widget wiring, 2-column grid layout, scope filter integration (`parametersMap` with `$(dashboard.params.*)` references), and dashboard delete/update with cleanup. Visualization types: bar, line, pie, table, area, treemap, heatmap, histogram. |
| **0.4.0** | **Alarms & safety:** OCI-native autonomous alerts from Log Analytics queries (metric extraction + OCI Monitoring alarms + ONS notifications). Two-step confirmation for destructive operations with per-user hashed secrets, audit logging, and query history and audit logging. |
| **0.3.0** | Multi-user learning: per-user query storage, preference tracking, shared query promotion with sensitive data sanitization, thread-safe file locking. |
| **0.2.0** | Cluster query accuracy fix, compact cluster output formatting, compartment persistence, startup responsiveness (deferred schema refresh), `--setup` and `--user` CLI flags, Windows setup guide. |
| **0.1.0** | Initial release: 24 MCP tools, query execution, schema exploration, visualization, export, cross-compartment queries, caching, rate limiting, query auto-save. |
