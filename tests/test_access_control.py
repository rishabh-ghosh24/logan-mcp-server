# tests/test_access_control.py
"""Tests for the CAM access-control module."""
import textwrap
import pytest

from oci_logan_mcp.access_control import (
    AccessControlConfig,
    AccessConfigError,
    load_access_config,
)


def _write(tmp_path, body):
    p = tmp_path / "access_control.yaml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def test_load_minimal_config(tmp_path):
    path = _write(tmp_path, """
        tenancy_id: ocid1.tenancy.oc1..t
        compartment_id: ocid1.compartment.oc1..c
        namespace: ns123
        cams:
          cam_alice: { customers: [223, 66] }
          cam_bob:   { customers: [232], allow_delivery: false }
    """)
    cfg = load_access_config(path)
    assert isinstance(cfg, AccessControlConfig)
    assert cfg.compartment_id == "ocid1.compartment.oc1..c"
    assert cfg.namespace == "ns123"
    assert cfg.entity_field == "Entity"          # default
    assert cfg.default_allow_delivery is True     # default
    assert cfg.cams["cam_alice"].customers == (223, 66)
    assert cfg.cams["cam_alice"].allow_delivery is True   # inherits default
    assert cfg.cams["cam_bob"].allow_delivery is False    # per-cam override


def test_missing_file_raises(tmp_path):
    with pytest.raises(AccessConfigError):
        load_access_config(tmp_path / "nope.yaml")


def test_missing_required_field_raises(tmp_path):
    path = _write(tmp_path, """
        tenancy_id: ocid1.tenancy.oc1..t
        namespace: ns123
        cams: {}
    """)  # no compartment_id
    with pytest.raises(AccessConfigError):
        load_access_config(path)


from oci_logan_mcp.access_control import entity_matches, resolve_entities


def test_entity_matches_anchored():
    # number must be at the start, followed by '_' (or be the whole name)
    assert entity_matches("223_d360_silicone", 223)
    assert entity_matches("66_flsmidth_co_as", 66)
    assert entity_matches("223", 223)            # whole-name match
    # the digits inside the name part are never inspected
    assert not entity_matches("223_d360_silicone", 36)
    assert not entity_matches("223_d360_silicone", 360)
    # prefix collisions are rejected: next char must be '_'
    assert not entity_matches("232_elca", 2)
    assert not entity_matches("223_x", 22)
    assert not entity_matches("1400_indra", 140)
    assert entity_matches("1400_indra", 1400)


def test_resolve_entities_union():
    all_entities = ["223_d360_silicone", "66_flsmidth_co_as", "232_elca", "1400_indra"]
    assert resolve_entities((223, 66), all_entities) == frozenset(
        {"223_d360_silicone", "66_flsmidth_co_as"}
    )
    assert resolve_entities((9999,), all_entities) == frozenset()


from oci_logan_mcp.access_control import AccessProfile, build_profile


def _cfg(tmp_path):
    return load_access_config(_write(tmp_path, """
        tenancy_id: ocid1.tenancy.oc1..t
        compartment_id: ocid1.compartment.oc1..c
        namespace: ns123
        cams:
          cam_alice: { customers: [223, 66] }
          cam_empty: { customers: [] }
    """))


ALL = ["223_d360_silicone", "66_flsmidth_co_as", "232_elca"]


def test_build_profile_resolves(tmp_path):
    prof = build_profile(_cfg(tmp_path), "cam_alice", ALL)
    assert isinstance(prof, AccessProfile)
    assert prof.entity_names == frozenset({"223_d360_silicone", "66_flsmidth_co_as"})
    assert prof.compartment_id == "ocid1.compartment.oc1..c"
    assert prof.namespace == "ns123"
    assert prof.allow_delivery is True


def test_build_profile_unknown_cam_fails_closed(tmp_path):
    with pytest.raises(AccessConfigError):
        build_profile(_cfg(tmp_path), "cam_ghost", ALL)


def test_build_profile_empty_customers_fails_closed(tmp_path):
    with pytest.raises(AccessConfigError):
        build_profile(_cfg(tmp_path), "cam_empty", ALL)


def test_build_profile_zero_resolved_fails_closed(tmp_path):
    # cam_alice's numbers don't match any live entity -> refuse (misconfig/typo)
    with pytest.raises(AccessConfigError):
        build_profile(_cfg(tmp_path), "cam_alice", ["999_other"])


from oci_logan_mcp.access_control import (
    CAM_QUERY_COMMANDS,
    QueryNotAllowed,
    scope_query,
    validate_cam_query,
)

ENTS = frozenset({"223_d360_silicone", "66_flsmidth_co_as"})


def test_validate_accepts_plain_reporting_query():
    validate_cam_query("* | stats count by 'Log Source' | sort -count | head 10")
    validate_cam_query("'Log Source' = 'Assurance_Image' | timestats count")


def test_validate_rejects_brackets_anywhere():
    with pytest.raises(QueryNotAllowed):
        validate_cam_query("* | addfields [ * | stats count ] as c")


def test_validate_rejects_command_form_head():
    # These commands may appear before the first pipe; CAM mode requires the head
    # to be a pure search/filter expression instead.
    for query in [
        "searchlookup table='t' | fields *",
        "lookup table='t'",
        "createview view='v' [ * | stats count ]",
        "map [ * | stats count ]",
        "updatetable table='t' [ * | stats count ]",
        "link Entity",
        "classify Severity",
        "addfields [ * | stats count ] as c",
    ]:
        with pytest.raises(QueryNotAllowed):
            validate_cam_query(query)


def test_validate_rejects_unknown_bare_command_form_head():
    # Fail closed: a bare leading token with command-style arguments is not a
    # field predicate, even if the command is not in our explicit command list.
    with pytest.raises(QueryNotAllowed):
        validate_cam_query("madeupcommand arg=value | stats count")


def test_validate_rejects_non_allowlisted_pipeline_command():
    with pytest.raises(QueryNotAllowed):
        validate_cam_query("* | classify Severity")


def test_scope_query_prepends_predicate():
    out = scope_query("* | stats count", ENTS, "Entity")
    assert out.startswith("'Entity' in (")
    assert "'223_d360_silicone'" in out and "'66_flsmidth_co_as'" in out
    assert out.endswith("| stats count")


def test_scope_query_wraps_non_star_head():
    out = scope_query("'Log Source' = 'X' | stats count", ENTS, "Entity")
    assert " and ('Log Source' = 'X') | stats count" in out


def test_scope_query_widen_attempt_becomes_empty_intersection():
    out = scope_query("Entity = '999_other' | stats count", ENTS, "Entity")
    # the user predicate is ANDed under the allowed-set predicate
    assert out.startswith("'Entity' in (")
    assert "and (Entity = '999_other')" in out


def test_scope_query_rejects_unsafe_via_validate():
    with pytest.raises(QueryNotAllowed):
        scope_query("searchlookup table='t'", ENTS, "Entity")


@pytest.mark.parametrize("bad_head_query", [
    "foo = 1 searchlookup table=x",
    "'Log Source' = 'x'\nlookup table=t",
    "( anything searchlookup",
    "not searchlookup table=x",
    "foo = 1 madeupcommand x",
])
def test_validate_rejects_trailing_command_in_head(bad_head_query):
    with pytest.raises(QueryNotAllowed):
        validate_cam_query(bad_head_query)


@pytest.mark.parametrize("good_head", [
    "*",
    "'Log Source' = 'Assurance_Image'",
    "'Entity' = '223_x'",
    "'Entity' in ('223_x', '66_y')",
    "Severity = error",
    "a = 1 and b = 2",
    "'x' = '1' or 'y' = '2'",
])
def test_validate_accepts_valid_search_heads(good_head):
    validate_cam_query(good_head + " | stats count")


def test_scope_query_head_has_no_command_keyword():
    out = scope_query("'Log Source' = 'X' | stats count", frozenset({"223_x"}), "Entity")
    assert "searchlookup" not in out and "lookup" not in out


def test_quote_value_rejects_unsafe_entity_name():
    with pytest.raises(QueryNotAllowed):
        scope_query("* | stats count", frozenset({"bad') or '1'='1"}), "Entity")
