"""Validated requests and durable state primitives for CAM administration."""

from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import json
import os
import shutil
import stat
import struct
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence, Tuple

import yaml

from .access_control import (
    AccessConfigError,
    validate_cam_id,
    validate_customer_numbers,
)


class AdminRequestError(ValueError):
    """Raised when an administration request is not exact and safe."""


@dataclass(frozen=True)
class ParsedPublicKey:
    line: str
    algorithm: str
    encoded_blob: str
    comment: str
    fingerprint: str


def _read_ssh_string(blob: bytes, offset: int) -> tuple[bytes, int]:
    if offset + 4 > len(blob):
        raise AdminRequestError("ed25519 public key has truncated wire data")
    length = struct.unpack(">I", blob[offset : offset + 4])[0]
    start = offset + 4
    end = start + length
    if end > len(blob):
        raise AdminRequestError("ed25519 public key has truncated wire data")
    return blob[start:end], end


def parse_ed25519_public_key(value: object) -> ParsedPublicKey:
    """Parse one option-free canonical OpenSSH ed25519 public-key record."""
    if not isinstance(value, str) or "\n" in value or "\r" in value:
        raise AdminRequestError("public_key must be one OpenSSH line")
    parts = value.split()
    if len(parts) != 3 or parts[0] != "ssh-ed25519":
        raise AdminRequestError(
            "public_key must be one option-free ssh-ed25519 record"
        )
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AdminRequestError("public_key contains invalid base64") from exc

    algorithm, offset = _read_ssh_string(blob, 0)
    key_bytes, offset = _read_ssh_string(blob, offset)
    if algorithm != b"ssh-ed25519" or len(key_bytes) != 32 or offset != len(blob):
        raise AdminRequestError("public_key is not a canonical ed25519 key")

    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii")
    fingerprint = f"SHA256:{digest.rstrip('=')}"
    return ParsedPublicKey(
        line=value,
        algorithm="ssh-ed25519",
        encoded_blob=parts[1],
        comment=parts[2],
        fingerprint=fingerprint,
    )


def _exact_object(payload: object, required: set[str]) -> Mapping[str, Any]:
    if not isinstance(payload, dict) or set(payload) != required:
        raise AdminRequestError(f"request fields must be exactly {sorted(required)}")
    return payload


@dataclass(frozen=True)
class ProvisionRequest:
    cam_id: str
    customers: Tuple[int, ...]
    allow_delivery: bool
    key: ParsedPublicKey

    @classmethod
    def from_json(cls, payload: object) -> "ProvisionRequest":
        raw = _exact_object(
            payload,
            {"cam_id", "customers", "allow_delivery", "public_key"},
        )
        try:
            cam_id = validate_cam_id(raw["cam_id"])
            customers = validate_customer_numbers(raw["customers"], "customers")
        except AccessConfigError as exc:
            raise AdminRequestError(str(exc)) from exc
        if not customers:
            raise AdminRequestError(
                "customers must contain at least one positive integer"
            )
        if type(raw["allow_delivery"]) is not bool:
            raise AdminRequestError("allow_delivery must be a boolean")

        key = parse_ed25519_public_key(raw["public_key"])
        if key.comment != f"logan-cam:{cam_id}":
            raise AdminRequestError("public key comment must match the CAM id")
        return cls(cam_id, customers, raw["allow_delivery"], key)


@dataclass(frozen=True)
class DeprovisionRequest:
    cam_id: str
    expected_fingerprint: str
    confirm: bool

    @classmethod
    def from_json(cls, payload: object) -> "DeprovisionRequest":
        raw = _exact_object(
            payload,
            {"cam_id", "expected_fingerprint", "confirm"},
        )
        try:
            cam_id = validate_cam_id(raw["cam_id"])
        except AccessConfigError as exc:
            raise AdminRequestError(str(exc)) from exc
        fingerprint = raw["expected_fingerprint"]
        if not isinstance(fingerprint, str) or not fingerprint.startswith("SHA256:"):
            raise AdminRequestError(
                "expected_fingerprint must be an SHA256 fingerprint"
            )
        if raw["confirm"] is not True:
            raise AdminRequestError("confirm must be true")
        return cls(cam_id, fingerprint, True)


class ConcurrentMutationError(RuntimeError):
    """Raised when rollback would overwrite an unexpected live file."""


@dataclass(frozen=True)
class CamAdminPaths:
    policy_path: Path = Path("/etc/logan-mcp/access_control.yaml")
    authorized_keys_path: Path = Path("/home/cam/.ssh/authorized_keys")
    lock_path: Path = Path("/var/lock/logan-cam-admin.lock")
    backup_dir: Path = Path("/var/lib/logan-cam-admin/backups")
    audit_path: Path = Path("/var/log/logan-cam-admin.jsonl")
    launcher_path: Path = Path("/opt/logan-mcp/bin/cam-launch")
    runtime_python: Path = Path("/opt/logan-mcp/venv/bin/python")
    host_key_path: Path = Path("/etc/ssh/ssh_host_ed25519_key.pub")
    connection_path: Path = Path("/etc/logan-mcp/connection.json")


@dataclass(frozen=True)
class LiveState:
    policy: dict[str, Any]
    authorized_key_lines: tuple[str, ...]
    policy_hash: str
    authorized_keys_hash: str
    policy_stat: os.stat_result
    authorized_keys_stat: os.stat_result


@dataclass(frozen=True)
class BackupPair:
    policy_path: Path
    authorized_keys_path: Path


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def serialize_policy(policy: Mapping[str, Any]) -> bytes:
    return yaml.safe_dump(dict(policy), sort_keys=False).encode("utf-8")


def serialize_authorized_keys(lines: Sequence[str]) -> bytes:
    return (("\n".join(lines)).rstrip("\n") + "\n").encode("utf-8")


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_replace(path: Path, content: bytes, source_stat: os.stat_result) -> str:
    candidate: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            candidate = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temp_stat = os.fstat(handle.fileno())
            if (temp_stat.st_uid, temp_stat.st_gid) != (
                source_stat.st_uid,
                source_stat.st_gid,
            ):
                os.fchown(handle.fileno(), source_stat.st_uid, source_stat.st_gid)
            os.fchmod(handle.fileno(), stat.S_IMODE(source_stat.st_mode))

        os.replace(candidate, path)
        fsync_directory(path.parent)
    finally:
        if candidate is not None:
            candidate.unlink(missing_ok=True)
    return sha256_file(path)


def build_forced_key_line(
    cam_id: str,
    key: ParsedPublicKey,
    launcher_path: Path,
) -> str:
    try:
        validated_cam_id = validate_cam_id(cam_id)
    except AccessConfigError as exc:
        raise AdminRequestError(str(exc)) from exc
    if key.comment != f"logan-cam:{validated_cam_id}":
        raise AdminRequestError("public key comment must match the CAM id")
    options = [
        "restrict",
        f'command="{launcher_path} {validated_cam_id}"',
        "no-pty",
        "no-port-forwarding",
        "no-agent-forwarding",
        "no-X11-forwarding",
        "no-user-rc",
    ]
    return f"{','.join(options)} {key.line}"


class CamStateStore:
    def __init__(self, paths: CamAdminPaths):
        self.paths = paths

    @contextmanager
    def locked(self) -> Iterator[None]:
        self.paths.lock_path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not self.paths.lock_path.exists()
        with self.paths.lock_path.open("a+b") as handle:
            if new_file:
                os.fchmod(handle.fileno(), 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def read_live(self) -> LiveState:
        policy_content = self.paths.policy_path.read_bytes()
        try:
            policy = yaml.safe_load(policy_content.decode("utf-8"))
        except (UnicodeError, yaml.YAMLError) as exc:
            raise AdminRequestError(f"live access policy is invalid: {exc}") from exc
        if not isinstance(policy, dict):
            raise AdminRequestError("live access policy must be a mapping")

        key_content = self.paths.authorized_keys_path.read_bytes()
        try:
            authorized_key_lines = tuple(key_content.decode("utf-8").splitlines())
        except UnicodeError as exc:
            raise AdminRequestError("live authorized_keys is not valid UTF-8") from exc
        return LiveState(
            policy=policy,
            authorized_key_lines=authorized_key_lines,
            policy_hash=sha256_bytes(policy_content),
            authorized_keys_hash=sha256_bytes(key_content),
            policy_stat=self.paths.policy_path.stat(),
            authorized_keys_stat=self.paths.authorized_keys_path.stat(),
        )

    def back_up(self, live: LiveState, operation_id: str) -> BackupPair:
        target = self.paths.backup_dir / operation_id
        target.mkdir(parents=True, mode=0o700)
        os.chmod(target, 0o700)
        policy_backup = target / "access_control.yaml"
        keys_backup = target / "authorized_keys"
        shutil.copy2(self.paths.policy_path, policy_backup)
        shutil.copy2(self.paths.authorized_keys_path, keys_backup)
        for path in (policy_backup, keys_backup):
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
        fsync_directory(target)
        fsync_directory(target.parent)
        if sha256_file(policy_backup) != live.policy_hash:
            raise OSError("policy backup hash mismatch")
        if sha256_file(keys_backup) != live.authorized_keys_hash:
            raise OSError("authorized_keys backup hash mismatch")
        return BackupPair(policy_backup, keys_backup)

    def replace_policy(self, policy: dict[str, Any], live: LiveState) -> str:
        return _atomic_replace(
            self.paths.policy_path,
            serialize_policy(policy),
            live.policy_stat,
        )

    def replace_authorized_keys(
        self,
        lines: Sequence[str],
        live: LiveState,
    ) -> str:
        return _atomic_replace(
            self.paths.authorized_keys_path,
            serialize_authorized_keys(lines),
            live.authorized_keys_stat,
        )

    def restore_authorized_keys(
        self,
        backups: BackupPair,
        expected_hash: str,
    ) -> str:
        if sha256_file(self.paths.authorized_keys_path) != expected_hash:
            raise ConcurrentMutationError(
                "authorized_keys changed after this operation"
            )
        return _atomic_replace(
            self.paths.authorized_keys_path,
            backups.authorized_keys_path.read_bytes(),
            self.paths.authorized_keys_path.stat(),
        )

    def restore_policy(self, backups: BackupPair, expected_hash: str) -> str:
        if sha256_file(self.paths.policy_path) != expected_hash:
            raise ConcurrentMutationError("policy changed after this operation")
        return _atomic_replace(
            self.paths.policy_path,
            backups.policy_path.read_bytes(),
            self.paths.policy_path.stat(),
        )

    def append_audit(self, event: Mapping[str, Any]) -> None:
        forbidden = {
            "public_key",
            "private_key",
            "environment",
            "confirmation_secret",
        }
        if forbidden.intersection(event):
            raise ValueError("audit event contains forbidden secret fields")
        if {"timestamp", "event_id"}.intersection(event):
            raise ValueError("audit event contains reserved fields")

        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_id": f"camadm_{uuid.uuid4().hex}",
            **event,
        }
        line = json.dumps(record, sort_keys=True) + "\n"
        self.paths.audit_path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not self.paths.audit_path.exists()
        with self.paths.audit_path.open("a", encoding="utf-8") as handle:
            if new_file:
                os.fchmod(handle.fileno(), 0o600)
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        if new_file:
            fsync_directory(self.paths.audit_path.parent)


@dataclass(frozen=True)
class AuthorizedKeyRecord:
    index: int
    key: ParsedPublicKey
    managed_cam_id: str | None
    line: str


def parse_authorized_key_line(
    index: int,
    line: str,
) -> AuthorizedKeyRecord | None:
    tokens = line.split()
    try:
        key_index = tokens.index("ssh-ed25519")
    except ValueError:
        return None
    if key_index + 1 >= len(tokens):
        raise AdminRequestError(
            f"malformed ed25519 record on authorized_keys line {index + 1}"
        )
    comment = (
        tokens[key_index + 2]
        if key_index + 2 < len(tokens)
        else f"line-{index + 1}"
    )
    key = parse_ed25519_public_key(
        f"ssh-ed25519 {tokens[key_index + 1]} {comment}"
    )
    managed_cam_id = None
    if comment.startswith("logan-cam:"):
        try:
            managed_cam_id = validate_cam_id(comment.removeprefix("logan-cam:"))
        except AccessConfigError as exc:
            raise AdminRequestError(
                f"invalid managed CAM id on authorized_keys line {index + 1}: {exc}"
            ) from exc
    return AuthorizedKeyRecord(index, key, managed_cam_id, line)


def authorized_key_records(
    lines: tuple[str, ...],
) -> tuple[AuthorizedKeyRecord, ...]:
    records = []
    for index, line in enumerate(lines):
        record = parse_authorized_key_line(index, line)
        if record is not None:
            records.append(record)
    return tuple(records)


def managed_key_records(
    lines: tuple[str, ...],
) -> tuple[AuthorizedKeyRecord, ...]:
    return tuple(
        record
        for record in authorized_key_records(lines)
        if record.managed_cam_id is not None
    )
