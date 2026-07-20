"""Offline checks for unrestricted Assurance-user installers."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tomllib
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
WINDOWS_INSTALLER = ROOT / "windows-setup" / "logan-mcp.ps1"
MACOS_INSTALLER = ROOT / "macos-setup" / "Install-Logan-MCP.command"


def test_windows_installer_uses_stable_production_launcher_and_name():
    source = WINDOWS_INSTALLER.read_text(encoding="utf-8")

    assert "/home/opc/logan-mcp-server" not in source
    assert "sudo -n /opt/logan-mcp/bin/admin-launch" in source
    assert "[mcp_servers.assurance-logan]" in source
    assert 'command = "ssh.exe"' in source
    assert 'StrictHostKeyChecking=yes' in source
    assert 'UserKnownHostsFile=' in source
    assert 'StrictHostKeyChecking=no' not in source
    assert "logan-mcp|assurance-logan" in source
    assert "New-PrivateFileSecurity" in source
    assert "AreAccessRulesProtected" in source
    assert "Assert-SafeExistingFile" in source
    assert source.rindex("Test-LoganSshConnection -KeyPath") < source.rindex(
        "Remove-OldInstalledKeys -InstallDir"
    )


def test_macos_installer_is_executable_posix_shell_with_pinned_host():
    result = subprocess.run(
        ["/bin/sh", "-n", str(MACOS_INSTALLER)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(MACOS_INSTALLER.stat().st_mode) == 0o755

    source = MACOS_INSTALLER.read_text(encoding="utf-8")
    assert source.startswith("#!/bin/sh\nset -eu\numask 077\n")
    assert "/home/opc/logan-mcp-server" not in source
    assert "/opt/logan-mcp/bin/admin-launch" in source
    assert "[mcp_servers.assurance-logan]" in source
    assert "StrictHostKeyChecking=yes" in source
    assert "UserKnownHostsFile=" in source
    assert "StrictHostKeyChecking=no" not in source


def _run_macos_installer(tmp_path: Path, original: str, user: str = "Andrei.Popa"):
    bundle = tmp_path / "Unrestricted Bundle"
    bundle.mkdir()
    installer = bundle / MACOS_INSTALLER.name
    shutil.copy2(MACOS_INSTALLER, installer)
    installer.chmod(0o755)
    (bundle / "logan.key").write_text("TEST PRIVATE KEY\n", encoding="utf-8")

    home = tmp_path / "Home With Spaces"
    config = home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(original, encoding="utf-8")
    result = subprocess.run(
        [
            "/bin/sh",
            str(installer),
            "--user",
            user,
            "--non-interactive",
            "--skip-ssh-test",
        ],
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
    )
    return result, home, config


@pytest.mark.parametrize(
    "original",
    (
        '[mcp_servers.logan-mcp]\ncommand = "ssh"\nargs = ["legacy"]\n',
        '[mcp_servers."assurance-logan"]\ncommand = "ssh"\nargs = ["old"]\n',
        (
            'model = "gpt-test"\n'
            '[mcp_servers.assurance-logan]\ncommand = "ssh"\nargs = ["old"]\n'
            '[mcp_servers.assurance-logan.env]\nOLD = "remove"\n'
            '[mcp_servers.other]\ncommand = "keep"\n'
        ),
    ),
)
def test_macos_installer_migrates_legacy_connection(original, tmp_path):
    result, home, config = _run_macos_installer(tmp_path, original)
    assert result.returncode == 0, result.stderr
    assert "Configured Codex MCP server: assurance-logan" in result.stdout

    parsed = tomllib.loads(config.read_text(encoding="utf-8"))
    table = parsed["mcp_servers"]["assurance-logan"]
    assert table["command"] == "ssh"
    assert table["args"][-2] == "opc@130.162.53.112"
    assert table["args"][-1] == (
        "sudo -n /opt/logan-mcp/bin/admin-launch andrei.popa"
    )
    assert "logan-mcp" not in parsed.get("mcp_servers", {})
    assert "OLD" not in config.read_text(encoding="utf-8")
    if "[mcp_servers.other]" in original:
        assert parsed["mcp_servers"]["other"]["command"] == "keep"

    for path in (
        config,
        home / ".logan-mcp" / "logan.key",
        home / ".logan-mcp" / "known_hosts",
    ):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(config.parent.glob("config.toml.backup-*"))


def test_macos_installer_rejects_invalid_identity_without_changing_config(tmp_path):
    original = '[mcp_servers.other]\ncommand = "keep"\n'
    result, _home, config = _run_macos_installer(
        tmp_path,
        original,
        user="andrei;id",
    )
    assert result.returncode != 0
    assert config.read_text(encoding="utf-8") == original


def test_macos_installer_rejects_ambiguous_inline_logan_config(tmp_path):
    original = '[mcp_servers]\n"logan-mcp" = { command = "ssh" }\n'
    result, _home, config = _run_macos_installer(tmp_path, original)
    assert result.returncode != 0
    assert "Unsupported inline Logan MCP declaration" in result.stderr
    assert config.read_text(encoding="utf-8") == original


def test_cam_revoke_wrappers_remain_available_on_both_admin_platforms():
    expected = (
        ROOT / "cam-setup" / "admin" / "macos" / "Deprovision-Logan-CAM.command",
        ROOT / "cam-setup" / "admin" / "windows" / "Deprovision-Logan-CAM.ps1",
        ROOT / "cam-setup" / "admin" / "windows" / "Deprovision-Logan-CAM.cmd",
    )
    assert all(path.is_file() for path in expected)


def test_shared_private_key_package_paths_are_gitignored():
    for relative in ("windows-setup/logan.key", "macos-setup/logan.key"):
        result = subprocess.run(
            ["git", "check-ignore", "--quiet", "--no-index", relative],
            cwd=ROOT,
        )
        assert result.returncode == 0, f"shared key path is not ignored: {relative}"


def test_handover_builder_creates_four_self_contained_role_packages(tmp_path):
    key = tmp_path / "approved-shared.key"
    key.write_text("TEST PRIVATE KEY\n", encoding="utf-8")
    output = tmp_path / "handover packages"
    result = subprocess.run(
        [
            str(ROOT / "scripts" / "build-handover-packages.sh"),
            "--key",
            str(key),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    expected = {
        "assurance-logan-user-macos": {
            "Install-Logan-MCP.command",
            "README.html",
            "logan.key",
        },
        "assurance-logan-user-windows": {
            "Double-Click-to-Install.cmd",
            "logan-mcp.ps1",
            "README.html",
            "logan.key",
        },
        "assurance-logan-admin-macos": {
            "1-Install-My-Assurance-Profile.command",
            "2-Provision-CAM.command",
            "3-Revoke-CAM.command",
            "README.html",
            "logan.key",
            "internal/bin/ssh",
            "internal/known_hosts",
            "internal/cam-setup/admin/macos/Provision-Logan-CAM.command",
            "internal/cam-setup/admin/macos/Deprovision-Logan-CAM.command",
            "internal/cam-setup/bundle/windows/Install-Logan-MCP.ps1",
        },
        "assurance-logan-admin-windows": {
            "1-Install-My-Assurance-Profile.cmd",
            "2-Provision-CAM.cmd",
            "2-Provision-CAM.ps1",
            "3-Revoke-CAM.cmd",
            "3-Revoke-CAM.ps1",
            "README.html",
            "logan-mcp.ps1",
            "logan.key",
            "internal/known_hosts",
            "internal/cam-setup/admin/windows/Provision-Logan-CAM.ps1",
            "internal/cam-setup/admin/windows/Deprovision-Logan-CAM.ps1",
            "internal/cam-setup/bundle/macos/Install-Logan-MCP.command",
        },
    }
    for package, required in expected.items():
        archive = output / f"{package}.zip"
        assert archive.is_file()
        with zipfile.ZipFile(archive) as zipped:
            names = set(zipped.namelist())
            assert not any("__MACOSX" in name for name in names)
            assert {f"{package}/{name}" for name in required} <= names
            assert zipped.read(f"{package}/logan.key") == b"TEST PRIVATE KEY\n"

    checksums = (output / "SHA256SUMS.txt").read_text(encoding="utf-8")
    assert checksums.count(".zip") == 4


def test_windows_cam_admin_scripts_accept_a_package_supplied_ssh_config():
    for name in ("Provision-Logan-CAM.ps1", "Deprovision-Logan-CAM.ps1"):
        source = (ROOT / "cam-setup" / "admin" / "windows" / name).read_text(
            encoding="utf-8"
        )
        assert "[string]$SshConfigFile" in source
        assert "@SshConfigArguments" in source
        assert "ssh.exe -G $Target" not in source
