# tests/test_preferences.py
import pytest
from pathlib import Path
from oci_logan_mcp.preferences import PreferenceStore

@pytest.fixture
def store(tmp_path):
    return PreferenceStore(user_dir=tmp_path)

class TestPreferences:
    def test_remember_and_recall(self, store):
        store.remember("postgresql_errors", resolved_value="'Log Source' = 'OCI PostgreSQL Service Logs'")
        pref = store.get("postgresql_errors")
        assert pref is not None
        assert pref["resolved_value"] == "'Log Source' = 'OCI PostgreSQL Service Logs'"

    def test_usage_count_increments(self, store):
        store.remember("postgresql_errors", resolved_value="value1")
        store.remember("postgresql_errors", resolved_value="value1")
        pref = store.get("postgresql_errors")
        assert pref["usage_count"] == 2

    def test_override_with_new_value(self, store):
        store.remember("intent_key", resolved_value="old")
        store.remember("intent_key", resolved_value="new")
        pref = store.get("intent_key")
        assert pref["resolved_value"] == "new"

    def test_list_all(self, store):
        store.remember("k1", resolved_value="v1")
        store.remember("k2", resolved_value="v2")
        all_prefs = store.list_all()
        assert len(all_prefs) == 2

    def test_unknown_key_returns_none(self, store):
        assert store.get("nonexistent") is None

    def test_persists_across_instances(self, tmp_path):
        s1 = PreferenceStore(user_dir=tmp_path)
        s1.remember("key", resolved_value="val")
        s2 = PreferenceStore(user_dir=tmp_path)
        assert s2.get("key") is not None

class TestSourceFieldAffinity:
    def test_track_field_usage(self, store):
        store.track_field_usage("OCI PostgreSQL Service Logs", "usrname")
        store.track_field_usage("OCI PostgreSQL Service Logs", "usrname")
        store.track_field_usage("OCI PostgreSQL Service Logs", "dbname")
        fields = store.get_common_fields("OCI PostgreSQL Service Logs")
        assert "usrname" in fields
        assert "dbname" in fields

    def test_common_fields_sorted_by_frequency(self, store):
        store.track_field_usage("source1", "fieldA")
        store.track_field_usage("source1", "fieldA")
        store.track_field_usage("source1", "fieldA")
        store.track_field_usage("source1", "fieldB")
        fields = store.get_common_fields("source1")
        assert fields[0] == "fieldA"

class TestTimeRangeDefaults:
    def test_track_and_suggest_time_range(self, store):
        store.track_time_range("OCI Audit Logs", "last_7_days")
        store.track_time_range("OCI Audit Logs", "last_7_days")
        store.track_time_range("OCI Audit Logs", "last_24_hours")
        suggested = store.suggest_time_range("OCI Audit Logs")
        assert suggested == "last_7_days"

    def test_unknown_source_returns_none(self, store):
        assert store.suggest_time_range("unknown") is None
