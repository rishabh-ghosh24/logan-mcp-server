"""Root-only transactional administration service for CAM access."""

from __future__ import annotations

import copy
import stat
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping

from .access_control import (
    AccessConfigError,
    AccessControlConfig,
    build_profile,
    validate_cam_id,
)
from .cam_admin_store import (
    CamStateStore,
    ConcurrentMutationError,
    ProvisionRequest,
    authorized_key_records,
    build_forced_key_line,
    managed_key_records,
    serialize_authorized_keys,
    serialize_policy,
    sha256_bytes,
    validate_policy_document,
)


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
