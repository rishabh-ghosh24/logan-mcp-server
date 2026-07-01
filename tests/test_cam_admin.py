import base64
import json
import os
import struct
from pathlib import Path

import pytest
import yaml

from oci_logan_mcp.cam_admin import CamAdminError, CamAdminService
from oci_logan_mcp.cam_admin_store import (
    CamAdminPaths,
    CamStateStore,
    ProvisionRequest,
)


def _key(cam_id, fill=b"k"):
    algorithm = b"ssh-ed25519"
    blob = (
        struct.pack(">I", len(algorithm))
        + algorithm
        + struct.pack(">I", 32)
        + fill * 32
    )
    return (
        "ssh-ed25519 "
        + base64.b64encode(blob).decode("ascii")
        + f" logan-cam:{cam_id}"
    )


def _service(tmp_path, entity_names=("223_customer", "66_customer")):
    paths = CamAdminPaths(
        policy_path=tmp_path / "etc" / "logan-mcp" / "access_control.yaml",
        authorized_keys_path=(
            tmp_path / "home" / "cam" / ".ssh" / "authorized_keys"
        ),
        lock_path=tmp_path / "var" / "lock" / "logan-cam-admin.lock",
        backup_dir=(
            tmp_path / "var" / "lib" / "logan-cam-admin" / "backups"
        ),
        audit_path=tmp_path / "var" / "log" / "logan-cam-admin.jsonl",
        launcher_path=Path("/opt/logan-mcp/bin/cam-launch"),
        runtime_python=Path("/opt/logan-mcp/venv/bin/python"),
        host_key_path=(
            tmp_path / "etc" / "ssh" / "ssh_host_ed25519_key.pub"
        ),
    )
    paths.policy_path.parent.mkdir(parents=True)
    paths.authorized_keys_path.parent.mkdir(parents=True)
    paths.host_key_path.parent.mkdir(parents=True)
    paths.policy_path.write_text(
        "compartment_id: c\n"
        "namespace: ns\n"
        "defaults:\n"
        "  allow_delivery: false\n"
        "cams: {}\n",
        encoding="utf-8",
    )
    paths.authorized_keys_path.write_text(
        "# keep this line\n",
        encoding="utf-8",
    )
    paths.host_key_path.write_text(
        _key("host", b"h").replace("logan-cam:host", "root@host") + "\n",
        encoding="utf-8",
    )
    os.chmod(paths.policy_path, 0o640)
    os.chmod(paths.authorized_keys_path, 0o640)

    async def resolver(policy):
        assert policy.compartment_id == "c"
        assert policy.namespace == "ns"
        return list(entity_names)

    return (
        CamAdminService(
            store=CamStateStore(paths),
            entity_resolver=resolver,
            connection={
                "host": "130.162.53.112",
                "port": 22,
                "remote_user": "cam",
            },
            actor_provider=lambda: "test-admin",
        ),
        paths,
    )


def _provision_request(cam_id="cam_alice", customers=(223, 66), fill=b"k"):
    return ProvisionRequest.from_json(
        {
            "cam_id": cam_id,
            "customers": list(customers),
            "allow_delivery": False,
            "public_key": _key(cam_id, fill),
        }
    )


@pytest.mark.asyncio
async def test_provision_writes_policy_then_exact_forced_key_and_audit(tmp_path):
    service, paths = _service(tmp_path)
    request = _provision_request()

    response = await service.provision(request)

    policy = yaml.safe_load(paths.policy_path.read_text(encoding="utf-8"))
    keys = paths.authorized_keys_path.read_text(encoding="utf-8")
    audit = [
        json.loads(line)
        for line in paths.audit_path.read_text(encoding="utf-8").splitlines()
    ]
    assert policy["cams"]["cam_alice"] == {
        "customers": [223, 66],
        "allow_delivery": False,
    }
    assert "cam-launch cam_alice" in keys
    assert "# keep this line" in keys
    assert response["status"] == "SUCCESS"
    assert response["fingerprint"] == request.key.fingerprint
    assert response["connection"]["remote_user"] == "cam"
    assert audit[-1]["operation"] == "provision"
    assert audit[-1]["outcome"] == "success"
    assert "public_key" not in audit[-1]


@pytest.mark.asyncio
async def test_provision_refuses_existing_cam_without_changing_files(tmp_path):
    service, paths = _service(tmp_path)
    request = _provision_request(customers=(223,))
    await service.provision(request)
    before_policy = paths.policy_path.read_bytes()
    before_keys = paths.authorized_keys_path.read_bytes()

    with pytest.raises(CamAdminError, match="already exists"):
        await service.provision(request)

    assert paths.policy_path.read_bytes() == before_policy
    assert paths.authorized_keys_path.read_bytes() == before_keys


@pytest.mark.asyncio
async def test_provision_refuses_unmanaged_duplicate_fingerprint(tmp_path):
    service, paths = _service(tmp_path)
    request = _provision_request(customers=(223,))
    unmanaged = request.key.line.rsplit(" ", 1)[0] + " workstation@example"
    paths.authorized_keys_path.write_text(unmanaged + "\n", encoding="utf-8")
    before_policy = paths.policy_path.read_bytes()
    before_keys = paths.authorized_keys_path.read_bytes()

    with pytest.raises(CamAdminError, match="fingerprint already exists"):
        await service.provision(request)

    assert paths.policy_path.read_bytes() == before_policy
    assert paths.authorized_keys_path.read_bytes() == before_keys


@pytest.mark.asyncio
async def test_provision_refuses_zero_live_entities_before_writing(tmp_path):
    service, paths = _service(tmp_path, entity_names=("999_other",))
    request = _provision_request(customers=(223,))
    before_policy = paths.policy_path.read_bytes()
    before_keys = paths.authorized_keys_path.read_bytes()

    with pytest.raises(CamAdminError, match="matched no live entities"):
        await service.provision(request)

    assert paths.policy_path.read_bytes() == before_policy
    assert paths.authorized_keys_path.read_bytes() == before_keys


@pytest.mark.asyncio
async def test_provision_rolls_policy_back_when_key_replace_fails(
    tmp_path, monkeypatch
):
    service, paths = _service(tmp_path)
    request = _provision_request(customers=(223,))
    before_policy = paths.policy_path.read_bytes()
    before_keys = paths.authorized_keys_path.read_bytes()
    monkeypatch.setattr(
        service.store,
        "replace_authorized_keys",
        lambda lines, live: (_ for _ in ()).throw(
            OSError("injected key failure")
        ),
    )

    with pytest.raises(OSError, match="injected key failure"):
        await service.provision(request)

    assert paths.policy_path.read_bytes() == before_policy
    assert paths.authorized_keys_path.read_bytes() == before_keys


@pytest.mark.asyncio
async def test_provision_rolls_both_files_back_when_audit_fails(
    tmp_path, monkeypatch
):
    service, paths = _service(tmp_path)
    request = _provision_request(customers=(223,))
    before_policy = paths.policy_path.read_bytes()
    before_keys = paths.authorized_keys_path.read_bytes()
    monkeypatch.setattr(
        service.store,
        "append_audit",
        lambda event: (_ for _ in ()).throw(OSError("injected audit failure")),
    )

    with pytest.raises(OSError, match="injected audit failure"):
        await service.provision(request)

    assert paths.policy_path.read_bytes() == before_policy
    assert paths.authorized_keys_path.read_bytes() == before_keys


@pytest.mark.asyncio
async def test_provision_detects_applied_rename_when_directory_fsync_raises(
    tmp_path, monkeypatch
):
    service, paths = _service(tmp_path)
    request = _provision_request(customers=(223,))
    before_policy = paths.policy_path.read_bytes()
    before_keys = paths.authorized_keys_path.read_bytes()
    original = service.store.replace_policy

    def replace_then_fail(policy, live):
        original(policy, live)
        raise OSError("injected post-rename directory fsync failure")

    monkeypatch.setattr(service.store, "replace_policy", replace_then_fail)
    with pytest.raises(OSError, match="post-rename"):
        await service.provision(request)

    assert paths.policy_path.read_bytes() == before_policy
    assert paths.authorized_keys_path.read_bytes() == before_keys


@pytest.mark.asyncio
async def test_provision_removes_applied_key_when_key_directory_fsync_raises(
    tmp_path, monkeypatch
):
    service, paths = _service(tmp_path)
    request = _provision_request(customers=(223,))
    before_policy = paths.policy_path.read_bytes()
    before_keys = paths.authorized_keys_path.read_bytes()
    original = service.store.replace_authorized_keys

    def replace_then_fail(lines, live):
        original(lines, live)
        raise OSError("injected key post-rename directory fsync failure")

    monkeypatch.setattr(
        service.store,
        "replace_authorized_keys",
        replace_then_fail,
    )
    with pytest.raises(OSError, match="key post-rename"):
        await service.provision(request)

    assert paths.policy_path.read_bytes() == before_policy
    assert paths.authorized_keys_path.read_bytes() == before_keys


@pytest.mark.asyncio
async def test_show_and_verify_return_current_assignment(tmp_path):
    service, _ = _service(tmp_path)
    request = _provision_request(customers=(223,))
    await service.provision(request)

    shown = service.show("cam_alice")
    verified = await service.verify("cam_alice")

    assert shown["customers"] == [223]
    assert shown["fingerprint"] == request.key.fingerprint
    assert verified["resolved_entities"] == ["223_customer"]
