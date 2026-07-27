"""
MeshCore-compatible Ed25519 vanity key generator.

Generates Ed25519 keys whose public key hex starts with a user-chosen prefix.
Algorithm matches MeshCore's custom scalar clamping (see meshcore-keygen).

Requires: PyNaCl (pip install PyNaCl)
"""

import hashlib
import secrets
from typing import Optional, Tuple

from nacl.bindings import crypto_scalarmult_ed25519_base_noclamp


def _has_disallowed_public_prefix(public_key: bytes) -> bool:
    """Return True when public key starts with a forbidden prefix byte."""
    return bool(public_key) and public_key[0] in (0x00, 0xFF)


def generate_meshcore_keypair() -> Tuple[bytes, bytes]:
    """Generate a MeshCore-compatible Ed25519 keypair.

    Returns:
        (public_key, private_key) as raw bytes.
        public_key is 32 bytes, private_key is 64 bytes.
    """
    while True:
        # 1. Random 32-byte seed
        seed = secrets.token_bytes(32)

        # 2. SHA-512 hash
        digest = hashlib.sha512(seed).digest()

        # 3. Ed25519 scalar clamping on first 32 bytes
        clamped = bytearray(digest[:32])
        clamped[0] &= 248  # Clear bottom 3 bits
        clamped[31] &= 63  # Clear top 2 bits
        clamped[31] |= 64  # Set bit 6

        # 4. Derive public key
        public_key = crypto_scalarmult_ed25519_base_noclamp(bytes(clamped))

        # Disallow keys with 0x00 / 0xFF leading byte.
        if _has_disallowed_public_prefix(public_key):
            continue

        # 5. Private key = [clamped_scalar][sha512_upper_half]
        private_key = bytes(clamped) + digest[32:64]

        return public_key, private_key


def generate_vanity_key(
    prefix: str,
    max_iterations: int = 5_000_000,
) -> Optional[dict]:
    """Generate a MeshCore keypair whose public key hex starts with *prefix*.

    Args:
        prefix: Hex prefix (1-4 chars, case-insensitive).
        max_iterations: Safety cap to avoid infinite loops.

    Returns:
        Dict with public_hex, private_hex, attempts on success; None if cap hit.
    """
    target = prefix.upper()

    if target.startswith("00") or target.startswith("FF"):
        return None

    for attempt in range(1, max_iterations + 1):
        pub, priv = generate_meshcore_keypair()
        if pub.hex().upper().startswith(target):
            return {
                "public_hex": pub.hex(),
                "private_hex": priv.hex(),
                "attempts": attempt,
            }

    return None
