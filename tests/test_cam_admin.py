import base64
import json
import os
import stat
import struct
import subprocess
from io import StringIO
from pathlib import Path

import pytest
import yaml

from oci_logan_mcp.cam_admin import (
    CamAdminError,
    CamAdminService,
    SystemAccount,
    main,
)
from oci_logan_mcp.cam_admin_store import (
    CamAdminPaths,
    CamStateStore,
    DeprovisionRequest,
    ProvisionRequest,
)
from oci_logan_mcp.cam_processes import TerminationResult

CONTRACT_FIXTURES = Path(__file__).parent / "fixtures" / "cam_admin_contract_v1"


def _contract_fixture(name):
    return json.loads((CONTRACT_FIXTURES / name).read_text(encoding="utf-8"))


def _normalize_contract_response(response):
    normalized = dict(response)
    if "backup_dir" in normalized:
        normalized["backup_dir"] = "<BACKUP_DIR>"
    return normalized


def _key(cam_id, fill=b"k"):
    algorithm = b"ssh-ed25519"
    blob = (
        struct.pack(">I", len(algorithm))
        + algorithm
        + struct.pack(">I", 32)
        + fill * 32
    )
    return (
        "ssh-ed25519 " + base64.b64encode(blob).decode("ascii") + f" logan-cam:{cam_id}"
    )


def _service(tmp_path, entity_names=("223_customer", "66_customer")):
    paths = CamAdminPaths(
        policy_path=tmp_path / "etc" / "logan-mcp" / "access_control.yaml",
        authorized_keys_path=(tmp_path / "home" / "cam" / ".ssh" / "authorized_keys"),
        lock_path=tmp_path / "var" / "lock" / "logan-cam-admin.lock",
        backup_dir=(tmp_path / "var" / "lib" / "logan-cam-admin" / "backups"),
        audit_path=tmp_path / "var" / "log" / "logan-cam-admin.jsonl",
        launcher_path=Path("/opt/logan-mcp/bin/cam-launch"),
        runtime_python=Path("/opt/logan-mcp/venv/bin/python"),
        host_key_path=(tmp_path / "etc" / "ssh" / "ssh_host_ed25519_key.pub"),
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
                "host_public_key": _key("host", b"h").replace(
                    "logan-cam:host",
                    "root@host",
                ),
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
        lambda lines, live: (_ for _ in ()).throw(OSError("injected key failure")),
    )

    with pytest.raises(OSError, match="injected key failure"):
        await service.provision(request)

    assert paths.policy_path.read_bytes() == before_policy
    assert paths.authorized_keys_path.read_bytes() == before_keys


@pytest.mark.asyncio
async def test_provision_rolls_both_files_back_when_audit_fails(tmp_path, monkeypatch):
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
    assert verified["allow_delivery"] is False


class FakeTerminator:
    def __init__(self, result=TerminationResult(1, 1, 0), fail_exact=False):
        self.result = result
        self.fail_exact = fail_exact
        self.exact_calls = []
        self.fallback_calls = 0

    def terminate_cam(self, cam_id, grace_seconds=5.0):
        self.exact_calls.append(cam_id)
        if self.fail_exact:
            raise RuntimeError("identity verification unavailable")
        return self.result

    def terminate_restricted_account(self, grace_seconds=5.0):
        self.fallback_calls += 1
        return 2


async def _provision_alice(service):
    request = _provision_request(customers=(223,))
    await service.provision(request)
    return request


def _deprovision_request(provisioned, fingerprint=None):
    return DeprovisionRequest.from_json(
        {
            "cam_id": "cam_alice",
            "expected_fingerprint": fingerprint or provisioned.key.fingerprint,
            "confirm": True,
        }
    )


@pytest.mark.asyncio
async def test_deprovision_removes_policy_and_key_terminates_process_and_keeps_state(
    tmp_path,
):
    service, paths = _service(tmp_path)
    terminator = FakeTerminator()
    service.process_terminator = terminator
    provisioned = await _provision_alice(service)
    user_dir = tmp_path / "home" / "cam" / ".oci-logan-mcp" / "users" / "cam_alice"
    user_dir.mkdir(parents=True)
    (user_dir / "learned_queries.yaml").write_text(
        "queries: []\n",
        encoding="utf-8",
    )

    response = await service.deprovision(_deprovision_request(provisioned))

    assert response["status"] == "SUCCESS"
    policy = yaml.safe_load(paths.policy_path.read_text(encoding="utf-8"))
    assert "cam_alice" not in policy["cams"]
    assert "logan-cam:cam_alice" not in paths.authorized_keys_path.read_text(
        encoding="utf-8"
    )
    assert terminator.exact_calls == ["cam_alice"]
    assert (user_dir / "learned_queries.yaml").is_file()


@pytest.mark.asyncio
async def test_deprovision_requires_terminator_before_mutation(tmp_path):
    service, paths = _service(tmp_path)
    provisioned = await _provision_alice(service)
    before_policy = paths.policy_path.read_bytes()
    before_keys = paths.authorized_keys_path.read_bytes()

    with pytest.raises(CamAdminError, match="process terminator is required"):
        await service.deprovision(_deprovision_request(provisioned))

    assert paths.policy_path.read_bytes() == before_policy
    assert paths.authorized_keys_path.read_bytes() == before_keys


@pytest.mark.asyncio
async def test_deprovision_never_restores_removed_key_after_policy_cleanup_failure(
    tmp_path, monkeypatch
):
    service, paths = _service(tmp_path)
    service.process_terminator = FakeTerminator()
    provisioned = await _provision_alice(service)
    original_replace_policy = service.store.replace_policy

    def fail_policy(policy, live):
        if "cam_alice" not in policy.get("cams", {}):
            raise OSError("injected policy cleanup failure")
        return original_replace_policy(policy, live)

    monkeypatch.setattr(service.store, "replace_policy", fail_policy)
    response = await service.deprovision(_deprovision_request(provisioned))

    assert response["status"] == "FAILED_ACCESS_REVOKED_CLEANUP_REQUIRED"
    assert response["access_revoked"] is True
    assert "logan-cam:cam_alice" not in paths.authorized_keys_path.read_text(
        encoding="utf-8"
    )
    assert service.process_terminator.fallback_calls == 1


@pytest.mark.asyncio
async def test_deprovision_policy_removal_stays_revoked_when_key_cleanup_fails(
    tmp_path, monkeypatch
):
    service, paths = _service(tmp_path)
    service.process_terminator = FakeTerminator()
    provisioned = await _provision_alice(service)
    monkeypatch.setattr(
        service.store,
        "replace_authorized_keys",
        lambda lines, live: (_ for _ in ()).throw(
            OSError("injected key cleanup failure")
        ),
    )

    response = await service.deprovision(_deprovision_request(provisioned))

    assert response["status"] == "FAILED_ACCESS_REVOKED_CLEANUP_REQUIRED"
    assert response["access_revoked"] is True
    assert (
        "cam_alice"
        not in yaml.safe_load(paths.policy_path.read_text(encoding="utf-8"))["cams"]
    )
    assert "logan-cam:cam_alice" in paths.authorized_keys_path.read_text(
        encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_deprovision_reports_unconfirmed_when_both_revocation_gates_fail(
    tmp_path, monkeypatch
):
    service, paths = _service(tmp_path)
    service.process_terminator = FakeTerminator()
    provisioned = await _provision_alice(service)
    monkeypatch.setattr(
        service.store,
        "replace_policy",
        lambda policy, live: (_ for _ in ()).throw(OSError("policy failed")),
    )
    monkeypatch.setattr(
        service.store,
        "replace_authorized_keys",
        lambda lines, live: (_ for _ in ()).throw(OSError("keys failed")),
    )

    response = await service.deprovision(_deprovision_request(provisioned))

    assert response["status"] == "FAILED_REVOCATION_UNCONFIRMED"
    assert response["access_revoked"] is False
    assert service.process_terminator.fallback_calls == 1
    assert (
        "cam_alice"
        in yaml.safe_load(paths.policy_path.read_text(encoding="utf-8"))["cams"]
    )
    assert "logan-cam:cam_alice" in paths.authorized_keys_path.read_text(
        encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_deprovision_uses_shared_fallback_when_exact_termination_is_unverified(
    tmp_path,
):
    service, _ = _service(tmp_path)
    service.process_terminator = FakeTerminator(fail_exact=True)
    provisioned = await _provision_alice(service)

    response = await service.deprovision(_deprovision_request(provisioned))

    assert response["status"] == "SUCCESS"
    assert response["shared_account_fallback"] is True
    assert service.process_terminator.fallback_calls == 1


@pytest.mark.asyncio
async def test_deprovision_audit_failure_does_not_restore_access(tmp_path, monkeypatch):
    service, paths = _service(tmp_path)
    service.process_terminator = FakeTerminator()
    provisioned = await _provision_alice(service)
    monkeypatch.setattr(
        service.store,
        "append_audit",
        lambda event: (_ for _ in ()).throw(OSError("audit unavailable")),
    )

    response = await service.deprovision(_deprovision_request(provisioned))

    assert response["status"] == "FAILED_ACCESS_REVOKED_CLEANUP_REQUIRED"
    assert response["access_revoked"] is True
    assert (
        "cam_alice"
        not in yaml.safe_load(paths.policy_path.read_text(encoding="utf-8"))["cams"]
    )
    assert "logan-cam:cam_alice" not in paths.authorized_keys_path.read_text(
        encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_deprovision_refuses_fingerprint_mismatch_without_mutation(tmp_path):
    service, paths = _service(tmp_path)
    service.process_terminator = FakeTerminator()
    provisioned = await _provision_alice(service)
    before_policy = paths.policy_path.read_bytes()
    before_keys = paths.authorized_keys_path.read_bytes()

    with pytest.raises(CamAdminError, match="fingerprint changed"):
        await service.deprovision(
            _deprovision_request(provisioned, fingerprint="SHA256:not-current")
        )

    assert paths.policy_path.read_bytes() == before_policy
    assert paths.authorized_keys_path.read_bytes() == before_keys


def test_cli_provision_reads_json_stdin_and_writes_one_json_response():
    class FakeService:
        async def provision(self, request):
            return {"status": "SUCCESS", "cam_id": request.cam_id}

    stdin = StringIO(
        json.dumps(
            {
                "cam_id": "cam_alice",
                "customers": [223],
                "allow_delivery": False,
                "public_key": _key("cam_alice"),
            }
        )
    )
    stdout = StringIO()
    stderr = StringIO()

    code = main(
        ["provision", "--json"],
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        service=FakeService(),
        geteuid=lambda: 0,
    )

    assert code == 0
    assert json.loads(stdout.getvalue()) == {
        "status": "SUCCESS",
        "cam_id": "cam_alice",
    }
    assert stdout.getvalue().count("\n") == 1
    assert stderr.getvalue() == ""


def test_cli_refuses_non_root_before_reading_request():
    class UnreadableInput:
        def read(self):
            raise AssertionError("stdin must not be read")

    stdout = StringIO()
    stderr = StringIO()

    code = main(
        ["show", "--cam", "cam_alice", "--json"],
        stdin=UnreadableInput(),
        stdout=stdout,
        stderr=stderr,
        service=object(),
        geteuid=lambda: 1000,
    )

    assert code == 1
    assert stdout.getvalue() == ""
    assert "must run as root" in stderr.getvalue()


def test_cli_rejects_trailing_json_without_calling_service():
    class FakeService:
        async def provision(self, request):
            raise AssertionError("invalid input must not reach the service")

    payload = {
        "cam_id": "cam_alice",
        "customers": [223],
        "allow_delivery": False,
        "public_key": _key("cam_alice"),
    }
    stdout = StringIO()
    stderr = StringIO()

    code = main(
        ["provision", "--json"],
        stdin=StringIO(json.dumps(payload) + " {}"),
        stdout=stdout,
        stderr=stderr,
        service=FakeService(),
        geteuid=lambda: 0,
    )

    assert code != 0
    assert stdout.getvalue() == ""
    assert "trailing" in stderr.getvalue().lower()
    assert _key("cam_alice") not in stderr.getvalue()


def test_cli_emits_failed_operation_json_and_nonzero_exit():
    class FakeService:
        async def deprovision(self, request):
            return {
                "status": "FAILED_ACCESS_REVOKED_CLEANUP_REQUIRED",
                "cam_id": request.cam_id,
                "access_revoked": True,
            }

    stdout = StringIO()
    stderr = StringIO()
    payload = {
        "cam_id": "cam_alice",
        "expected_fingerprint": "SHA256:fixture",
        "confirm": True,
    }

    code = main(
        ["deprovision", "--json"],
        stdin=StringIO(json.dumps(payload)),
        stdout=stdout,
        stderr=stderr,
        service=FakeService(),
        geteuid=lambda: 0,
    )

    assert code == 1
    assert json.loads(stdout.getvalue())["access_revoked"] is True
    assert stderr.getvalue() == ""


class FakeSystemInspector:
    def __init__(
        self,
        stats,
        sshd_output=None,
        instance_returncode=0,
        tree_secure=True,
    ):
        self.stats = stats
        self.sshd_output = sshd_output or (
            "authenticationmethods publickey\n"
            "passwordauthentication no\n"
            "kbdinteractiveauthentication no\n"
            "permituserenvironment no\n"
            "permituserrc no\n"
            "allowagentforwarding no\n"
            "allowtcpforwarding no\n"
            "gatewayports no\n"
            "x11forwarding no\n"
            "permittunnel no\n"
            "permittty no\n"
            "acceptenv LANG LC_*\n"
        )
        self.instance_returncode = instance_returncode
        self.tree_secure_result = tree_secure
        self.commands = []

    def stat(self, path):
        return self.stats[Path(path)]

    def lookup_user(self, name):
        assert name == "cam"
        return SystemAccount(uid=2001, gid=2001, groups=("cam",))

    def run(self, argv, env=None):
        self.commands.append((tuple(argv), env))
        if "-T" in argv:
            return subprocess.CompletedProcess(argv, 0, self.sshd_output, "")
        if tuple(argv[:2]) == ("/usr/bin/passwd", "-S"):
            return subprocess.CompletedProcess(
                argv, 0, "cam LK 2026-01-01 0 99999 7 -1\n", ""
            )
        return subprocess.CompletedProcess(
            argv,
            self.instance_returncode,
            "",
            "instance principal failed" if self.instance_returncode else "",
        )

    def write_probe(self, path, uid, gid):
        return Path(path) in self.stats and uid == 2001 and gid == 2001

    def tree_secure(self, path, uid):
        return self.tree_secure_result and Path(path) in self.stats and uid == 0


def _fake_stat(uid, gid, mode):
    return type(
        "FakeStat",
        (),
        {"st_uid": uid, "st_gid": gid, "st_mode": mode},
    )()


def _bootstrap_service(tmp_path, **inspector_kwargs):
    service, paths = _service(tmp_path)
    runtime_root = paths.runtime_python.parents[2]
    admin_command = runtime_root / "bin" / "cam-admin"
    config_path = paths.policy_path.parent / "config.yaml"
    connection_path = paths.policy_path.parent / "connection.json"
    cam_home = paths.authorized_keys_path.parent.parent
    ssh_dir = paths.authorized_keys_path.parent
    state_dir = paths.authorized_keys_path.parent.parent / ".oci-logan-mcp"
    stats = {
        runtime_root: _fake_stat(0, 0, stat.S_IFDIR | 0o755),
        paths.launcher_path: _fake_stat(0, 0, stat.S_IFREG | 0o755),
        admin_command: _fake_stat(0, 0, stat.S_IFREG | 0o755),
        paths.policy_path.parent: _fake_stat(0, 2001, stat.S_IFDIR | 0o750),
        config_path: _fake_stat(0, 2001, stat.S_IFREG | 0o640),
        connection_path: _fake_stat(0, 2001, stat.S_IFREG | 0o640),
        paths.policy_path: _fake_stat(0, 2001, stat.S_IFREG | 0o640),
        cam_home: _fake_stat(0, 2001, stat.S_IFDIR | 0o750),
        ssh_dir: _fake_stat(0, 2001, stat.S_IFDIR | 0o750),
        paths.authorized_keys_path: _fake_stat(
            0,
            2001,
            stat.S_IFREG | 0o640,
        ),
        state_dir: _fake_stat(2001, 2001, stat.S_IFDIR | 0o700),
    }
    inspector = FakeSystemInspector(stats, **inspector_kwargs)
    service.system_inspector = inspector
    return service, inspector


def test_bootstrap_check_verifies_full_linux_security_boundary(tmp_path):
    service, inspector = _bootstrap_service(tmp_path)

    response = service.bootstrap_check()

    assert response == {
        "status": "SUCCESS",
        "checks": {
            "runtime_root_owned": True,
            "launcher_root_owned": True,
            "admin_command_root_owned": True,
            "config_parent_immutable": True,
            "config_immutable": True,
            "policy_immutable": True,
            "authorized_keys_immutable": True,
            "runtime_state_writable": True,
            "cam_password_locked": True,
            "cam_not_admin": True,
            "sshd_effective_config": True,
            "instance_principal_init": True,
        },
    }
    sshd_call = next(command for command, _ in inspector.commands if "-T" in command)
    assert "user=cam,host=130.162.53.112,addr=127.0.0.1" in sshd_call


def test_bootstrap_check_rejects_dangerous_accepted_environment(tmp_path):
    output = (
        "authenticationmethods publickey\n"
        "passwordauthentication no\n"
        "kbdinteractiveauthentication no\n"
        "permituserenvironment no\n"
        "permituserrc no\n"
        "allowagentforwarding no\n"
        "allowtcpforwarding no\n"
        "gatewayports no\n"
        "x11forwarding no\n"
        "permittunnel no\n"
        "permittty no\n"
        "acceptenv LANG PYTHONPATH\n"
    )
    service, _ = _bootstrap_service(tmp_path, sshd_output=output)

    response = service.bootstrap_check()

    assert response["status"] == "FAILED"
    assert response["checks"]["sshd_effective_config"] is False


def test_bootstrap_check_reports_instance_principal_failure(tmp_path):
    service, _ = _bootstrap_service(tmp_path, instance_returncode=1)

    response = service.bootstrap_check()

    assert response["status"] == "FAILED"
    assert response["checks"]["instance_principal_init"] is False


def test_bootstrap_check_rejects_writable_runtime_descendant(tmp_path):
    service, _ = _bootstrap_service(tmp_path, tree_secure=False)

    response = service.bootstrap_check()

    assert response["status"] == "FAILED"
    assert response["checks"]["runtime_root_owned"] is False


def test_bootstrap_check_requires_root_control_of_authorized_keys_parents(tmp_path):
    service, inspector = _bootstrap_service(tmp_path)
    ssh_dir = service.store.paths.authorized_keys_path.parent
    inspector.stats[ssh_dir] = _fake_stat(
        2001,
        2001,
        stat.S_IFDIR | 0o700,
    )

    response = service.bootstrap_check()

    assert response["status"] == "FAILED"
    assert response["checks"]["authorized_keys_immutable"] is False


@pytest.mark.asyncio
async def test_golden_contract_provision_show_and_verify(tmp_path):
    service, _ = _service(tmp_path)
    request = ProvisionRequest.from_json(_contract_fixture("provision.request.json"))

    provisioned = await service.provision(request)
    shown = service.show("cam_alice")
    verified = await service.verify("cam_alice")

    assert provisioned == _contract_fixture("provision.response.json")
    assert shown == _contract_fixture("show.response.json")
    assert verified == _contract_fixture("verify.response.json")


@pytest.mark.asyncio
async def test_golden_contract_deprovision_success(tmp_path):
    service, _ = _service(tmp_path)
    service.process_terminator = FakeTerminator()
    await service.provision(
        ProvisionRequest.from_json(_contract_fixture("provision.request.json"))
    )
    request = DeprovisionRequest.from_json(
        _contract_fixture("deprovision.request.json")
    )

    response = await service.deprovision(request)

    assert _normalize_contract_response(response) == _contract_fixture(
        "deprovision.success.response.json"
    )


@pytest.mark.asyncio
async def test_golden_contract_deprovision_cleanup_required(tmp_path, monkeypatch):
    service, _ = _service(tmp_path)
    service.process_terminator = FakeTerminator()
    await service.provision(
        ProvisionRequest.from_json(_contract_fixture("provision.request.json"))
    )
    original_replace_policy = service.store.replace_policy

    def fail_policy(policy, live):
        if "cam_alice" not in policy.get("cams", {}):
            raise OSError("injected policy cleanup failure")
        return original_replace_policy(policy, live)

    monkeypatch.setattr(service.store, "replace_policy", fail_policy)
    response = await service.deprovision(
        DeprovisionRequest.from_json(_contract_fixture("deprovision.request.json"))
    )

    assert _normalize_contract_response(response) == _contract_fixture(
        "deprovision.cleanup-required.response.json"
    )


@pytest.mark.asyncio
async def test_golden_contract_deprovision_unconfirmed(tmp_path, monkeypatch):
    service, _ = _service(tmp_path)
    service.process_terminator = FakeTerminator()
    await service.provision(
        ProvisionRequest.from_json(_contract_fixture("provision.request.json"))
    )
    monkeypatch.setattr(
        service.store,
        "replace_policy",
        lambda policy, live: (_ for _ in ()).throw(OSError("policy failed")),
    )
    monkeypatch.setattr(
        service.store,
        "replace_authorized_keys",
        lambda lines, live: (_ for _ in ()).throw(OSError("keys failed")),
    )
    response = await service.deprovision(
        DeprovisionRequest.from_json(_contract_fixture("deprovision.request.json"))
    )

    assert _normalize_contract_response(response) == _contract_fixture(
        "deprovision.unconfirmed.response.json"
    )


def test_golden_contract_bootstrap_check(tmp_path):
    service, _ = _bootstrap_service(tmp_path)

    assert service.bootstrap_check() == _contract_fixture(
        "bootstrap-check.response.json"
    )


def test_golden_contract_cli_exit_codes():
    exit_codes = _contract_fixture("manifest.json")["cli"]["exit_codes"]

    class BootstrapService:
        def __init__(self, status):
            self.status = status

        def bootstrap_check(self):
            return {"status": self.status, "checks": {}}

    assert (
        main(
            ["bootstrap-check", "--json"],
            stdin=StringIO(),
            stdout=StringIO(),
            stderr=StringIO(),
            service=BootstrapService("SUCCESS"),
            geteuid=lambda: 0,
        )
        == exit_codes["success"]
    )
    assert (
        main(
            ["bootstrap-check", "--json"],
            stdin=StringIO(),
            stdout=StringIO(),
            stderr=StringIO(),
            service=BootstrapService("FAILED"),
            geteuid=lambda: 0,
        )
        == exit_codes["operation_failure"]
    )
    assert (
        main(
            ["not-a-command"],
            stdin=StringIO(),
            stdout=StringIO(),
            stderr=StringIO(),
            service=object(),
            geteuid=lambda: 0,
        )
        == exit_codes["usage_error"]
    )
