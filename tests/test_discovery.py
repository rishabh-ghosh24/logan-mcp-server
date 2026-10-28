"""Keyword discovery: users say "VCN flow logs", not the exact source name (RQ-10)."""
import pytest

from oci_logan_mcp.discovery import compact_row, envelope, filter_rows, rank_sources, tokens

SOURCES = [
    {"display_name": "OCI VCN Flow Unified Schema Logs", "name": "ociVcnFlowUnifiedSchemaLogs",
     "description": "Virtual Cloud Network flow records in the unified schema."},
    {"display_name": "OCI Audit Logs", "name": "ociAuditLogs", "description": "Audit events for the tenancy."},
    {"display_name": "Kubernetes Control Plane Logs", "name": "ociOKEControlPlane",
     "description": "Control plane components of OKE clusters."},
    {"display_name": "OCI PostgreSQL Database Logs", "name": "ociPostgreSQLLogs", "description": ""},
    {"display_name": "Apache Webserver Access Logs", "name": "apacheWebserverAccess", "description": "HTTP access log."},
    {"display_name": "Linux Syslog Logs", "name": "omc_linux_syslog", "description": "Standard syslog messages."},
    {"display_name": "OCI Load Balancer Access Logs", "name": "ociLBAccessLogs", "description": ""},
    {"display_name": "EBS Concurrent Manager Logs", "name": "ebsConcurrentManager", "description": "E-Business Suite."},
    {"display_name": "Oracle Alert Logs", "name": "omc_oracle_alert", "description": "Alerts from the database instance."},
]
VOLUME = {"OCI VCN Flow Unified Schema Logs": 1_876_384, "OCI Audit Logs": 1_375_109,
          "Kubernetes Control Plane Logs": 12_000, "OCI PostgreSQL Database Logs": 300}


def top(keyword, volume=VOLUME):
    return [row["display_name"] for row in rank_sources(SOURCES, keyword, volume)]


class TestTokens:
    def test_camel_case_is_split_and_the_unsplit_word_is_kept(self):
        assert {"ocipostgresqllogs", "sql"} <= set(tokens("ociPostgreSQLLogs"))  # unsplit word kept; SQLLogs split
        assert {"kubernetes", "control", "plane"} <= set(tokens("ociOKEControlPlane"))  # oke is aliased

    def test_stop_words_are_dropped_and_aliases_expanded(self):
        assert tokens("the OCI log service logs") == []
        assert tokens("k8s") == ["kubernetes"] and tokens("oke") == ["kubernetes"]
        assert tokens("pg") == ["postgresql"] and tokens("lb") == ["load", "balancer"]


class TestRankSources:
    @pytest.mark.parametrize("phrase, expected", [
        ("VCN flow logs", "OCI VCN Flow Unified Schema Logs"),
        ("audit", "OCI Audit Logs"),
        ("kubernetes", "Kubernetes Control Plane Logs"),
        ("k8s control plane", "Kubernetes Control Plane Logs"),
        ("postgres", "OCI PostgreSQL Database Logs"),
        ("linux syslog", "Linux Syslog Logs"),
        ("load balancer", "OCI Load Balancer Access Logs"),
        ("flow", "OCI VCN Flow Unified Schema Logs"),
    ])
    def test_the_verified_phrases_rank_the_right_source_first(self, phrase, expected):
        assert top(phrase)[0] == expected

    def test_short_tokens_never_match_inside_longer_words(self):
        assert top("ebs") == ["EBS Concurrent Manager Logs"]

    def test_sources_with_records_in_the_window_come_first(self):
        # "logs" is a stop word, so "database" is the only token: PostgreSQL (has data) before Oracle Alert (description only)
        assert top("database logs") == ["OCI PostgreSQL Database Logs", "Oracle Alert Logs"]

    def test_an_exact_display_name_wins_even_without_data(self):
        assert top("Oracle Alert Logs", volume={"OCI PostgreSQL Database Logs": 5})[0] == "Oracle Alert Logs"

    def test_every_token_must_hit(self):
        assert top("audit tenancy") == ["OCI Audit Logs"]  # both tokens hit one source
        assert top("audit kubernetes") == []

    def test_no_keyword_returns_everything_with_data_first(self):
        names = top("")
        assert len(names) == len(SOURCES)
        assert names[:2] == ["OCI VCN Flow Unified Schema Logs", "OCI Audit Logs"]


class TestFilterRows:
    ENTITIES = [
        {"name": "ms-grafana", "entity_type": "Host (Linux)", "hostname": "ms-grafana.sub.oraclevcn.com"},
        {"name": "prod-db-01", "entity_type": "Oracle Database", "hostname": "db01.sub.oraclevcn.com"},
        {"name": "jf-vm-ashburn", "entity_type": "Host (Linux)", "hostname": "jf-vm-ashburn.sub.oraclevcn.com"},
    ]

    def test_matches_across_the_named_columns_and_sorts_by_name(self):
        rows = filter_rows(self.ENTITIES, "linux host", ("name", "entity_type", "hostname"))
        assert [r["name"] for r in rows] == ["jf-vm-ashburn", "ms-grafana"]

    def test_alias_db_finds_the_database_entity(self):
        assert [r["name"] for r in filter_rows(self.ENTITIES, "db", ("name", "entity_type", "hostname"))] == ["prod-db-01"]

    def test_no_keyword_keeps_the_input_order(self):
        assert filter_rows(self.ENTITIES, None, ("name",)) == self.ENTITIES


class TestEnvelope:
    def test_limit_total_and_truncation(self):
        rows = [{"name": f"s{i}"} for i in range(7)]
        out = envelope("sources", rows, keyword=None, limit=3)
        assert out["sources"] == rows[:3]
        assert (out["total"], out["returned"], out["truncated"]) == (7, 3, True)
        assert "did_you_mean" not in out and out["keyword"] is None

    def test_did_you_mean_when_a_keyword_matches_nothing(self):
        out = envelope("sources", [], keyword="audti", limit=50,
                       names=["OCI Audit Logs", "Linux Syslog Logs"])
        assert out["total"] == 0 and out["did_you_mean"] == ["OCI Audit Logs"]


class TestCompactRow:
    def test_drops_empty_values_and_cuts_long_descriptions(self):
        row = {"name": "x", "description": "d" * 200, "entity_types": [], "hint": None, "data_type": "STRING"}
        out = compact_row(row)
        assert out == {"name": "x", "description": "d" * 157 + "...", "data_type": "STRING"}
