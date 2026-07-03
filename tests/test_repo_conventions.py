"""Repo-hygiene guards that scan tracked files for convention drift.

These tests run fully offline (no OCI connectivity) and exist to stop classes
of mistakes from silently re-entering the repo.
"""

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# The Python virtualenv is named `venv` (no leading dot) everywhere a path is
# referenced: README.md, the setup/run scripts, run_tests.py, and the MCP client
# configs. A stray dotted `.venv/` path silently breaks deployments whose venv is
# `venv/` -- the launcher dies with `bash: .../.venv/bin/oci-logan-mcp: No such
# file or directory`. Keep everything on the `venv/` convention.
#
# Files/dirs allowed to mention `.venv/`:
#   - .gitignore intentionally ignores BOTH `venv/` and `.venv/`.
#   - docs/ holds frozen, point-in-time plan documents (historical record).
#   - this guard file necessarily contains the search pattern itself.
ALLOWED_FILES = {".gitignore", "tests/test_repo_conventions.py"}
ALLOWED_PREFIXES = ("docs/",)
ALLOWED_PRIVATE_KEY_MARKER_FILES = {
    # Documentation-only examples whose PEM bodies are literally `...`.
    "windows-setup/windows-setup.html",
}


def _dot_venv_references():
    """Return tracked-file lines (path:line:content) that reference `.venv/`."""
    result = subprocess.run(
        ["git", "grep", "-In", r"\.venv/"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    offenders = []
    for line in result.stdout.splitlines():
        path = line.split(":", 1)[0]
        if path in ALLOWED_FILES:
            continue
        if any(path.startswith(prefix) for prefix in ALLOWED_PREFIXES):
            continue
        offenders.append(line)
    return offenders


def test_no_dot_venv_in_live_files():
    """Live scripts/docs/configs must use `venv/` (no dot), never `.venv/`."""
    offenders = _dot_venv_references()
    assert not offenders, (
        "Found dotted `.venv/` references; the repo convention is `venv` (no "
        "leading dot) to match README.md and scripts/. Fix these to `venv/`:\n  "
        + "\n  ".join(offenders)
    )


def _git_grep(pattern, *pathspecs):
    """Return tracked-file matches for an extended regular expression."""
    return subprocess.run(
        ["git", "grep", "-InE", pattern, "--", *pathspecs],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    ).stdout.splitlines()


def test_no_tracked_private_keys_or_generated_cam_bundles():
    """Bearer credentials and generated CAM bundles must never be committed."""
    marker = "BEGIN (OPENSSH|RSA|EC) PRIVATE" + " KEY"
    offenders = _git_grep(
        marker,
        ".",
        ":!tests/**",
        ":!cam-setup/tests/**",
    )
    offenders = [
        line
        for line in offenders
        if line.split(":", 1)[0] not in ALLOWED_PRIVATE_KEY_MARKER_FILES
    ]
    assert offenders == []

    tracked = subprocess.run(
        [
            "git",
            "ls-files",
            "logan-cam-*.zip",
            "logan-cam-*.key",
            "cam-setup/output/**",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert tracked == []


def test_generated_cam_bearer_credentials_are_ignored():
    """Every normal generated-bundle location must be ignored by Git."""
    generated_paths = (
        "cam-setup/output/logan-cam-test/logan-cam.key",
        "cam-setup/output/logan-cam-test.zip",
        "logan-cam-test/logan-cam.key",
        "logan-cam-test.zip",
        "test.cam-provision.log",
        "test.cam-deprovision.log",
    )
    for generated_path in generated_paths:
        result = subprocess.run(
            ["git", "check-ignore", "--quiet", "--no-index", generated_path],
            cwd=REPO_ROOT,
        )
        assert (
            result.returncode == 0
        ), f"generated CAM artifact is not ignored: {generated_path}"


def test_cam_bundle_templates_have_no_editable_identity_or_remote_command():
    """CAM bundles rely only on the server-side forced-command identity."""
    bundle_root = REPO_ROOT / "cam-setup" / "bundle"
    offenders = []
    for path in bundle_root.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for forbidden in (
            "--user",
            "--enforce-access",
            "OCI_LOGAN_MCP_ACCESS_CONFIG",
            "OCI_LOGAN_MCP_ENFORCE_ACCESS",
        ):
            if forbidden in text:
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {forbidden}")
    assert offenders == []


def test_cam_layer1_admin_guide_is_linked_and_covers_the_lifecycle():
    """The operator guide must keep every security-critical lifecycle gate visible."""
    guide_path = REPO_ROOT / "docs" / "cam-layer1-admin-guide.md"
    assert guide_path.is_file()
    guide = guide_path.read_text(encoding="utf-8")
    required_phrases = (
        "Responsibility split",
        "bootstrap-cam-server.sh",
        "bootstrap-check --json",
        "Provision-Logan-CAM.command",
        "Provision-Logan-CAM.cmd",
        "recipient-specific",
        "seven days",
        "cam-admin show",
        "cam-admin verify",
        "FAILED_ACCESS_REVOKED_CLEANUP_REQUIRED",
        "FAILED_REVOCATION_UNCONFIRMED",
        "saved and learned queries",
        "Deferred archive action item",
        "Native Windows",
        "automation1",
    )
    for phrase in required_phrases:
        assert phrase in guide, f"administrator guide is missing: {phrase}"

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/cam-layer1-admin-guide.md" in readme


def test_cam_layer1_live_probe_contract():
    """The probe must validate scope and create an explicit retention artifact."""
    probe = REPO_ROOT / "scripts" / "cam-layer1-live-probe.py"
    assert probe.is_file()
    subprocess.run(["python3", "-m", "py_compile", str(probe)], check=True)
    text = probe.read_text(encoding="utf-8")
    for required in (
        '"set_compartment"',
        '"list_entities"',
        '"save_learned_query"',
        "session.call_tool",
        '"status": "PASS"',
        '"--ready-file"',
        '"--attempt-env-injection"',
    ):
        assert required in text


def test_cam_layer1_live_acceptance_harness_contract():
    """The live harness must be explicit-gate, complete, and retention-safe."""
    harness = REPO_ROOT / "scripts" / "cam-layer1-live-acceptance.sh"
    assert harness.is_file()
    subprocess.run(["bash", "-n", str(harness)], check=True)
    text = harness.read_text(encoding="utf-8")
    for required in (
        "CAM-LAYER1-LIVE",
        "bootstrap-check --json",
        "cam-admin provision --json",
        "cam-admin deprovision --json",
        "cam-layer1-live-probe.py",
        "command-override",
        "environment-injection",
        "local-forwarding",
        "agent-forwarding",
        "x11-forwarding",
        "scp-refusal",
        "sftp-refusal",
        "learned_queries.yaml",
    ):
        assert required in text
    assert "rm -rf /home/cam" not in text
    assert 'rm -rf "/home/cam' not in text


def test_live_acceptance_refuses_unsafe_invocation_before_ssh(tmp_path):
    """Missing confirmation or a non-automation target must not reach SSH."""
    harness = REPO_ROOT / "scripts" / "cam-layer1-live-acceptance.sh"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "ssh-called"
    fake_ssh = fake_bin / "ssh"
    fake_ssh.write_text(
        '#!/bin/sh\n: > "$FAKE_SSH_MARKER"\nexit 99\n',
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)
    env = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "FAKE_SSH_MARKER": str(marker),
    }

    missing_confirmation = subprocess.run(
        [str(harness), "--target", "automation1", "--customer", "223"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert missing_confirmation.returncode != 0
    assert not marker.exists()

    wrong_target = subprocess.run(
        [
            str(harness),
            "--target",
            "another-host",
            "--customer",
            "223",
            "--confirm-live",
            "CAM-LAYER1-LIVE",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert wrong_target.returncode != 0
    assert not marker.exists()
