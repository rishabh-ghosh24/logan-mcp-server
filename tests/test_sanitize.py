# tests/test_sanitize.py
import pytest
from oci_logan_mcp.sanitize import looks_sensitive, sanitize_query_text, sanitize_pattern

class TestLooksSensitive:
    def test_detects_ocid(self):
        assert looks_sensitive("ocid1.compartment.oc1..aaaaaaa123")

    def test_detects_ipv4(self):
        assert looks_sensitive("filter by 192.168.1.100")

    def test_detects_email(self):
        assert looks_sensitive("user@example.com")

    def test_detects_secret_keywords(self):
        assert looks_sensitive("set api_key=abc123")

    def test_clean_text_is_not_sensitive(self):
        assert not looks_sensitive("'Log Source' = 'OCI Audit Logs' | stats count")

class TestSanitizeQueryText:
    def test_redacts_ocids(self):
        result = sanitize_query_text("compartmentId = 'ocid1.compartment.oc1..aaa123'")
        assert "ocid1" not in result
        assert "<resource_ocid>" in result

    def test_redacts_ips(self):
        result = sanitize_query_text("clnthostip = '10.0.1.50'")
        assert "10.0.1.50" not in result
        assert "<ip_address>" in result

    def test_preserves_query_structure(self):
        result = sanitize_query_text("'Log Source' = 'Linux' | stats count by 'Host Name'")
        assert result == "'Log Source' = 'Linux' | stats count by 'Host Name'"

    def test_rejects_queries_with_secrets(self):
        result = sanitize_query_text("password = 'hunter2'")
        assert result is None

class TestSanitizePattern:
    def test_rejects_sensitive_patterns(self):
        assert sanitize_pattern("connect to 192.168.1.1") is None

    def test_keeps_clean_patterns(self):
        assert sanitize_pattern("show top errors by host") == "show top errors by host"

    def test_strips_whitespace(self):
        assert sanitize_pattern("  query logs  ") == "query logs"

    def test_rejects_empty(self):
        assert sanitize_pattern("") is None
        assert sanitize_pattern("   ") is None


class TestNormalizeQueryText:
    def test_normalize_query_text_collapses_whitespace(self):
        from oci_logan_mcp.sanitize import normalize_query_text
        assert normalize_query_text("  * |  stats  count  ") == "* | stats count"
        assert normalize_query_text("'Error'\n|\tstats count") == "'Error' | stats count"

    def test_normalize_query_text_preserves_case(self):
        from oci_logan_mcp.sanitize import normalize_query_text
        assert normalize_query_text("'ERROR' | stats COUNT") == "'ERROR' | stats COUNT"


"""Tests for redact_dict."""

from oci_logan_mcp.sanitize import redact_dict


def test_redact_dict_masks_email_in_value():
    out = redact_dict({"user": "alice@example.com"})
    assert "alice@example.com" not in repr(out)


def test_redact_dict_masks_ocid():
    out = redact_dict({"resource": "ocid1.compartment.oc1..aaaaaaaexample"})
    assert "ocid1.compartment" not in repr(out)


def test_redact_dict_masks_ipv4():
    out = redact_dict({"source": "10.0.0.42"})
    assert "10.0.0.42" not in repr(out)


def test_redact_dict_masks_secretish_key_names():
    out = redact_dict({"password": "hunter2", "api_key": "live-abc", "bearer": "xyz"})
    assert "hunter2" not in repr(out)
    assert "live-abc" not in repr(out)
    assert "xyz" not in repr(out)


def test_redact_dict_recurses_into_nested_structures():
    out = redact_dict({
        "nested": {"email": "bob@example.com"},
        "list": ["ocid1.alarm.oc1..xyz", "harmless"],
    })
    s = repr(out)
    assert "bob@example.com" not in s
    assert "ocid1.alarm" not in s
    assert "harmless" in s


def test_redact_dict_leaves_plain_strings_alone():
    out = redact_dict({"name": "Linux Syslog", "count": 42})
    assert out["name"] == "Linux Syslog"
    assert out["count"] == 42


def test_redact_dict_handles_non_string_values():
    out = redact_dict({"n": None, "b": True, "i": 42, "f": 1.5})
    assert out == {"n": None, "b": True, "i": 42, "f": 1.5}


def test_redact_dict_handles_top_level_list():
    out = redact_dict(["alice@example.com", "ok"])
    assert "alice@example.com" not in repr(out)
    assert "ok" in repr(out)


def test_redact_dict_returns_same_shape():
    inp = {"a": {"b": ["x", "y"]}, "c": 1}
    out = redact_dict(inp)
    assert isinstance(out, dict)
    assert isinstance(out["a"], dict)
    assert isinstance(out["a"]["b"], list)
