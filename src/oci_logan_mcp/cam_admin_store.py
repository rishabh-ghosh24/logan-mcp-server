"""Validated requests and durable state primitives for CAM administration."""

from __future__ import annotations

import base64
import binascii
import hashlib
import struct
from dataclasses import dataclass
from typing import Any, Mapping, Tuple

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
