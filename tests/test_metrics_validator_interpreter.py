"""Validator interpreter selection must survive its private working directory."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests.test_protocol_smoke import GOOD_CONFIG, _params


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/validate_metrics_live.py"


@pytest.mark.parametrize("selection", ["relative", "relative with spaces", "on_path"])
def test_validator_keeps_the_selected_environment_after_changing_directory(tmp_path, selection):
    params = _params(tmp_path, GOOD_CONFIG, True)
    metrics = tmp_path / "metrics-requests"
    metrics.mkdir()
    environment = params.env
    environment["LOGAN_FAKE_METRICS_DIR"] = str(metrics)
    if selection == "on_path":
        environment["PATH"] = str(Path(sys.executable).parent) + os.pathsep + environment.get("PATH", "")
        interpreter = Path(sys.executable).name
    else:
        directory = "venv" if selection == "relative" else "environment with spaces"
        (tmp_path / directory).symlink_to(sys.prefix, target_is_directory=True)
        interpreter = str(Path(directory) / "bin" / Path(sys.executable).name)
    config = tmp_path / "input.yaml"
    config.write_text(GOOD_CONFIG + "monitoring:\n  default_compartment_id: chosen\n  region: us-phoenix-1\n")
    evidence = tmp_path / "evidence"
    completed = subprocess.run([
        sys.executable, str(SCRIPT), "--config", str(config), "--python", interpreter,
        "--compartment-id", "chosen", "--region", "us-phoenix-1", "--output-dir", str(evidence),
    ], cwd=tmp_path, env=environment, text=True, capture_output=True, timeout=90)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads((evidence / "review-summary.json").read_text())
    assert report["status"] == "passed" and report["positive_query_count"] >= 1
    assert report["direct_comparison"] == "passed"
    private = json.loads((evidence / "private-results.json").read_text())
    assert private["sdk_comparison"]["equal"] is True
    assert private["sdk_comparison"]["point_count"] == 2
    calls = [json.loads(line) for line in (metrics / "requests.jsonl").read_text().splitlines()]
    assert sum(row["operation"] == "query" for row in calls) >= 4


@pytest.mark.parametrize("interpreter", ["missing-venv/bin/python", "metrics-no-such-interpreter"])
def test_validator_rejects_a_missing_interpreter_before_creating_evidence(tmp_path, interpreter):
    evidence = tmp_path / "evidence"
    completed = subprocess.run([
        sys.executable, str(SCRIPT), "--config", str(tmp_path / "unused.yaml"),
        "--python", interpreter, "--compartment-id", "unused", "--output-dir", str(evidence),
    ], cwd=tmp_path, text=True, capture_output=True, timeout=30)
    assert completed.returncode == 2, completed.stdout + completed.stderr
    assert "--python" in completed.stderr and "executable" in completed.stderr
    assert not evidence.exists()
