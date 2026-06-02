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
Root-owned, on the VM (default location alongside existing config under the
config dir; path is configurable). Example:

```yaml
# OCI scope CAMs are pinned to (the Assurance environment)
tenancy_id: ocid1.tenancy.oc1..aaaaaaaacwe5cve7esnjg5lkllxdqjagivfsp6hvczk3pqajjlfga3qub4ja
compartment_id: ocid1.compartment.oc1..aaaaaaaaqvparsvna5cozzy65r4xasbecrkdk5mucyhtfu7pktmutd6bwajq
namespace: <assurance4emea-object-storage-namespace>   # fill at deploy time
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
`run_query`. The scope transform is applied here so no path can bypass it.

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
   **rejected** if it contains sub-query brackets (`[` / `]`) or any
   context-opening command. v1 uses an **allowlist of pipeline commands** known to
   operate only on the already-scoped record set (e.g. `stats`, `timestats`,
   `eventstats`, `where`, `eval`, `sort`, `head`, `tail`, `fields`,
   `fieldsummary`, `distinct`, `top`, `bottom`, `rename`, `addfields` **without**
   a sub-query, `classify` as needed) - any command not on the list is rejected.
   The allowlist and the bracket/lookup rejection are validated against the
   Oracle Command Reference. (A full LQL-aware recursive scoper that scopes every
   search context is future work; until then, rejection is the safe default.)
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

This is the complete classification of the live tool registry
(`handlers.py` handler map). The drift test asserts every registered tool appears
in exactly one of these sets; a new tool is blocked until classified.

**Allowed for CAMs (read + report):**
- Scoped data reads (entity-scoped via 6.5; compartment pinned via 6.9):
  `run_query`, `run_batch_queries`, `get_log_summary`, `find_rare_events`,
  `pivot_on_entity` (entity arg also validated, 6.7), `investigate_incident`,
  `investigate_and_generate_report`, `diff_time_windows`, `trace_request_id`.
- Entity listing: `list_entities` (filtered, 6.7).
- Generic metadata (no customer identity leaked): `list_log_groups`,
  `list_log_sources`, `list_fields`, `list_labels`, `list_parsers`.
- Query helpers (must operate on the scoped query / not execute unscoped):
  `validate_query`, `explain_query`, `get_query_examples` (shared excluded, 6.11).
- Reporting / visualization: `visualize`, `export_results`,
  `generate_incident_report`, `get_incident_report`, `list_incident_reports`,
  `get_report_delivery_options`, `prepare_report_delivery`, `export_transcript`
  (own session only).
- Outbound push (gated by `allow_delivery`, 6.8): `deliver_report`,
  `send_to_slack`, `send_to_telegram`.
- Session info: `get_current_context`, `get_session_budget`, `get_preferences`,
  `test_connection`.

**Blocked for CAMs:**
- Scope changes: `set_compartment`, `set_namespace`, `update_tenancy_context`.
- Compartment / topic enumeration (reveal tenancy structure beyond the pin):
  `list_compartments`, `find_compartment`, `list_notification_topics`.
- Cross-customer artifacts (names / stored query text / content may name other
  customers): `list_dashboards`, `list_saved_searches`, `run_saved_search`,
  `related_dashboards_and_searches`, `list_alerts`, `why_did_this_fire`,
  `list_playbooks`, `get_playbook`.
- Cross-source diagnostics not entity-scoped (not needed for capacity reporting;
  could leak other customers' state): `ingestion_health`, `parser_failure_triage`.
- All OCI mutations: `create_alert`, `update_alert`, `delete_alert`,
  `create_saved_search`, `update_saved_search`, `delete_saved_search`,
  `create_dashboard`, `add_dashboard_tile`, `delete_dashboard`,
  `create_log_source_from_sample`.
- State / catalog / secret writes: `save_learned_query`, `remember_preference`,
  `record_investigation`, `delete_playbook`, `setup_confirmation_secret`.

`list_alerts` exposes stored Logan alert queries (`alarm_service.py`),
`why_did_this_fire` fetches arbitrary alarm metadata and runs its stored query
(`alarm_postmortem.py`), and `list_notification_topics` walks compartments
(`client.py`) - all confirmed cross-customer leak vectors, hence blocked.

### 6.7 list_entities filtering and pivot validation
- `list_entities` results are filtered to the CAM's resolved entity set before
  return, so a CAM only ever sees their own entities.
- `pivot_on_entity` validates its `entity` argument is in the resolved set;
  otherwise it is rejected (defense-in-depth and a clear error, in addition to
  6.5's query scoping).

### 6.8 Export vs outbound delivery
- **Export / download** (CSV, HTML, local report artifacts via `export_results`
  and report generation) is always allowed for CAMs - it is their core job and is
  not an external channel.
- **Outbound push** (`send_to_slack`, `send_to_telegram`, `deliver_report`) is
  gated by `allow_delivery`: global `defaults.allow_delivery` (default `true`)
  with an optional per-CAM override. When disabled, these three tools are blocked
  for that CAM.

### 6.9 Compartment / namespace pinning (in the client, not just the resolver)
The `AccessProfile` is attached to `OCILogAnalyticsClient`. For CAMs, namespace
and compartment are pinned **inside every OCI-facing client method**, not only in
the handler-layer `_resolve_scope`. This is required because some paths bypass
`_resolve_scope`: `_list_log_sources` accepts a `compartment_id` override
(`handlers.py`), `run_batch_queries` allows a per-query `compartment_id`
(`query_engine.py`), and notification-topic listing can walk compartments
(`client.py`). In CAM mode the client ignores any caller-supplied
`compartment_id`/namespace and `scope=tenancy`, so no tool argument can redirect
to other data.

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
Shared/promoted query text and shared/legacy report content can name other
customers' entities, so in CAM mode:
- The query catalog excludes shared/promoted entries - **builtin + personal
  only**. `get_query_examples` and any catalog view used by a CAM drop the shared
  source (`catalog.py`).
- Legacy shared-report import into the per-user store is **skipped**
  (`report_store.py` `_import_legacy_shared_reports`), so a CAM never inherits
  another customer's legacy reports.
- `related_dashboards_and_searches` (which loads shared queries and lists all
  dashboards/saved searches, `related_resources.py`) is blocked (also in 6.6).

### 6.12 MCP resource gating (separate protocol surface)
MCP resources are served by `read_resource`/`list_resources` (`server.py`),
which is a **separate path from `handle_tool_call`** and therefore not covered by
tool gating. In CAM mode:
- `loganalytics://schema` (returns `get_full_schema()`, which includes **all
  entities**, `schema_manager.py`) has its entity list filtered to the CAM's
  resolved set.
- `loganalytics://tenancy-context` (persisted entities/compartments,
  `context_manager.py`), `loganalytics://recent-queries` (query history), and
  `loganalytics://query-templates` (shared queries) are **blocked**.
- Static resources `loganalytics://syntax-guide` and
  `loganalytics://reference-docs` remain available.
- `list_resources` advertises only the resources a CAM may read.

Additionally, CAM mode suppresses shared-state auto-capture: `_list_log_sources`
and `_list_fields` write into the shared tenancy context unless suppressed
(`handlers.py`, currently keyed on `read_only`); CAM mode suppresses these writes
too, so CAMs neither read nor pollute shared tenancy context.

## 7. Threat model - what this does and does not protect

Protects (under Layer 1 + Layer 2):
- A CAM reading another customer's data through any MCP tool, **MCP resource**, or
  shared artifact (catalog/saved-search/dashboard/legacy report).
- A CAM widening scope via raw queries (incl. sub-queries/lookups - rejected),
  compartment/namespace switching, entity/compartment enumeration, or pivot.
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

- Object-storage **namespace** for the `assurance4emea` tenancy - needed in
  `access_control.yaml` at deploy time.
- Smoke tests run against the Assurance VM MCP connection, which the user will
  set up when the feature is ready for testing. This run also yields the
  namespace value above. (Instance-principal auth to the tenancy is already in
  place - see Section 8.)

## 10. Testing strategy (TDD)

Unit:
- Entity-number matching: all edge cases incl. `223`/`232`, `2`/`22`/`23`,
  `140`/`1400`, exact-number, multi-entity-per-number, leading-zero rejection.
- Query-scope transform: predicate injection; empty/`*` head; head with
  `and`/`or`; first-pipe split respecting quotes; multi-pipe pipelines;
  adversarial widen attempts (`Entity = 'other'` -> empty); unparseable ->
  reject (never unscoped).
- Query-scope grammar validation (6.5 step 1): reject sub-query brackets `[`/`]`
  and the lookup/view family (`lookup`, `searchlookup`, `createview`, `map`,
  `updatetable`); allowlisted reporting commands pass; reject command not on the
  allowlist.
- Tool gating: CAM allowlist; blocked tools error; **drift test** that every
  registered tool AND every MCP resource is classified (allowed/blocked).
- MCP resource gating (6.12): `schema` entity list filtered; `tenancy-context`,
  `recent-queries`, `query-templates` blocked; static resources allowed;
  `list_resources` advertises only permitted resources.
- Shared-catalog isolation (6.11): `get_query_examples` excludes shared entries;
  legacy shared-report import skipped; shared-state auto-capture suppressed.
- Fail-closed startup: unknown CAM -> refuse; empty `customers` -> refuse; zero
  resolved entities -> refuse; OCI unreachable / client-init failure at startup
  -> process exits non-zero (overrides the swallow-and-continue path).
- `list_entities` filtering; `pivot_on_entity` entity validation.
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

## 11. Out of scope / future

- Streamable HTTP + per-request identity. This would require redesigning the
  per-user profile system (today keyed per process via `--user`) to be
  request-scoped, plus TLS and a token store. Deferred.
- Per-customer OCI compartments.
