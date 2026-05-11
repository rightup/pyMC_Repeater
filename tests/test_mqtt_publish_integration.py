"""End-to-end integration test for MQTT packet publishing.

Asserts the full wire path: AirtimeManager computes airtime_ms, the value is
stored on packet_record, PacketRecord.from_packet_record serializes it as the
``duration`` field, and mqtt_handler.publish_packet hands a JSON payload to
the paho-mqtt client whose topic and content match the documented contract.

This complements ``tests/test_packet_duration.py`` (which unit-tests the
serializer and Semtech formula in isolation) by locking the wire format end
to end. paho-mqtt's network layer is mocked so no real broker is required.
"""

import json
import math
from unittest.mock import MagicMock

from repeater.airtime import AirtimeManager
from repeater.data_acquisition.mqtt_handler import MeshCoreToMqttPusher
from repeater.data_acquisition.storage_utils import PacketRecord


# --------------------------------------------------------------------
# Test scaffolding
# --------------------------------------------------------------------
class _FakeIdentity:
    """Minimal LocalIdentity stand-in for constructor wiring."""

    def __init__(self, public_key_hex: str):
        self._pk = bytes.fromhex(public_key_hex)

    def get_public_key(self) -> bytes:
        return self._pk


def _make_config(format_value: str = "letsmesh", iata_code: str = "LAX") -> dict:
    """Minimal pyMC_Repeater config sufficient to construct MeshCoreToMqttPusher."""
    return {
        "repeater": {"node_name": "test-node"},
        "radio": {
            "spreading_factor": 8,
            "bandwidth": 62500,
            "coding_rate": 8,
            "preamble_length": 17,
            "frequency": 869618000,
            "tx_power": 14,
        },
        "duty_cycle": {"max_airtime_per_minute": 3600},
        "mqtt_brokers": {
            "iata_code": iata_code,
            "status_interval": 0,
            "owner": "",
            "email": "",
            "brokers": [
                {
                    "name": "test-broker",
                    "enabled": True,
                    "host": "broker.example",
                    "port": 1883,
                    "transport": "tcp",
                    "format": format_value,
                    "use_jwt_auth": False,
                    "tls": {"enabled": False, "insecure": False},
                }
            ],
        },
    }


def _attach_capturing_client(conn) -> list:
    """Replace ``conn.client`` with a Mock and return the capture list.

    The list is appended to on every paho-mqtt ``client.publish`` call. We also
    flip ``conn._running = True`` so the publish path doesn't short-circuit on
    the "not connected" guard.
    """
    captured: list = []

    def _fake_publish(topic, payload, retain=False, qos=0):
        captured.append(
            {"topic": topic, "payload": payload, "retain": retain, "qos": qos}
        )
        return None

    conn._running = True
    conn.client = MagicMock()
    conn.client.publish = _fake_publish
    return captured


# --------------------------------------------------------------------
# End-to-end integration test
# --------------------------------------------------------------------
def test_mqtt_published_packet_carries_semtech_duration_end_to_end():
    """A packet flowing through the publisher must produce JSON whose
    ``duration`` field equals the Semtech-derived airtime in ms.

    Steps mirror production:
      1. AirtimeManager (engine.py would call this in _build_packet_record).
      2. Store airtime_ms on the packet_record dict.
      3. PacketRecord.from_packet_record serializes it as 'duration'.
      4. mqtt_handler.publish_packet flows JSON to the paho client.
      5. Topic and JSON content are asserted byte-by-byte.
    """
    config = _make_config(format_value="letsmesh", iata_code="LAX")
    public_key_hex = "AB" * 32  # 64 hex chars = 32-byte Ed25519 pubkey
    identity = _FakeIdentity(public_key_hex)

    # Construct the real publisher; this builds one _BrokerConnection that
    # holds a paho-mqtt Client object. We then swap that object for a Mock.
    pusher = MeshCoreToMqttPusher(local_identity=identity, config=config)
    assert len(pusher.connections) == 1
    captured = _attach_capturing_client(pusher.connections[0])

    # Step 1: compute airtime exactly the way engine.py does.
    raw_bytes = bytes(range(40))  # 40-byte packet (typical MeshCore size)
    airtime_mgr = AirtimeManager(config)
    expected_airtime_ms = airtime_mgr.calculate_airtime(len(raw_bytes))
    expected_duration = str(int(round(expected_airtime_ms)))

    # Step 2: build a packet_record with airtime_ms (matches _build_packet_record).
    packet_record = {
        "timestamp": 1700000000.0,
        "type": 4,
        "route": 1,
        "rssi": -90,
        "snr": 7.5,
        "score": 0.5,
        "payload_length": 32,
        "packet_hash": "DEADBEEF" + "00" * 4,
        "raw_packet": raw_bytes.hex(),
        "airtime_ms": expected_airtime_ms,
    }

    # Step 3: serialize via the production code path.
    record = PacketRecord.from_packet_record(
        packet_record, origin="test-node", origin_id=public_key_hex.upper()
    )
    assert record is not None

    # Step 4: publish via the real publisher chain.
    pusher.publish_packet(record.to_dict())

    # Step 5: assertions on the wire output.
    assert len(captured) == 1, "expected exactly one paho publish call"
    publish = captured[0]

    # Topic follows the MC2MQTT family convention for letsmesh format.
    assert publish["topic"] == f"meshcore/LAX/{public_key_hex.upper()}/packets"

    # Payload is JSON; parse and verify duration is the Semtech value.
    payload_dict = json.loads(publish["payload"])
    assert payload_dict["duration"] == expected_duration
    assert payload_dict["duration"] != "0", "duration must not be hard-coded zero"
    assert 0 < int(payload_dict["duration"]) < 10_000, (
        "duration should be a sane time-on-air in ms"
    )

    # Sanity: other key fields flowed through correctly.
    assert payload_dict["origin"] == "test-node"
    assert payload_dict["origin_id"] == public_key_hex.upper()
    assert payload_dict["len"] == str(len(raw_bytes))
    assert payload_dict["raw"] == raw_bytes.hex()
    assert payload_dict["RSSI"] == "-90"
    assert payload_dict["type"] == "PACKET"
    assert payload_dict["direction"] == "rx"
    assert payload_dict["route"] == "F"  # route=1 -> "F" (flood)


def test_mqtt_published_packet_topic_uses_mc2mqtt_structure_for_waev_format():
    """The waev format flavor must publish on the same MC2MQTT topic structure
    as letsmesh. Locks the contract that all MC2MQTT family formats share
    ``meshcore/{IATA}/{PUBKEY}/packets``.
    """
    config = _make_config(format_value="waev", iata_code="SFO")
    public_key_hex = "CD" * 32
    identity = _FakeIdentity(public_key_hex)

    pusher = MeshCoreToMqttPusher(local_identity=identity, config=config)
    captured = _attach_capturing_client(pusher.connections[0])

    raw_bytes = bytes(range(20))
    airtime_mgr = AirtimeManager(config)
    packet_record = {
        "timestamp": 1700000000.0,
        "type": 1,
        "route": 2,
        "rssi": -75,
        "snr": 5.0,
        "score": 0.3,
        "payload_length": 14,
        "packet_hash": "CAFEBABE" + "00" * 4,
        "raw_packet": raw_bytes.hex(),
        "airtime_ms": airtime_mgr.calculate_airtime(len(raw_bytes)),
    }
    record = PacketRecord.from_packet_record(
        packet_record, origin="test", origin_id=public_key_hex.upper()
    )
    pusher.publish_packet(record.to_dict())

    assert len(captured) == 1
    assert captured[0]["topic"] == f"meshcore/SFO/{public_key_hex.upper()}/packets"
    payload_dict = json.loads(captured[0]["payload"])
    assert int(payload_dict["duration"]) > 0
    assert payload_dict["route"] == "D"  # route=2 -> "D" (direct)


def test_mqtt_published_packet_legacy_mqtt_format_uses_singular_packet_topic():
    """The legacy ``format: mqtt`` path keeps its custom topic shape:
    ``meshcore/repeater/{node_name}/packet`` (singular). This test locks the
    backward-compat behavior we explicitly preserve.
    """
    config = _make_config(format_value="mqtt", iata_code="LAX")
    public_key_hex = "EF" * 32
    identity = _FakeIdentity(public_key_hex)

    pusher = MeshCoreToMqttPusher(local_identity=identity, config=config)
    captured = _attach_capturing_client(pusher.connections[0])

    raw_bytes = bytes(range(16))
    airtime_mgr = AirtimeManager(config)
    packet_record = {
        "timestamp": 1700000000.0,
        "type": 1,
        "route": 1,
        "rssi": -80,
        "snr": 4.0,
        "score": 0.2,
        "payload_length": 10,
        "packet_hash": "BADC0DE0" + "00" * 4,
        "raw_packet": raw_bytes.hex(),
        "airtime_ms": airtime_mgr.calculate_airtime(len(raw_bytes)),
    }
    record = PacketRecord.from_packet_record(
        packet_record, origin="test-node", origin_id=public_key_hex.upper()
    )
    pusher.publish_packet(record.to_dict())

    assert len(captured) == 1
    # Legacy "mqtt" format: custom topic prefix + singular subtopic
    assert captured[0]["topic"] == "meshcore/repeater/test-node/packet"
    payload_dict = json.loads(captured[0]["payload"])
    # Duration still flows through correctly even on the legacy topic
    assert int(payload_dict["duration"]) > 0
