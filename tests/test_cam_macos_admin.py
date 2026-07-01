import json
import os
import re
import shutil
import stat
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROVISION = ROOT / "cam-setup" / "admin" / "macos" / "Provision-Logan-CAM.command"
DEPROVISION = ROOT / "cam-setup" / "admin" / "macos" / "Deprovision-Logan-CAM.command"
MAC_INSTALLER = ROOT / "cam-setup" / "bundle" / "macos" / "Install-Logan-MCP.command"
README = ROOT / "cam-setup" / "bundle" / "README.html"
WINDOWS_PS = ROOT / "cam-setup" / "bundle" / "windows" / "Install-Logan-MCP.ps1"
WINDOWS_CMD = ROOT / "cam-setup" / "bundle" / "windows" / "Double-Click-to-Install.cmd"
CONTRACT = ROOT / "tests" / "fixtures" / "cam_admin_contract_v1"
PUBLIC_KEY = json.loads((CONTRACT / "provision.request.json").read_text())["public_key"]
FINGERPRINT = json.loads((CONTRACT / "provision.response.json").read_text())[
    "fingerprint"
]


def _write_executable(path, text):
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _make_admin_tree(tmp_path):
    tree = tmp_path / "Admin Tools With Spaces"
    admin = tree / "cam-setup" / "admin" / "macos"
    macos = tree / "cam-setup" / "bundle" / "macos"
    windows = tree / "cam-setup" / "bundle" / "windows"
    admin.mkdir(parents=True)
    macos.mkdir(parents=True)
    windows.mkdir(parents=True)
    shutil.copy2(PROVISION, admin / PROVISION.name)
    shutil.copy2(DEPROVISION, admin / DEPROVISION.name)
    shutil.copy2(MAC_INSTALLER, macos / MAC_INSTALLER.name)
    shutil.copy2(README, tree / "cam-setup" / "bundle" / README.name)
    shutil.copy2(WINDOWS_PS, windows / WINDOWS_PS.name)
    shutil.copy2(WINDOWS_CMD, windows / WINDOWS_CMD.name)
    return tree, admin / PROVISION.name, admin / DEPROVISION.name


def _fake_tools(tmp_path):
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "ssh",
        "#!/bin/sh\n"
        "set -eu\n"
        'printf \'%s\\n\' "$*" >> "$FAKE_SSH_LOG"\n'
        'if [ "${1:-}" = -G ]; then\n'
        '  [ "${FAKE_SSH_G_FAIL:-0}" = 0 ] || exit 255\n'
        "  printf 'hostname logan.example\\nuser admin\\nport 22\\n'\n"
        "  exit 0\n"
        "fi\n"
        'case "$*" in\n'
        "  *'cam-admin provision --json'*)\n"
        '    cat > "$FAKE_PROVISION_REQUEST"\n'
        '    cat "$FAKE_PROVISION_RESPONSE"\n'
        '    exit "${FAKE_PROVISION_EXIT:-0}" ;;\n'
        "  *'cam-admin show --cam '*' --json'*)\n"
        '    cat "$FAKE_SHOW_RESPONSE"\n'
        '    exit "${FAKE_SHOW_EXIT:-0}" ;;\n'
        "  *'cam-admin deprovision --json'*)\n"
        '    cat > "$FAKE_DEPROVISION_REQUEST"\n'
        '    cat "$FAKE_DEPROVISION_RESPONSE"\n'
        '    exit "${FAKE_DEPROVISION_EXIT:-0}" ;;\n'
        "esac\n"
        "exit 99\n",
    )
    _write_executable(
        fake_bin / "ssh-keygen",
        "#!/bin/sh\n"
        "set -eu\n"
        'case " $* " in\n'
        "  *' -lf '*)\n"
        f"    printf '256 {FINGERPRINT} logan-cam:cam_alice (ED25519)\\n'\n"
        "    exit 0 ;;\n"
        "esac\n"
        "key_path=\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = -f ]; then shift; key_path=$1; fi\n'
        "  shift\n"
        "done\n"
        '[ -n "$key_path" ]\n'
        "printf 'PRIVATE TEST KEY\\n' > \"$key_path\"\n"
        f"printf '%s\\n' '{PUBLIC_KEY}' > \"$key_path.pub\"\n"
        'chmod 600 "$key_path"\n'
        'chmod 644 "$key_path.pub"\n',
    )
    _write_executable(
        fake_bin / "osascript",
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "json_mode = sys.argv[-1] == 'json'\n"
        "file_arg = sys.argv[-3] if json_mode else sys.argv[-2]\n"
        "field = sys.argv[-2] if json_mode else sys.argv[-1]\n"
        "obj = json.load(open(file_arg, encoding='utf-8'))\n"
        "value = obj\n"
        "for part in field.split('.'):\n"
        "    value = value[part]\n"
        "if field == 'customers' and (not isinstance(value, list) or "
        "any(type(item) is not int or item <= 0 for item in value)):\n"
        "    raise SystemExit(1)\n"
        "if field in ('allow_delivery', 'access_revoked') and type(value) is not bool:\n"
        "    raise SystemExit(1)\n"
        "if field == 'connection.port' and type(value) is not int:\n"
        "    raise SystemExit(1)\n"
        "if field in ('failures', 'warnings') and (not isinstance(value, list) or "
        "any(not isinstance(item, str) for item in value)):\n"
        "    raise SystemExit(1)\n"
        "if field in ('status', 'cam_id', 'fingerprint', 'backup_dir', "
        "'connection.host', 'connection.remote_user', 'connection.host_public_key') "
        "and not isinstance(value, str):\n"
        "    raise SystemExit(1)\n"
        "if json_mode:\n"
        "    print(json.dumps(value, separators=(',', ':')))\n"
        "    raise SystemExit(0)\n"
        "if isinstance(value, list):\n"
        "    print(','.join(str(item) for item in value))\n"
        "elif value is True:\n"
        "    print('true')\n"
        "elif value is False:\n"
        "    print('false')\n"
        "else:\n"
        "    if not isinstance(value, (str, int)) or isinstance(value, bool):\n"
        "        raise SystemExit(1)\n"
        "    print(value)\n",
    )
    _write_executable(
        fake_bin / "ditto",
        "#!/bin/sh\n"
        "set -eu\n"
        'if [ "${FAKE_DITTO_FAIL:-0}" = 1 ]; then exit 71; fi\n'
        '/usr/bin/ditto "$@"\n'
        'if [ -n "${FAKE_CONCURRENT_ARCHIVE:-}" ]; then\n'
        "  printf 'concurrent owner data\\n' > \"$FAKE_CONCURRENT_ARCHIVE\"\n"
        "fi\n",
    )
    _write_executable(
        fake_bin / "mv",
        "#!/bin/sh\n"
        "set -eu\n"
        'case "$*" in\n'
        "  *recovery-metadata.json*)\n"
        '    if [ "${FAKE_METADATA_MV_FAIL:-0}" = 1 ]; then exit 74; fi ;;\n'
        "esac\n"
        'exec /bin/mv "$@"\n',
    )
    return fake_bin


def _responses(tmp_path):
    provision = tmp_path / "provision.response.json"
    show = tmp_path / "show.response.json"
    deprovision = tmp_path / "deprovision.response.json"
    provision.write_text((CONTRACT / "provision.response.json").read_text())
    show.write_text((CONTRACT / "show.response.json").read_text())
    deprovision.write_text((CONTRACT / "deprovision.success.response.json").read_text())
    return provision, show, deprovision


def _admin_env(tmp_path, fake_bin, provision, show, deprovision, **updates):
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(tmp_path / "home"),
        "FAKE_SSH_LOG": str(tmp_path / "ssh.log"),
        "FAKE_PROVISION_REQUEST": str(tmp_path / "provision.request.json"),
        "FAKE_PROVISION_RESPONSE": str(provision),
        "FAKE_SHOW_RESPONSE": str(show),
        "FAKE_DEPROVISION_REQUEST": str(tmp_path / "deprovision.request.json"),
        "FAKE_DEPROVISION_RESPONSE": str(deprovision),
    }
    env.update({key: str(value) for key, value in updates.items()})
    return env


def _run_provision(script, output, env, *extra):
    return subprocess.run(
        [
            str(script),
            "--cam",
            "cam_alice",
            "--customers",
            "223",
            "--allow-delivery",
            "false",
            "--ssh-target",
            "automation1",
            "--output",
            str(output),
            "--yes",
            *extra,
        ],
        env=env,
        capture_output=True,
        text=True,
    )


def _run_deprovision(script, env, *, yes=True, input_text=""):
    command = [
        str(script),
        "--cam",
        "cam_alice",
        "--ssh-target",
        "automation1",
    ]
    if yes:
        command.append("--yes")
    return subprocess.run(
        command, env=env, input=input_text, capture_output=True, text=True
    )


def test_admin_wrappers_parse_and_have_fixed_safe_command_surfaces():
    for script in (PROVISION, DEPROVISION):
        parsed = subprocess.run(
            ["/bin/sh", "-n", str(script)], capture_output=True, text=True
        )
        assert parsed.returncode == 0, parsed.stderr
        text = script.read_text(encoding="utf-8")
        assert "umask 077" in text
        assert "osascript -l JavaScript" in text
        assert "eval" not in text
        assert "ssh -G" in text
    provision = PROVISION.read_text(encoding="utf-8")
    assert "sudo /opt/logan-mcp/bin/cam-admin provision --json" in provision
    assert "sudo /opt/logan-mcp/bin/cam-admin deprovision --json" in provision
    assert "trap" in provision
    assert provision.index("trap early_stage_cleanup") < provision.index(
        'mkdir "$STAGE_ROOT"'
    )
    deprovision = DEPROVISION.read_text(encoding="utf-8")
    assert "sudo /opt/logan-mcp/bin/cam-admin show --cam" in deprovision
    assert "sudo /opt/logan-mcp/bin/cam-admin deprovision --json" in deprovision


def test_readme_uses_only_fixed_metadata_and_safe_end_user_guidance():
    text = README.read_text(encoding="utf-8")
    assert set(re.findall(r"@@CAM_[A-Z_]+@@", text)) == {
        "@@CAM_ID@@",
        "@@CAM_SERVER_NAME@@",
        "@@CAM_CREATED_AT@@",
        "@@CAM_FINGERPRINT@@",
    }
    for token in (
        "@@CAM_ID@@",
        "@@CAM_SERVER_NAME@@",
        "@@CAM_CREATED_AT@@",
        "@@CAM_FINGERPRINT@@",
    ):
        assert text.count(token) >= 1
    for guidance in (
        "Install-Logan-MCP.command",
        "Double-Click-to-Install.cmd",
        "Restart Codex",
        "compromise",
    ):
        assert guidance.lower() in text.lower()
    for forbidden in (
        "cam-admin",
        "allow_delivery",
        "customer",
        "access_control",
        "BEGIN OPENSSH PRIVATE KEY",
    ):
        assert forbidden not in text


def test_provision_sends_exact_request_and_publishes_flat_cross_platform_bundle(
    tmp_path,
):
    _, provision_script, _ = _make_admin_tree(tmp_path)
    fake_bin = _fake_tools(tmp_path)
    responses = _responses(tmp_path)
    env = _admin_env(tmp_path, fake_bin, *responses)
    output = tmp_path / "Output With Spaces"

    result = _run_provision(provision_script, output, env)

    assert result.returncode == 0, result.stderr
    assert json.loads(Path(env["FAKE_PROVISION_REQUEST"]).read_text()) == {
        "cam_id": "cam_alice",
        "customers": [223],
        "allow_delivery": False,
        "public_key": PUBLIC_KEY,
    }
    bundle = output / "logan-cam-cam_alice"
    assert sorted(path.name for path in bundle.iterdir()) == [
        "Double-Click-to-Install.cmd",
        "Install-Logan-MCP.command",
        "Install-Logan-MCP.ps1",
        "README.html",
        "logan-cam.key",
    ]
    assert (bundle / "logan-cam.key").read_text() == "PRIVATE TEST KEY\n"
    assert stat.S_IMODE((bundle / "logan-cam.key").stat().st_mode) == 0o600
    for rendered in bundle.iterdir():
        assert "@@CAM_" not in rendered.read_text(encoding="utf-8")
    mac = (bundle / "Install-Logan-MCP.command").read_text()
    assert "130.162.53.112" in mac
    assert "root@host" in mac
    readme = (bundle / "README.html").read_text()
    assert "cam_alice" in readme
    assert FINGERPRINT in readme
    archive = output / "logan-cam-cam_alice.zip"
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    with zipfile.ZipFile(archive) as zipped:
        names = set(zipped.namelist())
    assert "logan-cam-cam_alice/logan-cam.key" in names
    assert "plain ZIP" in result.stdout


@pytest.mark.parametrize(
    ("args", "env_update", "precreate"),
    (
        (("--customers", "223,0"), {}, None),
        (("--allow-delivery", "yes"), {}, None),
        ((), {"FAKE_SSH_G_FAIL": "1"}, None),
        ((), {}, "logan-cam-cam_alice"),
        ((), {}, "logan-cam-cam_alice.zip"),
    ),
)
def test_provision_validation_fails_before_remote_provision(
    tmp_path, args, env_update, precreate
):
    _, provision_script, _ = _make_admin_tree(tmp_path)
    fake_bin = _fake_tools(tmp_path)
    responses = _responses(tmp_path)
    env = _admin_env(tmp_path, fake_bin, *responses, **env_update)
    output = tmp_path / "output"
    output.mkdir()
    if precreate:
        target = output / precreate
        target.mkdir() if "." not in precreate else target.write_text("existing")
    base = [
        "--cam",
        "cam_alice",
        "--customers",
        "223",
        "--allow-delivery",
        "false",
        "--ssh-target",
        "automation1",
        "--output",
        str(output),
        "--yes",
    ]
    for index in range(0, len(args), 2):
        flag, value = args[index : index + 2]
        base[base.index(flag) + 1] = value

    result = subprocess.run(
        [str(provision_script), *base], env=env, capture_output=True, text=True
    )

    assert result.returncode != 0
    log = Path(env["FAKE_SSH_LOG"])
    if log.exists():
        assert "cam-admin provision --json" not in log.read_text()


def test_provision_response_mismatch_is_rolled_back_with_exact_fingerprint(tmp_path):
    _, provision_script, _ = _make_admin_tree(tmp_path)
    fake_bin = _fake_tools(tmp_path)
    provision, show, deprovision = _responses(tmp_path)
    payload = json.loads(provision.read_text())
    payload["customers"] = [999]
    provision.write_text(json.dumps(payload))
    env = _admin_env(tmp_path, fake_bin, provision, show, deprovision)
    output = tmp_path / "output"

    result = _run_provision(provision_script, output, env)

    assert result.returncode != 0
    assert json.loads(Path(env["FAKE_DEPROVISION_REQUEST"]).read_text()) == {
        "cam_id": "cam_alice",
        "expected_fingerprint": FINGERPRINT,
        "confirm": True,
    }
    assert not (output / "logan-cam-cam_alice").exists()
    assert not (output / "logan-cam-cam_alice.zip").exists()


@pytest.mark.parametrize(
    ("response_text", "ssh_exit"),
    (("", "255"), ("not-json\n", "0")),
)
def test_lost_or_malformed_provision_response_rolls_back_local_fingerprint(
    tmp_path, response_text, ssh_exit
):
    _, provision_script, _ = _make_admin_tree(tmp_path)
    fake_bin = _fake_tools(tmp_path)
    provision, show, deprovision = _responses(tmp_path)
    provision.write_text(response_text)
    env = _admin_env(
        tmp_path,
        fake_bin,
        provision,
        show,
        deprovision,
        FAKE_PROVISION_EXIT=ssh_exit,
    )
    output = tmp_path / "output"

    result = _run_provision(provision_script, output, env)

    assert result.returncode != 0
    assert json.loads(Path(env["FAKE_DEPROVISION_REQUEST"]).read_text()) == {
        "cam_id": "cam_alice",
        "expected_fingerprint": FINGERPRINT,
        "confirm": True,
    }
    assert not (output / "logan-cam-cam_alice").exists()
    assert not (output / "logan-cam-cam_alice.zip").exists()


def test_cleanup_required_rollback_retains_sanitized_actual_recovery_metadata(
    tmp_path,
):
    _, provision_script, _ = _make_admin_tree(tmp_path)
    fake_bin = _fake_tools(tmp_path)
    provision, show, deprovision = _responses(tmp_path)
    provision.write_text("not-json\n")
    deprovision.write_text(
        (CONTRACT / "deprovision.cleanup-required.response.json").read_text()
    )
    env = _admin_env(
        tmp_path,
        fake_bin,
        provision,
        show,
        deprovision,
        FAKE_DEPROVISION_EXIT="1",
    )
    output = tmp_path / "output"

    result = _run_provision(provision_script, output, env)

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "access is revoked" in combined.lower()
    assert "cleanup remains" in combined.lower()
    assert "HIGH-SEVERITY" not in combined
    retained = list(output.glob("logan-cam-cam_alice-FAILED-metadata.*"))
    assert len(retained) == 1
    metadata = json.loads(retained[0].read_text())
    assert metadata["rollback_request"] == {
        "cam_id": "cam_alice",
        "expected_fingerprint": FINGERPRINT,
        "confirm": True,
    }
    assert metadata["rollback_response"]["status"] == (
        "FAILED_ACCESS_REVOKED_CLEANUP_REQUIRED"
    )
    assert metadata["rollback_response"]["access_revoked"] is True
    assert metadata["rollback_response"]["failures"] == ["policy:OSError"]
    assert metadata["rollback_response"]["backup_dir"] == "<BACKUP_DIR>"
    assert metadata["rollback_response"]["warnings"] == []
    retained_text = retained[0].read_text()
    assert "public_key" not in retained_text
    assert "PRIVATE TEST KEY" not in retained_text


def test_cleanup_required_metadata_publish_failure_reports_unpublished_state(
    tmp_path,
):
    _, provision_script, _ = _make_admin_tree(tmp_path)
    fake_bin = _fake_tools(tmp_path)
    provision, show, deprovision = _responses(tmp_path)
    provision.write_text("not-json\n")
    deprovision.write_text(
        (CONTRACT / "deprovision.cleanup-required.response.json").read_text()
    )
    env = _admin_env(
        tmp_path,
        fake_bin,
        provision,
        show,
        deprovision,
        FAKE_DEPROVISION_EXIT="1",
        FAKE_METADATA_MV_FAIL="1",
    )
    output = tmp_path / "output"

    result = _run_provision(provision_script, output, env)

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "HIGH-SEVERITY" in combined
    assert "metadata retention failed" in combined.lower()
    assert "Recovery metadata path: NOT PUBLISHED" in combined
    assert "metadata was retained" not in combined
    exact_request = json.dumps(
        {
            "cam_id": "cam_alice",
            "expected_fingerprint": FINGERPRINT,
            "confirm": True,
        },
        separators=(",", ":"),
    )
    assert exact_request in combined
    assert "sudo /opt/logan-mcp/bin/cam-admin deprovision --json" in combined
    assert not list(output.glob("logan-cam-cam_alice-FAILED-metadata.*"))


def test_unconfirmed_rollback_retains_validated_sanitized_response_fields(tmp_path):
    _, provision_script, _ = _make_admin_tree(tmp_path)
    fake_bin = _fake_tools(tmp_path)
    provision, show, deprovision = _responses(tmp_path)
    provision.write_text("not-json\n")
    deprovision.write_text(
        (CONTRACT / "deprovision.unconfirmed.response.json").read_text()
    )
    env = _admin_env(
        tmp_path,
        fake_bin,
        provision,
        show,
        deprovision,
        FAKE_DEPROVISION_EXIT="1",
    )
    output = tmp_path / "output"

    result = _run_provision(provision_script, output, env)

    assert result.returncode != 0
    retained = list(output.glob("logan-cam-cam_alice-FAILED-metadata.*"))
    assert len(retained) == 1
    metadata = json.loads(retained[0].read_text())
    response = metadata["rollback_response"]
    fixture = json.loads(
        (CONTRACT / "deprovision.unconfirmed.response.json").read_text()
    )
    for field in (
        "status",
        "cam_id",
        "fingerprint",
        "access_revoked",
        "backup_dir",
        "failures",
        "warnings",
        "shared_account_fallback",
    ):
        assert response[field] == fixture[field]
    retained_text = retained[0].read_text()
    assert "public_key" not in retained_text
    assert "PRIVATE TEST KEY" not in retained_text


def test_recovery_metadata_is_unique_and_never_follows_existing_symlink(tmp_path):
    _, provision_script, _ = _make_admin_tree(tmp_path)
    fake_bin = _fake_tools(tmp_path)
    provision, show, deprovision = _responses(tmp_path)
    provision.write_text("not-json\n")
    deprovision.write_text(
        (CONTRACT / "deprovision.unconfirmed.response.json").read_text()
    )
    env = _admin_env(
        tmp_path,
        fake_bin,
        provision,
        show,
        deprovision,
        FAKE_DEPROVISION_EXIT="1",
    )
    output = tmp_path / "output"
    output.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("do not replace\n")
    legacy_path = output / "logan-cam-cam_alice-FAILED-metadata.json"
    legacy_path.symlink_to(victim)

    first = _run_provision(provision_script, output, env)
    first_records = {
        path: path.read_bytes()
        for path in output.glob("logan-cam-cam_alice-FAILED-metadata.*")
        if not path.is_symlink()
    }
    second = _run_provision(provision_script, output, env)

    assert first.returncode != 0
    assert second.returncode != 0
    assert victim.read_text() == "do not replace\n"
    assert legacy_path.is_symlink()
    records = [
        path
        for path in output.glob("logan-cam-cam_alice-FAILED-metadata.*")
        if not path.is_symlink()
    ]
    assert len(records) == 2
    assert all(path.read_bytes() == content for path, content in first_records.items())


def test_malformed_rollback_response_is_never_copied_into_metadata(tmp_path):
    _, provision_script, _ = _make_admin_tree(tmp_path)
    fake_bin = _fake_tools(tmp_path)
    provision, show, deprovision = _responses(tmp_path)
    provision.write_text("not-json\n")
    deprovision.write_text("BEGIN OPENSSH PRIVATE KEY injected\n")
    env = _admin_env(
        tmp_path,
        fake_bin,
        provision,
        show,
        deprovision,
        FAKE_DEPROVISION_EXIT="1",
    )
    output = tmp_path / "output"

    result = _run_provision(provision_script, output, env)

    assert result.returncode != 0
    retained = list(output.glob("logan-cam-cam_alice-FAILED-metadata.*"))
    assert len(retained) == 1
    retained_text = retained[0].read_text()
    assert "BEGIN OPENSSH PRIVATE KEY" not in retained_text
    assert json.loads(retained_text)["rollback_response"] == {
        "status": "UNCONFIRMED_OR_CONTRACT_MISMATCH"
    }


@pytest.mark.parametrize(
    ("rollback_fixture", "rollback_exit"),
    (
        ("deprovision.success.response.json", "1"),
        ("deprovision.cleanup-required.response.json", "0"),
        ("deprovision.unconfirmed.response.json", "1"),
    ),
)
def test_unconfirmed_or_exit_pair_mismatched_rollback_is_high_severity(
    tmp_path, rollback_fixture, rollback_exit
):
    _, provision_script, _ = _make_admin_tree(tmp_path)
    fake_bin = _fake_tools(tmp_path)
    provision, show, deprovision = _responses(tmp_path)
    provision.write_text("not-json\n")
    deprovision.write_text((CONTRACT / rollback_fixture).read_text())
    env = _admin_env(
        tmp_path,
        fake_bin,
        provision,
        show,
        deprovision,
        FAKE_DEPROVISION_EXIT=rollback_exit,
    )
    output = tmp_path / "output"

    result = _run_provision(provision_script, output, env)

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "HIGH-SEVERITY" in combined
    exact_request = json.dumps(
        {
            "cam_id": "cam_alice",
            "expected_fingerprint": FINGERPRINT,
            "confirm": True,
        },
        separators=(",", ":"),
    )
    assert exact_request in combined
    assert "sudo /opt/logan-mcp/bin/cam-admin deprovision --json" in combined


def test_provision_success_json_requires_zero_ssh_exit_and_rolls_back(tmp_path):
    _, provision_script, _ = _make_admin_tree(tmp_path)
    fake_bin = _fake_tools(tmp_path)
    responses = _responses(tmp_path)
    env = _admin_env(
        tmp_path,
        fake_bin,
        *responses,
        FAKE_PROVISION_EXIT="1",
    )
    output = tmp_path / "output"

    result = _run_provision(provision_script, output, env)

    assert result.returncode != 0
    assert Path(env["FAKE_DEPROVISION_REQUEST"]).exists()
    assert not (output / "logan-cam-cam_alice").exists()


def test_provision_normalizes_cam_and_trims_customer_csv(tmp_path):
    _, provision_script, _ = _make_admin_tree(tmp_path)
    fake_bin = _fake_tools(tmp_path)
    provision, show, deprovision = _responses(tmp_path)
    payload = json.loads(provision.read_text())
    payload["customers"] = [223, 66]
    provision.write_text(json.dumps(payload))
    env = _admin_env(tmp_path, fake_bin, provision, show, deprovision)
    output = tmp_path / "output"
    command = [
        str(provision_script),
        "--cam",
        "CAM_Alice",
        "--customers",
        " 223 , 066 ",
        "--allow-delivery",
        "false",
        "--ssh-target",
        "automation1",
        "--output",
        str(output),
        "--yes",
    ]

    result = subprocess.run(command, env=env, capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    request = json.loads(Path(env["FAKE_PROVISION_REQUEST"]).read_text())
    assert request["cam_id"] == "cam_alice"
    assert request["customers"] == [223, 66]
    assert (output / "logan-cam-cam_alice").is_dir()


def test_provision_interactive_empty_target_and_output_use_defaults(tmp_path):
    _, provision_script, _ = _make_admin_tree(tmp_path)
    fake_bin = _fake_tools(tmp_path)
    responses = _responses(tmp_path)
    env = _admin_env(tmp_path, fake_bin, *responses)
    command = [
        str(provision_script),
        "--cam",
        "cam_alice",
        "--customers",
        "223",
        "--allow-delivery",
        "false",
        "--yes",
    ]

    result = subprocess.run(
        command, env=env, input="\n\n", capture_output=True, text=True
    )

    assert result.returncode == 0, result.stderr
    default_output = Path(env["HOME"]) / "logan-cam-bundles"
    assert (default_output / "logan-cam-cam_alice").is_dir()
    assert "-G automation1" in Path(env["FAKE_SSH_LOG"]).read_text()


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("customers", ["223"]),
        ("allow_delivery", "false"),
        ("connection.host_public_key", PUBLIC_KEY.rsplit(" ", 1)[0] + " root\rhost"),
        ("connection.host_public_key", PUBLIC_KEY.rsplit(" ", 1)[0] + " root|host"),
    ),
)
def test_provision_rejects_wrong_json_types_and_unsafe_token_values(
    tmp_path, field, value
):
    _, provision_script, _ = _make_admin_tree(tmp_path)
    fake_bin = _fake_tools(tmp_path)
    provision, show, deprovision = _responses(tmp_path)
    payload = json.loads(provision.read_text())
    target = payload
    parts = field.split(".")
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = value
    provision.write_text(json.dumps(payload))
    env = _admin_env(tmp_path, fake_bin, provision, show, deprovision)
    output = tmp_path / "output"

    result = _run_provision(provision_script, output, env)

    assert result.returncode != 0
    assert not (output / "logan-cam-cam_alice").exists()
    assert not (output / "logan-cam-cam_alice.zip").exists()
    assert Path(env["FAKE_DEPROVISION_REQUEST"]).exists()


def test_publish_failure_and_failed_cleanup_retains_only_nonsecret_metadata(tmp_path):
    _, provision_script, _ = _make_admin_tree(tmp_path)
    fake_bin = _fake_tools(tmp_path)
    provision, show, deprovision = _responses(tmp_path)
    deprovision.write_text(
        (CONTRACT / "deprovision.unconfirmed.response.json").read_text()
    )
    env = _admin_env(
        tmp_path,
        fake_bin,
        provision,
        show,
        deprovision,
        FAKE_DITTO_FAIL="1",
        FAKE_DEPROVISION_EXIT="1",
    )
    output = tmp_path / "output"

    result = _run_provision(provision_script, output, env)

    assert result.returncode != 0
    assert "HIGH-SEVERITY" in result.stderr
    assert "manual" in result.stderr.lower()
    assert not (output / "logan-cam-cam_alice").exists()
    assert not (output / "logan-cam-cam_alice.zip").exists()
    retained = list(output.glob("logan-cam-cam_alice-FAILED-metadata.*"))
    assert len(retained) == 1
    retained_text = retained[0].read_text()
    assert "cam_alice" in retained_text
    assert FINGERPRINT in retained_text
    assert "PRIVATE TEST KEY" not in retained_text
    assert not any("logan-cam.key" in str(path) for path in output.rglob("*"))


def test_concurrent_archive_is_never_overwritten_or_removed(tmp_path):
    _, provision_script, _ = _make_admin_tree(tmp_path)
    fake_bin = _fake_tools(tmp_path)
    responses = _responses(tmp_path)
    output = tmp_path / "output"
    concurrent_archive = output / "logan-cam-cam_alice.zip"
    env = _admin_env(
        tmp_path,
        fake_bin,
        *responses,
        FAKE_CONCURRENT_ARCHIVE=concurrent_archive,
    )

    result = _run_provision(provision_script, output, env)

    assert result.returncode != 0
    assert concurrent_archive.read_text() == "concurrent owner data\n"
    assert not (output / "logan-cam-cam_alice").exists()
    assert Path(env["FAKE_DEPROVISION_REQUEST"]).exists()


@pytest.mark.parametrize(
    ("fixture", "ssh_exit", "expected_exit", "message"),
    (
        ("deprovision.success.response.json", "0", 0, "revoked"),
        (
            "deprovision.cleanup-required.response.json",
            "1",
            1,
            "cleanup remains",
        ),
        (
            "deprovision.unconfirmed.response.json",
            "1",
            1,
            "HIGH-SEVERITY",
        ),
    ),
)
def test_deprovision_branches_on_response_json_even_when_ssh_exits_nonzero(
    tmp_path, fixture, ssh_exit, expected_exit, message
):
    _, _, deprovision_script = _make_admin_tree(tmp_path)
    fake_bin = _fake_tools(tmp_path)
    provision, show, deprovision = _responses(tmp_path)
    deprovision.write_text((CONTRACT / fixture).read_text())
    env = _admin_env(
        tmp_path,
        fake_bin,
        provision,
        show,
        deprovision,
        FAKE_DEPROVISION_EXIT=ssh_exit,
    )

    result = _run_deprovision(deprovision_script, env)

    assert result.returncode == expected_exit
    assert message.lower() in (result.stdout + result.stderr).lower()
    assert json.loads(Path(env["FAKE_DEPROVISION_REQUEST"]).read_text()) == {
        "cam_id": "cam_alice",
        "expected_fingerprint": FINGERPRINT,
        "confirm": True,
    }


@pytest.mark.parametrize(
    ("fixture", "ssh_exit"),
    (
        ("deprovision.success.response.json", "1"),
        ("deprovision.cleanup-required.response.json", "0"),
        ("deprovision.unconfirmed.response.json", "0"),
    ),
)
def test_deprovision_rejects_frozen_status_exit_pair_mismatch(
    tmp_path, fixture, ssh_exit
):
    _, _, deprovision_script = _make_admin_tree(tmp_path)
    fake_bin = _fake_tools(tmp_path)
    provision, show, deprovision = _responses(tmp_path)
    deprovision.write_text((CONTRACT / fixture).read_text())
    env = _admin_env(
        tmp_path,
        fake_bin,
        provision,
        show,
        deprovision,
        FAKE_DEPROVISION_EXIT=ssh_exit,
    )

    result = _run_deprovision(deprovision_script, env)

    assert result.returncode != 0
    assert "HIGH-SEVERITY" in (result.stdout + result.stderr)


def test_show_success_json_requires_zero_ssh_exit_before_deprovision(tmp_path):
    _, _, deprovision_script = _make_admin_tree(tmp_path)
    fake_bin = _fake_tools(tmp_path)
    responses = _responses(tmp_path)
    env = _admin_env(tmp_path, fake_bin, *responses, FAKE_SHOW_EXIT="1")

    result = _run_deprovision(deprovision_script, env)

    assert result.returncode != 0
    assert "exit" in (result.stdout + result.stderr).lower()
    assert not Path(env["FAKE_DEPROVISION_REQUEST"]).exists()


def test_deprovision_normalizes_cam_and_empty_target_uses_default(tmp_path):
    _, _, deprovision_script = _make_admin_tree(tmp_path)
    fake_bin = _fake_tools(tmp_path)
    responses = _responses(tmp_path)
    env = _admin_env(tmp_path, fake_bin, *responses)
    command = [str(deprovision_script), "--cam", "CAM_Alice", "--yes"]

    result = subprocess.run(
        command, env=env, input="\n", capture_output=True, text=True
    )

    assert result.returncode == 0, result.stderr
    assert "-G automation1" in Path(env["FAKE_SSH_LOG"]).read_text()
    assert (
        json.loads(Path(env["FAKE_DEPROVISION_REQUEST"]).read_text())["cam_id"]
        == "cam_alice"
    )


def test_deprovision_requires_exact_typed_confirmation_without_yes(tmp_path):
    _, _, deprovision_script = _make_admin_tree(tmp_path)
    fake_bin = _fake_tools(tmp_path)
    responses = _responses(tmp_path)
    env = _admin_env(tmp_path, fake_bin, *responses)

    result = _run_deprovision(
        deprovision_script, env, yes=False, input_text="REVOKE someone_else\n"
    )

    assert result.returncode != 0
    assert not Path(env["FAKE_DEPROVISION_REQUEST"]).exists()
