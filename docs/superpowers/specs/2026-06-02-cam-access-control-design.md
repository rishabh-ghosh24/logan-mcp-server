# CAM Access Control - Design Spec

- Status: Approved for planning
- Date: 2026-06-02
- Branch: `access-control`
- Author: Rishabh Ghosh (with Claude)

## 1. Problem

The Assurance team uses the Logan MCP server to produce capacity reports for end
customers. Each end customer is represented by one or more OCI Log Analytics
**entities** in the `LoggingAnalyticsData` compartment of the `assurance4emea`
tenancy. Entity names follow the convention `<number>_<name>`, e.g.
`223_d360_silicone`, `66_flsmidth_co_as`, `1400_indra`. A single customer can
span multiple numbered entities (e.g. Hermes = `65_...` and `67_...`).

The team has sub-teams of Customer Account Managers (CAMs). A CAM represents one
or more customers and must be able to **read only their assigned customers'
data** and produce reports. A CAM for one customer must never see another
customer's data through the MCP server.

This requirement applies **only** to the Assurance CAMs. General-purpose MCP
users must be completely unaffected.

## 2. Goal and non-goals

### Goal
Restrict each CAM to read-only access of only the entities for their assigned
customer numbers, across every MCP tool, enforced inside the MCP layer, with zero
change to general-purpose users.

### Security boundary (guiding principle)
Two things are protected:
1. **Query result data** (the hard boundary) - the actual rows/values returned
   from a customer's logs, and any artifact that embeds them (reports, result
   previews, audit result summaries, raw log excerpts). A CAM must never obtain
   another customer's result data.
2. **The customer entity roster** - the *bulk list* of which customers/entities
   exist. A CAM sees only their own entities; we do not hand over the full
   customer list. Enforced cheaply by filtering `list_entities` (6.7), filtering
   `schema` resource entities, and blocking `tenancy-context` (6.12).

**Not the boundary (shareable):** query *text* / learned-query syntax / query
templates, and generic configuration metadata (log-source / log-group / parser /
label names). These carry no result data, and an entity name appearing
*incidentally* inside a shared query's text is acceptable - that is different from
bulk-enumerating the roster, which protection (2) prevents.

This principle decides every allow/block call below: result-bearing paths and
roster enumeration are scoped or blocked; query-text and config-metadata paths
can be shared.

### Non-goals
- OCI IAM-level isolation per CAM. The VM authenticates with a single instance
  principal that can read all entities, so OCI cannot distinguish CAMs. All
  per-CAM authorization lives in the MCP layer. (See memory:
  `project_instance_principals.md`.)
- Protecting against a trusted admin who edits the config or VM.
- Streamable HTTP transport (deferred - see Section 11).
- A new per-customer OCI compartment layout.

## 3. Decision: build in-place, gated - no fork

We implement this in the existing repo behind an explicit **`--enforce-access`**
mode rather than forking. General users launch the server exactly as today and
see no behavior change; the access-control code paths are inert unless
`--enforce-access` is set. A fork would diverge and rot; gating gives isolation
plus shared maintenance. This is the answer to the original "fork vs in-place"
question.

## 4. Two-layer model (both must hold)

Because CAMs hold the SSH key to the MCP VM, and any process on that VM can reach
the instance principal (and therefore all customer data), MCP-layer authorization
is only a real boundary if a CAM **cannot execute arbitrary code on the VM**.
The feature therefore has two independent layers, both required:

1. **Identity and anti-bypass (deployment / Section 5).** A CAM can only talk to
   the MCP server, as their own pinned identity, with no shell and no ability to
   change their identity or run other code.
2. **Authorization (code / Section 6).** That identity is restricted to its
   customers' entities across every tool.

Layer 2 is the bulk of the engineering work and is transport-agnostic. Layer 1 is
deployment hardening plus a launch script and provisioning tooling.

## 5. Layer 1 - identity and anti-bypass (forced-command SSH, stdio preserved)

Transport stays **stdio** (one process per connection), which preserves the
existing per-user profile system (learned queries, preferences, secrets keyed by
`--user`). Each CAM connects over SSH; we make that connection a hard boundary.

### 5.1 Forced-command SSH key per CAM
Each CAM gets a dedicated SSH key whose `authorized_keys` entry pins a forced
command and disables everything else:

```
command="/opt/logan/scripts/cam-launch.sh cam_alice",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding ssh-ed25519 AAAA...alice
```

- The forced command **ignores** `$SSH_ORIGINAL_COMMAND`, so the CAM cannot pass
  their own command or flags (cannot change `--user`, cannot get a shell).
- `no-pty` plus the forwarding restrictions prevent interactive shells, port
  forwarding, and agent hijacking. SFTP/SCP also hit the forced command, so file
  transfer is blocked too.

### 5.2 `cam-launch.sh` (root-owned)
A small wrapper, root-owned and not writable by the `cam` user:

```sh
#!/bin/sh
# usage: cam-launch.sh <cam_id>   (called only by forced command)
exec python -m oci_logan_mcp --user "$1" --enforce-access
```

It always sets the identity from its own argument (not from the environment or
the client), and always enables enforcement.

### 5.3 One shared Unix user, many keys
All CAM keys may map to a single unprivileged `cam` Unix user. Identities are
distinguished by the forced command's `<cam_id>` argument, not by OS account, so
no per-CAM OS accounts are needed. Per-CAM state still lives in
`users/<cam_id>/` as today.

### 5.4 File ownership requirements
`authorized_keys`, `cam-launch.sh`, and `access_control.yaml` must be
**root-owned and not writable by the `cam` user**. The provisioning script
enforces and verifies this.

### 5.5 Provisioning tooling (shipped with the feature)
- `scripts/provision-cam.sh <cam_id> --customers 223,66 [--no-delivery]` - one
  command: generates an ed25519 keypair (or accepts a supplied public key),
  appends the hardened `authorized_keys` line, writes/updates the CAM's entry in
  `access_control.yaml`, sets file ownership/permissions, and prints the private
  key plus a ready-to-paste MCP client config snippet to hand off.
- `scripts/deprovision-cam.sh <cam_id>` - removes the `authorized_keys` line and
  the `access_control.yaml` entry.
- Documentation of the hardening and provisioning flow under `docs/`.

## 6. Layer 2 - authorization (code)

### 6.1 New module `access_control.py`
A single source of truth, modeled on `read_only_guard.py` (which has a
drift-catching test). It provides:
- Loading and validation of `access_control.yaml`.
- `AccessProfile` - the resolved, per-process authorization context.
- Entity-number matching (6.4).
- Query scoping: grammar validation + the `scope_query()` pure transform (6.5).
- Tool gating (CAM tool allowlist, 6.6) and MCP resource gating (6.12).
- Catalog/report source filtering for CAM mode (6.11).

### 6.2 Config file `access_control.yaml`
This is an **opt-in deployment config**, not a repo-wide default. In normal
server mode (no `--enforce-access`) the repo behaves exactly as it does today,
including `set_compartment`. When `--enforce-access` is enabled, the server loads
this file and pins the scoped user's OCI boundary from it.

The file is root-owned on the VM (default location alongside existing config
under the config dir; path is configurable). The Assurance VM would use a config
like this:

```yaml
# OCI scope scoped users are pinned to (Assurance deployment example)
tenancy_id: ocid1.tenancy.oc1..aaaaaaaacwe5cve7esnjg5lkllxdqjagivfsp6hvczk3pqajjlfga3qub4ja
compartment_id: ocid1.compartment.oc1..aaaaaaaaqvparsvna5cozzy65r4xasbecrkdk5mucyhtfu7pktmutd6bwajq
namespace: frdul02gvsni       # assurance4emea object-storage namespace (confirmed live)
entity_field: "Entity"        # confirmed; overridable if the field ever changes

defaults:
  allow_delivery: true        # global default for outbound push; applies to all CAMs

cams:
  cam_alice: { customers: [223, 66] }                     # inherits allow_delivery: true
  cam_bob:   { customers: [232], allow_delivery: false }  # per-CAM override
```

Notes:
- `customers` is a list of integer customer numbers.
- Confirmed: the VM's instance principal already authenticates to the
  `assurance4emea` tenancy, so the server's OCI auth needs no change. Local
  testing can use config-file auth pointed at the same tenancy.
- Other teams can reuse the same code path with their own tenancy,
  compartment, namespace, entity field, and user-to-entity mapping. If they need
  different compartments per scoped user, `AccessProfile` should support an
  optional per-user `compartment_id` override; the rule remains the same:
  scoped users can discover compartments but cannot mutate their runtime scope
  with `set_compartment`.

### 6.3 Identity resolution, `--enforce-access`, fail-closed
At startup, when `--enforce-access` is set:
1. Load `access_control.yaml`. Pin compartment and namespace to its values.
2. Look up the `--user` id in `cams`. If **absent or `customers` is empty**, the
   server **refuses to start** with a clear error. A CAM session can never
   silently run unrestricted.
3. Resolve the CAM's customer numbers to concrete entity names by listing live
   entities (via the existing schema/entity path) and keeping those that match
   (Section 6.4). Store the resolved entity-name set in the `AccessProfile`.
4. If resolution yields **zero** entities (typo'd number, OCI unreachable), the
   server **refuses to start** with an error naming the unmatched numbers. This
   surfaces misconfiguration instead of masking it, and never falls back to
   unrestricted.

This validation runs **before the stdio transport starts**, and it must override
the current startup behavior: `initialize_core` today catches OCI-client init
failure and continues ("Server will start but OCI operations will fail",
`server.py`). Under `--enforce-access` that swallow-and-continue path is fatal -
if the OCI client cannot initialize or entities cannot be resolved, the process
exits non-zero rather than starting in a degraded state. Building and validating
the `AccessProfile` is a precondition of serving any request.

When `--enforce-access` is **not** set, no `AccessProfile` is created and every
code path behaves exactly as today (general users unaffected). This invariant is
covered by tests.

Because identity is fixed per process (stdio, one CAM per connection), the
`AccessProfile` is process-global, built once at startup. The resolved entity set
is captured at startup; new entities added mid-session are picked up on the next
connection (sessions are short). No per-request plumbing is needed.

### 6.4 Entity-number matching (anchored)
A customer number `N` matches an entity name `e` iff:

```
e == str(N)   or   e.startswith(f"{N}_")
```

i.e. the number appears at the very start, immediately followed by `_` (or is the
whole name). The number is matched as the exact integer string (no leading
zeros). The portion after the first `_` (the customer name, possibly containing
digits like `d360`) is never inspected.

Verified against real data:

| Grant | Matches | Does NOT match | Why |
|-------|---------|----------------|-----|
| 223   | `223_d360_silicone` | `232_...`, `1400_...` | anchored, then `_` |
| 66    | `66_flsmidth_co_as` | -                     | exact number prefix |
| 2     | (`2_foo`)           | `232_`, `223_`, `265_`, `238_` | next char is a digit, not `_` |
| 140   | (`140_x`)           | `1400_indra`          | next char is `0`, not `_` |

A CAM's resolved entity set is the union of matches across all their numbers. A
single number may match more than one entity; all are included.

### 6.5 Query scoping (the core)
Enforcement lives in the OCI client, which owns the `AccessProfile` (Section 6.9).
`OCILogAnalyticsClient.query()` (`client.py`, where `QueryDetails` is built and
sent) is the single chokepoint through which all query execution passes, including
queries built internally by `get_log_summary`, `find_rare_events`,
`pivot_on_entity`, `investigate_incident`, and `run_batch_queries` - not just
`run_query`. Some of those tools are blocked for CAMs in v1, but the scope
transform still belongs here so any future allowed query path cannot bypass it.

The scope transform itself is a **pure function** in `access_control.py`
(`profile.scope_query(q) -> str`) so the calling layer can also compute the
effective query for cache keys, cost estimation, logging, and audit (Section 6.10)
without re-deriving it.

When an `AccessProfile` with a resolved entity set is active, the query string is
rewritten before being sent:

```
'Entity' in ('65_...', '67_...') and (<original search>) | <rest of pipeline>
```

Algorithm:
1. **Validate against the CAM-safe grammar (fail-closed) BEFORE rewriting.** A
   single leading predicate only constrains the outer search; OCI Log Analytics
   supports constructs that open *additional* data contexts the predicate would
   not reach - sub-queries inside `addfields`, `createview`/`map`, `updatetable`,
   and the lookup family (`lookup`, `searchlookup`). In CAM mode the query is
   **rejected** if (a) it contains sub-query brackets `[` / `]` anywhere, or
   (b) any pipeline command (the token after a top-level `|`) is not in the exact
   v1 allowlist below, or (c) the **head segment** (before the first top-level
   `|`) is not a pure search/filter expression - i.e. it begins with a command
   keyword rather than a field predicate or `*`. Rule (c) is essential because
   `searchlookup` (and `lookup`) are **not bracketed** and can appear as the very
   first command, opening lookup-table contents that no leading predicate would
   constrain. So the head must parse as a search expression; any leading command
   token (`searchlookup`, `lookup`, `createview`, `map`, `updatetable`, `link`,
   `classify`, `addfields`, ...) is rejected. The allowlist is a fixed constant,
   not a fuzzy guideline:

   ```
   CAM_QUERY_COMMANDS = {stats, timestats, eventstats, where, eval, sort,
                         head, tail, fields, fieldsummary, distinct,
                         top, bottom, rename}
   ```

   Every command in this set operates only on the already-scoped record set and
   takes no sub-query and no source/time/entity re-selector, so it cannot open an
   unscoped context. The allowlist and the bracket/lookup rejection are validated
   against the Oracle Command Reference.

   `addfields` and `classify` were evaluated and **deliberately excluded from
   v1**: `addfields` is defined as `addfields <subquery>` so any real use is
   bracketed and caught by rule (a); `classify` is bracket-free and reads only
   upstream results (safe) but requires a preceding `link` stage and has no
   capacity-reporting need, so it is omitted to keep the allowlist tight. Either
   can be added later **with tests** if a concrete CAM workflow needs it. A full
   LQL-aware recursive scoper that safely scopes nested search contexts is the
   longer-term path; until then, reject-on-unsafe-grammar is the safe default.
2. Build the predicate `'<entity_field>' in (<quoted entity names>)` from the
   resolved set (always non-empty, guaranteed by Section 6.3).
3. Split the query into the head (the leading search expression) and tail (the
   command pipeline) on the **first top-level `|`**, respecting quoted strings
   so a `|` inside `'...'`/`"..."` does not split.
4. If the head is empty or `*`, the scoped query is `predicate | tail`. Otherwise
   it is `predicate and (head) | tail` - the head is parenthesized so the
   predicate ANDs with the whole user expression regardless of internal
   `and`/`or` precedence.
5. The predicate sits in the base record set **before** any aggregation, AND-ed
   in, so a CAM can only ever **narrow** within their entities, never widen. A
   user-supplied `Entity = '99_other'` just ANDs to an empty result.

**Reject-rather-than-pass:** if the query fails grammar validation (step 1),
cannot be safely tokenized (e.g. unbalanced quotes), or an entity value cannot be
safely quoted, it is **rejected** with a clear error and **never sent unscoped**.
This is fail-closed; ordinary capacity-reporting queries (aggregations over the
CAM's own entities) scope cleanly, so rejection is a rare backstop.

Scoping is by `Entity` only, never by Log Group, because a customer's data spans
the `DEFAULT` / `ExaCC` / `ExaCS` log groups; the entity filter follows the
customer across all groups.

### 6.6 Tool gating - default-deny allowlist
A CAM may only invoke tools on an explicit allowlist (`CAM_ALLOWED_TOOLS`).
Anything not on it is **blocked** with a clear "not permitted in access-controlled
mode" error. Default-deny means any tool added later is automatically blocked for
CAMs until consciously allowed. A drift-catching test (like the read-only guard's)
asserts every registered tool is classified as allowed or knowingly blocked, so
nothing is forgotten. In CAM mode `list_tools` also advertises only allowed
tools, so blocked tools are neither listed nor invocable. (The MCP server exposes
exactly four surfaces - `list_tools`/`call_tool` and `list_resources`/
`read_resource`, `server.py` - both of which are gated; there are no prompt or
sampling surfaces.)

**Gate placement.** In `handle_tool_call` the CAM allow/deny gate is inserted
**immediately after the unknown-tool check and before the read-only guard**
(`handlers.py` - after the handler-dict lookup at ~line 319, before the read-only
guard at ~line 321). This ensures a denied CAM tool never reaches the read-only
guard, the confirmation gate, query cost estimation, or handler dispatch. Order:
audit "invoked" -> unknown-tool check -> **CAM allow/deny** -> read-only guard ->
confirmation -> handler.

This is the complete classification of the live tool registry
(`handlers.py` handler map). The drift test asserts every registered tool appears
in exactly one of these sets: always allowed, conditionally allowed, or blocked.
A new tool is blocked until classified.

**Allowed for CAMs (capacity/reporting + safe personalization):**
- Scoped data reads (entity-scoped via 6.5; compartment pinned via 6.9):
  `run_query`, `run_batch_queries`, `get_log_summary`.
- Entity listing: `list_entities` (filtered, 6.7).
- Generic metadata / discovery: `list_log_groups`, `list_log_sources`,
  `list_fields`, `list_labels`, `list_parsers`, `list_saved_searches`,
  `list_compartments`, `find_compartment`.
- Query helpers (must operate on the scoped query / not execute unscoped):
  `validate_query`, `explain_query`, `get_query_examples` (built-in + personal +
  shared query text - shared text is shareable under the result-data boundary,
  6.11).
- Saved-search execution: `run_saved_search`, but it must fetch the saved query
  and then run it through the same CAM query-scoping path as `run_query`.
- Reporting / visualization: `visualize`, `export_results`.
- Per-user learning and preferences: `save_learned_query`, `get_preferences`,
  `remember_preference`. These are stored under the CAM's user id and never in
  shared tenancy context.
- Confirmation-secret bootstrap: `setup_confirmation_secret`. It is per-user
  and may be needed if a future allowed guarded operation is added.
- Session / health info: `get_current_context`, `get_session_budget`,
  `test_connection`.

**Conditionally allowed for CAMs (only when outbound delivery is enabled):**
- Report/delivery workflow: `get_report_delivery_options`,
  `prepare_report_delivery`, `deliver_report`.
- Direct outbound push: `send_to_slack`, `send_to_telegram`.
- Notification-topic discovery: `list_notification_topics`, but only within the
  pinned Assurance scope and/or a pre-approved topic list.

For CAMs, outbound delivery must reject arbitrary destination overrides unless
the destination is configured or explicitly pre-approved (for example Telegram
`chat_id`, Slack webhook, and ONS topic OCIDs). Any query supplied to a delivery
tool is still scoped through the client before results are sent.

**Blocked for CAMs:**
- Runtime scope/config changes: `set_compartment`, `set_namespace`,
  `update_tenancy_context`. The Assurance VM should pin LogAnalyticsData via
  `access_control.yaml` and/or the existing `OCI_LA_COMPARTMENT` env override,
  so CAMs can discover compartments but cannot mutate the shared default config.
- Troubleshooting / incident workflows that are not part of CAM capacity
  reporting by default: `diff_time_windows`, `pivot_on_entity`,
  `ingestion_health`, `parser_failure_triage`, `investigate_incident`,
  `investigate_and_generate_report`, `generate_incident_report`,
  `get_incident_report`, `list_incident_reports`, `why_did_this_fire`,
  `find_rare_events`, `trace_request_id`, `related_dashboards_and_searches`.
- Metadata-only discovery/management tools that are not part of the CAM v1
  read+report workflow (blocked for *relevance*, not because they leak result
  data - they return only names/ids/query text, which the boundary permits; may
  be revisited): `list_dashboards`, `list_alerts`, `list_playbooks`,
  `get_playbook`.
- All OCI mutations: `create_alert`, `update_alert`, `delete_alert`,
  `create_saved_search`, `update_saved_search`, `delete_saved_search`,
  `create_dashboard`, `add_dashboard_tile`, `delete_dashboard`,
  `create_log_source_from_sample`.
- Audit / investigation state: `export_transcript`, `record_investigation`,
  `delete_playbook`.

Two distinct reasons drive the block list, and the rationale must stay honest:
- **Relevance** (most of the above): these tools return only names, ids, or query
  *text* - which the result-data boundary explicitly permits - but they are not
  part of the CAM v1 read+report workflow, so they are blocked to keep the surface
  tight. `related_dashboards_and_searches` was verified live to return only
  `id`/`name`/`score`/`reason` (no result data); `list_alerts` exposes only stored
  alert query text. These can be revisited if a CAM workflow needs them.
- **Result-data / state**: `export_transcript` is the one genuine result-data
  concern - its export can include result summaries/previews, and (separately) a
  pre-existing bug let any user export another session by id (`audit.py`; filed
  and fixed on main as session-ownership enforcement). `why_did_this_fire` runs a
  stored alarm query (results would be entity-scoped via the chokepoint, but it is
  a troubleshooting flow). Mutations and state writes are blocked because CAMs are
  read-only.

### 6.7 list_entities filtering
- `list_entities` results are filtered to the CAM's resolved entity set before
  return, so a CAM only ever sees their own entities.
- `pivot_on_entity` is blocked for CAMs in v1 because it is a troubleshooting
  workflow, not a capacity-reporting primitive. If it is later enabled, its
  entity argument must be validated against the CAM's resolved entity set before
  execution.

### 6.8 Export vs outbound delivery
- **Export / download** (CSV/JSON/HTML-style outputs via `export_results`) is
  always allowed for CAMs - it is their core job and is not an external channel.
- **Outbound delivery** (`get_report_delivery_options`,
  `prepare_report_delivery`, `deliver_report`, `send_to_slack`,
  `send_to_telegram`, `list_notification_topics`) is gated by `allow_delivery`:
  global `defaults.allow_delivery` (default `true`) with an optional per-CAM
  override. When disabled, these tools are blocked for that CAM.

### 6.9 Compartment / namespace pinning (in the client, not just the resolver)
When `--enforce-access` is enabled, the resolved `AccessProfile` is attached to
`OCILogAnalyticsClient`. For scoped users, namespace and compartment are pinned
**inside every OCI-facing client method**, not only in the handler-layer
`_resolve_scope`. This is required because some paths bypass `_resolve_scope`:
`_list_log_sources` accepts a `compartment_id` override (`handlers.py`),
`run_batch_queries` allows a per-query `compartment_id` (`query_engine.py`), and
notification-topic listing can walk compartments (`client.py`). In scoped mode
the client ignores any caller-supplied `compartment_id`/namespace and
`scope=tenancy`, so no tool argument can redirect to other data.

When `--enforce-access` is **not** enabled, no `AccessProfile` is attached and
existing behavior remains unchanged: normal users can still use
`set_compartment`, `set_namespace`, and query-level compartment arguments as they
do today.

### 6.10 Audit and effective-query handling
- Every access decision is logged via the existing `AuditLogger` under the CAM's
  id: allow, deny (blocked tool), blocked resource, the **injected entity
  predicate / effective scoped query**, and any query rejection. The access audit
  event is emitted at the enforcement layer (client / `access_control`) so it
  records the *scoped* query, not the original (the existing `query_engine` query
  log records the original and is not the access-audit source of truth).
- The query cache is **in-memory and per-process** (`cache.py`, plain dicts).
  Because each CAM is a separate stdio process with a single pinned identity,
  there is no cross-CAM cache collision. As cheap defense-in-depth against any
  future shared-cache change, the cache key includes the profile/user id.
- The cost estimator currently runs on the original query; under scoping it would
  over-estimate (safe direction, not a leak). Passing the scoped query to the
  estimator for accuracy is a low-priority follow-up.

### 6.11 Shared-catalog and report isolation (CAM mode)
Applying the result-data boundary (Section 2): query *text* may be shared, but
report *result content* may not. Verified in code (workflow round 2):
- **Query catalog (text - shareable).** The catalog includes builtin + personal +
  shared/promoted entries. Shared query text that happens to name other customers'
  entities is acceptable under the security boundary, so no security filtering of
  shared query *text* is required. (An optional `cam_safe` curation may be added
  later for tidier examples, but it is not a security requirement.) The legacy
  learned-query migration (`user_store.py` `_migrate_legacy`) was verified to copy
  query **text only** (`learned_queries.yaml`), so it is safe and kept.
- `save_learned_query` is allowed because it writes to the CAM's own per-user
  store. It must not write to shared/promoted catalogs automatically.
- **Legacy report import (result content - blocked).** Legacy shared-report import
  into the per-user store is **skipped** (`report_store.py`
  `_import_legacy_shared_reports`, verified to copy full report markdown/HTML, i.e.
  result content), so a CAM never inherits another customer's report data.
- `related_dashboards_and_searches` is blocked (also in 6.6) for *relevance* (not
  a CAM v1 workflow); verified live to return only `id`/`name`/`score`/`reason` -
  no result data.

### 6.12 MCP resource gating (separate protocol surface)
MCP resources are served by `read_resource`/`list_resources` (`server.py`),
which is a **separate path from `handle_tool_call`** and therefore not covered by
tool gating. In CAM mode:
- `loganalytics://schema` (returns `get_full_schema()`, which includes **all
  entities**, `schema_manager.py`) has its entity list filtered to the CAM's
  resolved set (roster protection, Section 2).
- `loganalytics://tenancy-context` (persisted **all** entities/compartments,
  `context_manager.py`) is **blocked** - it bulk-enumerates the entity roster.
- `loganalytics://query-templates` (shared query *text*, `catalog.py`) is
  **allowed** - query text is shareable under the result-data boundary and is
  useful for query-building. (Aligned with `get_query_examples` in 6.6.)
- `loganalytics://recent-queries` (query history) is **blocked** for relevance -
  not part of the CAM workflow and may expose other sessions' query activity.
- Static resources `loganalytics://syntax-guide` and
  `loganalytics://reference-docs` remain available.
- `list_resources` advertises only the resources a CAM may read.

Additionally, CAM mode suppresses shared-state auto-capture: `_list_log_sources`
and `_list_fields` write into the shared tenancy context unless suppressed
(`handlers.py`, currently keyed on `read_only`); CAM mode suppresses these writes
too, so CAMs neither read nor pollute shared tenancy context.

## 7. Threat model - what this does and does not protect

Protects (under Layer 1 + Layer 2):
- A CAM reading another customer's **result data** through any MCP tool, MCP
  resource, or result-bearing artifact (report, result preview, audit transcript
  summary, legacy report).
- A CAM bulk-enumerating the customer **entity roster** (list_entities filtered,
  schema entities filtered, tenancy-context blocked).
- A CAM widening scope via raw queries (incl. sub-queries/lookups - rejected),
  compartment/namespace switching, unfiltered entity enumeration, saved-search
  execution, or troubleshooting tools.
- A CAM impersonating another identity (forced command pins `--user`).
- Silent unrestricted operation (fail-closed startup that aborts on any
  resolution failure; default-deny tools and resources; reject-on-unparseable and
  reject-on-unsafe-grammar queries).

Does not protect against (accepted):
- A trusted admin who edits `access_control.yaml`, `authorized_keys`, or the VM.
- Misconfigured `authorized_keys` (missing `no-pty`/forwarding flags). The
  provisioning script writes the correct line; manual edits are the admin's
  responsibility.
- Anyone with arbitrary code execution on the VM (which is exactly what Layer 1
  prevents for CAMs).

## 8. Confirmed facts (live verification)

- The OCI Log Analytics field carrying entity identity is **`Entity`** (internal
  name `mtgt`). It is queryable and filterable server-side (confirmed by query
  and by the Assurance "Filter Entity" UI).
- Assurance entity names are `<number>_<name>` (e.g. `223_d360_silicone`).
- Records not tagged to an entity (null `Entity`) are excluded by an
  `'Entity' in (...)` filter - fail-closed by construction.
- Customer data spans log groups `DEFAULT` / `ExaCC` / `ExaCS`; log source names
  are file-type names (`Assurance_Metrics_Compute`, ...), not customer names - so
  log group / source enumeration leaks no customer identity.
- The VM's instance principal already authenticates to the `assurance4emea`
  tenancy - no OCI-auth change is required for the feature.
- All query execution funnels through `OCILogAnalyticsClient.query()`
  (`client.py`) - the single enforcement chokepoint for scoping.
- The query/schema cache is **in-memory, per-process** (`cache.py`, plain dicts) -
  no cross-process sharing between CAM sessions.
- MCP resources are served by `read_resource`/`list_resources` (`server.py`), a
  protocol surface **separate** from `handle_tool_call` - it must be gated
  independently (Section 6.12).
- OCI LA supports sub-queries (`addfields`, `createview`/`map`, `updatetable`)
  and `lookup`/`searchlookup` (Oracle Command Reference) - these open data
  contexts a single leading predicate does not constrain, which is why CAM-mode
  query scoping validates against a safe grammar (Section 6.5).

## 9. Outstanding items (non-blocking for build)

- **Resolved:** the `assurance4emea` object-storage namespace is `frdul02gvsni`
  (confirmed live via `get_current_context`; now in the §6.2 config example).
- **Deployment drift to fix before smoke tests:** the build currently deployed on
  the Assurance VM is *behind* this repo. Confirmed live: `list_fields(source_name=...)`
  returns `list_fields got unknown kwargs: ['source_name']`, though the repo
  handler supports `source_name` (`handlers.py`). Redeploy the current code to the
  VM before smoke-testing, and add a smoke check that asserts the deployed tool
  schemas match the repo.
- Smoke tests run against the Assurance VM MCP connection (now connected as
  `assurance-logan`). Instance-principal auth to the tenancy is already in place
  (Section 8).

## 10. Testing strategy (TDD)

Unit:
- Entity-number matching: all edge cases incl. `223`/`232`, `2`/`22`/`23`,
  `140`/`1400`, exact-number, multi-entity-per-number, leading-zero rejection.
- Query-scope transform: predicate injection; empty/`*` head; head with
  `and`/`or`; first-pipe split respecting quotes; multi-pipe pipelines;
  adversarial widen attempts (`Entity = 'other'` -> empty); unparseable ->
  reject (never unscoped).
- Query-scope grammar validation (6.5 step 1): reject sub-query brackets `[`/`]`;
  reject a **command-form head** - `searchlookup`/`lookup`/`createview`/`map`/
  `updatetable`/`link`/`classify`/`addfields` at query start (before the first
  pipe) must be rejected, since `searchlookup`/`lookup` are unbracketed and would
  otherwise slip past the bracket rule; reject these and any non-allowlisted
  command after a pipe; allowlisted reporting commands pass.
- Tool gating: CAM allowlist; blocked tools error; **drift test** that every
  registered tool AND every MCP resource is classified (allowed/blocked).
- MCP resource gating (6.12): `schema` entity list filtered; `tenancy-context` and
  `recent-queries` blocked; `query-templates` **allowed** (shared query text);
  static resources allowed; `list_resources` advertises only permitted resources.
- Catalog (6.11): `get_query_examples` includes builtin + personal + **shared**
  query text (shared text is shareable); legacy learned-query migration kept (text
  only); legacy shared-**report** import skipped (result content); shared-state
  auto-capture suppressed.
- Delivery destination lock-down (6.6): in CAM mode, reject arbitrary destination
  overrides - explicit cases: `send_to_telegram.chat_id`,
  `deliver_report.recipients.telegram_chat_id`,
  `deliver_report.recipients.email_topic_ocid`, and any non-preapproved
  notification topic.
- Fail-closed startup: unknown CAM -> refuse; empty `customers` -> refuse; zero
  resolved entities -> refuse; OCI unreachable / client-init failure at startup
  -> process exits non-zero (overrides the swallow-and-continue path).
- `list_entities` filtering; troubleshooting tools such as `pivot_on_entity` and
  `investigate_incident` blocked for CAMs.
- `run_saved_search` fetches the saved query and executes the scoped effective
  query, never the raw saved query.
- Client-level compartment/namespace pinning ignores `compartment_id` (incl.
  `_list_log_sources` and per-query batch overrides) and `scope=tenancy`.
- Audit/cache: access audit records the effective scoped query; cache key
  includes profile/user id.
- Delivery gating: global default and per-CAM override.
- **General-user invariance:** without `--enforce-access`, behavior is unchanged
  (tools, resources, catalog, startup all identical to today).

Integration:
- Simulated CAM session end-to-end: filtered `list_entities`, scoped query,
  blocked tool rejected, blocked resource rejected, scope-change disabled,
  shared catalog excluded, delivery gating.

Deployment verification (documented, run against the Assurance compartment):
- Confirm `Entity` (`mtgt`) is populated with `<number>_<name>` values where the
  real data lives, and a scoped query returns only the assigned entities.
- Confirm the deployed build matches the repo (the VM is currently behind - see
  Section 9): `list_fields(source_name=...)` must not error with `unknown kwargs`.

## 11. Out of scope / future

- Streamable HTTP + per-request identity. This would require redesigning the
  per-user profile system (today keyed per process via `--user`) to be
  request-scoped, plus TLS and a token store. Deferred.
- Per-customer OCI compartments.
