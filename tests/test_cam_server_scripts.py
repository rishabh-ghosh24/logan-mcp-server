import base64
import os
import stat
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "cam-setup" / "server" / "cam-launch"
ADMIN = ROOT / "cam-setup" / "server" / "cam-admin"
BOOTSTRAP = ROOT / "cam-setup" / "server" / "bootstrap-cam-server.sh"
CONTRACT_MANIFEST = (
    ROOT / "tests" / "fixtures" / "cam_admin_contract_v1" / "manifest.json"
)


def test_server_shell_scripts_parse():
    for script in (LAUNCHER, ADMIN, BOOTSTRAP):
        result = subprocess.run(
            ["bash", "-n", str(script)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr


def test_forced_launcher_has_only_fixed_security_environment():
    text = LAUNCHER.read_text(encoding="utf-8")
    assert "/usr/bin/env -i" in text
    assert "OCI_LA_MCP_CONFIG=/etc/logan-mcp/config.yaml" in text
    assert "OCI_LOGAN_MCP_ACCESS_CONFIG=/etc/logan-mcp/access_control.yaml" in text
    assert "LOGAN_USER=$CAM_ID" in text
    assert "--enforce-access --user" in text
    assert "/opt/logan-mcp/venv/bin/python -I -m oci_logan_mcp" in text
    assert "SSH_ORIGINAL_COMMAND" not in text
    for dangerous in (
        "PYTHONPATH",
        "PYTHONHOME",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
    ):
        assert dangerous not in text


def test_forced_launcher_rejects_invalid_ids_before_exec():
    for value in ("Alice", "alice..smith", "alice;id", "../root", ""):
        result = subprocess.run(
            [str(LAUNCHER), value],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 64


def test_bootstrap_installs_defense_in_depth_sshd_settings():
    text = BOOTSTRAP.read_text(encoding="utf-8")
    required = (
        "AuthenticationMethods publickey",
        "PasswordAuthentication no",
        "KbdInteractiveAuthentication no",
        "PermitUserRC no",
        "AllowAgentForwarding no",
        "AllowTcpForwarding no",
        "X11Forwarding no",
        "PermitTunnel no",
        "PermitTTY no",
    )
    for setting in required:
        assert setting in text
    assert "sshd -t" in text
    assert "systemctl reload sshd" in text
    assert "Include /etc/ssh/sshd_config.d/*.conf" in text
    assert "PermitUserEnvironment is global-only" in text
    assert 'PATH="/usr/sbin:/usr/bin:/sbin:/bin"' in text
    assert '/bin/chmod -R a+rX,go-w "$OPT_DIR"' in text


def test_bootstrap_never_copies_policy_into_mutable_state_tree():
    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert 'ETC_DIR="${ROOT_PREFIX}/etc/logan-mcp"' in text
    assert 'STATE_DIR="${ROOT_PREFIX}/home/cam/.oci-logan-mcp"' in text
    assert "--exclude=config.yaml" in text
    assert "--exclude=access_control.yaml" in text


def test_admin_wrapper_uses_only_installed_runtime_and_forwards_arguments():
    text = ADMIN.read_text(encoding="utf-8")
    assert "exec /opt/logan-mcp/venv/bin/python -I -m oci_logan_mcp.cam_admin" in text
    assert '"$@"' in text
    assert "eval" not in text


def _public_key(comment="root@host"):
    algorithm = b"ssh-ed25519"
    blob = (
        struct.pack(">I", len(algorithm))
        + algorithm
        + struct.pack(">I", 32)
        + b"h" * 32
    )
    return "ssh-ed25519 " + base64.b64encode(blob).decode("ascii") + f" {comment}"


def _write_executable(path, text):
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _bootstrap_fixture(tmp_path):
    root_prefix = tmp_path / "root"
    host_key = root_prefix / "etc" / "ssh" / "ssh_host_ed25519_key.pub"
    host_key.parent.mkdir(parents=True)
    host_key.write_text(_public_key() + "\n", encoding="utf-8")
    (root_prefix / "etc" / "ssh" / "sshd_config").write_text(
        "X11Forwarding yes\nMatch User legacy\n    PermitTTY no\n",
        encoding="utf-8",
    )

    config = tmp_path / "config.yaml"
    policy = tmp_path / "access_control.yaml"
    config.write_text(
        "oci:\n  auth_type: instance_principal\n",
        encoding="utf-8",
    )
    policy.write_text(
        "compartment_id: c\nnamespace: ns\ncams: {}\n",
        encoding="utf-8",
    )

    legacy = tmp_path / "legacy-state"
    user = legacy / "users" / "cam_alice"
    reports = legacy / "reports"
    user.mkdir(parents=True)
    reports.mkdir(parents=True)
    (user / "learned_queries.yaml").write_text(
        "queries: []\n",
        encoding="utf-8",
    )
    (user / "preferences.yaml").write_text(
        "timezone: UTC\n",
        encoding="utf-8",
    )
    (reports / "retained.txt").write_text("keep\n", encoding="utf-8")
    (legacy / "config.yaml").write_text("must-not-copy\n", encoding="utf-8")
    (legacy / "access_control.yaml").write_text(
        "must-not-copy\n",
        encoding="utf-8",
    )

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    sshd_log = tmp_path / "sshd.log"
    systemctl_log = tmp_path / "systemctl.log"
    _write_executable(
        fake_bin / "sshd",
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" >> "$FAKE_SSHD_LOG"\n'
        'if [ "${FAKE_SSHD_FAIL:-0}" = 1 ] && [ "${1:-}" = -t ]; then exit 1; fi\n'
        "exit 0\n",
    )
    _write_executable(
        fake_bin / "systemctl",
        "#!/bin/sh\n" 'printf \'%s\\n\' "$*" >> "$FAKE_SYSTEMCTL_LOG"\n' "exit 0\n",
    )
    env = {
        **os.environ,
        "CAM_BOOTSTRAP_TEST_MODE": "1",
        "FAKE_SSHD_LOG": str(sshd_log),
        "FAKE_SYSTEMCTL_LOG": str(systemctl_log),
        "PATH": f"{fake_bin}:/usr/bin:/bin",
    }
    command = [
        str(BOOTSTRAP),
        "--repo",
        str(ROOT),
        "--config-source",
        str(config),
        "--policy-source",
        str(policy),
        "--state-source",
        str(legacy),
        "--public-host",
        "logan.example.internal",
        "--port",
        "2222",
        "--python",
        sys.executable,
        "--root-prefix",
        str(root_prefix),
    ]
    return root_prefix, command, env, sshd_log, systemctl_log


def test_fake_root_bootstrap_is_idempotent_and_preserves_keys_and_state(tmp_path):
    root_prefix, command, env, _, systemctl_log = _bootstrap_fixture(tmp_path)

    first = subprocess.run(command, env=env, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr

    authorized_keys = root_prefix / "home" / "cam" / ".ssh" / "authorized_keys"
    state = root_prefix / "home" / "cam" / ".oci-logan-mcp"
    audit = root_prefix / "var" / "log" / "logan-cam-admin.jsonl"
    managed_key = (
        'restrict,command="/opt/logan-mcp/bin/cam-launch cam_alice" '
        + _public_key("logan-cam:cam_alice")
        + "\n"
    )
    authorized_keys.write_text(managed_key, encoding="utf-8")
    policy = root_prefix / "etc" / "logan-mcp" / "access_control.yaml"
    policy.write_text(
        "compartment_id: c\nnamespace: ns\ncams:\n"
        "  cam_alice: { customers: [223], allow_delivery: false }\n",
        encoding="utf-8",
    )
    learned = state / "users" / "cam_alice" / "learned_queries.yaml"
    learned.write_text("queries:\n  - retained: true\n", encoding="utf-8")
    audit.write_text('{"retained":true}\n', encoding="utf-8")

    second = subprocess.run(command, env=env, capture_output=True, text=True)
    assert second.returncode == 0, second.stderr

    assert authorized_keys.read_text(encoding="utf-8") == managed_key
    assert policy.read_text(encoding="utf-8").endswith(
        "cam_alice: { customers: [223], allow_delivery: false }\n"
    )
    assert learned.read_text(encoding="utf-8") == "queries:\n  - retained: true\n"
    assert (state / "users" / "cam_alice" / "preferences.yaml").is_file()
    assert (state / "reports" / "retained.txt").is_file()
    assert audit.read_text(encoding="utf-8") == '{"retained":true}\n'
    assert not (state / "config.yaml").exists()
    assert not (state / "access_control.yaml").exists()
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE(authorized_keys.stat().st_mode) == 0o640
    assert systemctl_log.read_text(encoding="utf-8").splitlines() == [
        "reload sshd",
        "reload sshd",
    ]


def test_fake_root_bootstrap_includes_dropin_before_existing_match(tmp_path):
    root_prefix, command, env, _, _ = _bootstrap_fixture(tmp_path)

    result = subprocess.run(command, env=env, capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    sshd_config = (root_prefix / "etc" / "ssh" / "sshd_config").read_text(
        encoding="utf-8"
    )
    assert sshd_config.count("Include /etc/ssh/sshd_config.d/*.conf") == 1
    assert sshd_config.index("Include /etc/ssh/sshd_config.d/*.conf") < sshd_config.index(
        "Match User legacy"
    )


def test_fake_root_bootstrap_restores_main_sshd_config_on_validation_failure(tmp_path):
    root_prefix, command, env, _, systemctl_log = _bootstrap_fixture(tmp_path)
    sshd_config = root_prefix / "etc" / "ssh" / "sshd_config"
    before = sshd_config.read_text(encoding="utf-8")

    failed = subprocess.run(
        command,
        env={**env, "FAKE_SSHD_FAIL": "1"},
        capture_output=True,
        text=True,
    )

    assert failed.returncode != 0
    assert sshd_config.read_text(encoding="utf-8") == before
    assert not (root_prefix / "etc" / "ssh" / "sshd_config.d" / "90-logan-cam.conf").exists()
    assert not systemctl_log.exists()


def test_fake_root_bootstrap_rejects_predefined_initial_cam_policy(tmp_path):
    root_prefix, command, env, _, systemctl_log = _bootstrap_fixture(tmp_path)
    policy = Path(command[command.index("--policy-source") + 1])
    policy.write_text(
        "compartment_id: c\nnamespace: ns\ncams:\n"
        "  cam_alice: { customers: [223] }\n",
        encoding="utf-8",
    )

    result = subprocess.run(command, env=env, capture_output=True, text=True)

    assert result.returncode != 0
    assert "must not define CAMs" in result.stderr
    assert not (root_prefix / "etc" / "ssh" / "sshd_config.d" / "90-logan-cam.conf").exists()
    assert not systemctl_log.exists()


def test_fake_root_sshd_validation_failure_restores_dropin_without_reload(tmp_path):
    root_prefix, command, env, _, systemctl_log = _bootstrap_fixture(tmp_path)
    first = subprocess.run(command, env=env, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr

    dropin = root_prefix / "etc" / "ssh" / "sshd_config.d" / "90-logan-cam.conf"
    dropin.write_text("# previous drop-in\n", encoding="utf-8")
    before_reload = systemctl_log.read_text(encoding="utf-8")

    failed = subprocess.run(
        command,
        env={**env, "FAKE_SSHD_FAIL": "1"},
        capture_output=True,
        text=True,
    )

    assert failed.returncode != 0
    assert dropin.read_text(encoding="utf-8") == "# previous drop-in\n"
    assert systemctl_log.read_text(encoding="utf-8") == before_reload


def test_fake_root_bootstrap_refuses_controlled_path_symlink(tmp_path):
    root_prefix, command, env, _, _ = _bootstrap_fixture(tmp_path)
    ssh_dir = root_prefix / "home" / "cam" / ".ssh"
    ssh_dir.mkdir(parents=True)
    outside = tmp_path / "outside-authorized-keys"
    outside.write_text("do-not-touch\n", encoding="utf-8")
    (ssh_dir / "authorized_keys").symlink_to(outside)

    result = subprocess.run(command, env=env, capture_output=True, text=True)

    assert result.returncode != 0
    assert "symbolic link" in result.stderr
    assert outside.read_text(encoding="utf-8") == "do-not-touch\n"


def test_root_prefix_is_rejected_outside_explicit_test_mode(tmp_path):
    _, command, env, _, _ = _bootstrap_fixture(tmp_path)
    env.pop("CAM_BOOTSTRAP_TEST_MODE")

    result = subprocess.run(command, env=env, capture_output=True, text=True)

    assert result.returncode == 64


def test_golden_manifest_freezes_forced_ssh_and_cli_contract():
    import json
    import re

    from oci_logan_mcp.cam_admin_store import (
        ProvisionRequest,
        build_forced_key_line,
    )
    from oci_logan_mcp.cam_processes import CAM_LAUNCH_ARGV_TEMPLATE

    manifest = json.loads(CONTRACT_MANIFEST.read_text(encoding="utf-8"))

    assert manifest["contract_version"] == 1
    assert manifest["cli"]["exit_codes"] == {
        "success": 0,
        "operation_failure": 1,
        "usage_error": 2,
    }
    assert manifest["ssh"]["launcher_argv"] == list(CAM_LAUNCH_ARGV_TEMPLATE)
    launcher_text = re.sub(
        r"\\\n\s*",
        " ",
        LAUNCHER.read_text(encoding="utf-8"),
    )
    launcher_text = " ".join(launcher_text.split())
    expected_launcher = " ".join(
        '"$CAM_ID"' if value == "{cam_id}" else value
        for value in CAM_LAUNCH_ARGV_TEMPLATE
    )
    assert expected_launcher in launcher_text
    assert manifest["ssh"]["environment"]["OCI_LA_MCP_CONFIG"] == (
        "/etc/logan-mcp/config.yaml"
    )
    assert (
        manifest["ssh"]["environment"]["OCI_LOGAN_MCP_ACCESS_CONFIG"]
        == "/etc/logan-mcp/access_control.yaml"
    )
    request = ProvisionRequest.from_json(
        json.loads(
            (CONTRACT_MANIFEST.parent / "provision.request.json").read_text(
                encoding="utf-8"
            )
        )
    )
    forced_line = build_forced_key_line(
        request.cam_id,
        request.key,
        Path("/opt/logan-mcp/bin/cam-launch"),
    )
    prefix = ",".join(
        option.format(cam_id=request.cam_id)
        for option in manifest["ssh"]["authorized_key_options"]
    )
    assert forced_line.startswith(prefix + " ssh-ed25519 ")


def test_fake_root_bootstrap_rejects_invalid_policy_before_sshd_change(tmp_path):
    root_prefix, command, env, _, systemctl_log = _bootstrap_fixture(tmp_path)
    policy = Path(command[command.index("--policy-source") + 1])
    policy.write_text(
        "compartment_id: c\n"
        "namespace: ns\n"
        "cams:\n"
        "  cam_alice: {customers: ['223']}\n",
        encoding="utf-8",
    )

    result = subprocess.run(command, env=env, capture_output=True, text=True)

    assert result.returncode != 0
    assert not (
        root_prefix / "etc" / "ssh" / "sshd_config.d" / "90-logan-cam.conf"
    ).exists()
    assert not systemctl_log.exists()


def test_fake_root_bootstrap_requires_instance_principal_config(tmp_path):
    root_prefix, command, env, _, systemctl_log = _bootstrap_fixture(tmp_path)
    config = Path(command[command.index("--config-source") + 1])
    config.write_text(
        "oci:\n  auth_type: config_file\n",
        encoding="utf-8",
    )

    result = subprocess.run(command, env=env, capture_output=True, text=True)

    assert result.returncode != 0
    assert not (root_prefix / "etc" / "logan-mcp" / "config.yaml").exists()
    assert not systemctl_log.exists()
