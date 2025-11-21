"""Storage utility classes and functions for data acquisition."""

from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional


@dataclass
class PacketRecord:
    """
    Data class for packet record format.
    Converts internal packet_record format to standardized publish format.
    Reusable across MQTT, LetsMesh, and other handlers.
    """

    origin: str
    origin_id: str
    timestamp: str
    type: str
    direction: str
    time: str
    date: str
    len: str
    packet_type: str
    route: str
    payload_len: str
    raw: str
    SNR: str
    RSSI: str
    score: str
    duration: str
    hash: str

    @classmethod
    def from_packet_record(
        cls, packet_record: dict, origin: str, origin_id: str
    ) -> Optional["PacketRecord"]:
        """
        Create PacketRecord from internal packet_record format.

        Args:
            packet_record: Internal packet record dictionary
            origin: Node name
            origin_id: Public key of the node

        Returns:
            PacketRecord instance or None if raw_packet is missing
        """
        if "raw_packet" not in packet_record or not packet_record["raw_packet"]:
            return None

        # Extract timestamp and format date/time
        timestamp = packet_record.get("timestamp", 0)
        dt = datetime.fromtimestamp(timestamp)

        # Format route type (1=Flood->F, 2=Direct->D, etc)
        route_map = {1: "F", 2: "D"}
        route = route_map.get(packet_record.get("route", 0), str(packet_record.get("route", 0)))

        return cls(
            origin=origin,
            origin_id=origin_id,
            timestamp=dt.isoformat(),
            type="PACKET",
            direction="rx",
            time=dt.strftime("%H:%M:%S"),
            date=dt.strftime("%-d/%-m/%Y"),
            len=str(len(packet_record["raw_packet"]) // 2),
            packet_type=str(packet_record.get("type", 0)),
            route=route,
            payload_len=str(packet_record.get("payload_length", 0)),
            raw=packet_record["raw_packet"],
            SNR=str(packet_record.get("snr", 0)),
            RSSI=str(packet_record.get("rssi", 0)),
            score=str(int(packet_record.get("score", 0) * 1000)),
            duration="0",
            hash=packet_record.get("packet_hash", ""),
        )

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization"""
        return asdict(self)
