"""Path-hash identity resolution for the Mobile Companion API's RF
observation surface (design doc §10.5).

Path entries in ``packets.original_path`` / ``forwarded_path`` are
abbreviated hashes (commonly one byte, but the format supports longer
prefixes) and genuinely collide -- the same rendering philosophy as
``openhop_core``'s src-hash candidate handling in ``text.py``: collect every
matching candidate, never assume a single match is *the* match. Callers load
contacts ONCE per request and resolve every path element in memory against
that one snapshot -- never per-hash SQL (design doc §10.5 / §13).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

#: cap on returned candidates for an ambiguous resolution -- large collision
#: sets (pathological, but possible with 1-byte hashes on a big mesh) must
#: not blow up the response body.
_MAX_CANDIDATES = 5


def _contact_pubkey_hex(contact: dict) -> Optional[str]:
    """Return a contact's public key as lowercase hex, or None if unusable.

    ``companion_load_contacts`` returns ``pubkey`` as raw BLOB bytes; this
    also tolerates a pre-hexed string so callers can pass either shape.
    """
    pk = contact.get("pubkey")
    if pk is None:
        return None
    if isinstance(pk, (bytes, bytearray, memoryview)):
        pk_hex = bytes(pk).hex()
    else:
        pk_hex = str(pk)
    pk_hex = pk_hex.strip()
    if pk_hex.lower().startswith("0x"):
        pk_hex = pk_hex[2:]
    pk_hex = pk_hex.lower()
    return pk_hex or None


def build_prefix_index(contacts: List[dict]) -> List[Dict[str, str]]:
    """Precompute a flat (pubkey_hex, name) list once per request.

    Kept as a plain list rather than a trie/dict-by-prefix: contact counts
    are bounded (``max_contacts``, design doc §7.4) and a request resolves at
    most a handful of path elements, so a linear ``startswith`` scan per
    element is cheap and avoids building a more complex structure for no
    measurable benefit.
    """
    index = []
    for c in contacts:
        pk_hex = _contact_pubkey_hex(c)
        if pk_hex:
            index.append({"pubkey": pk_hex, "name": c.get("name") or ""})
    return index


def _normalize_raw_hash(raw_hash: Any) -> str:
    text = str(raw_hash or "").strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    return text


def resolve_path(raw_hashes: List[Any], contacts: List[dict]) -> List[Dict[str, Any]]:
    """Resolve each raw path hash against ``contacts``.

    Returns one dict per input element:
    ``{"raw_hash": <original value>, "resolution": "unique"|"ambiguous"|"unknown",
    "candidates": [{"pubkey": <hex>, "name": <str>}, ...]}``, with
    ``"truncated": True`` added when more than ``_MAX_CANDIDATES`` contacts
    matched (the design doc's "capped at ~5 candidates with a truncated
    flag").

    Match rule: a raw path element of N hex chars matches any contact whose
    pubkey hex starts with those N chars, case-insensitively. An empty/blank
    raw hash never matches anything (resolution "unknown") rather than
    matching every contact.
    """
    index = build_prefix_index(contacts)
    results: List[Dict[str, Any]] = []
    for raw in raw_hashes:
        raw_norm = _normalize_raw_hash(raw)
        matches = [c for c in index if c["pubkey"].startswith(raw_norm)] if raw_norm else []
        count = len(matches)
        if count == 0:
            resolution = "unknown"
        elif count == 1:
            resolution = "unique"
        else:
            resolution = "ambiguous"

        candidates = matches[:_MAX_CANDIDATES]
        entry: Dict[str, Any] = {
            "raw_hash": raw,
            "resolution": resolution,
            "candidates": [{"pubkey": c["pubkey"], "name": c["name"]} for c in candidates],
        }
        if count > _MAX_CANDIDATES:
            entry["truncated"] = True
        results.append(entry)
    return results
