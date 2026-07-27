import logging
from typing import Optional, Tuple

from openhop_core.protocol import PacketBuilder
from openhop_core.protocol.constants import ROUTE_TYPE_TRANSPORT_FLOOD

logger = logging.getLogger("RepeaterPacketUtils")


def create_scoped_advert_packet(
    *,
    local_identity,
    node_name: str,
    latitude: float,
    longitude: float,
    flags: int,
    default_region,
    scope_label: str,
) -> Tuple[object, Optional[str]]:
    """Create a flood advert packet and apply default-region transport scope when configured."""
    packet = PacketBuilder.create_advert(
        local_identity=local_identity,
        name=node_name,
        lat=latitude,
        lon=longitude,
        feature1=0,
        feature2=0,
        flags=flags,
        route_type="flood",
    )

    scoped_region_name = _apply_default_region_scope(
        packet=packet,
        default_region=default_region,
        scope_label=scope_label,
    )
    return packet, scoped_region_name


def _apply_default_region_scope(*, packet, default_region, scope_label: str) -> Optional[str]:
    """Apply transport-flood scoping for a default region if provided."""
    region_name = str(default_region).strip() if default_region not in (None, "") else ""
    if not region_name:
        return None

    try:
        from openhop_core.protocol.transport_keys import calc_transport_code, get_auto_key_for

        region_key = get_auto_key_for(region_name)
        packet.transport_codes[0] = calc_transport_code(region_key, packet)
        packet.transport_codes[1] = 0  # reserved for home region
        packet.header = (packet.header & ~0x03) | ROUTE_TYPE_TRANSPORT_FLOOD
        return region_name
    except Exception as scope_err:
        logger.warning(
            "Failed to apply default region scope '%s' to %s; sending unscoped flood: %s",
            region_name,
            scope_label,
            scope_err,
        )
        return None
