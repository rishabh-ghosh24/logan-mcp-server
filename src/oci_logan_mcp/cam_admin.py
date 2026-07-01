"""Root-only transactional administration service for CAM access."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import pwd
import stat
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping, Sequence, TextIO

from .access_control import (
    AccessConfigError,
    AccessControlConfig,
    build_profile,
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
    serialize_authorized_keys,
    serialize_policy,
    sha256_bytes,
    validate_policy_document,
)
from .cam_processes import ProcInspector, ProcessTerminator


class CamAdminError(RuntimeError):
    """Raised when a CAM administration operation cannot be completed safely."""


class CamAdminService:
    def __init__(
        self,
        store: CamStateStore,
        entity_resolver: Callable[[AccessControlConfig], Awaitable[list[str]]],
        connection: Mapping[str, Any],
        actor_provider: Callable[[], str],
        process_terminator: Any | None = None,
    ):
        self.store = store
        self.entity_resolver = entity_resolver
        self.connection = dict(connection)
        self.actor_provider = actor_provider
        self.process_terminator = process_terminator

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
