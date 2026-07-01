import signal
from pathlib import Path

from oci_logan_mcp.cam_processes import (
    ProcInspector,
    ProcessIdentity,
    ProcessTerminator,
    TerminationResult,
)

PYTHON = Path("/opt/logan-mcp/venv/bin/python")


def _proc(pid=101, cam_id="cam_alice", start_time=500, pgrp=101, uid=2001):
    return ProcessIdentity(
        pid=pid,
        uid=uid,
        executable=PYTHON,
        argv=(
            str(PYTHON),
            "-I",
            "-m",
            "oci_logan_mcp",
            "--enforce-access",
            "--user",
            cam_id,
        ),
        start_time=start_time,
        process_group=pgrp,
    )


class FakeInspector:
    def __init__(self, processes):
        self.processes = {process.pid: process for process in processes}

    def iter_processes(self):
        return tuple(self.processes.values())

    def inspect(self, pid):
        return self.processes.get(pid)


def test_terminate_cam_targets_only_exact_identity():
    alice = _proc(pid=101, cam_id="cam_alice", pgrp=301)
    bob = _proc(pid=102, cam_id="cam_bob", pgrp=302)
    signals = []
    inspector = FakeInspector([alice, bob])
    terminator = ProcessTerminator(
        inspector=inspector,
        killpg=lambda pgrp, sig: signals.append((pgrp, sig)),
        sleep=lambda seconds: inspector.processes.pop(101, None),
        cam_uid=2001,
        runtime_python=PYTHON,
    )

    result = terminator.terminate_cam("cam_alice", grace_seconds=0)

    assert result == TerminationResult(matched=1, terminated=1, killed=0)
    assert signals == [(301, signal.SIGTERM)]


def test_terminate_cam_rejects_near_match_arguments_and_executable():
    valid = _proc(pid=101, pgrp=301)
    wrong_args = ProcessIdentity(
        **{
            **valid.__dict__,
            "pid": 102,
            "process_group": 302,
            "argv": valid.argv + ("--extra",),
        }
    )
    wrong_executable = ProcessIdentity(
        **{
            **valid.__dict__,
            "pid": 103,
            "process_group": 303,
            "executable": Path("/usr/bin/python3"),
        }
    )
    signals = []
    inspector = FakeInspector([wrong_args, wrong_executable])
    terminator = ProcessTerminator(
        inspector=inspector,
        killpg=lambda pgrp, sig: signals.append((pgrp, sig)),
        sleep=lambda seconds: None,
        cam_uid=2001,
        runtime_python=PYTHON,
    )

    result = terminator.terminate_cam("cam_alice", grace_seconds=0)

    assert result == TerminationResult(matched=0, terminated=0, killed=0)
    assert signals == []


def test_terminate_cam_does_not_signal_pid_reused_after_term():
    original = _proc(pid=101, start_time=500, pgrp=301)
    replacement = _proc(pid=101, start_time=900, pgrp=999)
    signals = []
    inspector = FakeInspector([original])

    def replace_after_term(seconds):
        inspector.processes[101] = replacement

    terminator = ProcessTerminator(
        inspector=inspector,
        killpg=lambda pgrp, sig: signals.append((pgrp, sig)),
        sleep=replace_after_term,
        cam_uid=2001,
        runtime_python=PYTHON,
    )

    result = terminator.terminate_cam("cam_alice", grace_seconds=0)

    assert signals == [(301, signal.SIGTERM)]
    assert result.killed == 0


def test_terminate_cam_escalates_to_kill_and_reports_survivor():
    process = _proc(pid=101, pgrp=301)
    signals = []
    inspector = FakeInspector([process])
    terminator = ProcessTerminator(
        inspector=inspector,
        killpg=lambda pgrp, sig: signals.append((pgrp, sig)),
        sleep=lambda seconds: None,
        cam_uid=2001,
        runtime_python=PYTHON,
    )

    try:
        terminator.terminate_cam("cam_alice", grace_seconds=0)
    except RuntimeError as exc:
        assert "101" in str(exc)
    else:
        raise AssertionError("a surviving process must be reported")

    assert signals == [
        (301, signal.SIGTERM),
        (301, signal.SIGKILL),
    ]


def test_shared_fallback_terminates_all_restricted_account_groups_except_own():
    processes = [
        _proc(pid=101, cam_id="cam_alice", pgrp=301),
        _proc(pid=102, cam_id="cam_bob", pgrp=302),
        _proc(pid=103, cam_id="cam_admin", pgrp=999),
    ]
    signals = []
    inspector = FakeInspector(processes)

    def remove_signaled_groups(seconds):
        inspector.processes = {
            pid: process
            for pid, process in inspector.processes.items()
            if process.process_group == 999
        }

    terminator = ProcessTerminator(
        inspector=inspector,
        killpg=lambda pgrp, sig: signals.append((pgrp, sig)),
        sleep=remove_signaled_groups,
        getpgrp=lambda: 999,
        cam_uid=2001,
        runtime_python=PYTHON,
    )

    count = terminator.terminate_restricted_account(grace_seconds=0)

    assert count == 2
    assert signals == [(301, signal.SIGTERM), (302, signal.SIGTERM)]


def _write_fake_proc(proc_root, executable):
    process_root = proc_root / "101"
    process_root.mkdir(parents=True)
    (process_root / "status").write_text(
        "Name:\tpython\nUid:\t2001\t2001\t2001\t2001\n",
        encoding="utf-8",
    )
    (process_root / "cmdline").write_bytes(
        b"/opt/logan-mcp/venv/bin/python\0-I\0-m\0oci_logan_mcp\0"
        b"--enforce-access\0--user\0cam_alice\0"
    )
    # Fields after the closing ')' begin at field 3 (state). pgrp is field 5
    # and starttime is field 22.
    fields = ["S", "1", "301"] + ["0"] * 16 + ["500"]
    (process_root / "stat").write_text(
        f"101 (cam worker) {' '.join(fields)}\n",
        encoding="utf-8",
    )
    (process_root / "exe").symlink_to(executable)


def test_proc_inspector_reads_exact_linux_identity(tmp_path):
    executable = tmp_path / "python"
    executable.write_bytes(b"")
    proc_root = tmp_path / "proc"
    _write_fake_proc(proc_root, executable)

    identity = ProcInspector(proc_root).inspect(101)

    assert identity == ProcessIdentity(
        pid=101,
        uid=2001,
        executable=executable,
        argv=(
            "/opt/logan-mcp/venv/bin/python",
            "-I",
            "-m",
            "oci_logan_mcp",
            "--enforce-access",
            "--user",
            "cam_alice",
        ),
        start_time=500,
        process_group=301,
    )


def test_proc_inspector_skips_malformed_or_disappearing_entries(tmp_path):
    proc_root = tmp_path / "proc"
    malformed = proc_root / "101"
    malformed.mkdir(parents=True)
    (malformed / "status").write_text("Name:\tpython\n", encoding="utf-8")
    (proc_root / "not-a-pid").mkdir()

    inspector = ProcInspector(proc_root)

    assert inspector.inspect(101) is None
    assert tuple(inspector.iter_processes()) == ()
