import base64
import hashlib
import struct

import pytest

from oci_logan_mcp.cam_admin_store import (
    AdminRequestError,
    DeprovisionRequest,
    ProvisionRequest,
    parse_ed25519_public_key,
)


def _public_key(cam_id="cam_alice", key_bytes=b"k" * 32):
    algorithm = b"ssh-ed25519"
    blob = (
        struct.pack(">I", len(algorithm))
        + algorithm
        + struct.pack(">I", len(key_bytes))
        + key_bytes
    )
    encoded = base64.b64encode(blob).decode("ascii")
    return f"ssh-ed25519 {encoded} logan-cam:{cam_id}", blob


def test_parse_provision_request_is_exact_and_normalized():
    public_key, _ = _public_key()
    request = ProvisionRequest.from_json(
        {
            "cam_id": "cam_alice",
            "customers": [223, 66],
            "allow_delivery": False,
            "public_key": public_key,
        }
    )

    assert request.cam_id == "cam_alice"
    assert request.customers == (223, 66)
    assert request.allow_delivery is False
    assert request.key.comment == "logan-cam:cam_alice"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {
            "cam_id": "cam_alice",
            "customers": [],
            "allow_delivery": False,
            "public_key": "x",
        },
        {
            "cam_id": "cam_alice",
            "customers": ["223"],
            "allow_delivery": False,
            "public_key": "x",
        },
        {
            "cam_id": "cam_alice",
            "customers": [223],
            "allow_delivery": 0,
            "public_key": "x",
        },
        {
            "cam_id": "cam_alice",
            "customers": [223],
            "allow_delivery": False,
            "public_key": "x",
            "replace": True,
        },
    ],
)
def test_parse_provision_request_rejects_missing_ambiguous_or_unknown_fields(payload):
    with pytest.raises(AdminRequestError):
        ProvisionRequest.from_json(payload)


def test_parse_ed25519_key_validates_wire_format_and_fingerprint():
    line, blob = _public_key()
    parsed = parse_ed25519_public_key(line)
    expected = base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")

    assert parsed.fingerprint == f"SHA256:{expected}"


@pytest.mark.parametrize(
    "line",
    [
        "ssh-rsa AAAA logan-cam:cam_alice",
        "ssh-ed25519 not-base64 logan-cam:cam_alice",
        "ssh-ed25519 AAAA logan-cam:cam_alice\nssh-ed25519 BBBB injected",
        "environment=BAD ssh-ed25519 AAAA logan-cam:cam_alice",
    ],
)
def test_parse_ed25519_key_rejects_options_multiline_and_bad_wire_data(line):
    with pytest.raises(AdminRequestError):
        parse_ed25519_public_key(line)


def test_deprovision_request_requires_confirmation_and_expected_fingerprint():
    request = DeprovisionRequest.from_json(
        {
            "cam_id": "cam_alice",
            "expected_fingerprint": "SHA256:abc",
            "confirm": True,
        }
    )
    assert request.confirm is True

    with pytest.raises(AdminRequestError):
        DeprovisionRequest.from_json(
            {
                "cam_id": "cam_alice",
                "expected_fingerprint": "SHA256:abc",
                "confirm": False,
            }
        )
