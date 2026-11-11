from __future__ import annotations

import base64
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "crypto"


def test_golden_dsse_fixture_contains_exact_pae_and_valid_signature() -> None:
    envelope = json.loads((FIXTURES / "envelope.json").read_bytes())
    payload_type = envelope["payloadType"].encode()
    payload = base64.b64decode(envelope["payload"], validate=True)
    expected_pae = (
        b"DSSEv1 "
        + str(len(payload_type)).encode()
        + b" "
        + payload_type
        + b" "
        + str(len(payload)).encode()
        + b" "
        + payload
    )
    assert expected_pae == bytes.fromhex((FIXTURES / "pae.hex").read_text().strip())
    assert payload + b"\n" == (FIXTURES / "statement.json").read_bytes()
    signature = base64.b64decode(envelope["signatures"][0]["sig"], validate=True)
    assert signature == bytes.fromhex(
        (FIXTURES / "ed25519-signature.hex").read_text().strip()
    )
    public_key = Ed25519PublicKey.from_public_bytes(
        bytes.fromhex((FIXTURES / "ed25519-public-key.hex").read_text().strip())
    )
    public_key.verify(signature, expected_pae)
