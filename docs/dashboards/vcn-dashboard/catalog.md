# VCN Flow Dashboard — Widget Catalog (Phase 1)

**Status:** Awaiting bulk approval before Phase 2.
**Anchor:** `'Log Source' = 'OCI VCN Flow Unified Schema Logs'`
**Tenancy / Compartment:** OCI namespace `axfo51x8x2ap`, default compartment from `get_current_context`. Field census run 2026-05-15.

## Summary

| Metric | Value |
|---|---|
| Total widgets | 24 |
| Categories | 2 (*Performance & Capacity*, *Security & Investigation*) |
| Shape mix | `[Trend]` 11 · `[Top-N]` 9 · `[Alarm]` 4 |
| Visualization types used | 10/10 (`tile` 3 · `area` 1 · `line` 3 · `heatmap` 2 · `histogram` 1 · `pie` 1 · `treemap` 1 · `bar` 4 · `vertical_bar` 1 · `table` 7) |

---

## Field-population census (last 1 h sample, ~621 K flows)

### Confirmed populated → usable in widgets

| Display name | Internal | Coverage | Notes |
|---|---|---|---|
| `Source IP` / `Destination IP` | srcip / destip | 100 % | core 5-tuple |
| `Source Port` / `Destination Port` | srcport / destport | 100 % | |
| `Action` | actn | ~99.96 % | lowercase: `accept` / `reject` |
| `Protocol (Transport)` | tranprot | 100 % | tcp 192 K · udp 291 K · icmp 124 K · gre 32 · ipv4 3 · ipv6 10 |
| `Protocol Number` | protnum | 100 % | numeric form |
| `Content Size Out` | contszout | 100 % | sole bytes field — `Content Size In` is null |
| `Packets In` | pktsin | 100 % | sole packets field — `Packets Out` is null |
| `OCI Subnet OCID` | ocisubnetocid | 100 % | subnet group key |
| `OCI Resource OCID` / `Resource Name` / `Resource Type` | ocirsrc* | 100 % | VNIC identity |
| `OCI Parent Resource OCID` / `Parent Resource Type` | ociparentrsrc* | 100 % | **VCN OCID** + type |
| `Client Host Country` / `City` / `Country Code` / `Continent` | countryclnt / cityclnt / countrycodeclnt / continentclnt | ~41 % | geo of external side; apply `!= null` filter |

### Confirmed null in this tenancy → in Negative Requirements

`Content Size In`, `Packets Out`, `Network Protocol`, `Interface`, all `NAT *` (7 fields), `ICMP Type/Code/Identifier/Sequence`, `Packets Dropped (…)` (4 reasons), `Source/Destination Resource`, `Client ASN`, `Client IP Class`, `Host IP Address (Client/Server)`, `OCI Compartment OCID` (in record), `Compartment Name`.

---

## Negative requirements

- No widgets relying on the null fields listed above.
- No definitive protocol claims beyond TCP/UDP/ICMP/GRE/IPv4-encap/IPv6-encap (the only `Protocol (Transport)` values observed). Port-derived service identification is a **hint**, not proof.
- No friendly VCN/subnet names — only OCIDs available. HTML doc will note OCID→name mapping is left to the operator.
- Reused learned query: `top_10_oci_vcn_flow_unified_schema_lo_count_v2` (beaconing pattern, low CV on `contszout`) — cited where adapted.

---

## Parameter defaults

| Parameter | Value | Used by |
|---|---|---|
| Port-scan threshold (distinct dest ports per src) | 20 | W14 |
| Sensitive-port list | 22, 3389, 3306, 1433, 5432, 6379, 27017, 9200, 5900 | W15, W20 |
| Beaconing thresholds | `Flows ≥ 20 ∧ CV < 0.15` | W16 (reused from learned query) |
| Top-N defaults | 20 (bar/table) · 50 (conversation pairs) · 30 (treemap) | W9, W12, W13, W18, W21, W22 |
| Saved-search prefix | `RG | VCN Flow | <Category> | <Name>` | all |

---

## Category 1 — Performance & Capacity (14 widgets)

The “is everything OK, what’s busy, what’s changing” lens. Health tiles, traffic shape, protocol mix, top talkers, topology.

### Sub-grouping: At-a-Glance Tiles

#### W1 — Total Flows (Δ%)

- **Shape:** `[Trend]` · **Viz:** `tile` · **Default window:** `last_1_hour` vs prior 1 h
- **Fields needed:** `Action` (used as a count anchor)
- **Purpose:** Headline flow-volume number with % change vs the preceding equal-length window.
- **Tells you:** Whether overall network activity is up, down, or steady.
- **Action it drives:** A sudden ±50 % shift = investigate before the next stand-up — traffic outage, attack, deployment, or new workload rollout.
- **Notes:** Two `run_query` calls (current + prior period), compute Δ% client-side or via a single `link span` query. No counter-test (trend shape).

#### W2 — Total Bytes Out (Δ%)

- **Shape:** `[Trend]` · **Viz:** `tile` · **Default window:** `last_1_hour` vs prior 1 h
- **Fields needed:** `Content Size Out`
- **Purpose:** Total bytes leaving VNICs in this window, with delta.
- **Tells you:** Bandwidth posture + first-pass exfil canary.
- **Action it drives:** Spike in bytes without matching flow-count spike = larger transfers per flow = bulk transfer or exfil. Investigate W17 (egress to public).
- **Notes:** `Content Size In` is null in this tenancy — only “out” direction is measurable.

#### W3 — Reject Rate %

- **Shape:** `[Trend]` · **Viz:** `tile` · **Default window:** `last_1_hour` vs prior 1 h
- **Fields needed:** `Action`
- **Purpose:** `rejects / total * 100`, with Δ vs prior 1 h.
- **Tells you:** Whether denies are climbing relative to total traffic.
- **Action it drives:** Single largest leading indicator for probing, NSG misconfig, or app-level RST loops. Drill into W11–W13 when it climbs.
- **Notes:** Lives in P&C (not Security) because it sits with the other at-a-glance tiles and is the security drill-down entry point, not the conclusion.

### Sub-grouping: Traffic Trends

#### W4 — Flow Volume Over Time — Accept vs Reject

- **Shape:** `[Trend]` · **Viz:** `area` (stacked) · **Default window:** `last_24_hours`
- **Fields needed:** `Time`, `Action`
- **Purpose:** Stacked time-series splitting flows by `Action`.
- **Tells you:** When a reject surge began and whether accepted volume moved in lockstep.
- **Action it drives:** Pin the start of a reject spike to a specific minute; cross-reference with deploy timeline.
- **Notes:** `timestats count by Action` with span derived from window (15-minute buckets for 24 h).

#### W5 — Bytes Over Time with Anomaly Bands

- **Shape:** `[Trend]` · **Viz:** `line` + `classify Anomalies` · **Default window:** `last_24_hours`
- **Fields needed:** `Time`, `Content Size Out`
- **Purpose:** Throughput trend with statistical anomaly bands.
- **Tells you:** “Is this normal?” without needing a separate baseline doc.
- **Action it drives:** Anomaly-flagged interval = pivot to W17 (egress to public) and W21 (top senders).
- **Notes:** Use `link span=15minute Time | stats sum(contszout) as Bytes | classify Bytes as Anomalies`. Pattern from learned query `oci_vcn_flow_unified_schema_lo_sum_v2`.

#### W6 — Activity Heatmap — Hour × Day-of-Week

- **Shape:** `[Trend]` · **Viz:** `heatmap` · **Default window:** `last_7_days`
- **Fields needed:** `Time`
- **Purpose:** Flow count bucketed by hour-of-day × day-of-week.
- **Tells you:** Establishes the “normal business shape”; bright cells in off-hours = unexpected activity.
- **Action it drives:** A bright 3 AM Sunday cell on a prod subnet → investigate scheduled jobs or compromise.
- **Notes:** May require derived time fields via `eventstats` or `timestats span=1h` + post-grouping.

#### W7 — Flow Size Distribution

- **Shape:** `[Trend]` · **Viz:** `histogram` · **Default window:** `last_24_hours`
- **Fields needed:** `Content Size Out`
- **Purpose:** Histogram of bytes-per-flow.
- **Tells you:** Tiny flows (probes/keepalives) vs huge flows (bulk transfer) — shape shift = workload change.
- **Action it drives:** New mode appearing in the histogram = investigate the introducing source IP via W21.
- **Notes:** Bucket sizing tunable.

### Sub-grouping: Protocol & Service Mix

#### W8 — Protocol Mix (TCP / UDP / ICMP / Other)

- **Shape:** `[Trend]` · **Viz:** `pie` · **Default window:** `last_24_hours`
- **Fields needed:** `Protocol (Transport)`
- **Purpose:** Share of flows by transport protocol.
- **Tells you:** Sudden ICMP swell = ping flood or recon; UDP swell = DNS amp or media reflux.
- **Action it drives:** Protocol-share shift triggers drill into W10 (ICMP) or sensitive ports per protocol.
- **Notes:** Field confirmed 100 % populated. Pie justified because TCP/UDP/ICMP cover >99 % — slice noise minimal.

#### W9 — Top Destination Ports by Bytes

- **Shape:** `[Top-N]` · **Viz:** `treemap` · **Default window:** `last_24_hours`
- **Fields needed:** `Destination Port`, `Content Size Out`
- **Purpose:** Top 30 destination ports sized by bytes — visual “service map.”
- **Tells you:** Where your traffic terminates; spot service drift (unexpected 6379 / 11211 / 27017 = misconfig or exfil).
- **Action it drives:** New large tile = investigate src→dst pair via W22.
- **Notes:** Top 30 + “Other” bucket.

#### W10 — ICMP Volume Trend

- **Shape:** `[Trend]` · **Viz:** `line` + `classify Anomalies` · **Default window:** `last_24_hours`
- **Fields needed:** `Time`, `Protocol (Transport)`
- **Purpose:** ICMP flow count over time, anomaly-classified.
- **Tells you:** Storm detection — sustained jump = ping flood or path-MTU thrashing.
- **Action it drives:** Anomaly band breach = identify source via `where tranprot='icmp' | stats count by srcip`.
- **Notes:** ICMP detail fields (`Type`, `Code`) are null in this tenancy — only volume is measurable.

### Sub-grouping: Top Talkers & Topology

#### W21 — Top 20 Source IPs by Bytes Out

- **Shape:** `[Top-N]` · **Viz:** `bar` · **Default window:** `last_24_hours`
- **Fields needed:** `Source IP`, `Content Size Out`
- **Purpose:** Heaviest senders.
- **Tells you:** Capacity hogs, runaway processes, unexpected senders.
- **Action it drives:** Unfamiliar top source = trace back via instance ID; familiar top source with surprise volume = capacity planning input.
- **Notes:** `sum(contszout) | sort -Bytes | head 20`.

#### W22 — Top Conversation Pairs (src:port → dst:port)

- **Shape:** `[Top-N]` · **Viz:** `table` · **Default window:** `last_24_hours`
- **Fields needed:** `Source IP`, `Source Port`, `Destination IP`, `Destination Port`, `Content Size Out`
- **Purpose:** Top 50 src+dst+ports pairs by bytes and flow count.
- **Tells you:** App-dependency map; baseline for change detection.
- **Action it drives:** New conversation pair = investigate. Disappeared expected pair = service down.
- **Notes:** Group on all four key fields; sum bytes + count flows; sort by bytes.

#### W23 — Top Subnets by Flow Volume

- **Shape:** `[Top-N]` · **Viz:** `vertical_bar` · **Default window:** `last_24_hours`
- **Fields needed:** `OCI Subnet OCID`
- **Purpose:** Per-subnet flow count, ranked.
- **Tells you:** Which subnets carry the most traffic; identify hot subnets for right-sizing.
- **Action it drives:** Cost / capacity input; also where reject storms tend to cluster.
- **Notes:** Display will show OCIDs. HTML doc will note OCID→subnet-name mapping is left to operator.

#### W24 — VCN Activity Summary

- **Shape:** `[Top-N]` · **Viz:** `table` · **Default window:** `last_1_hour`
- **Fields needed:** `OCI Parent Resource OCID` (= VCN OCID), `Action`, `Content Size Out`
- **Purpose:** Per-VCN roll-up: flows, bytes, reject %.
- **Tells you:** One-row-per-VCN executive view — which VCNs are hot, which are problematic.
- **Action it drives:** High reject % on a single VCN = NSG audit; high flows + low reject = capacity / cost.
- **Notes:** Sort by flows descending.

---

## Category 2 — Security & Investigation (10 widgets)

The “what’s hostile, what’s blocked, what’s suspicious” lens. Rejection signals, threat patterns, geo exposure.

### Sub-grouping: Reject Analysis

#### W11 — Rejected Traffic Trend with Anomalies

- **Shape:** `[Trend]` · **Viz:** `line` + `classify Anomalies` · **Default window:** `last_24_hours`
- **Fields needed:** `Time`, `Action`
- **Purpose:** Reject count per interval with statistical anomaly bands.
- **Tells you:** Are we under probing right now?
- **Action it drives:** Anomaly cell = open W12/W13 to identify source and target.
- **Notes:** `where Action='reject' | link span=15minute Time | stats count as Rejects | classify Rejects as Anomalies`.

#### W12 — Top 20 Rejected Source IPs

- **Shape:** `[Top-N]` · **Viz:** `bar` (horizontal) · **Default window:** `last_24_hours`
- **Fields needed:** `Source IP`, `Action`
- **Purpose:** Source IPs causing the most denies.
- **Tells you:** Candidates for NSG blocklist or threat-intel lookup.
- **Action it drives:** Foreign IP with 1000+ denies = block at edge.
- **Notes:** `where Action='reject' | stats count as Denies by srcip | sort -Denies | head 20`.

#### W13 — Top 20 Rejected Destination Ports

- **Shape:** `[Top-N]` · **Viz:** `bar` · **Default window:** `last_24_hours`
- **Fields needed:** `Destination Port`, `Action`
- **Purpose:** Ports drawing the most denied attempts.
- **Tells you:** What attackers (or misconfigured clients) are trying to reach.
- **Action it drives:** Validates that closed ports stay closed; informs NSG hardening priority.

### Sub-grouping: Threat Patterns

#### W14 — Port Scan Suspects (1 src → many dst ports)

- **Shape:** `[Alarm]` · **Viz:** `table` · **Default window:** `last_1_hour`
- **Fields needed:** `Source IP`, `Destination Port`
- **Purpose:** Source IPs hitting > 20 distinct destination ports in the window.
- **Tells you:** Horizontal scan in progress.
- **Action it drives:** Block at edge; tag for IR.
- **Counter-test:** Same query with threshold `> 1` distinct port instead of `> 20`. Should return many rows when the data path works.
- **Notes:** `stats distinctcount(destport) as Ports by srcip | where Ports > 20 | sort -Ports`. Threshold tunable.

#### W15 — Brute-Force Suspects on Sensitive Ports

- **Shape:** `[Alarm]` · **Viz:** `table` · **Default window:** `last_24_hours`
- **Fields needed:** `Source IP`, `Destination IP`, `Destination Port`, `Action`
- **Purpose:** src→dst pairs with high reject counts on sensitive ports (22, 3389, 3306, 1433, 5432, 6379, 27017, 9200, 5900).
- **Tells you:** SSH / RDP / DB brute force.
- **Action it drives:** Tag for IR; rotate creds; block source.
- **Counter-test:** Drop the `Action='reject'` filter (any action on sensitive ports). Should return rows showing baseline traffic to those ports.
- **Notes:** Filter sensitive ports via `destport in (…)`.

#### W16 — Beaconing / C2 Suspects (low CV bytes)

- **Shape:** `[Alarm]` · **Viz:** `table` · **Default window:** `last_24_hours`
- **Fields needed:** `Source IP`, `Destination IP`, `Destination Port`, `Content Size Out`
- **Purpose:** src→dst pairs with stable byte sizes across many flows — classic beaconing signature.
- **Tells you:** Highest-fidelity C2 / exfil indicator available without full PCAP.
- **Action it drives:** IR investigation on the source.
- **Counter-test:** Loosen to `Flows >= 2 ∧ CV < 5` (effectively unrestricted). Should return many rows from any repeated conversation.
- **Notes:** **Reuses** `top_10_oci_vcn_flow_unified_schema_lo_count_v2` (learned query) verbatim — `Flows>=20 ∧ CV<0.15`. Adapt only the alias names.

#### W17 — Egress to Public IPs — Top Talkers

- **Shape:** `[Top-N]` · **Viz:** `table` · **Default window:** `last_24_hours`
- **Fields needed:** `Source IP`, `Destination IP`, `Content Size Out`, `Action`
- **Purpose:** RFC1918 source → public destination, ranked by bytes out.
- **Tells you:** First-pass exfiltration board.
- **Action it drives:** Unexpected high-volume egress = investigate process on the source host.
- **Notes:** Filter `srcip` to RFC1918 ranges via regex (`10\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.`); filter `destip` to non-RFC1918. Use `Action='accept'` only — denied egress isn't exfil.

### Sub-grouping: Geo & External Exposure

#### W18 — Top External Source Countries

- **Shape:** `[Top-N]` · **Viz:** `bar` · **Default window:** `last_24_hours`
- **Fields needed:** `Client Host Country`
- **Purpose:** Country breakdown of flows where geo is known (i.e., where one side is a public IP).
- **Tells you:** Geographic posture — "should we be talking to that country at all?"
- **Action it drives:** Unexpected country in top 5 = compliance / threat-model review.
- **Notes:** Filter `'Client Host Country' != null` to remove internal flows.

#### W19 — Geo Activity Heatmap — Country × Time

- **Shape:** `[Trend]` · **Viz:** `heatmap` · **Default window:** `last_7_days`
- **Fields needed:** `Client Host Country`, `Time`
- **Purpose:** Country × time-bucket heatmap.
- **Tells you:** Bursts from a specific country at specific times without flipping filters.
- **Action it drives:** A single-hour spike from RU/CN/KP = IR pivot.
- **Notes:** Top 15 countries + Other bucket to keep the heatmap readable.

#### W20 — Sensitive-Port Hits by Country

- **Shape:** `[Alarm]` · **Viz:** `table` · **Default window:** `last_24_hours`
- **Fields needed:** `Client Host Country`, `Source IP`, `Destination Port`, `Action`
- **Purpose:** Foreign-country sources touching sensitive ports (22, 3389, 3306, etc.), accept or reject.
- **Tells you:** Highest-priority IR triage table — who from where is touching admin ports.
- **Action it drives:** Any row = candidate to block at edge; investigate whether the user/service should be reaching from that country.
- **Counter-test:** Drop the sensitive-port filter — any foreign destination port. Should return rows showing baseline international traffic.
- **Notes:** Combines geo + sensitive port list.

---

## Awaiting bulk approval

Per the engagement contract, one **"Approved, go"** covers all 24 widgets and unlocks Phase 2. Reply with:

- **"Approved, go"** — Phase 2 begins.
- **"Approved with changes: drop / rename / merge X, Y, Z"** — I apply edits, re-confirm once.
- **"Rethink X"** — I revise that widget.
