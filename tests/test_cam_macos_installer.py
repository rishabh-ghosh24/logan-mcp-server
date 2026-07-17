import os
import re
import stat
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "cam-setup" / "bundle" / "macos" / "Install-Logan-MCP.command"
HOST_KEY = (
    "ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIGhoaGhoaGhoaGhoaGhoaGhoaGhoaGhoaGhoaGhoaGho "
    "root@host"
)


def _render_installer(tmp_path, *, host="logan.example", port="22"):
    bundle = tmp_path / "Bundle With Spaces"
    bundle.mkdir()
    script = bundle / INSTALLER.name
    text = INSTALLER.read_text(encoding="utf-8")
    replacements = {
        "@@CAM_HOST@@": host,
        "@@CAM_PORT@@": port,
        "@@CAM_REMOTE_USER@@": "cam",
        "@@CAM_HOST_PUBLIC_KEY@@": HOST_KEY,
    }
    for token, value in replacements.items():
        text = text.replace(token, value)
    script.write_text(text, encoding="utf-8")
    script.chmod(0o755)
    (bundle / "logan-cam.key").write_text("PRIVATE TEST KEY\n", encoding="utf-8")
    return script


def _run_installer(script, home, *args, input_text="", env_overrides=None):
    home.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "HOME": str(home), "TZ": "Pacific/Honolulu"}
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        ["/bin/sh", str(script), *args],
        env=env,
        input=input_text,
        capture_output=True,
        text=True,
    )


def _config(home):
    return home / ".codex" / "config.toml"


def _logan_table(config_text):
    parsed = tomllib.loads(config_text)
    return parsed["mcp_servers"]["assurance-logan"]


def test_installer_is_posix_shell_with_fixed_tokens_and_security_options():
    result = subprocess.run(
        ["/bin/sh", "-n", str(INSTALLER)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    text = INSTALLER.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh\nset -eu\n")
    assert "umask 077" in text
    assert "command -v mktemp" in text
    assert "candidate.$$" not in text
    for token in (
        "@@CAM_HOST@@",
        "@@CAM_PORT@@",
        "@@CAM_REMOTE_USER@@",
        "@@CAM_HOST_PUBLIC_KEY@@",
    ):
        assert token in text
    for option in (
        "BatchMode=yes",
        "IdentitiesOnly=yes",
        "StrictHostKeyChecking=yes",
        "UserKnownHostsFile",
        "ServerAliveInterval=60",
        "ServerAliveCountMax=3",
    ):
        assert option in text
    for forbidden in (
        "--user",
        "--enforce-access",
        "OCI_LOGAN_MCP_ACCESS_CONFIG",
        "OCI_LOGAN_MCP_ENFORCE_ACCESS",
        "SSH_ORIGINAL_COMMAND",
    ):
        assert forbidden not in text


@pytest.mark.parametrize(
    "original",
    (
        "",
        'model = "gpt-test"\n[projects."/tmp/work"]\ntrust_level = "trusted"\n',
        '[mcp_servers.logan-mcp]\ncommand = "regular"\n',
        '[mcp_servers.assurance-logan]\ncommand = "old"\nargs = ["bad"]\n',
        (
            "before = 1\n"
            '[mcp_servers.assurance-logan]\ncommand = "old"\n'
            '[mcp_servers.assurance-logan.env]\nSECRET = "remove"\n'
            '[unrelated]\nkeep = "yes"\n'
        ),
        (
            "before = 1\n"
            '[mcp_servers."assurance-logan"]\ncommand = "old"\n'
            '[mcp_servers."assurance-logan".env]\nSECRET = "remove"\n'
            "[other]\nkeep = true\n"
        ),
        (
            "before = 1\n"
            '["mcp_servers"."assurance-logan"]\ncommand = "old"\n'
            '["mcp_servers"."assurance-logan"."env"]\nSECRET = "remove"\n'
            "[[unrelated.items]]\nkeep = true\n"
        ),
        (
            "before = 1\n"
            '[["mcp_servers"."assurance-logan"]]\ncommand = "old"\n'
            '[[mcp_servers.assurance-logan.env]]\nSECRET = "remove"\n'
            '[[products]]\nname = "preserved"\n'
        ),
    ),
)
def test_installer_replaces_only_logan_tables_and_is_idempotent(tmp_path, original):
    script = _render_installer(tmp_path)
    home = tmp_path / "Home With Spaces"
    config = _config(home)
    config.parent.mkdir(parents=True)
    config.write_text(original, encoding="utf-8")

    first = _run_installer(script, home, "--non-interactive")
    assert first.returncode == 0, first.stderr
    installed = config.read_text(encoding="utf-8")
    table = _logan_table(installed)
    assert table["command"] == "ssh"
    assert table["args"][-1] == "cam@logan.example"
    assert installed.count("[mcp_servers.assurance-logan]") == 1
    assert 'command = "old"' not in installed
    assert 'SECRET = "remove"' not in installed
    if "[unrelated]" in original:
        assert '[unrelated]\nkeep = "yes"\n' in installed
    if "[other]" in original:
        assert "[other]\nkeep = true\n" in installed
    if "[[unrelated.items]]" in original:
        assert "[[unrelated.items]]\nkeep = true\n" in installed
    if "[[products]]" in original:
        assert '[[products]]\nname = "preserved"\n' in installed
    if "[mcp_servers.logan-mcp]" in original:
        assert '[mcp_servers.logan-mcp]\ncommand = "regular"\n' in installed

    second = _run_installer(script, home, "--non-interactive")
    assert second.returncode == 0, second.stderr
    assert config.read_text(encoding="utf-8") == installed


@pytest.mark.parametrize(
    "bad_config",
    (
        "[broken\nvalue = 1\n",
        '[mcp_servers.assurance-logan]\ncommand = "a"\n'
        '[mcp_servers."assurance-logan"]\ncommand = "b"\n',
        '[mcp_servers.assurance-logan]\ncommand = "a"\n'
        '[mcp_servers.assurance-logan]\ncommand = "b"\n',
        '[mcp_servers.assurance-logan]\ncommand = "a"\n'
        '[["mcp_servers"."assurance-logan"]]\ncommand = "b"\n',
        "[[broken]\nvalue = 1\n",
        '[mcp_servers."logan\\u002dmcp"]\ncommand = "must-stay"\n',
        'mcp_servers.assurance-logan = { command = "must-stay" }\n',
        '"mcp_servers"."assurance-logan" = { command = "must-stay" }\n',
        "'mcp_servers'.'assurance-logan' = { command = 'must-stay' }\n",
        'mcp_servers = { assurance-logan = { command = "must-stay" } }\n',
        '[mcp_servers]\nassurance-logan = { command = "must-stay" }\n',
        '[mcp_servers]\n"assurance-logan" = { command = "must-stay" }\n',
        "[mcp_servers]\n'assurance-logan' = { command = 'must-stay' }\n",
        'message = """\n[mcp_servers.assurance-logan]\ncommand = "text"\n"""\n',
        "message = '''\n[mcp_servers.assurance-logan]\ncommand = 'text'\n'''\n",
    ),
)
def test_installer_rejects_malformed_or_duplicate_target_tables_unchanged(
    tmp_path, bad_config
):
    script = _render_installer(tmp_path)
    home = tmp_path / "home"
    config = _config(home)
    config.parent.mkdir(parents=True)
    config.write_text(bad_config, encoding="utf-8")

    result = _run_installer(script, home, "--non-interactive")

    assert result.returncode != 0
    assert config.read_text(encoding="utf-8") == bad_config
    assert not list(config.parent.glob("config.toml.backup-*"))


def test_installer_refuses_config_path_directory_without_touching_it(tmp_path):
    script = _render_installer(tmp_path)
    home = tmp_path / "home"
    config = _config(home)
    config.mkdir(parents=True)
    original_mode = stat.S_IMODE(config.stat().st_mode)
    sentinel = config / "keep.txt"
    sentinel.write_text("untouched\n")

    result = _run_installer(script, home, "--non-interactive")

    assert result.returncode != 0
    assert config.is_dir()
    assert stat.S_IMODE(config.stat().st_mode) == original_mode
    assert list(config.iterdir()) == [sentinel]
    assert sentinel.read_text() == "untouched\n"
    assert not (home / ".logan-mcp" / "logan-cam.key").exists()


def test_installer_rejects_mktemp_dangling_symlink_without_clobber(tmp_path):
    script = _render_installer(tmp_path)
    home = tmp_path / "home"
    config = _config(home)
    config.parent.mkdir(parents=True)
    original = 'model = "keep"\n'
    config.write_text(original)
    victim = tmp_path / "victim"
    fake_candidate = config.parent / ".attacker-candidate"
    fake_candidate.symlink_to(victim)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_mktemp = fake_bin / "mktemp"
    fake_mktemp.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$FAKE_MKTEMP_PATH\"\n", encoding="utf-8"
    )
    fake_mktemp.chmod(0o755)

    result = _run_installer(
        script,
        home,
        "--non-interactive",
        env_overrides={
            "FAKE_MKTEMP_PATH": str(fake_candidate),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
        },
    )

    assert result.returncode != 0
    assert config.read_text() == original
    assert fake_candidate.is_symlink()
    assert not victim.exists()


def test_config_publish_failure_restores_key_and_known_hosts_bytes_and_modes(tmp_path):
    script = _render_installer(tmp_path)
    home = tmp_path / "home"
    config = _config(home)
    config.parent.mkdir(parents=True)
    config.write_text('model = "old"\n')
    config.chmod(0o640)
    install_dir = home / ".logan-mcp"
    install_dir.mkdir(mode=0o700)
    key = install_dir / "logan-cam.key"
    known_hosts = install_dir / "known_hosts"
    key.write_text("OLD PRIVATE KEY\n")
    known_hosts.write_text("old.example ssh-ed25519 OLD\n")
    key.chmod(0o640)
    known_hosts.chmod(0o644)
    before = {
        path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        for path in (config, key, known_hosts)
    }
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_mv = fake_bin / "mv"
    fake_mv.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "destination=\n"
        'for argument in "$@"; do destination=$argument; done\n'
        'if [ "${FAKE_CONFIG_MV_FAIL:-0}" = 1 ] && '
        '[ "$destination" = "$HOME/.codex/config.toml" ]; then exit 74; fi\n'
        'exec /bin/mv "$@"\n',
        encoding="utf-8",
    )
    fake_mv.chmod(0o755)

    result = _run_installer(
        script,
        home,
        "--non-interactive",
        env_overrides={
            "FAKE_CONFIG_MV_FAIL": "1",
            "PATH": f"{fake_bin}:/usr/bin:/bin",
        },
    )

    assert result.returncode != 0
    for path, (content, mode) in before.items():
        assert path.read_bytes() == content
        assert stat.S_IMODE(path.stat().st_mode) == mode


def test_installer_uses_pinned_host_key_secure_permissions_and_toml_safe_paths(
    tmp_path,
):
    script = _render_installer(tmp_path, host="host.example", port="2222")
    home = tmp_path / 'Home With "Quotes" And \\ Slash'

    result = _run_installer(script, home, "--non-interactive")

    assert result.returncode == 0, result.stderr
    install_dir = home / ".logan-mcp"
    key = install_dir / "logan-cam.key"
    known_hosts = install_dir / "known_hosts"
    assert key.read_text(encoding="utf-8") == "PRIVATE TEST KEY\n"
    assert known_hosts.read_text(encoding="utf-8") == (
        "[host.example]:2222 " + " ".join(HOST_KEY.split()[:2]) + "\n"
    )
    assert stat.S_IMODE(install_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert stat.S_IMODE(known_hosts.stat().st_mode) == 0o600
    args = _logan_table(_config(home).read_text(encoding="utf-8"))["args"]
    assert args == [
        "-i",
        str(key),
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        "-o",
        "ServerAliveInterval=60",
        "-o",
        "ServerAliveCountMax=3",
        "-p",
        "2222",
        "cam@host.example",
    ]


def test_installer_backs_up_existing_config_with_utc_timestamp(tmp_path):
    script = _render_installer(tmp_path)
    home = tmp_path / "home"
    config = _config(home)
    config.parent.mkdir(parents=True)
    original = 'model = "keep"\n'
    config.write_text(original, encoding="utf-8")

    result = _run_installer(script, home, "--non-interactive")

    assert result.returncode == 0, result.stderr
    backups = list(config.parent.glob("config.toml.backup-*Z"))
    assert len(backups) == 1
    assert re.fullmatch(r"config\.toml\.backup-\d{8}T\d{6}(?:\.\d+)?Z", backups[0].name)
    assert backups[0].read_text(encoding="utf-8") == original


def test_non_interactive_only_suppresses_final_pause(tmp_path):
    script = _render_installer(tmp_path)
    interactive_home = tmp_path / "interactive"
    batch_home = tmp_path / "batch"

    interactive = _run_installer(script, interactive_home, input_text="\n")
    batch = _run_installer(script, batch_home, "--non-interactive")

    assert interactive.returncode == 0, interactive.stderr
    assert batch.returncode == 0, batch.stderr
    assert "Press Return" in interactive.stdout
    assert "Press Return" not in batch.stdout
    assert _config(interactive_home).read_text() == _config(
        batch_home
    ).read_text().replace(str(batch_home), str(interactive_home))
