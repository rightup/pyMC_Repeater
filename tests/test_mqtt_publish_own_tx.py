"""Tests for publishing this node's own transmissions to MQTT.

A half-duplex radio never receives its own transmission, so a packet this node
relays can never appear in what it publishes: the received bytes carry the
sender's path, not ours. Without an explicit tx record, a repeater's relays are
invisible to MQTT aggregators unless a second observer is in radio range.

These tests lock the opt-in contract:
  * ``publish_tx: off`` (default) changes nothing.
  * ``publish_tx: on`` emits a ``direction: "tx"`` record carrying the
    *forwarded* bytes (which include this node's path hash).
  * ``publish_tx: advert`` narrows that to this node's own adverts.

paho-mqtt's network layer is mocked, so no broker is required.
"""

import json
from unittest.mock import MagicMock

import pytest

from repeater.data_acquisition.mqtt_handler import MeshCoreToMqttPusher
from repeater.data_acquisition.storage_utils import PacketRecord

PUBLIC_KEY_HEX = "AB" * 32
ADVERT_TYPE = 4
REQ_TYPE = 0

# Received bytes carry the upstream path only; the forwarded copy appends our hash.
RAW_RX = "0141" + "be50" + "deadbeef"
RAW_TX = "0142" + "be506742" + "deadbeef"


class _FakeIdentity:
    """Minimal LocalIdentity stand-in for constructor wiring."""

    def __init__(self, public_key_hex: str):
        self._pk = bytes.fromhex(public_key_hex)

    def get_public_key(self) -> bytes:
        return self._pk


def _make_config(publish_tx=None) -> dict:
    """Minimal config with a single letsmesh broker, optionally opted into tx."""
    broker = {
        "name": "test-broker",
        "enabled": True,
        "host": "broker.example",
        "port": 1883,
        "transport": "tcp",
        "format": "letsmesh",
        "use_jwt_auth": False,
        "tls": {"enabled": False, "insecure": False},
    }
    if publish_tx is not None:
        broker["publish_tx"] = publish_tx

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
            "iata_code": "LAX",
            "status_interval": 0,
            "owner": "",
            "email": "",
            "brokers": [broker],
        },
    }


def _attach_capturing_client(conn) -> list:
    """Replace ``conn.client`` with a Mock and return the capture list."""
    captured: list = []

    def _fake_publish(topic, payload, retain=False, qos=0):
        captured.append({"topic": topic, "payload": payload, "retain": retain, "qos": qos})
        return None

    conn._running = True
    conn.client = MagicMock()
    conn.client.publish = _fake_publish
    return captured


def _packet_record(packet_type: int = REQ_TYPE, transmitted: bool = True) -> dict:
    return {
        "timestamp": 1700000000.0,
        "type": packet_type,
        "route": 1,
        "rssi": -90,
        "snr": 7.5,
        "score": 0.5,
        "payload_length": 4,
        "packet_hash": "DEADBEEF" + "00" * 4,
        "raw_packet": RAW_RX,
        "raw_packet_tx": RAW_TX if transmitted else None,
        "transmitted": transmitted,
        "airtime_ms": 100.0,
    }


def _publish_both(pusher, record: dict):
    """Publish the rx record then the tx record, as storage_collector does."""
    rx = PacketRecord.from_packet_record(
        record, origin="test-node", origin_id=PUBLIC_KEY_HEX.upper()
    )
    pusher.publish_packet(rx.to_dict())

    tx = PacketRecord.from_packet_record(
        record, origin="test-node", origin_id=PUBLIC_KEY_HEX.upper(), direction="tx"
    )
    if tx is not None:
        pusher.publish_packet(tx.to_dict())


def _payloads(captured) -> list:
    return [json.loads(c["payload"]) for c in captured]


# --------------------------------------------------------------------
# Serializer
# --------------------------------------------------------------------
def test_tx_record_uses_forwarded_bytes_and_drops_receive_signal():
    """A tx record must carry the forwarded bytes, not the received ones."""
    record = PacketRecord.from_packet_record(
        _packet_record(), origin="n", origin_id=PUBLIC_KEY_HEX.upper(), direction="tx"
    ).to_dict()

    assert record["direction"] == "tx"
    assert record["raw"] == RAW_TX, "tx record must carry the bytes we transmitted"
    assert record["raw"] != RAW_RX
    assert record["len"] == str(len(RAW_TX) // 2)
    # A node cannot measure its own signal; reporting the receive-side values
    # here would attribute another node's link quality to our transmission.
    assert record["SNR"] == "0"
    assert record["RSSI"] == "0"


def test_rx_record_is_unchanged_by_the_new_parameter():
    """The default path must serialize exactly as before."""
    record = PacketRecord.from_packet_record(
        _packet_record(), origin="n", origin_id=PUBLIC_KEY_HEX.upper()
    ).to_dict()

    assert record["direction"] == "rx"
    assert record["raw"] == RAW_RX
    assert record["RSSI"] == "-90"
    assert record["SNR"] == "7.5"


def test_tx_record_is_none_when_packet_was_not_forwarded():
    """Nothing to report when the packet never went on the air."""
    assert (
        PacketRecord.from_packet_record(
            _packet_record(transmitted=False),
            origin="n",
            origin_id=PUBLIC_KEY_HEX.upper(),
            direction="tx",
        )
        is None
    )


# --------------------------------------------------------------------
# Per-broker opt-in
# --------------------------------------------------------------------
def test_default_is_off_and_suppresses_own_tx():
    """Absent config, behaviour is identical to before this feature."""
    pusher = MeshCoreToMqttPusher(
        local_identity=_FakeIdentity(PUBLIC_KEY_HEX), config=_make_config()
    )
    assert pusher.connections[0].publish_tx == "off"
    assert pusher.wants_own_tx() is False

    captured = _attach_capturing_client(pusher.connections[0])
    _publish_both(pusher, _packet_record())

    payloads = _payloads(captured)
    assert len(payloads) == 1, "only the rx record may be published by default"
    assert payloads[0]["direction"] == "rx"


def test_publish_tx_on_emits_the_tx_record():
    pusher = MeshCoreToMqttPusher(
        local_identity=_FakeIdentity(PUBLIC_KEY_HEX), config=_make_config(publish_tx="on")
    )
    assert pusher.wants_own_tx() is True

    captured = _attach_capturing_client(pusher.connections[0])
    _publish_both(pusher, _packet_record())

    payloads = _payloads(captured)
    assert [p["direction"] for p in payloads] == ["rx", "tx"]
    assert payloads[1]["raw"] == RAW_TX
    assert payloads[1]["origin_id"] == PUBLIC_KEY_HEX.upper()
    assert captured[1]["topic"] == f"meshcore/LAX/{PUBLIC_KEY_HEX.upper()}/packets"


@pytest.mark.parametrize(
    "packet_type,expect_tx",
    [(ADVERT_TYPE, True), (REQ_TYPE, False)],
)
def test_publish_tx_advert_only_emits_adverts(packet_type, expect_tx):
    pusher = MeshCoreToMqttPusher(
        local_identity=_FakeIdentity(PUBLIC_KEY_HEX), config=_make_config(publish_tx="advert")
    )
    captured = _attach_capturing_client(pusher.connections[0])
    _publish_both(pusher, _packet_record(packet_type=packet_type))

    directions = [p["direction"] for p in _payloads(captured)]
    assert directions == (["rx", "tx"] if expect_tx else ["rx"])


@pytest.mark.parametrize(
    "raw,expected",
    [
        # YAML 1.1 resolves bare on/off to booleans before we ever see them.
        (True, "on"),
        (False, "off"),
        ("on", "on"),
        ("off", "off"),
        ("true", "on"),
        ("false", "off"),
        ("advert", "advert"),
        (None, "off"),
    ],
)
def test_publish_tx_accepts_yaml_boolean_and_string_spellings(raw, expected):
    """``publish_tx: on`` in YAML arrives as True, not "on" -- both must work."""
    pusher = MeshCoreToMqttPusher(
        local_identity=_FakeIdentity(PUBLIC_KEY_HEX), config=_make_config(publish_tx=raw)
    )
    assert pusher.connections[0].publish_tx == expected


def test_yaml_bare_on_enables_own_tx_end_to_end():
    """Regression: a config written as `publish_tx: on` must actually uplink."""
    import yaml

    broker_yaml = yaml.safe_load("publish_tx: on")
    assert broker_yaml["publish_tx"] is True, "precondition: YAML gives us a bool"

    pusher = MeshCoreToMqttPusher(
        local_identity=_FakeIdentity(PUBLIC_KEY_HEX),
        config=_make_config(publish_tx=broker_yaml["publish_tx"]),
    )
    captured = _attach_capturing_client(pusher.connections[0])
    _publish_both(pusher, _packet_record())

    assert [p["direction"] for p in _payloads(captured)] == ["rx", "tx"]


def test_declined_tx_does_not_look_like_a_connectivity_failure(caplog):
    """A tx record every broker declines must not warn about broker connections.

    Regression: the skip used to `continue` without recording a result, leaving
    ``results`` empty and emitting "No active broker connections" -- which sends
    operators chasing a connectivity problem that does not exist.
    """
    pusher = MeshCoreToMqttPusher(
        local_identity=_FakeIdentity(PUBLIC_KEY_HEX), config=_make_config(publish_tx="off")
    )
    conn = pusher.connections[0]
    _attach_capturing_client(conn)

    tx = PacketRecord.from_packet_record(
        _packet_record(), origin="n", origin_id=PUBLIC_KEY_HEX.upper(), direction="tx"
    )
    with caplog.at_level("WARNING", logger="MQTTHandler"):
        results = pusher.publish_packet(tx.to_dict())

    assert results, "a deliberate skip must still be reported as a result"
    assert results[0][1] == "Skipped: publish_tx"
    assert "No active broker connections" not in caplog.text


def test_invalid_publish_tx_falls_back_to_off():
    """A typo must fail closed, never start uplinking unexpectedly."""
    pusher = MeshCoreToMqttPusher(
        local_identity=_FakeIdentity(PUBLIC_KEY_HEX), config=_make_config(publish_tx="yes-please")
    )
    assert pusher.connections[0].publish_tx == "off"
    assert pusher.wants_own_tx() is False


def test_publish_tx_value_is_case_and_whitespace_tolerant():
    pusher = MeshCoreToMqttPusher(
        local_identity=_FakeIdentity(PUBLIC_KEY_HEX), config=_make_config(publish_tx="  ON  ")
    )
    assert pusher.connections[0].publish_tx == "on"
