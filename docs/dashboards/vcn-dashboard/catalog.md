# VCN Flow Dashboard — Widget Catalog (Phase 1, rev 2)

**Status:** Awaiting bulk approval before Phase 2.
**Anchor:** `'Log Source' = 'OCI VCN Flow Unified Schema Logs'`
**Tenancy / Compartment:** OCI namespace `axfo51x8x2ap`, default compartment from `get_current_context`. Field census run 2026-05-15.

## Summary

| Metric | Value |
|---|---|
| Total widgets | **25** |
| Categories | 2 (*Performance & Capacity* — 14 · *Security & Investigation* — 11) |
| Shape mix | `[Trend]` 11 · `[Top-N]` 9 · `[Alarm]` 5 |
| Visualization types used | 9/10 (`tile` 4 · `area` 1 · `line` 4 · `heatmap` 2 · `pie` 1 · `treemap` 1 · `bar` 4 · `table` 8 · `histogram` 0 — see note below) |

> **Viz coverage note:** `vertical_bar` not used after the VCN+Subnet merge; `histogram` not used after Flow Size Distribution was consolidated into Bytes/Packet Outliers. Per the engagement contract, viz variety is a preference, not a hard rule — we use what fits the data.

## Revision log (rev 2 vs rev 1)

Per Codex review feedback:

- **Added:** `Capture Quality Status` (W4), `Horizontal Host Sweep Suspects` (W21), `Bytes/Packet Outliers` (W8).
- **Dropped:** *Flow Size Distribution* (subsumed by Bytes/Packet Outliers).
- **Merged:** *Top Subnets by Flow Volume* + *VCN Activity Summary* → single `VCN / Subnet Activity Summary` (W14).
- **Renamed for accuracy:** *Port Scan Suspects* → *Vertical Port Sweep Suspects* (W20); *Top External Source Countries* → *Top External Countries (Geo-Enriched)* (W24); *Sensitive-Port Hits by Country* → *Accepted External Sensitive-Port Hits* (W25), scoped to `Action = accept` only.
- **Tightened:** W23 (Brute-Force) gets explicit threshold; W25 action softened to "investigate / verify before blocking."
- **Renumbered:** W1–W25 in catalog order (was W1–W24 with reordered numbering).

---

## Field-population census (last 1 h sample, ~624 K flows)

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
| `Status` | status | 100 % | **NEW finding** — `ok` 623,545 / `nodata` 261 (~0.04 %) |
| `OCI Subnet OCID` | ocisubnetocid | 100 % | subnet group key |
| `OCI Resource OCID` / `Resource Name` / `Resource Type` | ocirsrc* | 100 % | VNIC identity |
| `OCI Parent Resource OCID` / `Parent Resource Type` | ociparentrsrc* | 100 % | **VCN OCID** + type |
| `Client Host Country` / `City` / `Country Code` / `Continent` | countryclnt / cityclnt / countrycodeclnt / continentclnt | ~41 % | geo of public-IP side; direction-agnostic |

### Confirmed null in this tenancy → in Negative Requirements

`Content Size In`, `Packets Out`, `Network Protocol`, `Interface`, all `NAT *` (7 fields), `ICMP Type/Code/Identifier/Sequence`, `Packets Dropped (…)` (4 reasons), `Source/Destination Resource`, `Client ASN`, `Client IP Class`, `Host IP Address (Client/Server)`, `OCI Compartment OCID` (in record), `Compartment Name`.

---

## Negative requirements

- No widgets relying on the null fields listed above.
- No definitive protocol claims beyond TCP/UDP/ICMP/GRE/IPv4-encap/IPv6-encap (the only `Protocol (Transport)` values observed). Port-derived service identification is a **hint**, not proof.
- No directional claims on `Client Host Country` widgets — the field enriches whichever side is public; we cannot assume source vs destination without explicit `srcip` / `destip` RFC1918 classification.
- No friendly VCN/subnet/VNIC names — only OCIDs available. HTML doc will note OCID→name mapping is left to operator.
- Reused learned query: `top_10_oci_vcn_flow_unified_schema_lo_count_v2` (beaconing pattern, low CV on `contszout`) — cited where adapted.

---

## Parameter defaults

| Parameter | Value | Used by |
|---|---|---|
| Port-sweep threshold (distinct dest ports per src) | 20 | W20 |
| Host-sweep threshold (distinct dest IPs per src, same port) | 20 | W21 |
| Brute-force threshold (rejects per src→dst→port) | 10 | W23 |
| Sensitive-port list | 22, 3389, 3306, 1433, 5432, 6379, 27017, 9200, 5900 | W23, W25 |
| Beaconing thresholds | `Flows ≥ 20 ∧ CV < 0.15` | W22 (reused from learned query) |
| Capture-quality thresholds | warn `% ok < 99.9`, critical `% ok < 99.0` | W4 |
| Top-N defaults | 20 (bar/table) · 50 (conversation pairs) · 30 (treemap) | W10, W12, W17, W19, W24 |
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
- **Notes:** Two `run_query` calls (current + prior period), compute Δ% client-side or via a single `link span` query.

#### W2 — Total Bytes Out (Δ%)

- **Shape:** `[Trend]` · **Viz:** `tile` · **Default window:** `last_1_hour` vs prior 1 h
- **Fields needed:** `Content Size Out`
- **Purpose:** Total bytes leaving VNICs in this window, with delta.
- **Tells you:** Bandwidth posture + first-pass exfil canary.
- **Action it drives:** Bytes spike without matching flow-count spike = larger transfers per flow = bulk transfer or exfil. Drill into W19 (egress to public).
- **Notes:** `Content Size In` is null in this tenancy — only “out” direction is measurable.

#### W3 — Reject Rate %

- **Shape:** `[Trend]` · **Viz:** `tile` · **Default window:** `last_1_hour` vs prior 1 h
- **Fields needed:** `Action`
- **Purpose:** `rejects / total * 100`, with Δ vs prior 1 h.
- **Tells you:** Whether denies are climbing relative to total traffic.
- **Action it drives:** Largest leading indicator for probing, NSG misconfig, or app-level RST loops. Drill into W15–W17 when it climbs.
- **Notes:** Lives in P&C (not Security) because it sits with the other at-a-glance tiles and is the security drill-down entry point, not the conclusion.

#### W4 — Capture Quality Status

- **Shape:** `[Trend]` · **Viz:** `tile` · **Default window:** `last_1_hour` vs prior 1 h
- **Fields needed:** `Status`
- **Purpose:** Percentage of flow records with `Status = 'ok'` vs `'nodata'`, with Δ.
- **Tells you:** Whether the flow-log collection itself is healthy. `nodata` means a VNIC was monitored but emitted no flows in that window (legitimate idle, agent issue, or instance gone).
- **Action it drives:**
  - **warn:** `% ok < 99.9` → check for recently terminated/stopped instances.
  - **critical:** `% ok < 99.0` → capture pipeline incident; flow visibility degraded, downstream widgets (W1–W3) may be under-reporting.
- **Notes:** Drill-down query when the tile alerts:
  `'Log Source' = 'OCI VCN Flow Unified Schema Logs' and Status != 'ok' | stats count by ocirsrcname, ocisubnetocid | sort -Count | head 50`.

### Sub-grouping: Traffic Trends

#### W5 — Flow Volume Over Time — Accept vs Reject

- **Shape:** `[Trend]` · **Viz:** `area` (stacked) · **Default window:** `last_24_hours`
- **Fields needed:** `Time`, `Action`
- **Purpose:** Stacked time-series splitting flows by `Action`.
- **Tells you:** When a reject surge began and whether accepted volume moved in lockstep.
- **Action it drives:** Pin the start of a reject spike to a specific minute; cross-reference with deploy timeline.
- **Notes:** `timestats count by Action` with span derived from window (15-minute buckets for 24 h).

#### W6 — Bytes Over Time with Anomaly Bands

- **Shape:** `[Trend]` · **Viz:** `line` + `classify Anomalies` · **Default window:** `last_24_hours`
- **Fields needed:** `Time`, `Content Size Out`
- **Purpose:** Throughput trend with statistical anomaly bands.
- **Tells you:** “Is this normal?” without needing a separate baseline doc.
- **Action it drives:** Anomaly-flagged interval = pivot to W19 (egress to public) and W17 (top senders).
- **Notes:** Use `link span=15minute Time | stats sum(contszout) as Bytes | classify Bytes as Anomalies`. Pattern adapted from learned query `oci_vcn_flow_unified_schema_lo_sum_v2`.

#### W7 — Activity Heatmap — Hour × Day-of-Week

- **Shape:** `[Trend]` · **Viz:** `heatmap` · **Default window:** `last_7_days`
- **Fields needed:** `Time`
- **Purpose:** Flow count bucketed by hour-of-day × day-of-week.
- **Tells you:** Establishes the “normal business shape”; bright cells in off-hours = unexpected activity.
- **Action it drives:** A bright 3 AM Sunday cell on a prod subnet → investigate scheduled jobs or compromise.
- **Notes:** May require derived time fields via `eventstats` or `timestats span=1h` + post-grouping.

#### W8 — Bytes/Packet Outliers

- **Shape:** `[Top-N]` · **Viz:** `table` · **Default window:** `last_24_hours`
- **Fields needed:** `Source IP`, `Destination IP`, `Destination Port`, `Content Size Out`, `Packets In`
- **Purpose:** Flows where the bytes-out-to-packets-in ratio is at the extremes (top decile and bottom decile).
- **Tells you:** Anomalous flow shape — tiny ratios cluster around scans/probes/keepalives; huge ratios cluster around bulk transfers and possibly exfil.
- **Action it drives:** Top decile (large ratio) → cross-check W19 egress board. Bottom decile (tiny ratio) → cross-check W20/W21 scan boards.
- **Notes:** **Directional caveat** — `Content Size Out` is egress from the VNIC; `Packets In` is ingress to it. The ratio is NOT a clean bidirectional packet-size measurement. Useful for outlier detection regardless, but interpret with care. If Phase 2 smoke-test shows the signal is too noisy in this tenancy, apply the skipped-widget rule and document a replacement.

### Sub-grouping: Protocol & Service

#### W9 — Protocol Mix (TCP / UDP / ICMP / Other)

- **Shape:** `[Trend]` · **Viz:** `pie` · **Default window:** `last_24_hours`
- **Fields needed:** `Protocol (Transport)`
- **Purpose:** Share of flows by transport protocol.
- **Tells you:** Sudden ICMP swell = ping flood or recon; UDP swell = DNS amp or media reflux.
- **Action it drives:** Protocol-share shift triggers drill into W11 (ICMP) or sensitive ports per protocol.
- **Notes:** Field confirmed 100 % populated. Pie justified because TCP/UDP/ICMP cover >99 % of values — slice noise minimal.

#### W10 — Top Destination Ports by Bytes

- **Shape:** `[Top-N]` · **Viz:** `treemap` · **Default window:** `last_24_hours`
- **Fields needed:** `Destination Port`, `Content Size Out`
- **Purpose:** Top 30 destination ports sized by bytes — visual “service map.”
- **Tells you:** Where your traffic terminates; spot service drift (unexpected 6379 / 11211 / 27017 = misconfig or exfil).
- **Action it drives:** New large tile = investigate src→dst pair via W13.
- **Notes:** Top 30 + “Other” bucket.

#### W11 — ICMP Volume Trend

- **Shape:** `[Trend]` · **Viz:** `line` + `classify Anomalies` · **Default window:** `last_24_hours`
- **Fields needed:** `Time`, `Protocol (Transport)`
- **Purpose:** ICMP flow count over time, anomaly-classified.
- **Tells you:** Storm detection — sustained jump = ping flood or path-MTU thrashing.
- **Action it drives:** Anomaly band breach = identify source via `where tranprot='icmp' | stats count by srcip`.
- **Notes:** ICMP detail fields (`Type`, `Code`) are null in this tenancy — only volume is measurable.

### Sub-grouping: Top Talkers & Topology

#### W12 — Top 20 Source IPs by Bytes Out

- **Shape:** `[Top-N]` · **Viz:** `bar` · **Default window:** `last_24_hours`
- **Fields needed:** `Source IP`, `Content Size Out`
- **Purpose:** Heaviest senders.
- **Tells you:** Capacity hogs, runaway processes, unexpected senders.
- **Action it drives:** Unfamiliar top source = trace back via instance ID; familiar top source with surprise volume = capacity planning input.
- **Notes:** `sum(contszout) | sort -Bytes | head 20`.

#### W13 — Top Conversation Pairs (src:port → dst:port)

- **Shape:** `[Top-N]` · **Viz:** `table` · **Default window:** `last_24_hours`
- **Fields needed:** `Source IP`, `Source Port`, `Destination IP`, `Destination Port`, `Content Size Out`
- **Purpose:** Top 50 src+dst+ports pairs by bytes and flow count.
- **Tells you:** App-dependency map; baseline for change detection.
- **Action it drives:** New conversation pair = investigate. Disappeared expected pair = service down.
- **Notes:** Group on all four key fields; sum bytes + count flows; sort by bytes.

#### W14 — VCN / Subnet Activity Summary

- **Shape:** `[Top-N]` · **Viz:** `table` · **Default window:** `last_1_hour`
- **Fields needed:** `OCI Parent Resource OCID` (VCN), `OCI Subnet OCID`, `Action`, `Content Size Out`
- **Purpose:** Hierarchical rollup — flows, bytes, reject % grouped by VCN + subnet.
- **Tells you:** Which VCN/subnet pair is hot, and which has the worst reject share.
- **Action it drives:** High reject % on a specific subnet inside a VCN = NSG audit on that subnet; high flows + low reject = capacity / cost.
- **Notes:** Merged from prior W23 (Top Subnets bar) + W24 (VCN Summary table). Display will show OCIDs — operator maps to friendly names. Sort by flows descending; reject % is the secondary signal.

---

## Category 2 — Security & Investigation (11 widgets)

The “what’s hostile, what’s blocked, what’s suspicious” lens. Rejection signals, scan/threat patterns, geo exposure.

### Sub-grouping: Reject Analysis

#### W15 — Rejected Traffic Trend with Anomalies

- **Shape:** `[Trend]` · **Viz:** `line` + `classify Anomalies` · **Default window:** `last_24_hours`
- **Fields needed:** `Time`, `Action`
- **Purpose:** Reject count per interval with statistical anomaly bands.
- **Tells you:** Are we under probing right now?
- **Action it drives:** Anomaly cell = open W16/W17 to identify source and target.
- **Notes:** `where Action='reject' | link span=15minute Time | stats count as Rejects | classify Rejects as Anomalies`.

#### W16 — Top 20 Rejected Source IPs

- **Shape:** `[Top-N]` · **Viz:** `bar` (horizontal) · **Default window:** `last_24_hours`
- **Fields needed:** `Source IP`, `Action`
- **Purpose:** Source IPs causing the most denies.
- **Tells you:** Candidates for NSG blocklist or threat-intel lookup.
- **Action it drives:** Foreign IP with 1000+ denies = candidate for edge block after threat-intel verification.
- **Notes:** `where Action='reject' | stats count as Denies by srcip | sort -Denies | head 20`.

#### W17 — Top 20 Rejected Destination Ports

- **Shape:** `[Top-N]` · **Viz:** `bar` · **Default window:** `last_24_hours`
- **Fields needed:** `Destination Port`, `Action`
- **Purpose:** Ports drawing the most denied attempts.
- **Tells you:** What attackers (or misconfigured clients) are trying to reach.
- **Action it drives:** Validates that closed ports stay closed; informs NSG hardening priority.

### Sub-grouping: Egress & Geo Exposure

#### W18 — Public Egress Top Talkers

- **Shape:** `[Top-N]` · **Viz:** `table` · **Default window:** `last_24_hours`
- **Fields needed:** `Source IP`, `Destination IP`, `Content Size Out`, `Action`
- **Purpose:** RFC1918 source → public destination, ranked by bytes out (accepted only).
- **Tells you:** First-pass exfiltration board — what's the chattiest outbound channel?
- **Action it drives:** Unexpected high-volume egress to a public IP = investigate the process on the source host; verify against approved egress allowlist.
- **Notes:** Filter `srcip` to RFC1918 ranges via regex (`10\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.`); filter `destip` to non-RFC1918. Use `Action='accept'` only — denied egress isn't exfil.

#### W19 — Top External Countries (Geo-Enriched)

- **Shape:** `[Top-N]` · **Viz:** `bar` · **Default window:** `last_24_hours`
- **Fields needed:** `Client Host Country`
- **Purpose:** Country breakdown of flows where geo enrichment fired (one side public).
- **Tells you:** Geographic posture — "what countries appear in our flow data at all?"
- **Action it drives:** Unexpected country in top 5 = compliance review; pivot to W21/W25 to see if those countries are touching sensitive ports.
- **Notes:** Filter `'Client Host Country' != null` to remove internal-only flows. **No directional claim** — the country could be source or destination of any given flow.

#### W20 — Geo Activity Heatmap — Country × Time

- **Shape:** `[Trend]` · **Viz:** `heatmap` · **Default window:** `last_7_days`
- **Fields needed:** `Client Host Country`, `Time`
- **Purpose:** Country × time-bucket heatmap of geo-enriched flow counts.
- **Tells you:** Bursts from a specific country at specific times.
- **Action it drives:** A single-hour spike from an unexpected country = IR pivot — check W19 and W25 for matching activity.
- **Notes:** Top 15 countries + Other bucket. **No directional claim** — same caveat as W19.

### Sub-grouping: Scan & Threat Patterns

#### W21 — Vertical Port Sweep Suspects

- **Shape:** `[Alarm]` · **Viz:** `table` · **Default window:** `last_1_hour`
- **Fields needed:** `Source IP`, `Destination IP`, `Destination Port`
- **Purpose:** Source IPs hitting > 20 distinct destination ports on a single destination IP — classic service-discovery probe ("what's open on this host?").
- **Tells you:** Vertical scan against a specific host.
- **Action it drives:** Investigate the source; if external + unfamiliar, candidate for edge block after threat-intel check.
- **Counter-test:** Same query with threshold `> 1` distinct port. Should return many rows when the data path works.
- **Notes:** `stats distinctcount(destport) as Ports by srcip, destip | where Ports > 20 | sort -Ports`. Threshold tunable.

#### W22 — Horizontal Host Sweep Suspects

- **Shape:** `[Alarm]` · **Viz:** `table` · **Default window:** `last_1_hour`
- **Fields needed:** `Source IP`, `Destination Port`, `Destination IP`
- **Purpose:** Source IPs hitting > 20 distinct destination IPs on the same destination port — classic host-discovery probe ("who's running SSH/RDP on this subnet?").
- **Tells you:** Horizontal scan across the subnet for a specific service.
- **Action it drives:** Often paired with vertical sweep activity (W21). Tag the source for IR and edge-block.
- **Counter-test:** Same query with threshold `> 1` distinct host. Should return many rows.
- **Notes:** `stats distinctcount(destip) as Hosts by srcip, destport | where Hosts > 20 | sort -Hosts`. Threshold tunable.

#### W23 — Brute-Force Suspects on Sensitive Ports

- **Shape:** `[Alarm]` · **Viz:** `table` · **Default window:** `last_24_hours`
- **Fields needed:** `Source IP`, `Destination IP`, `Destination Port`, `Action`
- **Purpose:** src→dst→port triples with **≥ 10 rejects** on sensitive ports (22, 3389, 3306, 1433, 5432, 6379, 27017, 9200, 5900).
- **Tells you:** Sustained SSH / RDP / DB credential-guessing attempts.
- **Action it drives:** Investigate; rotate credentials on the target; block source after verification.
- **Counter-test:** Drop the `Action='reject'` filter (any action on sensitive ports). Should return rows showing baseline authorized traffic to those ports.
- **Notes:** `where Action='reject' and destport in (22, 3389, 3306, 1433, 5432, 6379, 27017, 9200, 5900) | stats count as Rejects by srcip, destip, destport | where Rejects >= 10 | sort -Rejects`. Threshold tunable.

#### W24 — Beaconing / C2 Suspects (low CV bytes)

- **Shape:** `[Alarm]` · **Viz:** `table` · **Default window:** `last_24_hours`
- **Fields needed:** `Source IP`, `Destination IP`, `Destination Port`, `Content Size Out`
- **Purpose:** src→dst pairs with stable byte sizes across many flows — classic beaconing signature.
- **Tells you:** Highest-fidelity C2 / exfil indicator available without full PCAP.
- **Action it drives:** IR investigation on the source.
- **Counter-test:** Loosen to `Flows >= 2 ∧ CV < 5` (effectively unrestricted). Should return many rows from any repeated conversation.
- **Notes:** **Reuses** `top_10_oci_vcn_flow_unified_schema_lo_count_v2` (learned query) verbatim — `Flows >= 20 ∧ CV < 0.15`. Adapt only the alias names.

#### W25 — Accepted External Sensitive-Port Hits

- **Shape:** `[Alarm]` · **Viz:** `table` · **Default window:** `last_24_hours`
- **Fields needed:** `Client Host Country`, `Source IP`, `Destination IP`, `Destination Port`, `Action`
- **Purpose:** **Accepted** (not just attempted) connections from geo-enriched external sources to sensitive destination ports.
- **Tells you:** "Are we actually allowing external access to admin/database ports?" This is the high-stakes complement to W23 — W23 says "they're trying," W25 says "they're succeeding."
- **Action it drives:** **Investigate and verify before blocking** — admin teams may legitimately reach from remote offices or VPNs that geo-enrich to unexpected countries. Confirm the source IP and user before acting.
- **Counter-test:** Drop the sensitive-port filter (any foreign destination port, `Action='accept'`). Should return rows showing baseline accepted international traffic.
- **Notes:** `where 'Client Host Country' != null and Action='accept' and destport in (<sensitive-port-list>) | stats count by countryclnt, srcip, destip, destport | sort -Count`. Empty result = no external access to admin ports = healthy.

---

## Awaiting bulk approval

Per the engagement contract, one **"Approved, go"** covers all 25 widgets and unlocks Phase 2. Reply with:

- **"Approved, go"** — Phase 2 begins.
- **"Approved with changes: drop / rename / merge X, Y, Z"** — I apply edits, re-confirm once.
- **"Rethink X"** — I revise that widget.
