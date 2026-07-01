import base64
import hashlib
import json
import os
import stat
import struct
from pathlib import Path

import pytest

from oci_logan_mcp.cam_admin_store import (
    AdminRequestError,
    CamAdminPaths,
    CamStateStore,
    ConcurrentMutationError,
    DeprovisionRequest,
    ProvisionRequest,
    authorized_key_records,
    build_forced_key_line,
    managed_key_records,
    parse_ed25519_public_key,
    serialize_policy,
    sha256_bytes,
    sha256_file,
)


def _public_key(cam_id="cam_alice", key_bytes=b"k" * 32):
    algorithm = b"ssh-ed25519"
    blob = (
        struct.pack(">I", len(algorithm))
        + algorithm
        + struct.pack(">I", len(key_bytes))
        + key_bytes
    )
    encoded = base64.b64encode(blob).decode("ascii")
    return f"ssh-ed25519 {encoded} logan-cam:{cam_id}", blob


def test_parse_provision_request_is_exact_and_normalized():
    public_key, _ = _public_key()
    request = ProvisionRequest.from_json(
        {
            "cam_id": "cam_alice",
            "customers": [223, 66],
            "allow_delivery": False,
            "public_key": public_key,
        }
    )

    assert request.cam_id == "cam_alice"
    assert request.customers == (223, 66)
    assert request.allow_delivery is False
    assert request.key.comment == "logan-cam:cam_alice"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {
            "cam_id": "cam_alice",
            "customers": [],
            "allow_delivery": False,
            "public_key": "x",
        },
        {
            "cam_id": "cam_alice",
            "customers": ["223"],
            "allow_delivery": False,
            "public_key": "x",
        },
        {
            "cam_id": "cam_alice",
            "customers": [223],
            "allow_delivery": 0,
            "public_key": "x",
        },
        {
            "cam_id": "cam_alice",
            "customers": [223],
            "allow_delivery": False,
            "public_key": "x",
            "replace": True,
        },
    ],
)
def test_parse_provision_request_rejects_missing_ambiguous_or_unknown_fields(payload):
    with pytest.raises(AdminRequestError):
        ProvisionRequest.from_json(payload)


def test_parse_ed25519_key_validates_wire_format_and_fingerprint():
    line, blob = _public_key()
    parsed = parse_ed25519_public_key(line)
    expected = base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")

    assert parsed.fingerprint == f"SHA256:{expected}"


@pytest.mark.parametrize(
    "line",
    [
        "ssh-rsa AAAA logan-cam:cam_alice",
        "ssh-ed25519 not-base64 logan-cam:cam_alice",
        "ssh-ed25519 AAAA logan-cam:cam_alice\nssh-ed25519 BBBB injected",
        "environment=BAD ssh-ed25519 AAAA logan-cam:cam_alice",
    ],
)
def test_parse_ed25519_key_rejects_options_multiline_and_bad_wire_data(line):
    with pytest.raises(AdminRequestError):
        parse_ed25519_public_key(line)


def test_deprovision_request_requires_confirmation_and_expected_fingerprint():
    request = DeprovisionRequest.from_json(
        {
            "cam_id": "cam_alice",
            "expected_fingerprint": "SHA256:abc",
            "confirm": True,
        }
    )
    assert request.confirm is True

    with pytest.raises(AdminRequestError):
        DeprovisionRequest.from_json(
            {
                "cam_id": "cam_alice",
                "expected_fingerprint": "SHA256:abc",
                "confirm": False,
            }
        )


def _paths(tmp_path):
    policy = tmp_path / "etc" / "logan-mcp" / "access_control.yaml"
    keys = tmp_path / "home" / "cam" / ".ssh" / "authorized_keys"
    policy.parent.mkdir(parents=True)
    keys.parent.mkdir(parents=True)
    policy.write_text(
        "compartment_id: c\nnamespace: ns\ncams: {}\n",
        encoding="utf-8",
    )
    keys.write_text("# retained administrator comment\n", encoding="utf-8")
    os.chmod(policy, 0o640)
    os.chmod(keys, 0o640)
    return CamAdminPaths(
        policy_path=policy,
        authorized_keys_path=keys,
        lock_path=tmp_path / "var" / "lock" / "logan-cam-admin.lock",
        backup_dir=tmp_path / "var" / "lib" / "logan-cam-admin" / "backups",
        audit_path=tmp_path / "var" / "log" / "logan-cam-admin.jsonl",
        launcher_path=Path("/opt/logan-mcp/bin/cam-launch"),
        runtime_python=Path("/opt/logan-mcp/venv/bin/python"),
        host_key_path=tmp_path / "etc" / "ssh" / "ssh_host_ed25519_key.pub",
    )


def test_forced_key_line_pins_identity_and_all_restrictions():
    public_key, _ = _public_key()
    parsed = parse_ed25519_public_key(public_key)

    line = build_forced_key_line(
        "cam_alice",
        parsed,
        Path("/opt/logan-mcp/bin/cam-launch"),
    )

    assert line.startswith(
        'restrict,command="/opt/logan-mcp/bin/cam-launch cam_alice"'
    )
    for option in (
        "no-pty",
        "no-port-forwarding",
        "no-agent-forwarding",
        "no-X11-forwarding",
        "no-user-rc",
    ):
        assert option in line
    assert line.endswith(f" {public_key}")


def test_read_live_state_and_backups_are_hash_identified(tmp_path):
    paths = _paths(tmp_path)
    store = CamStateStore(paths)

    with store.locked():
        live = store.read_live()
        backups = store.back_up(live, operation_id="op-1")

    assert backups.policy_path.is_file()
    assert backups.authorized_keys_path.is_file()
    assert sha256_file(backups.policy_path) == live.policy_hash
    assert sha256_file(backups.authorized_keys_path) == live.authorized_keys_hash


def test_replace_preserves_target_owner_mode_and_fsyncs_directories(
    tmp_path, monkeypatch
):
    paths = _paths(tmp_path)
    calls = []
    monkeypatch.setattr(
        "oci_logan_mcp.cam_admin_store.fsync_directory",
        lambda path: calls.append(Path(path)),
    )
    store = CamStateStore(paths)

    with store.locked():
        live = store.read_live()
        store.replace_policy(
            {"compartment_id": "c", "namespace": "ns", "cams": {}},
            live,
        )

    assert stat.S_IMODE(paths.policy_path.stat().st_mode) == 0o640
    assert paths.policy_path.parent in calls


def test_replace_failure_leaves_original_and_removes_candidate(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    store = CamStateStore(paths)
    original = paths.policy_path.read_bytes()

    def fail_replace(source, target):
        raise OSError("replace failed")

    monkeypatch.setattr("oci_logan_mcp.cam_admin_store.os.replace", fail_replace)
    with store.locked():
        live = store.read_live()
        with pytest.raises(OSError, match="replace failed"):
            store.replace_policy(
                {"compartment_id": "changed", "namespace": "ns", "cams": {}},
                live,
            )

    assert paths.policy_path.read_bytes() == original
    assert not list(paths.policy_path.parent.glob(".access_control.yaml.*"))


def test_directory_fsync_failure_exposes_expected_intermediate_hash(
    tmp_path, monkeypatch
):
    paths = _paths(tmp_path)
    store = CamStateStore(paths)
    candidate = {"compartment_id": "changed", "namespace": "ns", "cams": {}}

    def fail_fsync(path):
        raise OSError("directory fsync failed")

    monkeypatch.setattr(
        "oci_logan_mcp.cam_admin_store.fsync_directory",
        fail_fsync,
    )
    with store.locked():
        live = store.read_live()
        with pytest.raises(OSError, match="directory fsync failed"):
            store.replace_policy(candidate, live)

    assert sha256_file(paths.policy_path) == sha256_bytes(serialize_policy(candidate))
    assert not list(paths.policy_path.parent.glob(".access_control.yaml.*"))


def test_conditional_restore_refuses_unexpected_live_hash(tmp_path):
    paths = _paths(tmp_path)
    store = CamStateStore(paths)

    with store.locked():
        original = store.read_live()
        backups = store.back_up(original, operation_id="op-2")
        intermediate_hash = store.replace_authorized_keys(
            ["# operation intermediate"],
            original,
        )
        paths.authorized_keys_path.write_text(
            "# external change\n",
            encoding="utf-8",
        )
        with pytest.raises(ConcurrentMutationError):
            store.restore_authorized_keys(
                backups,
                expected_hash=intermediate_hash,
            )

    assert paths.authorized_keys_path.read_text(encoding="utf-8") == (
        "# external change\n"
    )


def test_authorized_key_records_find_unmanaged_duplicates_but_manage_only_tagged():
    unmanaged_key, _ = _public_key(cam_id="other")
    unmanaged_key = unmanaged_key.rsplit(" ", 1)[0] + " workstation@example"
    managed_key, _ = _public_key(cam_id="cam_alice", key_bytes=b"m" * 32)
    managed_line = build_forced_key_line(
        "cam_alice",
        parse_ed25519_public_key(managed_key),
        Path("/opt/logan-mcp/bin/cam-launch"),
    )
    lines = ("# retained", unmanaged_key, managed_line)

    all_records = authorized_key_records(lines)
    managed_records = managed_key_records(lines)

    assert len(all_records) == 2
    assert all_records[0].managed_cam_id is None
    assert [record.managed_cam_id for record in managed_records] == ["cam_alice"]
    assert managed_records[0].index == 2


def test_audit_record_is_fsynced_and_excludes_public_key_material(tmp_path):
    paths = _paths(tmp_path)
    store = CamStateStore(paths)

    store.append_audit(
        {
            "operation": "provision",
            "cam_id": "cam_alice",
            "fingerprint": "SHA256:abc",
            "outcome": "success",
        }
    )

    record = json.loads(paths.audit_path.read_text(encoding="utf-8"))
    assert record["cam_id"] == "cam_alice"
    assert "public_key" not in record
    assert stat.S_IMODE(paths.audit_path.stat().st_mode) == 0o600

    with pytest.raises(ValueError, match="forbidden secret fields"):
        store.append_audit({"public_key": "must-not-be-written"})
