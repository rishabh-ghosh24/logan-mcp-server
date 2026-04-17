"""Tests for catalog module."""

from pathlib import Path

import yaml

from oci_logan_mcp.catalog import CatalogEntry, SourceType, UnifiedCatalog
from oci_logan_mcp.resources import get_query_templates
from oci_logan_mcp.user_store import UserStore


def test_catalog_loads_builtin_and_starter(tmp_path):
    """UnifiedCatalog loads builtin and starter entries."""
    catalog = UnifiedCatalog(base_dir=tmp_path)

    entries = catalog.load_builtins()
    assert len(entries) > 0
    assert all(e.source == SourceType.BUILTIN for e in entries)

    starters = catalog.load_starters()
    assert len(starters) > 0
    assert all(e.source == SourceType.STARTER for e in starters)


def test_catalog_entry_minimum_fields():
    """CatalogEntry constructs with minimum required fields."""
    entry = CatalogEntry(
        entry_id="abc-123",
        name="errors_last_hour",
        query="'Error' | timestats count",
        description="find errors",
        source=SourceType.BUILTIN,
    )
    assert entry.entry_id == "abc-123"
    assert entry.source == SourceType.BUILTIN
    assert entry.tags == []  # default


def test_source_type_values():
    """SourceType enum has correct values."""
    assert SourceType.BUILTIN.value == "builtin"
    assert SourceType.STARTER.value == "starter"
    assert SourceType.PERSONAL.value == "personal"
    assert SourceType.SHARED.value == "shared"


def test_get_query_templates_reads_yaml():
    """get_query_templates() reads from YAML and returns correct structure."""
    result = get_query_templates()

    # Check overall structure
    assert isinstance(result, dict)
    assert "templates" in result
    assert isinstance(result["templates"], list)
    assert len(result["templates"]) == 13

    # Check that key templates exist by name
    names = {t["name"] for t in result["templates"]}
    assert "errors_last_hour" in names
    assert "top_n_by_field" in names
    assert "trend_over_time" in names
    assert "search_keyword" in names
    assert "filter_by_severity" in names
    assert "error_by_host" in names
    assert "recent_logs" in names
    assert "log_volume_by_source" in names
    assert "top_sources_with_alias" in names
    assert "volume_trend_by_source" in names
    assert "audit_status_breakdown" in names
    assert "total_log_volume_kpi" in names
    assert "source_entity_heatmap" in names

    # Verify each template has required fields
    for template in result["templates"]:
        assert "name" in template
        assert "description" in template
        assert "query" in template
        assert isinstance(template["query"], str)
        assert len(template["query"]) > 0

    # Byte-exact regression guard for at least one template
    errors = next(t for t in result["templates"] if t["name"] == "errors_last_hour")
    assert errors["query"] == "'Error' or 'Critical' | timestats span = 1hour count by 'Log Source'"


def test_parse_queries_skips_non_dict_entries(tmp_path):
    """_parse_queries skips non-dict entries and logs them."""
    catalog = UnifiedCatalog(base_dir=tmp_path)
    data = {
        "queries": [
            "not a dict",
            {"name": "ok", "query": "*", "description": "d"},
        ]
    }
    entries = catalog._parse_queries(data, SourceType.BUILTIN, origin="test")
    assert len(entries) == 1
    assert entries[0].name == "ok"


def test_parse_queries_skips_entries_missing_required_keys(tmp_path):
    """_parse_queries skips entries missing required keys and logs them."""
    catalog = UnifiedCatalog(base_dir=tmp_path)
    data = {
        "queries": [
            {"name": "no_query", "description": "x"},  # missing 'query'
            {"query": "*", "description": "x"},  # missing 'name'
            {"name": "ok", "query": "*", "description": "d"},
        ]
    }
    entries = catalog._parse_queries(data, SourceType.BUILTIN, origin="test")
    assert len(entries) == 1
    assert entries[0].name == "ok"


def test_parse_queries_coerces_non_list_tags(tmp_path):
    """_parse_queries coerces non-list tags to empty list and logs warning."""
    catalog = UnifiedCatalog(base_dir=tmp_path)
    data = {
        "queries": [
            {
                "name": "bad_tags",
                "query": "*",
                "description": "d",
                "tags": "not-a-list",
            },
        ]
    }
    entries = catalog._parse_queries(data, SourceType.BUILTIN, origin="test")
    assert len(entries) == 1
    assert entries[0].tags == []


def test_catalog_loads_personal_and_shared(tmp_path):
    """UnifiedCatalog.load_personal loads user's learned queries."""
    store = UserStore(base_dir=tmp_path, user_id="alice")
    store.save_query(name="q1", query="* | head 5", description="d", interest_score=3)

    catalog = UnifiedCatalog(base_dir=tmp_path)
    personal = catalog.load_personal(user_id="alice")
    assert any(e.name == "q1" for e in personal)
    assert all(e.source == SourceType.PERSONAL for e in personal)


def test_catalog_loads_shared(tmp_path):
    """UnifiedCatalog.load_shared loads shared promoted queries."""
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    (shared_dir / "promoted_queries.yaml").write_text(
        yaml.dump(
            {
                "queries": [
                    {
                        "name": "shared_q",
                        "query": "* | head 10",
                        "description": "shared",
                    }
                ]
            }
        )
    )
    catalog = UnifiedCatalog(base_dir=tmp_path)
    shared = catalog.load_shared()
    assert len(shared) == 1
    assert shared[0].source == SourceType.SHARED


def test_catalog_returns_empty_for_missing_personal_user(tmp_path):
    """UnifiedCatalog.load_personal returns [] for non-existent user."""
    catalog = UnifiedCatalog(base_dir=tmp_path)
    assert catalog.load_personal(user_id="nonexistent") == []


def test_catalog_personal_entry_includes_metrics(tmp_path):
    """Personal catalog entries include interest_score and other metrics."""
    store = UserStore(base_dir=tmp_path, user_id="bob")
    store.save_query(
        name="q2", query="* | stats count", description="d", interest_score=5
    )
    catalog = UnifiedCatalog(base_dir=tmp_path)
    entries = catalog.load_personal(user_id="bob")
    assert len(entries) == 1
    assert entries[0].interest_score == 5


def test_for_my_queries_personal_wins_over_shared(tmp_path):
    # Seed shared with q1
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    (shared_dir / "promoted_queries.yaml").write_text(yaml.dump({
        "queries": [{"name": "q1", "query": "* shared", "description": "shared version"}]
    }))
    # Seed personal with q1 (different query)
    store = UserStore(base_dir=tmp_path, user_id="alice")
    store.save_query(name="q1", query="* personal", description="personal version")

    catalog = UnifiedCatalog(base_dir=tmp_path)
    merged = catalog.for_my_queries_view(user_id="alice")
    q1 = [e for e in merged if e.name == "q1"]
    assert len(q1) == 1
    assert q1[0].source == SourceType.PERSONAL
    assert q1[0].query == "* personal"


def test_for_templates_resource_excludes_personal(tmp_path):
    store = UserStore(base_dir=tmp_path, user_id="alice")
    store.save_query(name="my_personal_query", query="*", description="d")

    catalog = UnifiedCatalog(base_dir=tmp_path)
    merged = catalog.for_templates_resource()
    assert not any(e.source == SourceType.PERSONAL for e in merged)
    assert not any(e.name == "my_personal_query" for e in merged)
    # Builtin should still be there
    assert any(e.source == SourceType.BUILTIN for e in merged)


def test_for_templates_resource_shared_does_not_shadow_builtin(tmp_path):
    """builtin > shared precedence: if shared has a name colliding with a builtin,
    the builtin wins and the shared entry is dropped."""
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    (shared_dir / "promoted_queries.yaml").write_text(yaml.dump({
        "queries": [{"name": "errors_last_hour", "query": "* shared override",
                     "description": "should not win"}]
    }))
    catalog = UnifiedCatalog(base_dir=tmp_path)
    merged = catalog.for_templates_resource()
    elh = [e for e in merged if e.name.lower() == "errors_last_hour"]
    assert len(elh) == 1
    assert elh[0].source == SourceType.BUILTIN


def test_for_onboarding_returns_starters_only(tmp_path):
    catalog = UnifiedCatalog(base_dir=tmp_path)
    entries = catalog.for_onboarding()
    assert len(entries) > 0
    assert all(e.source == SourceType.STARTER for e in entries)


def test_merge_by_name_case_insensitive(tmp_path):
    """Case-insensitive name collision: personal 'myquery' shadows shared 'MyQuery'."""
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    (shared_dir / "promoted_queries.yaml").write_text(yaml.dump({
        "queries": [{"name": "MyQuery", "query": "* shared", "description": "shared"}]
    }))
    store = UserStore(base_dir=tmp_path, user_id="alice")
    store.save_query(name="myquery", query="* personal", description="personal")

    catalog = UnifiedCatalog(base_dir=tmp_path)
    merged = catalog.for_my_queries_view(user_id="alice")
    # Only one entry should remain (case-insensitive collision)
    matching = [e for e in merged if e.name.lower() == "myquery"]
    assert len(matching) == 1
    assert matching[0].source == SourceType.PERSONAL
