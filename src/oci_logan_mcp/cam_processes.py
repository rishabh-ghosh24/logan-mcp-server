"""Validated Linux process inspection and CAM session termination."""

from __future__ import annotations

import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Protocol

DEFAULT_RUNTIME_PYTHON = Path("/opt/logan-mcp/venv/bin/python")
CAM_LAUNCH_ARGV_TEMPLATE = (
    str(DEFAULT_RUNTIME_PYTHON),
    "-I",
    "-m",
    "oci_logan_mcp",
    "--enforce-access",
    "--user",
    "{cam_id}",
)


def render_cam_launch_argv(
    cam_id: str,
    runtime_python: Path = DEFAULT_RUNTIME_PYTHON,
) -> tuple[str, ...]:
    return (
        str(runtime_python),
        *(value.format(cam_id=cam_id) for value in CAM_LAUNCH_ARGV_TEMPLATE[1:]),
    )


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    uid: int
    executable: Path
    argv: tuple[str, ...]
    start_time: int
    process_group: int


@dataclass(frozen=True)
class TerminationResult:
    matched: int
    terminated: int
    killed: int


class Inspector(Protocol):
    def iter_processes(self) -> Iterable[ProcessIdentity]:
        raise NotImplementedError

    def inspect(self, pid: int) -> ProcessIdentity | None:
        raise NotImplementedError


class ProcInspector:
    def __init__(self, proc_root: Path = Path("/proc")):
        self.proc_root = proc_root

    def iter_processes(self) -> Iterable[ProcessIdentity]:
        try:
            children = tuple(self.proc_root.iterdir())
        except OSError:
            return
        for child in children:
            if child.name.isdigit():
                identity = self.inspect(int(child.name))
                if identity is not None:
                    yield identity

    def inspect(self, pid: int) -> ProcessIdentity | None:
        root = self.proc_root / str(pid)
        try:
            status = (root / "status").read_text(encoding="utf-8")
            uid_line = next(
                line for line in status.splitlines() if line.startswith("Uid:")
            )
            uid = int(uid_line.split()[1])
            executable = (root / "exe").resolve(strict=True)
            argv = tuple(
                part.decode("utf-8", errors="strict")
                for part in (root / "cmdline").read_bytes().split(b"\0")
                if part
            )
            raw_stat = (root / "stat").read_text(encoding="utf-8")
            fields = raw_stat[raw_stat.rfind(")") + 2 :].split()
            process_group = int(fields[2])
            start_time = int(fields[19])
            return ProcessIdentity(
                pid=pid,
                uid=uid,
                executable=executable,
                argv=argv,
                start_time=start_time,
                process_group=process_group,
            )
        except (OSError, StopIteration, ValueError, UnicodeError, IndexError):
            return None


class ProcessTerminator:
    def __init__(
        self,
        inspector: Inspector,
        killpg: Callable[[int, int], None] = os.killpg,
        sleep: Callable[[float], None] = time.sleep,
        getpgrp: Callable[[], int] = os.getpgrp,
        cam_uid: int = -1,
        runtime_python: Path = DEFAULT_RUNTIME_PYTHON,
    ):
        self.inspector = inspector
        self.killpg = killpg
        self.sleep = sleep
        self.getpgrp = getpgrp
        self.cam_uid = cam_uid
        self.runtime_python_path = Path(runtime_python)
        self.runtime_python = self.runtime_python_path.resolve(strict=False)

    def _is_cam(self, process: ProcessIdentity, cam_id: str) -> bool:
        expected_argv = render_cam_launch_argv(cam_id, self.runtime_python_path)
        return (
            process.uid == self.cam_uid
            and process.executable.resolve(strict=False) == self.runtime_python
            and process.argv == expected_argv
        )

    @staticmethod
    def _same_process(
        before: ProcessIdentity,
        after: ProcessIdentity | None,
    ) -> bool:
        return after is not None and (
            before.pid,
            before.uid,
            before.executable,
            before.argv,
            before.start_time,
            before.process_group,
        ) == (
            after.pid,
            after.uid,
            after.executable,
            after.argv,
            after.start_time,
            after.process_group,
        )

    def terminate_cam(
        self,
        cam_id: str,
        grace_seconds: float = 5.0,
    ) -> TerminationResult:
        matches = [
            process
            for process in self.inspector.iter_processes()
            if self._is_cam(process, cam_id)
        ]
        groups: dict[int, ProcessIdentity] = {}
        own_group = self.getpgrp()
        for process in matches:
            if process.process_group != own_group:
                groups.setdefault(process.process_group, process)

        terminated = 0
        killed = 0
        for process in groups.values():
            current = self.inspector.inspect(process.pid)
            if not self._same_process(process, current):
                continue
            try:
                self.killpg(process.process_group, signal.SIGTERM)
            except ProcessLookupError:
                continue
            terminated += 1

        if terminated:
            self.sleep(grace_seconds)
        for process in groups.values():
            current = self.inspector.inspect(process.pid)
            if self._same_process(process, current):
                try:
                    self.killpg(process.process_group, signal.SIGKILL)
                except ProcessLookupError:
                    continue
                killed += 1

        if killed:
            self.sleep(0.1)
        survivors = [
            process.pid
            for process in groups.values()
            if self._same_process(process, self.inspector.inspect(process.pid))
        ]
        if survivors:
            raise RuntimeError(f"CAM processes survived termination: {survivors}")
        if len(groups) != len({p.process_group for p in matches}):
            raise RuntimeError("refusing to terminate the administration process group")
        return TerminationResult(len(matches), terminated, killed)

    def terminate_restricted_account(self, grace_seconds: float = 5.0) -> int:
        own_group = self.getpgrp()
        groups = sorted(
            {
                process.process_group
                for process in self.inspector.iter_processes()
                if process.uid == self.cam_uid and process.process_group != own_group
            }
        )
        for process_group in groups:
            try:
                self.killpg(process_group, signal.SIGTERM)
            except ProcessLookupError:
                continue

        if groups:
            self.sleep(grace_seconds)
        live_groups = {
            process.process_group
            for process in self.inspector.iter_processes()
            if process.uid == self.cam_uid and process.process_group != own_group
        }
        killed = 0
        for process_group in sorted(live_groups):
            try:
                self.killpg(process_group, signal.SIGKILL)
            except ProcessLookupError:
                continue
            killed += 1
        if killed:
            self.sleep(0.1)

        survivors = sorted(
            {
                process.process_group
                for process in self.inspector.iter_processes()
                if process.uid == self.cam_uid and process.process_group != own_group
            }
        )
        if survivors:
            raise RuntimeError(
                f"restricted-account process groups survived termination: {survivors}"
            )
        return len(groups)
