"""Build a core :class:`RegionMap` from the repeater's served transport-key regions.

Core's flood-reply scoping (``region_map.apply_reply_scope``) re-scopes a flood
reply to the region its request arrived under, mirroring firmware
``simple_repeater::sendFloodReply``. For that to engage, the dispatcher and every
companion bridge need a ``RegionMap`` describing the named regions this repeater
serves. This module builds that map from the ``transport_keys`` table — the same
source ``login.LoginHelper._format_region_names`` reads.

Firmware-parity notes:

- The ``*`` wildcard (unscoped flood) is deliberately **not** a region entry. A
  plain FLOOD request replies plain, so ``find_match`` must return ``None`` for
  it. Wildcard handling lives in ``capture_recv_region`` (route-type based), not
  here, and ``mesh.unscoped_flood_allow`` never changes the map contents.
- A deny-flood region carries ``REGION_DENY_FLOOD`` so
  ``find_match(mask=REGION_DENY_FLOOD)`` skips it => its request replies plain.
- The transport key is derived from the region name via ``get_auto_key_for`` —
  the same derivation senders and the repeater's own outgoing floods use. An
  explicit stored key is only carried when it is genuinely custom material the
  name would not reproduce (a private ``$`` region, or imported key material via
  Glass sync); a redundant key that disagreed with the name would silently
  re-scope replies to the wrong code.
"""

from __future__ import annotations

import base64
import binascii
import logging
from typing import Optional

from openhop_core.protocol.region_map import REGION_DENY_FLOOD, RegionEntry, RegionMap
from openhop_core.protocol.transport_keys import get_auto_key_for

logger = logging.getLogger("RepeaterRegionMap")


def _decode_stored_key(raw) -> Optional[bytes]:
    """Decode a stored ``transport_key`` to 16 raw bytes, or ``None``.

    Keys are stored base64-encoded (see ``SQLiteHandler.generate_transport_key``);
    tolerate raw bytes too. Anything that is not exactly 16 bytes is ignored, so a
    corrupt or wrong-length key falls back to name hashing rather than breaking
    matching.
    """
    if not raw:
        return None
    if isinstance(raw, (bytes, bytearray)):
        key = bytes(raw)
    else:
        try:
            key = base64.b64decode(str(raw), validate=True)
        except (binascii.Error, ValueError):
            return None
    return key if len(key) == 16 else None


def build_region_map(config, sqlite_handler) -> RegionMap:
    """Return a :class:`RegionMap` of the named regions this repeater serves.

    ``config`` is currently unused (the ``*`` wildcard is not a map entry) but is
    kept in the signature so a future config-driven region source stays a
    drop-in change for every caller.
    """
    region_map = RegionMap()
    if sqlite_handler is None:
        return region_map

    try:
        records = sqlite_handler.get_transport_keys()
    except Exception as exc:  # defensive: never let a bad read break startup
        logger.warning("Failed to read transport keys for region map: %s", exc)
        return region_map

    for rec in records or []:
        name = (rec.get("name") or "").strip()
        # Skip empty names and the wildcard: a plain/unscoped flood replies plain,
        # so find_match must not resolve it to a region.
        if not name or name == "*":
            continue

        flood_policy = (rec.get("flood_policy") or "deny").strip().lower()
        flags = 0 if flood_policy == "allow" else REGION_DENY_FLOOD

        private_keys = None
        key_bytes = _decode_stored_key(rec.get("transport_key"))
        if key_bytes is not None:
            if name.startswith("$"):
                # Private region: core never name-hashes a "$" name, so the stored
                # key is the only usable key. Without it the region matches nothing.
                private_keys = [key_bytes]
            else:
                # Public region: rely on name hashing unless the stored key is
                # genuinely custom material the name would not reproduce.
                try:
                    derived = get_auto_key_for(name)
                except ValueError:
                    derived = None
                if derived != key_bytes:
                    private_keys = [key_bytes]

        try:
            region_id = int(rec.get("id") or 0)
        except (TypeError, ValueError):
            region_id = 0

        parent_raw = rec.get("parent_id")
        try:
            parent = int(parent_raw) if parent_raw is not None else 0
        except (TypeError, ValueError):
            parent = 0

        region_map.add_region(
            RegionEntry(
                id=region_id,
                parent=parent,
                flags=flags,
                name=name,
                private_keys=private_keys,
            )
        )

    return region_map


def resolve_default_scope_key(config, region_map: RegionMap) -> Optional[bytes]:
    """Return the transport key for ``mesh.default_region``, or ``None``.

    This is firmware's ``default_scope``. ``simple_repeater`` resolves it once at
    boot from ``region_map.getDefaultRegion()`` via
    ``getTransportKeysFor(*r, &default_scope, 1)``, and ``sendFloodReply`` uses it
    for the ``REPLY_SCOPE_DEFAULT`` row: a reply whose request scope is
    *unknowable* rather than un-scoped -- a DIRECT request we hold no return path
    for, or a transport code that matched no served region. Firmware's comment on
    that branch is why it matters::

        // un-scoped would be dropped at hop 0 by repeaters running flood.max.unscoped=0

    ``None`` is firmware's ``default_scope.isNull()``, which ``chooseReplyScope``
    turns into ``REPLY_SCOPE_NONE`` -- a plain flood. That is the correct answer
    for an unset default and for the ``*`` wildcard, which is deliberately not a
    region entry (see the module docstring).

    Resolution goes through the built map rather than hashing the configured name
    directly, for parity with ``getDefaultRegion()``, which can only ever return a
    region the map holds:

    - a ``$private`` default resolves to its stored key material, which its name
      cannot reproduce;
    - a default naming a region this repeater does not serve resolves to nothing,
      rather than to a scope no local Region would match on the way back in;
    - a deny-flood default still resolves, because ``REGION_DENY_FLOOD`` gates
      *inbound* ``find_match``, not what this node may scope its own replies with.

    The name is matched case-insensitively against each region's display name (the
    stored ``#`` stripped), as ``web.api_endpoints.default_region`` matches it when
    auto-creating the region. The key itself always comes from the matched entry,
    so it derives from the name the table actually holds.
    """
    mesh_cfg = config.get("mesh", {}) if isinstance(config, dict) else {}
    if not isinstance(mesh_cfg, dict):
        return None

    raw = mesh_cfg.get("default_region")
    name = str(raw).strip() if raw not in (None, "") else ""
    if name.startswith("#"):
        name = name[1:].strip()
    if not name or name == "*":
        return None

    needle = name.lower()
    for region in region_map.regions:
        display = (region.name or "").strip()
        if display.startswith("#"):
            display = display[1:]
        if display.lower() == needle:
            key = region_map.first_key_for(region)
            if key is None:
                logger.warning(
                    "mesh.default_region '%s' resolves to no usable transport key; "
                    "replies with an unknowable request scope will flood un-scoped",
                    name,
                )
            return key

    logger.warning(
        "mesh.default_region '%s' is not a served region; replies with an "
        "unknowable request scope will flood un-scoped",
        name,
    )
    return None
