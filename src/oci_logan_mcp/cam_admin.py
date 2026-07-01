"""Root-only transactional administration service for CAM access."""

from __future__ import annotations

import argparse
import asyncio
import copy
import grp
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence, TextIO

from .access_control import (
    AccessConfigError,
    AccessControlConfig,
    build_profile,
    load_access_config,
    validate_cam_id,
)
from .cam_admin_store import (
    AdminRequestError,
    CamAdminPaths,
    CamStateStore,
    ConcurrentMutationError,
    DeprovisionRequest,
    ProvisionRequest,
    authorized_key_records,
    build_forced_key_line,
    managed_key_records,
    parse_ed25519_public_key,
    serialize_authorized_keys,
    serialize_policy,
    sha256_bytes,
    validate_policy_document,
)
from .cam_processes import ProcInspector, ProcessTerminator


class CamAdminError(RuntimeError):
    """Raised when a CAM administration operation cannot be completed safely."""


@dataclass(frozen=True)
class SystemAccount:
    uid: int
    gid: int
    groups: tuple[str, ...]


class SystemInspector:
    """Small injectable adapter for Linux ownership and command checks."""

    def stat(self, path: Path) -> os.stat_result:
        return Path(path).stat()

    def lookup_user(self, name: str) -> SystemAccount:
        account = pwd.getpwnam(name)
        groups = {
            group.gr_name
            for group in grp.getgrall()
            if name in group.gr_mem
        }
        try:
            groups.add(grp.getgrgid(account.pw_gid).gr_name)
        except KeyError:
            pass
        return SystemAccount(account.pw_uid, account.pw_gid, tuple(sorted(groups)))

    def run(
        self,
        argv: Sequence[str],
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=None if env is None else dict(env),
        )

    def write_probe(self, path: Path, uid: int, gid: int) -> bool:
        path = Path(path)
        probe = path / f".logan-cam-write-probe-{uuid.uuid4().hex}"
        if os.geteuid() not in (0, uid):
            return False
        child = os.fork()
        if child == 0:
            try:
                if os.geteuid() == 0:
                    os.setgroups([])
                    os.setgid(gid)
                    os.setuid(uid)
                descriptor = os.open(
                    probe,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                os.close(descriptor)
                probe.unlink()
            except Exception:
                os._exit(1)
            os._exit(0)

        _, wait_status = os.waitpid(child, 0)
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass
        return os.WIFEXITED(wait_status) and os.WEXITSTATUS(wait_status) == 0

    def tree_secure(self, path: Path, uid: int) -> bool:
        root = Path(path)
        try:
            entries = [root]
            for current, directories, files in os.walk(root, followlinks=False):
                current_path = Path(current)
                entries.extend(current_path / name for name in directories)
                entries.extend(current_path / name for name in files)
            for entry in entries:
                metadata = entry.lstat()
                if metadata.st_uid != uid:
                    return False
                if stat.S_ISLNK(metadata.st_mode):
                    target = entry.resolve(strict=True).stat()
                    if target.st_uid != uid or stat.S_IMODE(target.st_mode) & 0o022:
                        return False
                elif stat.S_IMODE(metadata.st_mode) & 0o022:
                    return False
        except OSError:
            return False
        return True


class CamAdminService:
    def __init__(
        self,
        store: CamStateStore,
        entity_resolver: Callable[[AccessControlConfig], Awaitable[list[str]]],
        connection: Mapping[str, Any],
        actor_provider: Callable[[], str],
        process_terminator: Any | None = None,
        system_inspector: Any | None = None,
    ):
        self.store = store
        self.entity_resolver = entity_resolver
        self.connection = dict(connection)
        self.actor_provider = actor_provider
        self.process_terminator = process_terminator
        self.system_inspector = system_inspector

    @staticmethod
    def _operation_id(operation: str, cam_id: str) -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        return f"{operation}-{cam_id}-{timestamp}-{uuid.uuid4().hex[:12]}"

    @staticmethod
    def _build_profile(config, cam_id, entities):
        try:
            return build_profile(config, cam_id, entities)
        except AccessConfigError as exc:
            raise CamAdminError(str(exc)) from exc

    @staticmethod
    def _validate_cam_id(cam_id: object) -> str:
        try:
            return validate_cam_id(cam_id)
        except AccessConfigError as exc:
            raise CamAdminError(str(exc)) from exc

    def show(self, cam_id: str) -> dict[str, Any]:
        cam_id = self._validate_cam_id(cam_id)
        with self.store.locked():
            live = self.store.read_live()
            entry = live.policy.get("cams", {}).get(cam_id)
            records = [
                record
                for record in managed_key_records(live.authorized_key_lines)
                if record.managed_cam_id == cam_id
            ]
            if not isinstance(entry, dict) or len(records) != 1:
                raise CamAdminError(
                    f"CAM '{cam_id}' does not have one consistent live record"
                )
            return {
                "status": "SUCCESS",
                "cam_id": cam_id,
                "customers": list(entry.get("customers", [])),
                "allow_delivery": entry.get("allow_delivery", False),
                "fingerprint": records[0].key.fingerprint,
            }

    async def verify(self, cam_id: str) -> dict[str, Any]:
        cam_id = self._validate_cam_id(cam_id)
        with self.store.locked():
            live = self.store.read_live()
            config = validate_policy_document(self.store, live.policy)
            records = [
                record
                for record in managed_key_records(live.authorized_key_lines)
                if record.managed_cam_id == cam_id
            ]
            if len(records) != 1:
                raise CamAdminError(f"CAM '{cam_id}' does not have one forced key")
            entities = await self.entity_resolver(config)
            profile = self._build_profile(config, cam_id, entities)
            return {
                "status": "SUCCESS",
                "cam_id": cam_id,
                "customers": list(profile.customer_numbers),
                "resolved_entities": sorted(profile.entity_names),
                "fingerprint": records[0].key.fingerprint,
            }

    async def provision(self, request: ProvisionRequest) -> dict[str, Any]:
        operation_id = self._operation_id("provision", request.cam_id)
        with self.store.locked():
            live = self.store.read_live()
            records = managed_key_records(live.authorized_key_lines)
            if request.cam_id in live.policy.get("cams", {}) or any(
                record.managed_cam_id == request.cam_id for record in records
            ):
                raise CamAdminError(f"CAM '{request.cam_id}' already exists")

            all_keys = authorized_key_records(live.authorized_key_lines)
            if any(
                record.key.fingerprint == request.key.fingerprint
                for record in all_keys
            ):
                raise CamAdminError("public-key fingerprint already exists")

            candidate = copy.deepcopy(live.policy)
            candidate.setdefault("cams", {})[request.cam_id] = {
                "customers": list(request.customers),
                "allow_delivery": request.allow_delivery,
            }
            config = validate_policy_document(self.store, candidate)
            entities = await self.entity_resolver(config)
            profile = self._build_profile(config, request.cam_id, entities)

            forced_line = build_forced_key_line(
                request.cam_id,
                request.key,
                self.store.paths.launcher_path,
            )
            candidate_lines = (*live.authorized_key_lines, forced_line)
            expected_policy_hash = sha256_bytes(serialize_policy(candidate))
            expected_key_hash = sha256_bytes(
                serialize_authorized_keys(candidate_lines)
            )
            backups = self.store.back_up(live, operation_id)

            try:
                policy_hash = self.store.replace_policy(candidate, live)
                key_hash = self.store.replace_authorized_keys(candidate_lines, live)
                if (
                    policy_hash != expected_policy_hash
                    or key_hash != expected_key_hash
                ):
                    raise CamAdminError("candidate hash mismatch")

                verified = self.store.read_live()
                if (
                    verified.policy_hash != policy_hash
                    or verified.authorized_keys_hash != key_hash
                ):
                    raise CamAdminError("post-write hash verification failed")
                verified_entry = verified.policy.get("cams", {}).get(request.cam_id)
                verified_records = [
                    record
                    for record in managed_key_records(
                        verified.authorized_key_lines
                    )
                    if record.managed_cam_id == request.cam_id
                ]
                if verified_entry != candidate["cams"][request.cam_id]:
                    raise CamAdminError("post-write policy verification failed")
                if (
                    len(verified_records) != 1
                    or verified_records[0].key.fingerprint
                    != request.key.fingerprint
                ):
                    raise CamAdminError("post-write forced-key verification failed")

                policy_metadata = (
                    verified.policy_stat.st_uid,
                    verified.policy_stat.st_gid,
                    stat.S_IMODE(verified.policy_stat.st_mode),
                )
                original_policy_metadata = (
                    live.policy_stat.st_uid,
                    live.policy_stat.st_gid,
                    stat.S_IMODE(live.policy_stat.st_mode),
                )
                if policy_metadata != original_policy_metadata:
                    raise CamAdminError(
                        "post-write policy ownership verification failed"
                    )
                key_metadata = (
                    verified.authorized_keys_stat.st_uid,
                    verified.authorized_keys_stat.st_gid,
                    stat.S_IMODE(verified.authorized_keys_stat.st_mode),
                )
                original_key_metadata = (
                    live.authorized_keys_stat.st_uid,
                    live.authorized_keys_stat.st_gid,
                    stat.S_IMODE(live.authorized_keys_stat.st_mode),
                )
                if key_metadata != original_key_metadata:
                    raise CamAdminError(
                        "post-write key ownership verification failed"
                    )

                self.store.append_audit(
                    {
                        "actor": self.actor_provider(),
                        "operation": "provision",
                        "cam_id": request.cam_id,
                        "customers": list(request.customers),
                        "allow_delivery": request.allow_delivery,
                        "fingerprint": request.key.fingerprint,
                        "before": {
                            "policy": live.policy_hash,
                            "authorized_keys": live.authorized_keys_hash,
                        },
                        "after": {
                            "policy": policy_hash,
                            "authorized_keys": key_hash,
                        },
                        "backup_dir": str(backups.policy_path.parent),
                        "outcome": "success",
                    }
                )
            except Exception as exc:
                current = self.store.read_live()
                if current.authorized_keys_hash == expected_key_hash:
                    self.store.restore_authorized_keys(
                        backups,
                        expected_hash=expected_key_hash,
                    )
                elif current.authorized_keys_hash != live.authorized_keys_hash:
                    raise ConcurrentMutationError(
                        "cannot roll back unexpected authorized_keys content"
                    ) from exc

                current = self.store.read_live()
                if current.policy_hash == expected_policy_hash:
                    self.store.restore_policy(
                        backups,
                        expected_hash=expected_policy_hash,
                    )
                elif current.policy_hash != live.policy_hash:
                    raise ConcurrentMutationError(
                        "cannot roll back unexpected policy content"
                    ) from exc
                raise

            return {
                "status": "SUCCESS",
                "cam_id": request.cam_id,
                "customers": list(profile.customer_numbers),
                "allow_delivery": profile.allow_delivery,
                "fingerprint": request.key.fingerprint,
                "connection": dict(self.connection),
            }

    async def deprovision(
        self,
        request: DeprovisionRequest,
    ) -> dict[str, Any]:
        operation_id = self._operation_id("deprovision", request.cam_id)
        with self.store.locked():
            live = self.store.read_live()
            records = [
                record
                for record in managed_key_records(live.authorized_key_lines)
                if record.managed_cam_id == request.cam_id
            ]
            entry = live.policy.get("cams", {}).get(request.cam_id)
            if not isinstance(entry, dict) or len(records) != 1:
                raise CamAdminError(
                    "CAM does not have one consistent policy/key pair"
                )
            record = records[0]
            if record.key.fingerprint != request.expected_fingerprint:
                raise CamAdminError(
                    "CAM key fingerprint changed; refresh show output"
                )
            if self.process_terminator is None:
                raise CamAdminError(
                    "process terminator is required for deprovisioning"
                )

            backups = self.store.back_up(live, operation_id)
            candidate_policy = copy.deepcopy(live.policy)
            del candidate_policy["cams"][request.cam_id]
            candidate_lines = tuple(
                line
                for index, line in enumerate(live.authorized_key_lines)
                if index != record.index
            )
            expected_policy_hash = sha256_bytes(
                serialize_policy(candidate_policy)
            )
            expected_key_hash = sha256_bytes(
                serialize_authorized_keys(candidate_lines)
            )

            failures: list[str] = []
            try:
                self.store.replace_policy(candidate_policy, live)
            except Exception as exc:
                failures.append(f"policy:{type(exc).__name__}")
            try:
                self.store.replace_authorized_keys(candidate_lines, live)
            except Exception as exc:
                failures.append(f"authorized_keys:{type(exc).__name__}")

            observed = self.store.read_live()
            policy_removed = observed.policy_hash == expected_policy_hash
            key_removed = observed.authorized_keys_hash == expected_key_hash
            if observed.policy_hash not in (
                live.policy_hash,
                expected_policy_hash,
            ):
                failures.append("policy:unexpected_live_hash")
            if observed.authorized_keys_hash not in (
                live.authorized_keys_hash,
                expected_key_hash,
            ):
                failures.append("authorized_keys:unexpected_live_hash")

            warnings: list[str] = []
            shared_fallback = not policy_removed
            exact_result = None
            try:
                exact_result = self.process_terminator.terminate_cam(
                    request.cam_id
                )
            except Exception as exc:
                warnings.append(
                    f"exact_process_termination:{type(exc).__name__}"
                )
                shared_fallback = True

            fallback_count = 0
            if shared_fallback:
                try:
                    fallback_count = (
                        self.process_terminator.terminate_restricted_account()
                    )
                except Exception as exc:
                    failures.append(
                        f"shared_process_termination:{type(exc).__name__}"
                    )

            if not policy_removed and not key_removed:
                status = "FAILED_REVOCATION_UNCONFIRMED"
            elif failures:
                status = "FAILED_ACCESS_REVOKED_CLEANUP_REQUIRED"
            else:
                status = "SUCCESS"

            verified = self.store.read_live()
            verified_records = [
                item
                for item in managed_key_records(verified.authorized_key_lines)
                if item.managed_cam_id == request.cam_id
            ]
            policy_absent = request.cam_id not in verified.policy.get("cams", {})
            key_absent = not verified_records
            if status == "SUCCESS" and not (policy_absent and key_absent):
                failures.append("verification:record_still_present")
                status = "FAILED_ACCESS_REVOKED_CLEANUP_REQUIRED"

            event = {
                "actor": self.actor_provider(),
                "operation": "deprovision",
                "cam_id": request.cam_id,
                "customers": list(entry.get("customers", [])),
                "allow_delivery": entry.get("allow_delivery", False),
                "fingerprint": record.key.fingerprint,
                "before": {
                    "policy": live.policy_hash,
                    "authorized_keys": live.authorized_keys_hash,
                },
                "after": {
                    "policy": observed.policy_hash,
                    "authorized_keys": observed.authorized_keys_hash,
                },
                "backup_dir": str(backups.policy_path.parent),
                "terminated_processes": (
                    0 if exact_result is None else exact_result.terminated
                ),
                "killed_processes": (
                    0 if exact_result is None else exact_result.killed
                ),
                "shared_account_fallback": shared_fallback,
                "shared_groups_terminated": fallback_count,
                "warnings": warnings,
                "failures": failures,
                "outcome": status.lower(),
            }
            try:
                self.store.append_audit(event)
            except Exception as exc:
                failures.append(f"audit:{type(exc).__name__}")
                if status == "SUCCESS":
                    status = "FAILED_ACCESS_REVOKED_CLEANUP_REQUIRED"

            return {
                "status": status,
                "cam_id": request.cam_id,
                "fingerprint": record.key.fingerprint,
                "access_revoked": policy_removed or key_removed,
                "shared_account_fallback": shared_fallback,
                "warnings": warnings,
                "failures": failures,
                "backup_dir": str(backups.policy_path.parent),
            }

    def bootstrap_check(self) -> dict[str, Any]:
        inspector = self.system_inspector or SystemInspector()
        paths = self.store.paths
        runtime_root = paths.runtime_python.parents[2]
        admin_command = runtime_root / "bin" / "cam-admin"
        config_path = paths.policy_path.parent / "config.yaml"
        connection_path = paths.policy_path.parent / "connection.json"
        cam_home = paths.authorized_keys_path.parent.parent
        ssh_dir = paths.authorized_keys_path.parent
        state_dir = cam_home / ".oci-logan-mcp"

        try:
            account = inspector.lookup_user("cam")
        except Exception:
            account = None

        def path_check(path, uid, gid, mode, kind):
            try:
                metadata = inspector.stat(path)
            except Exception:
                return False
            return (
                metadata.st_uid == uid
                and metadata.st_gid == gid
                and stat.S_IMODE(metadata.st_mode) == mode
                and kind(metadata.st_mode)
            )

        root_gid = 0
        cam_uid = -1 if account is None else account.uid
        cam_gid = -1 if account is None else account.gid
        runtime_root_owned = path_check(
            runtime_root,
            0,
            root_gid,
            0o755,
            stat.S_ISDIR,
        )
        if runtime_root_owned:
            try:
                runtime_root_owned = inspector.tree_secure(runtime_root, 0)
            except Exception:
                runtime_root_owned = False
        cam_home_immutable = path_check(
            cam_home,
            0,
            cam_gid,
            0o750,
            stat.S_ISDIR,
        )
        ssh_parent_immutable = path_check(
            ssh_dir,
            0,
            cam_gid,
            0o750,
            stat.S_ISDIR,
        )
        config_file_immutable = path_check(
            config_path,
            0,
            cam_gid,
            0o640,
            stat.S_ISREG,
        )
        connection_file_immutable = path_check(
            connection_path,
            0,
            cam_gid,
            0o640,
            stat.S_ISREG,
        )
        policy_file_immutable = path_check(
            paths.policy_path,
            0,
            cam_gid,
            0o640,
            stat.S_ISREG,
        )
        if policy_file_immutable:
            try:
                load_access_config(paths.policy_path)
            except Exception:
                policy_file_immutable = False

        checks: dict[str, bool] = {
            "runtime_root_owned": runtime_root_owned,
            "launcher_root_owned": path_check(
                paths.launcher_path,
                0,
                root_gid,
                0o755,
                stat.S_ISREG,
            ),
            "admin_command_root_owned": path_check(
                admin_command,
                0,
                root_gid,
                0o755,
                stat.S_ISREG,
            ),
            "config_parent_immutable": path_check(
                paths.policy_path.parent,
                0,
                cam_gid,
                0o750,
                stat.S_ISDIR,
            ),
            "config_immutable": (
                config_file_immutable and connection_file_immutable
            ),
            "policy_immutable": policy_file_immutable,
            "authorized_keys_immutable": (
                cam_home_immutable
                and ssh_parent_immutable
                and path_check(
                    paths.authorized_keys_path,
                    0,
                    cam_gid,
                    0o640,
                    stat.S_ISREG,
                )
            ),
            "runtime_state_writable": False,
            "cam_password_locked": False,
            "cam_not_admin": False,
            "sshd_effective_config": False,
            "instance_principal_init": False,
        }

        if account is not None:
            state_metadata_ok = path_check(
                state_dir,
                cam_uid,
                cam_gid,
                0o700,
                stat.S_ISDIR,
            )
            try:
                can_write_state = inspector.write_probe(
                    state_dir,
                    cam_uid,
                    cam_gid,
                )
            except Exception:
                can_write_state = False
            checks["runtime_state_writable"] = (
                cam_home_immutable and state_metadata_ok and can_write_state
            )

            try:
                password = inspector.run(["/usr/bin/passwd", "-S", "cam"])
                fields = password.stdout.split()
                checks["cam_password_locked"] = (
                    password.returncode == 0
                    and len(fields) >= 2
                    and fields[1].upper().startswith("L")
                )
            except Exception:
                pass

            admin_groups = {"root", "wheel", "sudo", "admin", "adm"}
            checks["cam_not_admin"] = (
                account.uid != 0
                and admin_groups.isdisjoint(
                    {group.lower() for group in account.groups}
                )
            )

        host = self.connection.get("host")
        if isinstance(host, str) and host:
            try:
                sshd = inspector.run(
                    [
                        "/usr/sbin/sshd",
                        "-T",
                        "-C",
                        f"user=cam,host={host},addr=127.0.0.1",
                    ]
                )
                checks["sshd_effective_config"] = (
                    sshd.returncode == 0
                    and self._valid_effective_sshd_config(sshd.stdout)
                )
            except Exception:
                pass

        instance_code = (
            "from oci_logan_mcp.client import OCILogAnalyticsClient; "
            "from oci_logan_mcp.config import load_config; "
            "settings=load_config(); "
            "assert settings.oci.auth_type == 'instance_principal'; "
            "OCILogAnalyticsClient(settings)"
        )
        try:
            instance = inspector.run(
                [str(paths.runtime_python), "-I", "-c", instance_code],
                env={
                    "HOME": str(cam_home),
                    "USER": "cam",
                    "LOGNAME": "cam",
                    "LANG": "C.UTF-8",
                    "PATH": f"{runtime_root}/venv/bin:/usr/bin:/bin",
                    "OCI_LA_MCP_CONFIG": str(config_path),
                },
            )
            checks["instance_principal_init"] = instance.returncode == 0
        except Exception:
            pass

        return {
            "status": "SUCCESS" if all(checks.values()) else "FAILED",
            "checks": checks,
        }

    @staticmethod
    def _valid_effective_sshd_config(output: str) -> bool:
        values: dict[str, str] = {}
        accepted_environment: list[str] = []
        for line in output.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2:
                continue
            key, value = parts[0].lower(), parts[1].strip()
            values[key] = value.lower()
            if key == "acceptenv":
                accepted_environment.extend(value.split())

        required = {
            "authenticationmethods": "publickey",
            "passwordauthentication": "no",
            "kbdinteractiveauthentication": "no",
            "permituserenvironment": "no",
            "permituserrc": "no",
            "allowagentforwarding": "no",
            "allowtcpforwarding": "no",
            "gatewayports": "no",
            "x11forwarding": "no",
            "permittunnel": "no",
            "permittty": "no",
        }
        if any(values.get(key) != value for key, value in required.items()):
            return False
        dangerous = re.compile(r"^(PYTHON|LD_|PATH$|OCI_|LOGAN_)", re.IGNORECASE)
        return not any(dangerous.match(name) for name in accepted_environment)


async def _resolve_live_entities(config: AccessControlConfig) -> list[str]:
    from .client import OCILogAnalyticsClient
    from .config import load_config

    settings = load_config()
    settings.log_analytics.namespace = config.namespace
    settings.log_analytics.default_compartment_id = config.compartment_id
    client = OCILogAnalyticsClient(settings)
    client.namespace = config.namespace
    client.compartment_id = config.compartment_id
    entities = await client.list_entities()
    return [
        item["name"]
        for item in entities
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    ]


def _admin_actor() -> str:
    sudo_user = os.environ.get("SUDO_USER", "")
    user = os.environ.get("USER", "unknown")
    ssh_connection = os.environ.get("SSH_CONNECTION", "")
    source = ssh_connection.split()[0] if ssh_connection else "local"
    return f"{sudo_user or user}@{source}"


def build_default_service() -> CamAdminService:
    paths = CamAdminPaths()
    try:
        connection = json.loads(
            paths.connection_path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CamAdminError("connection.json cannot be loaded") from exc
    required = {"host", "port", "remote_user", "host_public_key"}
    if not isinstance(connection, dict) or set(connection) != required:
        raise CamAdminError("connection.json has an invalid shape")
    if (
        not isinstance(connection["host"], str)
        or not connection["host"]
        or "\n" in connection["host"]
        or "\r" in connection["host"]
        or type(connection["port"]) is not int
        or not 1 <= connection["port"] <= 65535
        or connection["remote_user"] != "cam"
        or not isinstance(connection["host_public_key"], str)
    ):
        raise CamAdminError("connection.json contains invalid values")
    host_key_parts = connection["host_public_key"].split()
    host_key_candidate = connection["host_public_key"]
    if len(host_key_parts) == 2:
        host_key_candidate += " host-key"
    try:
        parse_ed25519_public_key(host_key_candidate)
    except AdminRequestError as exc:
        raise CamAdminError("connection.json host public key is invalid") from exc

    try:
        cam_uid = pwd.getpwnam("cam").pw_uid
    except KeyError as exc:
        raise CamAdminError("the restricted 'cam' account does not exist") from exc
    terminator = ProcessTerminator(
        inspector=ProcInspector(),
        cam_uid=cam_uid,
        runtime_python=paths.runtime_python,
    )
    return CamAdminService(
        store=CamStateStore(paths),
        entity_resolver=_resolve_live_entities,
        connection=connection,
        actor_provider=_admin_actor,
        process_terminator=terminator,
    )


class _CliUsageError(ValueError):
    pass


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise _CliUsageError(message)


def _build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="logan-cam-admin")
    subparsers = parser.add_subparsers(dest="operation", required=True)

    bootstrap = subparsers.add_parser("bootstrap-check")
    bootstrap.add_argument("--json", action="store_true", required=True)

    provision = subparsers.add_parser("provision")
    provision.add_argument("--json", action="store_true", required=True)

    show = subparsers.add_parser("show")
    show.add_argument("--cam", required=True)
    show.add_argument("--json", action="store_true", required=True)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--cam", required=True)
    verify.add_argument("--json", action="store_true", required=True)

    deprovision = subparsers.add_parser("deprovision")
    deprovision.add_argument("--json", action="store_true", required=True)
    return parser


def _read_one_json(stdin: TextIO) -> object:
    content = stdin.read()
    stripped = content.lstrip()
    if not stripped:
        raise AdminRequestError("request body must contain one JSON object")
    try:
        payload, end = json.JSONDecoder().raw_decode(stripped)
    except json.JSONDecodeError as exc:
        raise AdminRequestError("request body is not valid JSON") from exc
    if stripped[end:].strip():
        raise AdminRequestError("request body contains trailing data")
    return payload


def _write_json(stdout: TextIO, response: Mapping[str, Any]) -> None:
    stdout.write(json.dumps(dict(response), sort_keys=True) + "\n")
    stdout.flush()


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    service: Any | None = None,
    geteuid: Callable[[], int] = os.geteuid,
) -> int:
    """Run one root-only administration operation with a JSON contract."""
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    try:
        args = _build_parser().parse_args(argv)
    except _CliUsageError as exc:
        stderr.write(f"logan-cam-admin: {exc}\n")
        return 2

    if geteuid() != 0:
        stderr.write("logan-cam-admin must run as root\n")
        return 1

    try:
        active_service = service if service is not None else build_default_service()
        if args.operation == "provision":
            request = ProvisionRequest.from_json(_read_one_json(stdin))
            response = asyncio.run(active_service.provision(request))
        elif args.operation == "deprovision":
            request = DeprovisionRequest.from_json(_read_one_json(stdin))
            response = asyncio.run(active_service.deprovision(request))
        elif args.operation == "show":
            response = active_service.show(args.cam)
        elif args.operation == "verify":
            response = asyncio.run(active_service.verify(args.cam))
        else:
            response = active_service.bootstrap_check()

        if not isinstance(response, Mapping) or not isinstance(
            response.get("status"), str
        ):
            raise CamAdminError("service returned an invalid response")
        _write_json(stdout, response)
        return 0 if response["status"] == "SUCCESS" else 1
    except Exception as exc:
        stderr.write(f"logan-cam-admin: {type(exc).__name__}: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
