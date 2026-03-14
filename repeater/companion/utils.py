"""Shared utilities for Companion (e.g. validation for config sync)."""

_INVALID_NODE_NAME_CHARS = "\n\r\x00"


def normalize_companion_identity_key(identity_key: str) -> str:
    """Strip whitespace and remove optional 0x prefix so fromhex() is consistent across installs."""
    s = identity_key.strip()
    if s.lower().startswith("0x"):
        s = s[2:].strip()
    return s


def validate_companion_node_name(value: str) -> str:
    """Validate node_name for config sync: non-empty, max 31 bytes UTF-8, no control chars."""
    if not isinstance(value, str):
        raise ValueError("node_name must be a string")
    s = value.strip()
    if not s:
        raise ValueError("node_name cannot be empty")
    if len(s.encode("utf-8")) > 31:
        raise ValueError("node_name too long (max 31 bytes UTF-8)")
    if any(c in s for c in _INVALID_NODE_NAME_CHARS):
        raise ValueError("node_name contains invalid characters")
    return s
