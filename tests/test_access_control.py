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
