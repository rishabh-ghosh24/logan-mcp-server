# tests/test_access_control.py
"""Tests for the CAM access-control module."""
import textwrap
import pytest

from oci_logan_mcp.access_control import (
    AccessControlConfig,
    AccessConfigError,
    EntityAccessDenied,
    load_access_config,
    validate_cam_id,
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


@pytest.mark.parametrize("body", [
    """
        compartment_id: c
        namespace: ns
        defaults:
          allow_delivery: "false"
        cams:
          cam_alice: { customers: [223] }
    """,
    """
        compartment_id: c
        namespace: ns
        cams:
          cam_alice: { customers: [223], allow_delivery: "false" }
    """,
])
def test_allow_delivery_must_be_boolean(tmp_path, body):
    path = _write(tmp_path, body)
    with pytest.raises(AccessConfigError):
        load_access_config(path)


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


@pytest.mark.parametrize(
    "cam_id",
    ["cam_alice", "firstname.lastname", "john.smith2", "mary-jane.watson", "alice"],
)
def test_validate_cam_id_accepts_supported_ids(cam_id):
    assert validate_cam_id(cam_id) == cam_id


@pytest.mark.parametrize(
    "cam_id",
    [
        "",
        ".alice",
        "alice.",
        "alice..smith",
        "Alice",
        "alice smith",
        "a/../../root",
        "alice;id",
        "alice|id",
        "alice&id",
        "alice$(id)",
        "alice`id`",
        "alice'id",
        'alice"id',
        "alice\nid",
        "-alice",
        "a" * 65,
    ],
)
def test_validate_cam_id_rejects_unsafe_ids(cam_id):
    with pytest.raises(AccessConfigError):
        validate_cam_id(cam_id)


@pytest.mark.parametrize(
    "customers",
    [223, "223", ["223"], [True], [1.5], [0], [-1], {"223": True}],
)
def test_load_access_config_rejects_coerced_customer_shapes(tmp_path, customers):
    import yaml

    path = tmp_path / "access_control.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "compartment_id": "c",
                "namespace": "ns",
                "cams": {"cam_alice": {"customers": customers}},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AccessConfigError, match="list of positive integers"):
        load_access_config(path)


def test_load_access_config_keeps_empty_list_for_runtime_fail_closed_check(tmp_path):
    path = _write(
        tmp_path,
        """
        compartment_id: c
        namespace: ns
        cams:
          cam_empty: { customers: [] }
        """,
    )

    assert load_access_config(path).cams["cam_empty"].customers == ()


@pytest.mark.parametrize(
    "entity_field",
    ["Entity", "Log Source", "entity.name", "Customer-Entity", "field_2"],
)
def test_validate_entity_field_accepts_safe_quoted_identifiers(entity_field):
    from oci_logan_mcp.access_control import validate_entity_field

    assert validate_entity_field(entity_field) == entity_field


@pytest.mark.parametrize(
    "entity_field",
    [
        "",
        " Entity",
        "Entity ",
        "Entity' | stats count",
        "Entity\\name",
        "Entity\nName",
        "a" * 129,
        123,
    ],
)
def test_load_access_config_rejects_unsafe_entity_field(tmp_path, entity_field):
    import yaml

    path = tmp_path / "access_control.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "compartment_id": "c",
                "namespace": "ns",
                "entity_field": entity_field,
                "cams": {},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AccessConfigError, match="entity_field"):
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


@pytest.mark.parametrize("query", [
    "Entity = '999_other' | stats count",
    "'Entity' = \"999_other\" | stats count",
    "Entity in ('223_d360_silicone', '999_other') | stats count",
    "* | where entityname = '999_other' | stats count",
    "* | where entityname in ('223_d360_silicone', '999_other') | stats count",
])
def test_scope_query_explicitly_denies_unallocated_entity(query):
    with pytest.raises(EntityAccessDenied, match="do not have access"):
        scope_query(query, ENTS, "Entity")


def test_scope_query_allows_explicitly_allocated_entity():
    out = scope_query("Entity = '223_d360_silicone' | stats count", ENTS, "Entity")
    assert out.startswith("'Entity' in (")
    assert "and (Entity = '223_d360_silicone')" in out


def test_scope_query_allows_allocated_entityname_where_filter():
    out = scope_query(
        "* | where entityname = '223_d360_silicone' | stats count",
        ENTS,
        "Entity",
    )
    assert "where entityname = '223_d360_silicone'" in out


def test_scope_query_does_not_treat_negated_entity_predicate_as_a_request():
    out = scope_query("not Entity = '999_other' | stats count", ENTS, "Entity")
    assert "and (not Entity = '999_other')" in out


def test_scope_query_rejects_unsafe_via_validate():
    with pytest.raises(QueryNotAllowed):
        scope_query("searchlookup table='t'", ENTS, "Entity")


def test_scope_query_rejects_unsafe_entity_field_even_for_direct_call():
    with pytest.raises(QueryNotAllowed, match="entity_field"):
        scope_query("* | stats count", ENTS, "Entity' | stats count")


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


from oci_logan_mcp.access_control import (
    CAM_ALLOWED_TOOLS,
    CAM_CONDITIONAL_TOOLS,
    CAM_BLOCKED_TOOLS,
    CAM_ALLOWED_RESOURCES,
    is_tool_allowed,
    is_resource_allowed,
)
from oci_logan_mcp.tools import get_tools
from oci_logan_mcp.resources import get_resources


def test_tool_sets_partition_the_registry():
    registered = {t["name"] for t in get_tools()}
    classified = CAM_ALLOWED_TOOLS | CAM_CONDITIONAL_TOOLS | CAM_BLOCKED_TOOLS
    # every registered tool is classified exactly once; no stale names
    assert registered - classified == set(), f"unclassified: {registered - classified}"
    assert classified - registered == set(), f"stale: {classified - registered}"
    assert (CAM_ALLOWED_TOOLS & CAM_BLOCKED_TOOLS) == set()
    assert (CAM_ALLOWED_TOOLS & CAM_CONDITIONAL_TOOLS) == set()
    assert (CAM_CONDITIONAL_TOOLS & CAM_BLOCKED_TOOLS) == set()


def test_known_blocked_and_allowed():
    assert "set_compartment" in CAM_BLOCKED_TOOLS
    assert "investigate_incident" in CAM_BLOCKED_TOOLS
    assert "export_transcript" in CAM_BLOCKED_TOOLS
    assert "run_query" in CAM_ALLOWED_TOOLS
    assert "list_entities" in CAM_ALLOWED_TOOLS
    assert "deliver_report" in CAM_CONDITIONAL_TOOLS


def test_is_tool_allowed_respects_delivery_flag(tmp_path):
    prof_yes = build_profile(_cfg(tmp_path), "cam_alice", ALL)            # allow_delivery True
    assert is_tool_allowed(prof_yes, "run_query")
    assert is_tool_allowed(prof_yes, "deliver_report")
    assert not is_tool_allowed(prof_yes, "set_compartment")

    cfg2 = load_access_config(_write(tmp_path, """
        compartment_id: c
        namespace: ns
        cams:
          cam_alice: { customers: [223], allow_delivery: false }
    """))
    prof_no = build_profile(cfg2, "cam_alice", ALL)
    assert not is_tool_allowed(prof_no, "deliver_report")   # gated off


def test_resource_gating():
    assert is_resource_allowed("loganalytics://query-templates")
    assert is_resource_allowed("loganalytics://schema")        # filtered, but readable
    assert not is_resource_allowed("loganalytics://tenancy-context")
    assert not is_resource_allowed("loganalytics://recent-queries")


def test_resource_sets_partition_the_registry():
    from oci_logan_mcp.access_control import CAM_BLOCKED_RESOURCES

    registered = {r["uri"] for r in get_resources()}
    classified = CAM_ALLOWED_RESOURCES | CAM_BLOCKED_RESOURCES
    assert registered - classified == set(), f"unclassified: {registered - classified}"
    assert classified - registered == set(), f"stale: {classified - registered}"
    assert (CAM_ALLOWED_RESOURCES & CAM_BLOCKED_RESOURCES) == set()


from oci_logan_mcp.config import Settings


def test_settings_has_enforce_access_default_false():
    assert Settings().enforce_access is False
